"""
Walk-Forward 검증 시스템
=========================
매 폴드마다 해당 시점까지의 데이터로 모델을 재학습(expanding window)하고,
이후 구간(Out-of-sample)에서 백테스트를 수행합니다.

기본 폴드 구성 (expanding window, 테스트 1년):
  Fold 01 : 훈련 2006~2012  →  테스트 2013
  Fold 02 : 훈련 2006~2013  →  테스트 2014
  ...
  Fold 13 : 훈련 2006~2024  →  테스트 2025

출력:
  walk_forward/
    fold_01/models/          재학습된 모델 (.h5, .scaler)
    fold_01/results/         periods.csv, history.csv, trades.csv
    ...
    summary.csv              전 폴드 통합 성과

실행 예시:
  python -X utf8 walk_forward.py
  python -X utf8 walk_forward.py --test-years 2        # 테스트 2년씩
  python -X utf8 walk_forward.py --rolling 7           # rolling: 최근 7년치만 훈련
  python -X utf8 walk_forward.py --from-fold 3         # fold 3부터 재개
  python -X utf8 walk_forward.py --rebuild-prep        # 전처리 캐시 재빌드
"""

import os, sys, argparse, pickle, warnings, time, gc, shutil, json
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
warnings.filterwarnings("ignore")

import tensorflow as tf
from tensorflow.keras.models import load_model

# GPU 설정 — TF 임포트 직후 (최우선)
def _setup_gpu():
    gpus = tf.config.list_physical_devices('GPU')
    if not gpus:
        return False, 1024
    try:
        for gpu in gpus:
            tf.config.experimental.set_memory_growth(gpu, True)
        return True, 4096
    except Exception as e:
        if "cannot be modified after being initialized" in str(e):
            return True, 4096
        return False, 1024

_USE_GPU, _BATCH_SZ = _setup_gpu()
if _USE_GPU:
    print(f"   ✅ GPU 사용  배치: {_BATCH_SZ}")
else:
    print(f"   ℹ️  CPU 모드  배치: {_BATCH_SZ}")

# ─── 경로 설정 ──────────────────────────────────────────────────────────────────
ROOT      = Path(r"C:\Projects\RealtimeMonitor")
DATA_DIR  = ROOT / "Data" / "Stock"
WF_DIR    = ROOT / "walk_forward"
PREP_DIR  = ROOT / "Data" / "_prep_wf_v3"   # walk-forward 전용 전처리 캐시 (2006~)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "Model_Training"))

# ─── 전략 파라미터 (backtest.py 와 동일) ────────────────────────────────────────
INITIAL_CAPITAL  = 1_000_000_000

# 매수: (최소 점수, 목표 비율)  — 점수 높은 순
BUY_TIERS  = [(0.8, 0.20), (0.7, 0.15), (0.6, 0.10), (0.5, 0.05)]
# 매도: (점수 상한, 보유 비율)  — 점수 낮은 순 (먼저 매칭되는 rule 적용)
SELL_TIERS = [(0.25, 0.00), (0.30, 0.05), (0.35, 0.10), (0.40, 0.15)]
# 거래비용: 수수료 0.015%, 매도 증권거래세 0.18%
BUY_FEE_RATE  = 0.00015
SELL_FEE_RATE = 0.00195

# 제외 종목 (build_universe_filter.py 생성, main에서 채워짐)
EXCLUDED_CODES: set = set()


def _buy_target_ratio(score):
    """점수 → 매수 목표 비율. 0.5 미만이면 None."""
    for min_s, ratio in BUY_TIERS:
        if score >= min_s:
            return ratio
    return None


def _sell_target_ratio(score):
    """점수 → 매도 후 보유 비율. 0.40 이상이면 None (매도 불필요)."""
    for max_s, ratio in SELL_TIERS:
        if score < max_s:
            return ratio
    return None

# 모델 학습 피처와 동일 (rsi_v3 등 _v3 접미사, prep 캐시에서 읽음)
WF_FEATURES = [
    'change_pct', 'volume_ratio', 'vol_power',
    'prog_net_ratio', 'prog_ratio_vol',
    'disparity_5', 'disparity_20',
    'rsi_v3', 'bb_p_v3', 'bb_w_v3', 'adx_v3',
    'kospi_change', 'kosdaq_change',
]

MODEL_SETTINGS = {
    "target1":  {"lb": 21, "thr": 0.4974, "weight": 0.1384, "type": "surge"},
    "target5":  {"lb": 50, "thr": 0.6327, "weight": 0.3099, "type": "surge"},
    "target20": {"lb": 60, "thr": 0.9046, "weight": 0.5517, "type": "surge"},
    "drop1":    {"lb": 10, "thr": 0.4349, "weight": 0.2411, "type": "drop"},
    "drop5":    {"lb": 94, "thr": 0.4314, "weight": 0.3714, "type": "drop"},
    "drop20":   {"lb": 98, "thr": 0.4686, "weight": 0.3875, "type": "drop"},
}
MAX_LOOKBACK = max(s["lb"] for s in MODEL_SETTINGS.values())


# ══════════════════════════════════════════════════════════════════════════════
# 제외 종목 로드
# ══════════════════════════════════════════════════════════════════════════════

def load_excluded_codes(path: Path) -> set:
    if not path.exists():
        print(f"   ⚠️  제외 목록 파일 없음: {path}")
        return set()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    codes = set(data.get("excluded", {}).keys())
    print(f"   📋 제외 종목 로드: {len(codes)}개 ({path.name})")
    return codes


# ══════════════════════════════════════════════════════════════════════════════
# 폴드 생성
# ══════════════════════════════════════════════════════════════════════════════

def generate_folds(data_start="20060101", min_train_years=7, test_years=1,
                   data_end="20260101", rolling_years=None):
    """
    expanding window (기본) 또는 rolling window 폴드 목록 생성.
    반환: [{"fold_id", "train_start", "train_end", "test_start", "test_end"}, ...]
    """
    folds = []
    ds    = pd.Timestamp(data_start)
    de    = pd.Timestamp(data_end)

    test_start = ds + pd.DateOffset(years=min_train_years)
    fold_id    = 1

    while test_start < de:
        test_end  = test_start + pd.DateOffset(years=test_years)
        test_end  = min(test_end, de)
        train_end = test_start

        if rolling_years:
            train_start = max(train_end - pd.DateOffset(years=rolling_years), ds)
        else:
            train_start = ds

        folds.append({
            "fold_id":     fold_id,
            "train_start": train_start.strftime("%Y%m%d"),
            "train_end":   train_end.strftime("%Y%m%d"),
            "test_start":  test_start.strftime("%Y%m%d"),
            "test_end":    test_end.strftime("%Y%m%d"),
        })
        test_start = test_end
        fold_id   += 1

    return folds


# ══════════════════════════════════════════════════════════════════════════════
# Phase 1 : 전처리 캐시 빌드 (walk-forward 전용, 1회)
# ══════════════════════════════════════════════════════════════════════════════

def build_prep_cache(rebuild=False, train_end=None):
    """
    2006~현재 전 종목 전처리 캐시를 PREP_DIR에 생성.
    이후 각 폴드는 train_end 필터로 샘플 추출.
    """
    from train_v3 import run_global_caching

    if train_end is None:
        train_end = datetime.now().strftime("%Y%m%d")

    if not rebuild and PREP_DIR.exists() and len(list(PREP_DIR.glob("*"))) > 100:
        all_files = [str(p) for p in sorted(PREP_DIR.glob("A*.feather"))]
        if EXCLUDED_CODES:
            all_files = [f for f in all_files if Path(f).stem[1:] not in EXCLUDED_CODES]
        print(f"📦 전처리 캐시 재사용: {PREP_DIR}  ({len(all_files)}개, 제외 {len(EXCLUDED_CODES)}종목 제거)")
        return all_files

    # rebuild=True: run_global_caching 내부의 자체 캐시 체크를 우회하기 위해 먼저 삭제
    if rebuild and PREP_DIR.exists():
        print(f"   기존 캐시 삭제 중: {PREP_DIR}  ({len(list(PREP_DIR.glob('*')))}개 파일)")
        shutil.rmtree(PREP_DIR)
        PREP_DIR.mkdir(parents=True)

    print("\n" + "=" * 60)
    print(f"전처리 캐시 빌드 (walk-forward 전용, 20060101 ~ {train_end})")
    print("=" * 60)

    feature_cols = [
        'change_pct', 'volume_ratio', 'vol_power', 'prog_net_ratio', 'prog_ratio_vol',
        'disparity_5', 'disparity_20', 'rsi_v3', 'bb_p_v3', 'bb_w_v3', 'adx_v3',
        'kospi_change', 'kosdaq_change'
    ]
    data_dir = ROOT / "Data"
    cached_files = run_global_caching(
        data_dir, PREP_DIR, feature_cols,
        train_start="20060101", train_end=train_end
    )

    # 제외 종목 prep 파일 삭제
    if EXCLUDED_CODES:
        removed = 0
        for fpath in list(PREP_DIR.glob("A*.feather")):
            code = fpath.stem[1:]
            if code in EXCLUDED_CODES:
                fpath.unlink()
                removed += 1
        if removed:
            print(f"   🗑️  제외 종목 prep 파일 삭제: {removed}개")
        cached_files = [f for f in cached_files if Path(f).stem[1:] not in EXCLUDED_CODES]

    return cached_files


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 : 폴드별 모델 재학습
# ══════════════════════════════════════════════════════════════════════════════

def retrain_fold(fold, cached_files):
    """fold의 train_end까지 데이터로 모델 재학습 → fold 디렉토리에 저장."""
    from train_v3 import EnhancedSurgeTrainerV3, EnhancedDropTrainerV3, settings

    fold_id        = fold["fold_id"]
    train_start    = fold["train_start"]
    train_end      = fold["train_end"]
    fold_model_dir = WF_DIR / f"fold_{fold_id:02d}" / "models"
    fold_model_dir.mkdir(parents=True, exist_ok=True)

    # 이미 완료된 fold skip
    expected = [fold_model_dir / f"{m}_lstm_v3.h5" for m in MODEL_SETTINGS]
    if all(f.exists() for f in expected):
        print(f"\n   Fold {fold_id:02d}: 기존 모델 재사용 (학습 skip)")
        return fold_model_dir

    print(f"\n{'='*60}")
    print(f"Fold {fold_id:02d} 학습 | 훈련 기간: {train_start} ~ {train_end}")
    print(f"{'='*60}")

    for spec in settings.MODELS["LSTM"]["SURGE"]:
        EnhancedSurgeTrainerV3(
            spec, "wf", cached_files,
            output_dir=str(fold_model_dir),
            train_end=train_end,
        ).run()
        tf.keras.backend.clear_session()
        gc.collect()
    for spec in settings.MODELS["LSTM"]["DROP"]:
        EnhancedDropTrainerV3(
            spec, "wf", cached_files,
            output_dir=str(fold_model_dir),
            train_end=train_end,
        ).run()
        tf.keras.backend.clear_session()
        gc.collect()

    gc.collect()
    tf.keras.backend.clear_session()
    return fold_model_dir


# ══════════════════════════════════════════════════════════════════════════════
# Phase 3 : 폴드 모델 로드 & 스코어 계산
# ══════════════════════════════════════════════════════════════════════════════

def load_fold_models(fold_model_dir):
    """fold 디렉토리에서 모델 로드."""
    models  = {}
    best_f1 = {}
    for m_name, cfg in MODEL_SETTINGS.items():
        h5  = fold_model_dir / f"{m_name}_lstm_v3.h5"
        scl = fold_model_dir / f"{m_name}_lstm_v3.scaler"
        log = fold_model_dir / f"log_{m_name}_v3.csv"
        if not (h5.exists() and scl.exists()):
            print(f"   ⚠️  {m_name} 없음 — 스킵")
            continue
        with open(scl, 'rb') as f:
            scaler = pickle.load(f)
        model     = load_model(str(h5), compile=False)
        actual_lb = model.input_shape[1]

        if log.exists():
            log_df          = pd.read_csv(log)
            best_row        = log_df.loc[log_df['f1'].idxmax()]
            threshold       = float(best_row['threshold'])
            best_f1[m_name] = float(best_row['f1'])
        else:
            threshold = cfg["thr"]

        models[m_name] = {
            "model":     model,
            "scaler":    scaler,
            "lookback":  actual_lb,
            "threshold": threshold,
            "weight":    cfg["weight"],   # 아래에서 동적 갱신
            "type":      cfg["type"],
        }

    # F1 비례 가중치: weight_Nd = f1_Nd / sum(f1 in group)
    for group in ("surge", "drop"):
        names = [n for n, c in MODEL_SETTINGS.items() if c["type"] == group and n in models]
        f1s   = [best_f1.get(n) for n in names]
        if all(f is not None for f in f1s):
            total = sum(f1s)
            for n, f in zip(names, f1s):
                models[n]["weight"] = f / total

    for m_name, info in models.items():
        print(f"     {m_name:<10} lb={info['lookback']:>3}  thr={info['threshold']:.4f}  w={info['weight']:.4f}")
    return models


def _compute_scores(feat_data, models):
    """종목 1개 분량의 피처 배열 → {day_idx: total_score}."""
    n      = len(feat_data)
    n_feat = feat_data.shape[1]
    day_probs = {}

    for m_name, info in models.items():
        lb = info["lookback"]
        if n < lb:
            continue
        n_win = n - lb + 1

        idx  = np.arange(n_win)[:, None] + np.arange(lb)[None, :]
        wins = feat_data[idx].astype(np.float32)
        flat = info["scaler"].transform(wins.reshape(-1, n_feat)).astype(np.float32)
        wins = tf.constant(flat.reshape(n_win, lb, n_feat))

        preds = np.concatenate([
            info["model"](wins[s: s + _BATCH_SZ], training=False).numpy().flatten()
            for s in range(0, n_win, _BATCH_SZ)
        ])

        for i, prob in enumerate(preds):
            day_probs.setdefault(lb - 1 + i, {})[m_name] = float(prob)

        del wins, flat, preds

    result = {}
    for di, probs in day_probs.items():
        s, d = 0.0, 0.0
        for m_name, prob in probs.items():
            info = models[m_name]
            if prob > info["threshold"]:
                if info["type"] == "surge": s += prob * info["weight"]
                else:                       d += prob * info["weight"]
        result[di] = round(s - d, 4)
    return result


def compute_fold_scores(models, test_start_str, test_end_str):
    """
    test 구간 전 종목 스코어 계산 → DataFrame(date, code, score).
    prep 캐시(rsi_v3 등 _v3 피처)를 사용해야 학습 피처와 일치.
    """
    ts        = pd.Timestamp(test_start_str)
    te        = pd.Timestamp(test_end_str)
    actual_max_lb = max(info["lookback"] for info in models.values())
    load_from     = ts - pd.DateOffset(days=actual_max_lb * 2)

    prep_files = sorted(PREP_DIR.glob("A*.feather"))
    prep_files = [f for f in prep_files if f.stem[1:] not in EXCLUDED_CODES]
    records    = []
    t0         = time.time()

    print(f"\n   스코어 계산: {ts.date()} ~ {te.date()} ({len(prep_files)}개 종목)")

    for i, fpath in enumerate(prep_files):
        code = fpath.stem[1:]   # A005930 → 005930
        try:
            df = pd.read_feather(str(fpath))
            df['date'] = pd.to_datetime(df['date'])
            df = df[df['date'] >= load_from].sort_values('date').reset_index(drop=True)

            for col in WF_FEATURES:
                if col not in df.columns:
                    df[col] = 0.0
            df[WF_FEATURES] = df[WF_FEATURES].fillna(0)

            if len(df) < actual_max_lb:
                continue

            scores = _compute_scores(df[WF_FEATURES].values, models)

            for di, score in scores.items():
                date = df['date'].iloc[di]
                if ts <= date < te:
                    records.append({"date": date, "code": code, "score": score})
        except Exception:
            pass

        if (i + 1) % 300 == 0 or i == len(prep_files) - 1:
            elapsed = time.time() - t0
            rate    = (i + 1) / elapsed
            remain  = (len(prep_files) - i - 1) / rate if rate > 0 else 0
            print(f"   [{i+1:4d}/{len(prep_files)}]  {elapsed/60:.1f}분 경과 | 남은: {remain/60:.1f}분")

    score_df = pd.DataFrame(records)
    if not score_df.empty:
        score_df['date'] = pd.to_datetime(score_df['date'])
    return score_df


# ══════════════════════════════════════════════════════════════════════════════
# Phase 4 : 시뮬레이션 (backtest.py 로직 그대로)
# ══════════════════════════════════════════════════════════════════════════════

class Portfolio:
    def __init__(self, initial_capital):
        self.cash       = float(initial_capital)
        self.positions  = {}   # code → {qty, avg_price, alloc_ratio}
        self._trade_log = []

    def total_assets(self, prices):
        mv = sum(pos["qty"] * prices.get(code, pos["avg_price"])
                 for code, pos in self.positions.items())
        return self.cash + mv

    def market_value(self, code, prices):
        pos = self.positions.get(code)
        return pos["qty"] * prices.get(code, pos["avg_price"]) if pos else 0.0

    def buy(self, code, amount, price, date, alloc_ratio):
        if price <= 0 or amount <= 0:
            return False
        qty        = int(amount / price)
        cost       = qty * price
        total_cost = cost * (1 + BUY_FEE_RATE)
        if qty == 0 or total_cost > self.cash:
            return False
        self.cash -= total_cost
        if code in self.positions:
            pos     = self.positions[code]
            new_qty = pos["qty"] + qty
            pos["avg_price"]   = (pos["qty"] * pos["avg_price"] + qty * price) / new_qty
            pos["qty"]         = new_qty
            pos["alloc_ratio"] = alloc_ratio
        else:
            self.positions[code] = {"qty": qty, "avg_price": price, "alloc_ratio": alloc_ratio}
        self._trade_log.append({"date": date, "action": "BUY",  "code": code, "qty": qty, "price": price})
        return True

    def sell(self, code, qty, price, date, alloc_ratio=None):
        if code not in self.positions:
            return
        pos      = self.positions[code]
        qty      = min(qty, pos["qty"])
        proceeds = qty * price * (1 - SELL_FEE_RATE)
        self.cash  += proceeds
        pos["qty"] -= qty
        if pos["qty"] == 0:
            del self.positions[code]
        elif alloc_ratio is not None:
            pos["alloc_ratio"] = alloc_ratio
        self._trade_log.append({"date": date, "action": "SELL", "code": code, "qty": qty, "price": price})

    def sell_all(self, code, price, date):
        if code in self.positions:
            self.sell(code, self.positions[code]["qty"], price, date)


def _load_prices(start_date, end_date):
    frames = []
    for fpath in DATA_DIR.glob("A*.csv"):
        code = fpath.stem[1:]
        try:
            df = pd.read_csv(fpath, encoding='utf-8-sig', usecols=['date', 'close'])
            df['date'] = pd.to_datetime(df['date'])
            df['code'] = code
            df = df[(df['date'] >= start_date) & (df['date'] <= end_date)]
            frames.append(df)
        except Exception:
            pass
    all_df = pd.concat(frames, ignore_index=True)
    pivot  = all_df.pivot_table(index='date', columns='code', values='close').sort_index()
    return pivot


def run_simulation(score_df, test_start_str, test_end_str):
    ts = pd.Timestamp(test_start_str)
    te = pd.Timestamp(test_end_str)

    print(f"   종가 로딩...")
    price_pivot = _load_prices(ts, te)

    scores_by_date = (
        score_df.groupby('date')
        .apply(lambda g: dict(zip(g['code'], g['score'])))
        .to_dict()
    )

    trading_days = price_pivot.loc[ts:te].index.tolist()
    print(f"   거래일: {len(trading_days)}일")

    portfolio = Portfolio(INITIAL_CAPITAL)
    history   = []

    for date in trading_days:
        prices_today = price_pivot.loc[date].dropna().to_dict()
        today_scores = scores_by_date.get(date, {})
        total        = portfolio.total_assets(prices_today)

        # ── 1. 매도 (먼저 실행) ──────────────────────────────────────────────
        for code in list(portfolio.positions.keys()):
            score = today_scores.get(code)
            price = prices_today.get(code)
            if score is None or not price or price <= 0:
                continue
            sell_target = _sell_target_ratio(score)
            if sell_target is None:
                continue  # score >= 0.40, 매도 불필요
            if sell_target == 0.0:
                portfolio.sell_all(code, price, date)
            else:
                target_amount = sell_target * total
                cur_mv        = portfolio.market_value(code, prices_today)
                if cur_mv > target_amount:
                    sell_qty = int((cur_mv - target_amount) / price)
                    if sell_qty > 0:
                        portfolio.sell(code, sell_qty, price, date, alloc_ratio=sell_target)

        # ── 2. 매수 (매도 후 실행, 점수 높은 순) ─────────────────────────────
        # 신규·기존 포지션 모두 포함: 현재 alloc_ratio보다 목표가 높을 때만 추가매수
        total      = portfolio.total_assets(prices_today)
        candidates = []
        for code, score in today_scores.items():
            price = prices_today.get(code)
            if not price or price <= 0:
                continue
            target_ratio = _buy_target_ratio(score)
            if target_ratio is None:
                continue  # score < 0.5
            pos       = portfolio.positions.get(code)
            cur_alloc = pos["alloc_ratio"] if pos else 0.0
            if target_ratio > cur_alloc:
                candidates.append((code, score, target_ratio, target_ratio - cur_alloc))

        for code, score, target_ratio, incr_ratio in sorted(candidates, key=lambda x: x[1], reverse=True):
            buy_amount = incr_ratio * total
            price      = prices_today[code]
            if portfolio.cash < buy_amount:
                continue  # 현금 부족, 다음 후보로
            portfolio.buy(code, buy_amount, price, date, alloc_ratio=target_ratio)

        history.append({
            "date": date, "total_assets": portfolio.total_assets(prices_today),
            "cash": portfolio.cash, "n_positions": len(portfolio.positions),
        })

    return pd.DataFrame(history), pd.DataFrame(portfolio._trade_log)


# ══════════════════════════════════════════════════════════════════════════════
# Phase 5 : 성과 분석
# ══════════════════════════════════════════════════════════════════════════════

def analyze(history_df):
    ta        = history_df.set_index("date")["total_assets"]
    ret       = (ta.iloc[-1] / ta.iloc[0] - 1) * 100
    daily_ret = ta.pct_change().dropna()
    sharpe    = (daily_ret.mean() / daily_ret.std() * np.sqrt(252)
                 if daily_ret.std() > 0 else 0)
    cummax    = ta.cummax()
    max_dd    = ((ta - cummax) / cummax).min() * 100
    win_rate  = (daily_ret > 0).sum() / len(daily_ret) * 100
    return {
        "수익률(%)":   round(ret, 2),
        "Sharpe":      round(sharpe, 2),
        "최대낙폭(%)": round(max_dd, 2),
        "일승률(%)":   round(win_rate, 1),
        "거래일수":    len(ta),
        "시작자산(억)": round(ta.iloc[0] / 1e8, 2),
        "종료자산(억)": round(ta.iloc[-1] / 1e8, 2),
    }


def save_fold_results(fold, history_df, trade_df):
    fold_id   = fold["fold_id"]
    result_dir = WF_DIR / f"fold_{fold_id:02d}" / "results"
    result_dir.mkdir(parents=True, exist_ok=True)

    history_df.to_csv(result_dir / "history.csv", index=False, encoding='utf-8-sig')
    trade_df.to_csv(  result_dir / "trades.csv",  index=False, encoding='utf-8-sig')

    stats = analyze(history_df)
    stats.update({
        "fold_id":     fold_id,
        "train_start": fold["train_start"],
        "train_end":   fold["train_end"],
        "test_start":  fold["test_start"],
        "test_end":    fold["test_end"],
        "매수횟수":    int((trade_df['action'] == 'BUY').sum())  if len(trade_df) else 0,
        "매도횟수":    int((trade_df['action'] == 'SELL').sum()) if len(trade_df) else 0,
    })
    return stats


# ══════════════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Walk-Forward 검증")
    parser.add_argument("--min-train",    type=int,   default=7,
                        help="최소 훈련 기간(년, 기본: 7)")
    parser.add_argument("--test-years",   type=int,   default=1,
                        help="폴드당 테스트 기간(년, 기본: 1)")
    parser.add_argument("--rolling",      type=int,   default=None,
                        help="rolling window 훈련 기간(년). 미지정시 expanding")
    parser.add_argument("--data-start",   default="20070101",
                        help="데이터 시작일 (기본: 20070101)")
    parser.add_argument("--data-end",     default=datetime.now().strftime("%Y%m%d"),
                        help="데이터 종료일 (기본: 오늘)")
    parser.add_argument("--from-fold",    type=int,   default=1,
                        help="이 fold 번호부터 실행 (중간 재개, 기본: 1)")
    parser.add_argument("--rebuild-prep", action="store_true",
                        help="전처리 캐시 강제 재빌드")
    parser.add_argument("--exclude-file", default=str(ROOT / "excluded_stocks.json"),
                        help="제외 종목 JSON 파일 경로 (기본: excluded_stocks.json)")
    args = parser.parse_args()

    # 제외 종목 전역 로드
    global EXCLUDED_CODES
    EXCLUDED_CODES = load_excluded_codes(Path(args.exclude_file))

    # rolling 모드면 별도 디렉토리 사용 (expanding 결과와 섞이지 않도록)
    global WF_DIR
    if args.rolling:
        WF_DIR = ROOT / f"walk_forward_rolling{args.rolling}y"

    WF_DIR.mkdir(parents=True, exist_ok=True)

    folds = generate_folds(
        data_start=args.data_start,
        min_train_years=args.min_train,
        test_years=args.test_years,
        data_end=args.data_end,
        rolling_years=args.rolling,
    )

    print(f"\n🚀 Walk-Forward 검증 시작")
    mode_str = f"rolling {args.rolling}년" if args.rolling else "expanding"
    print(f"   모드: {mode_str}  |  최소훈련: {args.min_train}년  |  테스트: {args.test_years}년")
    print(f"   총 폴드: {len(folds)}개  |  시작 폴드: {args.from_fold}")
    print(f"\n{'폴드':>6}  {'훈련 기간':^21}  {'테스트 기간':^21}")
    print("-" * 55)
    for f in folds:
        marker = "← 시작" if f["fold_id"] == args.from_fold else ""
        print(f"  {f['fold_id']:02d}   {f['train_start']} ~ {f['train_end']}   "
              f"{f['test_start']} ~ {f['test_end']}  {marker}")

    # 전처리 캐시 빌드 (1회)
    cached_files = build_prep_cache(rebuild=args.rebuild_prep, train_end=args.data_end)
    if not cached_files:
        print("❌ 전처리 캐시 빌드 실패. Data 폴더를 확인하세요.")
        sys.exit(1)

    all_stats = []

    for fold in folds:
        if fold["fold_id"] < args.from_fold:
            continue

        fold_id = fold["fold_id"]
        print(f"\n{'#'*60}")
        print(f"# Fold {fold_id:02d}/{len(folds)}")
        print(f"# 훈련: {fold['train_start']} ~ {fold['train_end']}")
        print(f"# 테스트: {fold['test_start']} ~ {fold['test_end']}")
        print(f"{'#'*60}")

        t_fold = time.time()

        # 1) 재학습
        fold_model_dir = retrain_fold(fold, cached_files)

        # 2) 모델 로드
        print(f"\n📂 Fold {fold_id:02d} 모델 로딩...")
        models = load_fold_models(fold_model_dir)
        if not models:
            print(f"   ❌ 모델 로드 실패 — fold {fold_id} 건너뜀")
            continue

        # 3) 스코어 계산
        score_df = compute_fold_scores(models, fold["test_start"], fold["test_end"])

        # GPU 메모리 해제
        del models
        gc.collect()
        tf.keras.backend.clear_session()

        if score_df.empty:
            print(f"   ⚠️  스코어 없음 — fold {fold_id} 건너뜀")
            continue

        # 4) 시뮬레이션
        print(f"\n   시뮬레이션...")
        history_df, trade_df = run_simulation(score_df, fold["test_start"], fold["test_end"])

        # 5) 저장 및 성과 집계
        stats = save_fold_results(fold, history_df, trade_df)
        all_stats.append(stats)

        elapsed = time.time() - t_fold
        print(f"\n   ✅ Fold {fold_id:02d} 완료 ({elapsed/60:.1f}분)")
        print(f"      수익률: {stats['수익률(%)']:+.2f}%  "
              f"Sharpe: {stats['Sharpe']:.2f}  "
              f"최대낙폭: {stats['최대낙폭(%)']:.2f}%")

        # 중간 summary 저장 (중단돼도 지금까지 결과 보존)
        _save_summary(all_stats)

    print(f"\n{'='*60}")
    print("Walk-Forward 검증 완료")
    _print_summary(all_stats)
    _save_summary(all_stats)
    _build_comparison()


def _save_summary(all_stats):
    if not all_stats:
        return
    cols = ["fold_id", "train_start", "train_end", "test_start", "test_end",
            "수익률(%)", "Sharpe", "최대낙폭(%)", "일승률(%)", "거래일수",
            "매수횟수", "매도횟수", "시작자산(억)", "종료자산(억)"]
    df = pd.DataFrame(all_stats)
    df = df[[c for c in cols if c in df.columns]]
    df.to_csv(WF_DIR / "summary.csv", index=False, encoding='utf-8-sig')


def _build_comparison(summary_df=None):
    """summary.csv의 전략 수익률과 KOSPI/KOSDAQ 지수를 비교해 comparison.csv 저장."""
    if summary_df is None:
        path = WF_DIR / "summary.csv"
        if not path.exists():
            return
        summary_df = pd.read_csv(path)

    prep_files = sorted(PREP_DIR.glob("A*.feather"))
    if not prep_files:
        print("   ⚠️  prep 캐시 없음 → comparison 생성 불가")
        return

    ref = pd.read_feather(str(prep_files[0]))
    ref['date'] = pd.to_datetime(ref['date'])
    ref = ref.set_index('date')[['kospi_change', 'kosdaq_change']].sort_index()

    rows = []
    for _, row in summary_df.iterrows():
        ts = pd.Timestamp(str(int(row['test_start'])))
        te = pd.Timestamp(str(int(row['test_end'])))
        period = ref[(ref.index >= ts) & (ref.index < te)]

        kospi_ret  = ((1 + period['kospi_change']).prod()  - 1) * 100
        kosdaq_ret = ((1 + period['kosdaq_change']).prod() - 1) * 100
        strat_ret  = float(row['수익률(%)'])

        rows.append({
            "Fold":          int(row['fold_id']),
            "테스트기간":     "{:d}~{:d}".format(ts.year, te.year),
            "전략(%)":        round(strat_ret, 2),
            "KOSPI(%)":      round(kospi_ret, 2),
            "KOSDAQ(%)":     round(kosdaq_ret, 2),
            "vs KOSPI(%p)":  round(strat_ret - kospi_ret, 2),
            "vs KOSDAQ(%p)": round(strat_ret - kosdaq_ret, 2),
        })

    comp_df = pd.DataFrame(rows)
    comp_df.to_csv(WF_DIR / "comparison.csv", index=False, encoding='utf-8-sig')
    print(f"\n📊 comparison.csv 저장 ({len(rows)}개 폴드)")
    print(comp_df.to_string(index=False))
    return comp_df


def _print_summary(all_stats):
    if not all_stats:
        return
    df = pd.DataFrame(all_stats)
    print(f"\n{'폴드':>5}  {'테스트 기간':^21}  {'수익률':>8}  {'Sharpe':>7}  {'최대낙폭':>9}  {'일승률':>7}")
    print("-" * 65)
    for _, row in df.iterrows():
        print(f"  {int(row['fold_id']):02d}   {row['test_start']} ~ {row['test_end']}   "
              f"{row['수익률(%)']:+8.2f}%  "
              f"{row['Sharpe']:7.2f}  "
              f"{row['최대낙폭(%)']:8.2f}%  "
              f"{row['일승률(%)']:6.1f}%")
    if len(df) > 1:
        print("-" * 65)
        print(f"  평균                                   "
              f"{df['수익률(%)'].mean():+8.2f}%  "
              f"{df['Sharpe'].mean():7.2f}  "
              f"{df['최대낙폭(%)'].mean():8.2f}%  "
              f"{df['일승률(%)'].mean():6.1f}%")
    print(f"\n결과 저장: {WF_DIR / 'summary.csv'}")


if __name__ == "__main__":
    main()