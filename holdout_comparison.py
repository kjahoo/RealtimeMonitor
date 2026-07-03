"""
holdout_comparison.py
====================
Expanding WF (13폴드) + Rolling7y WF (8폴드) + Production 모델을
두 holdout 기간에서 연도별로 비교.

  Holdout-A : 2000~2006  (전 모델 학습 前 기간)
  Holdout-B : 2026       (전 모델 OOS後 기간, 연초~오늘)

출력 지표: 수익률(%), Sharpe, MDD(%), 일승률(%), 회전율(%), 거래횟수

실행:
  python -X utf8 holdout_comparison.py
  python -X utf8 holdout_comparison.py --no-cache   # 스코어 캐시 재계산
  python -X utf8 holdout_comparison.py --skip-a     # Holdout-A 건너뜀 (2026만)
  python -X utf8 holdout_comparison.py --skip-b     # Holdout-B 건너뜀 (2000~2006만)

결과:
  holdout_comparison_results.csv   전체 연도×모델 상세
  holdout_comparison_summary.csv   구간별 평균 요약
"""

import os, sys, gc, warnings, pickle, time, argparse
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import date as dtdate

warnings.filterwarnings("ignore")
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

import tensorflow as tf
from tensorflow.keras.models import load_model

ROOT      = Path(r"C:\Projects\RealtimeMonitor")
DATA_DIR  = ROOT / "Data" / "Stock"
PREP_DIR  = ROOT / "Data" / "_prep_wf_v3"
WF_DIR    = ROOT / "walk_forward"
ROLL_DIR  = ROOT / "walk_forward_rolling7y"
PROD_DIR  = ROOT / "Model"
CACHE_DIR = ROOT / "holdout_caches"
CACHE_DIR.mkdir(exist_ok=True)

# ── GPU ───────────────────────────────────────────────────────────────────────
def _setup_gpu():
    gpus = tf.config.list_physical_devices('GPU')
    if not gpus:
        return False, 1024
    try:
        for g in gpus:
            tf.config.experimental.set_memory_growth(g, True)
        return True, 4096
    except Exception as e:
        if "cannot be modified after being initialized" in str(e):
            return True, 4096
        return False, 1024

_USE_GPU, _BATCH_SZ = _setup_gpu()
print(f"   {'GPU' if _USE_GPU else 'CPU'}  배치: {_BATCH_SZ}")

# ── 품질 필터 헬퍼 ────────────────────────────────────────────────────────────
def _max_consecutive(bool_series: pd.Series) -> int:
    if bool_series.empty:
        return 0
    g = (bool_series != bool_series.shift()).cumsum()
    return int(bool_series.groupby(g).sum().max())


def _is_bad_stock(close: pd.Series, volume: pd.Series = None) -> bool:
    if len(close) < 2:
        return True
    if _max_consecutive(close.diff() == 0) >= 14:   # 연속 동일종가 15일+
        return True
    if (close.pct_change().abs() > 0.40).any():     # 일간 변동 40%+
        return True
    if volume is not None:
        if _max_consecutive(volume == 0) >= 10:     # 연속 거래량 0 10일+
            return True
    return False

# ── 전략 파라미터 (select_best_model.py 와 동일) ──────────────────────────────
INITIAL_CAPITAL = 1_000_000_000
BUY_THRESH      = 0.55
ALLOC_PER_STOCK = 0.10
SELL_THRESH     = 0.50
BUY_FEE_RATE    = 0.00015
SELL_FEE_RATE   = 0.00195

# ── 분석 기간 ──────────────────────────────────────────────────────────────────
HOLDOUT_A = list(range(2000, 2007))   # 2000~2006
HOLDOUT_B = [2026]                    # 2026 연초~오늘

TODAY = pd.Timestamp(dtdate.today())

# ── 피처 정의 ──────────────────────────────────────────────────────────────────
# WF 폴드용 피처 (prep feather 기준)
WF_FEATURES = [
    'change_pct', 'volume_ratio', 'vol_power',
    'prog_net_ratio', 'prog_ratio_vol',
    'disparity_5', 'disparity_20',
    'rsi_v3', 'bb_p_v3', 'bb_w_v3', 'adx_v3',
    'kospi_change', 'kosdaq_change',
]
# 프로덕션용 피처 (raw CSV 기준)
PROD_FEATURES = [
    'change_pct', 'volume_ratio', 'vol_power',
    'prog_net_ratio', 'prog_ratio_vol',
    'disparity_5', 'disparity_20',
    'rsi', 'bb_p', 'bb_w', 'adx',
    'kospi_change', 'kosdaq_change',
]
# raw CSV → WF 피처명 매핑 (2000~2005 구간에서 WF 모델 사용 시)
RAW_TO_WF_MAP = {'rsi': 'rsi_v3', 'bb_p': 'bb_p_v3', 'bb_w': 'bb_w_v3', 'adx': 'adx_v3'}

MODEL_DEFAULTS = {
    "target1":  {"lb": 21, "thr": 0.5108, "weight": 0.1962, "type": "surge"},
    "target5":  {"lb": 50, "thr": 0.6555, "weight": 0.4234, "type": "surge"},
    "target20": {"lb": 60, "thr": 0.3291, "weight": 0.3804, "type": "surge"},
    "drop1":    {"lb": 10, "thr": 0.4512, "weight": 0.2369, "type": "drop"},
    "drop5":    {"lb": 94, "thr": 0.3431, "weight": 0.3537, "type": "drop"},
    "drop20":   {"lb": 98, "thr": 0.5445, "weight": 0.4095, "type": "drop"},
}
MAX_LOOKBACK = max(v["lb"] for v in MODEL_DEFAULTS.values())   # 98


# ══════════════════════════════════════════════════════════════════════════════
# 모델 목록 수집
# ══════════════════════════════════════════════════════════════════════════════

def collect_model_defs():
    """비교할 모든 모델 정의를 반환."""
    mdefs = []

    # Expanding WF
    for fd in sorted(WF_DIR.glob("fold_*")):
        try:
            fold_id = int(fd.name.split("_")[1])
        except (IndexError, ValueError):
            continue
        model_files = [fd / "models" / f"{m}_lstm_v3.h5" for m in MODEL_DEFAULTS]
        if not all(f.exists() for f in model_files):
            continue
        mdefs.append({
            "tag":        f"exp_{fold_id:02d}",
            "label":      f"Exp-{fold_id:02d}",
            "fold_dir":   fd,
            "model_type": "wf",
        })

    # Rolling 7y WF
    for fd in sorted(ROLL_DIR.glob("fold_*")):
        try:
            fold_id = int(fd.name.split("_")[1])
        except (IndexError, ValueError):
            continue
        model_files = [fd / "models" / f"{m}_lstm_v3.h5" for m in MODEL_DEFAULTS]
        if not all(f.exists() for f in model_files):
            continue
        mdefs.append({
            "tag":        f"roll_{fold_id:02d}",
            "label":      f"Roll-{fold_id:02d}",
            "fold_dir":   fd,
            "model_type": "wf",
        })

    # Production
    prod_ok = all((PROD_DIR / f"{m}_lstm_v3.h5").exists() for m in MODEL_DEFAULTS)
    if prod_ok:
        mdefs.append({
            "tag":        "production",
            "label":      "Production",
            "fold_dir":   PROD_DIR,
            "model_type": "prod",
        })

    return mdefs


# ══════════════════════════════════════════════════════════════════════════════
# 모델 로드
# ══════════════════════════════════════════════════════════════════════════════

def load_tf_models(fold_dir: Path, is_prod: bool) -> dict:
    models  = {}
    best_f1 = {}

    for m_name, cfg in MODEL_DEFAULTS.items():
        if is_prod:
            h5  = fold_dir / f"{m_name}_lstm_v3.h5"
            scl = fold_dir / f"{m_name}_lstm_v3.scaler"
            log = fold_dir / f"log_{m_name}_v3_v3_unified.csv"
        else:
            h5  = fold_dir / "models" / f"{m_name}_lstm_v3.h5"
            scl = fold_dir / "models" / f"{m_name}_lstm_v3.scaler"
            log = fold_dir / "models" / f"log_{m_name}_v3.csv"

        if not h5.exists() or not scl.exists():
            continue
        with open(scl, 'rb') as f:
            scaler = pickle.load(f)
        model     = load_model(str(h5), compile=False)
        actual_lb = model.input_shape[1]

        if log.exists():
            ldf      = pd.read_csv(log)
            best_row = ldf.loc[ldf['f1'].idxmax()]
            threshold       = float(best_row['threshold'])
            best_f1[m_name] = float(best_row['f1'])
        else:
            threshold = cfg["thr"]

        models[m_name] = {
            "model":     model,
            "scaler":    scaler,
            "lookback":  actual_lb,
            "threshold": threshold,
            "weight":    cfg["weight"],
            "type":      cfg["type"],
        }

    # F1 비례 가중치 재계산
    for group in ("surge", "drop"):
        names = [n for n, c in MODEL_DEFAULTS.items() if c["type"] == group and n in models]
        f1s   = [best_f1.get(n) for n in names]
        if all(f is not None for f in f1s):
            tot = sum(f1s)
            for n, f in zip(names, f1s):
                models[n]["weight"] = f / tot

    return models


# ══════════════════════════════════════════════════════════════════════════════
# 스코어 계산
# ══════════════════════════════════════════════════════════════════════════════

def _score_raw(feat_data: np.ndarray, tf_models: dict) -> dict:
    n, n_feat = len(feat_data), feat_data.shape[1]
    day_probs: dict = {}

    for m_name, info in tf_models.items():
        lb = info["lookback"]
        if n < lb:
            continue
        n_win = n - lb + 1
        idx   = np.arange(n_win)[:, None] + np.arange(lb)[None, :]
        wins  = feat_data[idx].astype(np.float32)
        flat  = info["scaler"].transform(wins.reshape(-1, n_feat)).astype(np.float32)
        wins  = tf.constant(flat.reshape(n_win, lb, n_feat))

        preds = np.concatenate([
            info["model"](wins[s: s + _BATCH_SZ], training=False).numpy().flatten()
            for s in range(0, n_win, _BATCH_SZ)
        ])
        for i, prob in enumerate(preds):
            day_probs.setdefault(lb - 1 + i, {})[m_name] = float(prob)
        del wins, flat, preds

    result = {}
    for di, probs in day_probs.items():
        s = d = 0.0
        for m_name, prob in probs.items():
            info = tf_models[m_name]
            if prob > info["threshold"]:
                if info["type"] == "surge":
                    s += prob * info["weight"]
                else:
                    d += prob * info["weight"]
        result[di] = round(s - d, 4)
    return result


def _choose_source(year: int, is_prod: bool):
    """
    반환: (use_feather: bool)
    - prep feather: WF 모델 && 2006년 이상 && feather 파일 존재
    - 그 외: raw CSV
    """
    if is_prod:
        return False
    if year < 2006:
        return False
    return len(list(PREP_DIR.glob("A*.feather"))) > 0


def compute_scores_year(
    tf_models: dict,
    features:  list,
    year:      int,
    is_prod:   bool,
    cache_path: Path,
    no_cache:  bool,
) -> pd.DataFrame:
    if cache_path.exists() and not no_cache:
        df = pd.read_pickle(str(cache_path))
        print(f"캐시({len(df):,}건)", end=" ")
        return df

    use_feather = _choose_source(year, is_prod)
    y_start   = pd.Timestamp(f"{year}-01-01")
    y_end     = pd.Timestamp(f"{year+1}-01-01")
    if year == 2026:
        y_end = min(TODAY + pd.Timedelta(days=1), y_end)
    load_from = y_start - pd.DateOffset(days=MAX_LOOKBACK * 2)

    file_list = (sorted(PREP_DIR.glob("A*.feather")) if use_feather
                 else sorted(DATA_DIR.glob("A*.csv")))
    n_files = len(file_list)
    n_feat  = len(features)

    # ── Phase 1: 전종목 로딩 + 품질 필터 ─────────────────────────────────────
    stock_data = []   # [(code, dates_np, feat_np), ...]
    t0 = time.time()
    excl = 0

    for i, fpath in enumerate(file_list):
        code = fpath.stem[1:]
        try:
            if fpath.suffix == '.feather':
                df = pd.read_feather(str(fpath))
            else:
                df = pd.read_csv(fpath, encoding='utf-8-sig',
                                 dtype={'code': str, 'name': str})
                if not is_prod:
                    df = df.rename(columns=RAW_TO_WF_MAP)

            df['date'] = pd.to_datetime(df['date'])
            df = df[df['date'] >= load_from].sort_values('date').reset_index(drop=True)

            if len(df) < MAX_LOOKBACK:
                continue

            # 품질 필터: 전체 기간 기준
            close  = df['close'].astype(float) if 'close' in df.columns else None
            volume = df['volume'].astype(float) if 'volume' in df.columns else None
            if close is not None and _is_bad_stock(close, volume):
                excl += 1
                continue

            for col in features:
                if col not in df.columns:
                    df[col] = 0.0
            df[features] = df[features].fillna(0)

            stock_data.append((
                code,
                df['date'].values,
                df[features].values.astype(np.float32),
            ))
        except Exception:
            pass

        if (i + 1) % 500 == 0 or i == n_files - 1:
            el = time.time() - t0
            rate = (i + 1) / el if el > 0 else 1
            print(f"\r   로딩 [{i+1:4d}/{n_files}]  {el/60:.1f}분  "
                  f"유효: {len(stock_data):,}  제외: {excl}", end="", flush=True)

    print(f"\n   로딩 완료: 유효 {len(stock_data):,}종목 / 제외 {excl}종목")
    if not stock_data:
        return pd.DataFrame()

    # ── Phase 2: 모델별 GPU 배치 추론 ────────────────────────────────────────
    # (code, day_idx) → {m_name: prob}
    probs_map: dict = {}
    # cuDNN LSTM 배치 한계: lb×batch 기준, 2048이 안전. 감시 스크립트가
    # 반복 크래시 시 HOLDOUT_GPU_CHUNK 환경변수로 더 작게 낮춰 돌파할 수 있음.
    GPU_CHUNK = int(os.environ.get("HOLDOUT_GPU_CHUNK", _BATCH_SZ // 2))

    for m_name, info in tf_models.items():
        lb     = info["lookback"]
        scaler = info["scaler"]
        model  = info["model"]

        # 전종목 윈도우 수집
        all_wins  = []   # list of (n_win, lb, n_feat) arrays
        meta      = []   # list of (code, start_day_idx) per stock

        for code, dates_np, feat_arr in stock_data:
            n = len(feat_arr)
            if n < lb:
                continue
            n_win = n - lb + 1
            idx   = np.arange(n_win)[:, None] + np.arange(lb)[None, :]
            wins  = feat_arr[idx]                          # (n_win, lb, n_feat)
            flat  = scaler.transform(wins.reshape(-1, n_feat)).astype(np.float32)
            all_wins.append(flat.reshape(n_win, lb, n_feat))
            meta.append((code, lb - 1, n_win))

        if not all_wins:
            continue

        # 청크 단위 GPU 추론
        cat  = np.concatenate(all_wins, axis=0)    # (total_wins, lb, n_feat)
        total = len(cat)
        preds = np.empty(total, dtype=np.float32)
        for s in range(0, total, GPU_CHUNK):
            chunk = tf.constant(cat[s: s + GPU_CHUNK])
            preds[s: s + GPU_CHUNK] = model(chunk, training=False).numpy().flatten()
        del cat

        # 결과 매핑
        ptr = 0
        for code, base_di, n_win in meta:
            for wi in range(n_win):
                key = (code, base_di + wi)
                if key not in probs_map:
                    probs_map[key] = {}
                probs_map[key][m_name] = float(preds[ptr])
                ptr += 1
        del all_wins, preds
        gc.collect()

    # ── Phase 3: 최종 점수 계산 + 날짜 필터 ─────────────────────────────────
    code_dates = {code: dates_np for code, dates_np, _ in stock_data}
    records = []
    for (code, day_idx), probs in probs_map.items():
        s = d = 0.0
        for m_name, prob in probs.items():
            info = tf_models[m_name]
            if prob > info["threshold"]:
                if info["type"] == "surge":
                    s += prob * info["weight"]
                else:
                    d += prob * info["weight"]
        score     = round(s - d, 4)
        dates_np  = code_dates.get(code)
        if dates_np is None or day_idx >= len(dates_np):
            continue
        date = pd.Timestamp(dates_np[day_idx])
        if y_start <= date < y_end:
            records.append({"date": date, "code": code, "score": score})

    score_df = pd.DataFrame(records)
    if not score_df.empty:
        score_df['date'] = pd.to_datetime(score_df['date'])
        pd.to_pickle(score_df, str(cache_path))
    return score_df


# ══════════════════════════════════════════════════════════════════════════════
# 포트폴리오 & 시뮬레이션
# ══════════════════════════════════════════════════════════════════════════════

class _Portfolio:
    def __init__(self):
        self.cash       = float(INITIAL_CAPITAL)
        self.positions  = {}
        self.trade_log  = []   # {"date", "action", "code", "qty", "price", "amount"}

    def _price(self, code, prices):
        p = prices.get(code)
        if p and p > 0:
            return p
        pos = self.positions.get(code)
        return pos["avg_price"] if pos else 0.0

    def total_assets(self, prices):
        mv = sum(pos["qty"] * self._price(code, prices)
                 for code, pos in self.positions.items())
        return self.cash + mv

    def market_value(self, code, prices):
        pos = self.positions.get(code)
        return pos["qty"] * self._price(code, prices) if pos else 0.0

    def buy(self, code, amount, price, date):
        qty  = int(amount / price)
        cost = qty * price * (1 + BUY_FEE_RATE)
        if qty == 0 or cost > self.cash:
            return False
        self.cash -= cost
        if code in self.positions:
            pos     = self.positions[code]
            new_qty = pos["qty"] + qty
            pos["avg_price"] = (pos["qty"] * pos["avg_price"] + qty * price) / new_qty
            pos["qty"]       = new_qty
        else:
            self.positions[code] = {"qty": qty, "avg_price": price}
        self.trade_log.append({"date": date, "action": "BUY",  "code": code,
                                "qty": qty, "price": price, "amount": qty * price})
        return True

    def sell_all(self, code, price, date):
        pos = self.positions.pop(code, None)
        if not pos:
            return
        qty = pos["qty"]
        self.cash += qty * price * (1 - SELL_FEE_RATE)
        self.trade_log.append({"date": date, "action": "SELL", "code": code,
                                "qty": qty, "price": price, "amount": qty * price})


def run_simulation(score_df, year, price_pivot):
    y_start = pd.Timestamp(f"{year}-01-01")
    y_end   = pd.Timestamp(f"{year+1}-01-01")
    if year == 2026:
        y_end = min(TODAY + pd.Timedelta(days=1), y_end)

    scores_by_date = (
        score_df.groupby('date')
        .apply(lambda g: dict(zip(g['code'], g['score'])))
        .to_dict()
    )
    mask         = (price_pivot.index >= y_start) & (price_pivot.index < y_end)
    trading_days = price_pivot.loc[mask].index.tolist()

    portfolio = _Portfolio()
    history   = []

    for date in trading_days:
        if date not in price_pivot.index:
            continue
        prices       = price_pivot.loc[date].dropna().to_dict()
        today_scores = scores_by_date.get(date, {})
        total        = portfolio.total_assets(prices)

        # 매도: score < SELL_THRESH
        for code in list(portfolio.positions.keys()):
            score = today_scores.get(code)
            price = prices.get(code)
            if score is None or not price or price <= 0:
                continue
            if score < SELL_THRESH:
                portfolio.sell_all(code, price, date)

        # 매수: score >= BUY_THRESH, ALLOC_PER_STOCK 까지 증분 매수
        total      = portfolio.total_assets(prices)
        candidates = []
        for code, score in today_scores.items():
            price = prices.get(code)
            if not price or price <= 0 or score < BUY_THRESH:
                continue
            cur_alloc = portfolio.market_value(code, prices) / total if total > 0 else 0.0
            if ALLOC_PER_STOCK > cur_alloc + 0.01:
                candidates.append((code, score, ALLOC_PER_STOCK - cur_alloc))

        for code, score, incr in sorted(candidates, key=lambda x: x[1], reverse=True):
            buy_amount = incr * total
            price      = prices[code]
            if portfolio.cash < buy_amount:
                continue
            portfolio.buy(code, buy_amount, price, date)

        history.append({
            "date":         date,
            "total_assets": portfolio.total_assets(prices),
        })

    h_df = pd.DataFrame(history)
    t_df = pd.DataFrame(portfolio.trade_log)
    return h_df, t_df


# ══════════════════════════════════════════════════════════════════════════════
# 성과 지표 계산
# ══════════════════════════════════════════════════════════════════════════════

def calc_metrics(history_df, trade_df):
    if history_df.empty or len(history_df) < 5:
        return None

    ta        = history_df.set_index("date")["total_assets"]
    ret       = (ta.iloc[-1] / ta.iloc[0] - 1) * 100
    daily_ret = ta.pct_change().dropna()
    sharpe    = (daily_ret.mean() / daily_ret.std() * np.sqrt(252)
                 if daily_ret.std() > 0 else 0.0)
    max_dd    = ((ta - ta.cummax()) / ta.cummax()).min() * 100
    win_rate  = (daily_ret > 0).sum() / len(daily_ret) * 100

    n_days    = len(ta)
    avg_asset = ta.mean()

    # 회전율(%): 연환산 (매수+매도 총금액) / (2×평균자산) × 100
    if not trade_df.empty and avg_asset > 0:
        total_amount = trade_df['amount'].sum()
        turnover = (total_amount / avg_asset / 2) * (252 / n_days) * 100
    else:
        turnover = 0.0

    n_trades = len(trade_df) if not trade_df.empty else 0

    return {
        "수익률(%)":  round(ret,       2),
        "Sharpe":     round(sharpe,    2),
        "MDD(%)":     round(max_dd,    2),
        "일승률(%)":  round(win_rate,  1),
        "회전율(%)":  round(turnover,  1),
        "거래횟수":   n_trades,
        "거래일수":   n_days,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 데이터 로드 헬퍼
# ══════════════════════════════════════════════════════════════════════════════

def load_prices(years):
    """연도 리스트에 해당하는 종가 피벗 테이블 반환 (품질 필터 적용)."""
    y_min = min(years)
    y_max = max(years)
    h_start = pd.Timestamp(f"{y_min}-01-01")
    h_end   = pd.Timestamp(f"{y_max+1}-01-01")
    if y_max == 2026:
        h_end = min(TODAY + pd.Timedelta(days=1), h_end)

    frames = []
    excl = 0
    for fpath in DATA_DIR.glob("A*.csv"):
        code = fpath.stem[1:]
        try:
            df = pd.read_csv(fpath, encoding='utf-8-sig',
                             usecols=lambda c: c in ['date', 'close', 'volume'],
                             dtype={'close': float})
            df['date'] = pd.to_datetime(df['date'])
            df = df[(df['date'] >= h_start) & (df['date'] <= h_end)].sort_values('date').reset_index(drop=True)
            if df.empty:
                continue
            close  = df['close'].astype(float)
            volume = df['volume'].astype(float) if 'volume' in df.columns else None
            if _is_bad_stock(close, volume):
                excl += 1
                continue
            frames.append(df[['date', 'close']].assign(code=code))
        except Exception:
            pass

    print(f"   품질 필터 제외: {excl}개")
    if not frames:
        return pd.DataFrame()
    all_df = pd.concat(frames, ignore_index=True)
    return all_df.pivot_table(index='date', columns='code', values='close').sort_index()


def get_kospi_by_year(years):
    """연도별 KOSPI 수익률 딕셔너리. raw CSV에서 읽어 2000년부터 지원."""
    result = {}
    ref_path = DATA_DIR / "A005930.csv"
    if not ref_path.exists():
        candidates = sorted(DATA_DIR.glob("A*.csv"))
        if not candidates:
            return result
        ref_path = candidates[0]
    try:
        ref = pd.read_csv(str(ref_path), encoding='utf-8-sig',
                           usecols=['date', 'kospi_change'])
        ref['date'] = pd.to_datetime(ref['date'])
        ref = ref.set_index('date').sort_index()
        for year in years:
            period = ref[ref.index.year == year]
            if not period.empty:
                result[year] = round(((1 + period['kospi_change']).prod() - 1) * 100, 2)
    except Exception:
        pass
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 출력 헬퍼
# ══════════════════════════════════════════════════════════════════════════════

def print_annual_table(df, period_tag, kospi_dict, metric="수익률(%)"):
    sub = df[df['구분'] == period_tag].copy()
    if sub.empty:
        print("  (결과 없음)")
        return

    years  = sorted(sub['연도'].unique())
    models = sub['모델'].unique()

    # 헤더
    yr_w  = 9
    hdr   = f"  {'모델':<20}"
    for y in years:
        hdr += f"  {y:>{yr_w}}"
    hdr += f"  {'평균':>{yr_w}}  {'KOSPI초과':>10}"
    print(hdr)
    print("  " + "─" * (20 + (yr_w + 2) * (len(years) + 1) + 14))

    # 각 모델 행
    for model in models:
        mdf  = sub[sub['모델'] == model]
        line = f"  {model:<20}"
        yr_vals  = []
        exc_vals = []
        for y in years:
            row = mdf[mdf['연도'] == y]
            if row.empty:
                line += f"  {'N/A':>{yr_w}}"
            else:
                v = float(row[metric].iloc[0])
                yr_vals.append(v)
                k = kospi_dict.get(y, float('nan'))
                if not np.isnan(k):
                    exc_vals.append(v - k)
                if metric == "수익률(%)":
                    line += f"  {v:>+{yr_w}.1f}%"
                elif metric in ("Sharpe", "회전율(%)"):
                    line += f"  {v:>{yr_w}.2f}"
                elif metric == "MDD(%)":
                    line += f"  {v:>{yr_w}.1f}%"
                elif metric == "일승률(%)":
                    line += f"  {v:>{yr_w}.1f}%"
                else:
                    line += f"  {v:>{yr_w}g}"
        avg = np.mean(yr_vals)  if yr_vals  else float('nan')
        exc = np.mean(exc_vals) if exc_vals else float('nan')
        if metric == "수익률(%)":
            line += f"  {avg:>+{yr_w}.1f}%"
            line += f"  {exc:>+9.1f}%p" if not np.isnan(exc) else "       N/A"
        else:
            line += f"  {avg:>{yr_w}.2f}" + " " * 12
        print(line)

    # KOSPI 벤치마크 행
    if metric == "수익률(%)":
        kline = f"  {'[KOSPI]':<20}"
        kvals = []
        for y in years:
            k = kospi_dict.get(y, float('nan'))
            kvals.append(k)
            kline += f"  {k:>+{yr_w}.1f}%" if not np.isnan(k) else f"  {'N/A':>{yr_w}}"
        kavg = np.nanmean(kvals) if kvals else float('nan')
        kline += f"  {kavg:>+{yr_w}.1f}%"
        print(kline)


def print_summary(df, kospi_dict):
    """모델별 구간 평균 요약 출력."""
    print(f"\n  {'모델':<20}  {'구분':<12}  {'수익률':>8}  {'KOSPI초과':>10}  "
          f"{'Sharpe':>7}  {'MDD':>8}  {'일승률':>7}  {'회전율':>7}  {'거래/년':>7}")
    print("  " + "─" * 92)

    for model in df['모델'].unique():
        mdf   = df[df['모델'] == model]
        first = True
        for tag in ["Holdout-A", "Holdout-B"]:
            part = mdf[mdf['구분'] == tag]
            if part.empty:
                continue
            valid    = part.dropna(subset=['KOSPI(%)'])
            avg_ret  = part['수익률(%)'].mean()
            avg_exc  = valid['초과(%p)'].mean() if not valid.empty else float('nan')
            avg_sh   = part['Sharpe'].mean()
            avg_mdd  = part['MDD(%)'].mean()
            avg_wr   = part['일승률(%)'].mean()
            avg_turn = part['회전율(%)'].mean()
            n_tr_yr  = part['거래횟수'].mean()
            mname    = model if first else ""
            first    = False
            exc_s    = f"{avg_exc:>+9.1f}%p" if not np.isnan(avg_exc) else "        N/A"
            print(f"  {mname:<20}  {tag:<12}  {avg_ret:>+7.1f}%  {exc_s}  "
                  f"{avg_sh:>7.2f}  {avg_mdd:>7.1f}%  {avg_wr:>6.1f}%  "
                  f"{avg_turn:>6.1f}%  {n_tr_yr:>7.0f}")
        if not first:
            print("  " + "─" * 92)


# ══════════════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="전체 모델 Holdout 비교")
    parser.add_argument("--no-cache", action="store_true", help="스코어 캐시 재계산")
    parser.add_argument("--skip-a",  action="store_true", help="Holdout-A(2000~2006) 건너뜀")
    parser.add_argument("--skip-b",  action="store_true", help="Holdout-B(2026) 건너뜀")
    args = parser.parse_args()

    target_years = []
    if not args.skip_a:
        target_years += HOLDOUT_A
    if not args.skip_b:
        target_years += HOLDOUT_B

    if not target_years:
        print("❌ 분석할 기간 없음 (--skip-a 와 --skip-b 를 동시에 주면 아무것도 안 함)")
        return

    print("\n" + "=" * 72)
    print("📊  전체 모델 Holdout 비교 분석")
    print(f"    Holdout-A : 2000~2006  (전 모델 학습 前)")
    print(f"    Holdout-B : 2026       (전 모델 OOS後, {TODAY.date()} 까지)")
    print(f"    전략: BUY>={BUY_THRESH}  SELL<{SELL_THRESH}  ALLOC={ALLOC_PER_STOCK*100:.0f}%/종목")
    print("=" * 72)

    # 모델 목록
    model_list = collect_model_defs()
    print(f"\n모델 수: {len(model_list)}개")
    for m in model_list:
        print(f"  {m['label']:<20}  ({m['model_type']})  {m['fold_dir'].name}")

    # KOSPI 벤치마크
    kospi_dict = get_kospi_by_year(target_years)
    print(f"\nKOSPI 벤치마크:")
    for y in target_years:
        k = kospi_dict.get(y, float('nan'))
        ks = f"{k:+.2f}%" if not np.isnan(k) else "N/A"
        print(f"  {y}: {ks}")

    # 가격 데이터 (전 기간 한 번만 로딩). 감시 스크립트의 잦은 재시작 시
    # 매번 ~6분 재로딩을 피하려고 pkl 캐시. (당일 내 종가는 안정적이라 무방)
    print(f"\n[공통] 종가 데이터 로딩 ({min(target_years)}~{max(target_years)})...")
    pp_cache = CACHE_DIR / f"_price_pivot_{min(target_years)}_{max(target_years)}.pkl"
    if pp_cache.exists() and not args.no_cache:
        price_pivot = pd.read_pickle(str(pp_cache))
        print(f"   가격 캐시 사용: {pp_cache.name}")
    else:
        price_pivot = load_prices(target_years)
        if not price_pivot.empty:
            pd.to_pickle(price_pivot, str(pp_cache))
    if price_pivot.empty:
        print("❌ 가격 데이터 없음")
        return
    print(f"   종목: {price_pivot.shape[1]:,}  거래일: {price_pivot.shape[0]:,}")

    all_rows = []

    for mi, mdef in enumerate(model_list):
        tag      = mdef["tag"]
        label    = mdef["label"]
        fold_dir = mdef["fold_dir"]
        is_prod  = (mdef["model_type"] == "prod")
        features = PROD_FEATURES if is_prod else WF_FEATURES

        print(f"\n{'='*72}")
        print(f"[{mi+1}/{len(model_list)}]  {label}  ({fold_dir.name})")
        print(f"{'='*72}")

        # 모델 로드
        print("  모델 로딩...", end=" ", flush=True)
        tf_models = load_tf_models(fold_dir, is_prod)
        if not tf_models:
            print("❌ 실패, 건너뜀")
            continue
        print(f"{len(tf_models)}개 서브모델 로드 완료")
        for m_name, info in tf_models.items():
            print(f"     {m_name:<10} lb={info['lookback']:>3}  thr={info['threshold']:.4f}  "
                  f"w={info['weight']:.4f}")

        for year in target_years:
            period_tag = "Holdout-A" if year in HOLDOUT_A else "Holdout-B"
            cache_path = CACHE_DIR / f"scores_{tag}_{year}.pkl"

            print(f"\n  [{year}] 스코어 계산... ", end="", flush=True)
            score_df = compute_scores_year(tf_models, features, year, is_prod,
                                            cache_path, args.no_cache)

            if score_df.empty:
                print("스코어 없음 — 건너뜀")
                continue

            # 시뮬레이션
            history_df, trade_df = run_simulation(score_df, year, price_pivot)

            # 지표 계산
            stats = calc_metrics(history_df, trade_df)
            if not stats:
                print("거래 없음 — 건너뜀")
                continue

            kospi  = kospi_dict.get(year, float('nan'))
            excess = round(stats['수익률(%)'] - kospi, 2) if not np.isnan(kospi) else float('nan')

            # 콘솔 출력
            kospi_s  = f"{kospi:+.2f}%" if not np.isnan(kospi) else " N/A"
            excess_s = f"{excess:+.2f}%p" if not np.isnan(excess) else " N/A"
            print(f"  수익:{stats['수익률(%)']:+7.2f}%  KOSPI:{kospi_s}  "
                  f"초과:{excess_s}  Sh:{stats['Sharpe']:5.2f}  "
                  f"MDD:{stats['MDD(%)']:6.2f}%  승:{stats['일승률(%)']: 5.1f}%  "
                  f"회전:{stats['회전율(%)']: 6.1f}%  거래:{stats['거래횟수']}건")

            all_rows.append({
                "구분":       period_tag,
                "연도":       year,
                "모델":       label,
                "태그":       tag,
                "수익률(%)":  stats["수익률(%)"],
                "Sharpe":     stats["Sharpe"],
                "MDD(%)":     stats["MDD(%)"],
                "일승률(%)":  stats["일승률(%)"],
                "회전율(%)":  stats["회전율(%)"],
                "거래횟수":   stats["거래횟수"],
                "거래일수":   stats["거래일수"],
                "KOSPI(%)":   kospi,
                "초과(%p)":   excess,
            })

        # 메모리 정리
        del tf_models
        gc.collect()
        tf.keras.backend.clear_session()

    if not all_rows:
        print("\n❌ 결과 없음")
        return

    result_df = pd.DataFrame(all_rows)

    # ── 결과 저장 ─────────────────────────────────────────────────────────────
    detail_path  = ROOT / "holdout_comparison_results.csv"
    summary_path = ROOT / "holdout_comparison_summary.csv"
    result_df.to_csv(detail_path, index=False, encoding='utf-8-sig')
    print(f"\n\n💾 상세 결과: {detail_path}")

    # 요약 CSV: 모델 × 구간별 평균
    summary_rows = []
    for model in result_df['모델'].unique():
        mdf = result_df[result_df['모델'] == model]
        for tag in ["Holdout-A", "Holdout-B"]:
            part = mdf[mdf['구분'] == tag]
            if part.empty:
                continue
            valid = part.dropna(subset=['KOSPI(%)'])
            summary_rows.append({
                "모델":       model,
                "구분":       tag,
                "연수":       len(part),
                "평균수익(%)":  round(part['수익률(%)'].mean(), 2),
                "평균KOSPI초과(%p)": round(valid['초과(%p)'].mean(), 2) if not valid.empty else float('nan'),
                "KOSPI초과(연수)": f"{int((valid['초과(%p)']>0).sum())}/{len(valid)}",
                "평균Sharpe":   round(part['Sharpe'].mean(), 2),
                "평균MDD(%)":   round(part['MDD(%)'].mean(), 2),
                "평균일승률(%)": round(part['일승률(%)'].mean(), 1),
                "평균회전율(%)": round(part['회전율(%)'].mean(), 1),
                "평균거래횟수":  round(part['거래횟수'].mean(), 0),
                "누적수익(%)":   round(((1 + part['수익률(%)']/100).prod() - 1) * 100, 2),
            })
    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(summary_path, index=False, encoding='utf-8-sig')
    print(f"💾 요약 결과: {summary_path}")

    # ── 콘솔 최종 출력 ────────────────────────────────────────────────────────
    if not args.skip_a:
        print("\n\n" + "=" * 90)
        print(f" 📊 Holdout-A  수익률(%) — 2000~2006 연도별")
        print("=" * 90)
        print_annual_table(result_df, "Holdout-A", kospi_dict, "수익률(%)")

        print("\n" + "─" * 90)
        print(" Sharpe")
        print("─" * 90)
        print_annual_table(result_df, "Holdout-A", kospi_dict, "Sharpe")

        print("\n" + "─" * 90)
        print(" MDD(%)")
        print("─" * 90)
        print_annual_table(result_df, "Holdout-A", kospi_dict, "MDD(%)")

    if not args.skip_b:
        print("\n\n" + "=" * 90)
        print(f" 📊 Holdout-B  수익률(%) — 2026년 결과")
        print("=" * 90)
        print_annual_table(result_df, "Holdout-B", kospi_dict, "수익률(%)")

    print("\n\n" + "=" * 92)
    print(" 📋 전체 요약 (구간별 평균)")
    print("=" * 92)
    print_summary(result_df, kospi_dict)

    print(f"\n✅ 완료")


if __name__ == "__main__":
    main()