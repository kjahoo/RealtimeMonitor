"""
Walk-Forward 시뮬레이션 전용 스크립트
======================================
모델이 완성된 fold에 대해 스코어 계산 + 시뮬레이션 + 결과 저장만 수행.
walk_forward.py 에서 학습만 끝나고 results가 없는 경우 이 스크립트로 결과를 생성.

실행:
  python -X utf8 walk_forward_sim.py                  # 결과 없는 fold 전체 실행
  python -X utf8 walk_forward_sim.py --folds 1 2 3    # 지정 fold만 실행
  python -X utf8 walk_forward_sim.py --rebuild         # 이미 결과 있어도 재실행
"""

import os, sys, argparse, pickle, warnings, time, gc
import numpy as np
import pandas as pd
from pathlib import Path

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
warnings.filterwarnings("ignore")

import tensorflow as tf
from tensorflow.keras.models import load_model

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
print(f"   {'✅ GPU' if _USE_GPU else 'ℹ️  CPU'}  배치: {_BATCH_SZ}")

ROOT     = Path(r"C:\Projects\RealtimeMonitor")
DATA_DIR = ROOT / "Data" / "Stock"
PREP_DIR = ROOT / "Data" / "_prep_wf_v3"   # rsi_v3, bb_p_v3 등 v3 피처 포함
WF_DIR   = ROOT / "walk_forward"
sys.path.insert(0, str(ROOT))

# ─── 전략 파라미터 (walk_forward.py 와 동일) ────────────────────────────────────
INITIAL_CAPITAL = 1_000_000_000

# 매수: (최소 점수, 목표 비율)  — 점수 높은 순
BUY_TIERS  = [(0.8, 0.20), (0.7, 0.15), (0.6, 0.10), (0.5, 0.05)]
# 매도: (점수 상한, 보유 비율)  — 점수 낮은 순 (먼저 매칭되는 rule 적용)
SELL_TIERS = [(0.25, 0.00), (0.30, 0.05), (0.35, 0.10), (0.40, 0.15)]
# 거래비용: 수수료 0.015%, 매도 증권거래세 0.18%
BUY_FEE_RATE  = 0.00015
SELL_FEE_RATE = 0.00195


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

# 모델 학습 시 사용한 피처명과 완전히 일치해야 함 (rsi_v3 등 _v3 접미사)
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
# fold 목록 자동 감지
# ══════════════════════════════════════════════════════════════════════════════

def detect_folds(target_ids=None, rebuild=False):
    """
    WF_DIR에서 모델이 완성된 fold를 찾고, 폴드 기간을 로그 파일로 추정.
    target_ids: 지정 fold 번호 리스트. None이면 전체.
    """
    fold_dirs = sorted(WF_DIR.glob("fold_*"))
    folds = []

    for fd in fold_dirs:
        fold_id = int(fd.name.split("_")[1])
        if target_ids and fold_id not in target_ids:
            continue

        # 모델 6개 모두 있는지 확인
        model_files = [fd / "models" / f"{m}_lstm_v3.h5" for m in MODEL_SETTINGS]
        if not all(f.exists() for f in model_files):
            print(f"   Fold {fold_id:02d}: 모델 미완성 — 건너뜀")
            continue

        # 이미 결과 있으면 skip (--rebuild 아닐 때)
        result_file = fd / "results" / "history.csv"
        if result_file.exists() and not rebuild:
            print(f"   Fold {fold_id:02d}: 결과 이미 존재 — 건너뜀 (--rebuild 로 강제 재실행 가능)")
            continue

        # 학습 로그에서 train_end 추정 → test 기간 계산
        period = _infer_period(fd, fold_id)
        if period is None:
            print(f"   Fold {fold_id:02d}: 기간 추정 실패 — --data-start/--min-train 확인 필요")
            continue

        folds.append({"fold_id": fold_id, "fold_dir": fd, **period})

    return folds


def _infer_period(fold_dir, fold_id, data_start="20060101", min_train_years=7, test_years=1):
    """generate_folds 와 동일한 로직으로 fold 기간 재계산."""
    ds         = pd.Timestamp(data_start)
    test_start = ds + pd.DateOffset(years=min_train_years) + pd.DateOffset(years=fold_id - 1)
    test_end   = test_start + pd.DateOffset(years=test_years)
    train_end  = test_start
    return {
        "train_start": ds.strftime("%Y%m%d"),
        "train_end":   train_end.strftime("%Y%m%d"),
        "test_start":  test_start.strftime("%Y%m%d"),
        "test_end":    test_end.strftime("%Y%m%d"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 모델 로드
# ══════════════════════════════════════════════════════════════════════════════

def load_fold_models(fold_dir):
    models  = {}
    best_f1 = {}
    for m_name, cfg in MODEL_SETTINGS.items():
        h5  = fold_dir / "models" / f"{m_name}_lstm_v3.h5"
        scl = fold_dir / "models" / f"{m_name}_lstm_v3.scaler"
        log = fold_dir / "models" / f"log_{m_name}_v3.csv"

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


# ══════════════════════════════════════════════════════════════════════════════
# 스코어 계산
# ══════════════════════════════════════════════════════════════════════════════

def _compute_scores(feat_data, models):
    n, n_feat = len(feat_data), feat_data.shape[1]
    day_probs = {}

    for m_name, info in models.items():
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
            info = models[m_name]
            if prob > info["threshold"]:
                if info["type"] == "surge": s += prob * info["weight"]
                else:                       d += prob * info["weight"]
        result[di] = round(s - d, 4)
    return result


def compute_scores(models, test_start_str, test_end_str):
    """
    prep 캐시(rsi_v3 등 _v3 피처 포함)로 스코어 계산.
    모델 학습에 사용된 피처와 동일한 소스를 사용해야 스케일이 맞음.
    """
    ts        = pd.Timestamp(test_start_str)
    te        = pd.Timestamp(test_end_str)
    # 실제 모델의 lookback 중 최대값으로 여유 확보
    actual_max_lb = max(info["lookback"] for info in models.values())
    load_from = ts - pd.DateOffset(days=actual_max_lb * 2)

    if not PREP_DIR.exists() or not list(PREP_DIR.glob("*.pkl")):
        print(f"   ❌ prep 캐시 없음: {PREP_DIR}")
        print("      walk_forward.py --rebuild-prep 로 캐시를 먼저 빌드하세요.")
        return pd.DataFrame()

    prep_files = sorted(PREP_DIR.glob("*.pkl"))
    records    = []
    t0         = time.time()

    for i, fpath in enumerate(prep_files):
        code = fpath.stem[1:]   # A005930 → 005930
        try:
            df = pd.read_pickle(str(fpath))
            df['date'] = pd.to_datetime(df['date'])
            df = df[df['date'] >= load_from].sort_values('date').reset_index(drop=True)

            # 없는 컬럼은 0으로 채움
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
            print(f"   [{i+1:4d}/{len(prep_files)}]  {elapsed/60:.1f}분 경과  남은: {remain/60:.1f}분  "
                  f"레코드: {len(records):,}")

    score_df = pd.DataFrame(records)
    if not score_df.empty:
        score_df['date'] = pd.to_datetime(score_df['date'])
    return score_df


# ══════════════════════════════════════════════════════════════════════════════
# 포트폴리오 & 시뮬레이션
# ══════════════════════════════════════════════════════════════════════════════

class Portfolio:
    def __init__(self):
        self.cash       = float(INITIAL_CAPITAL)
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
        qty      = int(amount / price)
        cost     = qty * price
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


def load_prices(start_date, end_date):
    frames = []
    for fpath in DATA_DIR.glob("A*.csv"):
        code = fpath.stem[1:]
        try:
            df = pd.read_csv(fpath, encoding='utf-8-sig', usecols=['date', 'close'])
            df['date'] = pd.to_datetime(df['date'])
            df['code'] = code
            df = df[(df['date'] >= start_date) & (df['date'] <= end_date)]
            if not df.empty:
                frames.append(df)
        except Exception:
            pass
    all_df = pd.concat(frames, ignore_index=True)
    return all_df.pivot_table(index='date', columns='code', values='close').sort_index()


def run_simulation(score_df, test_start_str, test_end_str):
    ts = pd.Timestamp(test_start_str)
    te = pd.Timestamp(test_end_str)

    print(f"   종가 데이터 로딩...")
    price_pivot = load_prices(ts, te)

    scores_by_date = (
        score_df.groupby('date')
        .apply(lambda g: dict(zip(g['code'], g['score'])))
        .to_dict()
    )
    trading_days = price_pivot.loc[ts:te].index.tolist()
    print(f"   거래일: {len(trading_days)}일  ({trading_days[0].date()} ~ {trading_days[-1].date()})")

    portfolio = Portfolio()
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
            "date":         date,
            "total_assets": portfolio.total_assets(prices_today),
            "cash":         portfolio.cash,
            "n_positions":  len(portfolio.positions),
        })

    return pd.DataFrame(history), pd.DataFrame(portfolio._trade_log)


# ══════════════════════════════════════════════════════════════════════════════
# 성과 분석 & 저장
# ══════════════════════════════════════════════════════════════════════════════

def analyze(history_df):
    ta        = history_df.set_index("date")["total_assets"]
    ret       = (ta.iloc[-1] / ta.iloc[0] - 1) * 100
    daily_ret = ta.pct_change().dropna()
    sharpe    = (daily_ret.mean() / daily_ret.std() * np.sqrt(252)
                 if daily_ret.std() > 0 else 0)
    max_dd    = ((ta - ta.cummax()) / ta.cummax()).min() * 100
    win_rate  = (daily_ret > 0).sum() / len(daily_ret) * 100
    return {
        "수익률(%)":    round(ret, 2),
        "Sharpe":       round(sharpe, 2),
        "최대낙폭(%)":  round(max_dd, 2),
        "일승률(%)":    round(win_rate, 1),
        "거래일수":     len(ta),
        "시작자산(억)": round(ta.iloc[0] / 1e8, 2),
        "종료자산(억)": round(ta.iloc[-1] / 1e8, 2),
    }


def save_results(fold, history_df, trade_df):
    result_dir = fold["fold_dir"] / "results"
    result_dir.mkdir(parents=True, exist_ok=True)
    history_df.to_csv(result_dir / "history.csv", index=False, encoding='utf-8-sig')
    trade_df.to_csv(  result_dir / "trades.csv",  index=False, encoding='utf-8-sig')

    stats = analyze(history_df)
    stats.update({
        "fold_id":     fold["fold_id"],
        "train_start": fold["train_start"],
        "train_end":   fold["train_end"],
        "test_start":  fold["test_start"],
        "test_end":    fold["test_end"],
        "매수횟수":    int((trade_df['action'] == 'BUY').sum())  if len(trade_df) else 0,
        "매도횟수":    int((trade_df['action'] == 'SELL').sum()) if len(trade_df) else 0,
    })
    return stats


def save_summary(all_stats):
    cols = ["fold_id", "train_start", "train_end", "test_start", "test_end",
            "수익률(%)", "Sharpe", "최대낙폭(%)", "일승률(%)", "거래일수",
            "매수횟수", "매도횟수", "시작자산(억)", "종료자산(억)"]
    df = pd.DataFrame(all_stats)
    df = df[[c for c in cols if c in df.columns]]
    df.to_csv(WF_DIR / "summary.csv", index=False, encoding='utf-8-sig')


def build_comparison(summary_df=None):
    """summary.csv의 전략 수익률과 KOSPI/KOSDAQ 지수를 비교해 comparison.csv 저장."""
    if summary_df is None:
        path = WF_DIR / "summary.csv"
        if not path.exists():
            return
        summary_df = pd.read_csv(path)

    prep_files = sorted(PREP_DIR.glob("*.pkl"))
    if not prep_files:
        print("   ⚠️  prep 캐시 없음 → comparison 생성 불가")
        return

    ref = pd.read_pickle(str(prep_files[0]))
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


def print_summary(all_stats):
    df = pd.DataFrame(all_stats).sort_values("fold_id")
    print(f"\n{'폴드':>5}  {'테스트 기간':^21}  {'수익률':>8}  {'Sharpe':>7}  {'최대낙폭':>9}  {'일승률':>7}")
    print("-" * 67)
    for _, r in df.iterrows():
        print(f"  {int(r['fold_id']):02d}   {r['test_start']} ~ {r['test_end']}   "
              f"{r['수익률(%)']:+8.2f}%  {r['Sharpe']:7.2f}  "
              f"{r['최대낙폭(%)']:8.2f}%  {r['일승률(%)']:6.1f}%")
    if len(df) > 1:
        print("-" * 67)
        print(f"  평균                                   "
              f"{df['수익률(%)'].mean():+8.2f}%  {df['Sharpe'].mean():7.2f}  "
              f"{df['최대낙폭(%)'].mean():8.2f}%  {df['일승률(%)'].mean():6.1f}%")


# ══════════════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Walk-Forward 시뮬레이션 전용")
    parser.add_argument("--folds",       type=int, nargs="+", default=None,
                        help="실행할 fold 번호 (예: --folds 1 2 3). 미지정시 전체")
    parser.add_argument("--rebuild",     action="store_true",
                        help="이미 결과가 있어도 재실행")
    parser.add_argument("--min-train",   type=int, default=7,
                        help="walk_forward.py 와 동일한 값 (기본: 7)")
    parser.add_argument("--test-years",  type=int, default=1,
                        help="walk_forward.py 와 동일한 값 (기본: 1)")
    parser.add_argument("--data-start",  default="20060101",
                        help="walk_forward.py 와 동일한 값 (기본: 20060101)")
    args = parser.parse_args()

    print(f"\n🚀 Walk-Forward 시뮬레이션 전용")

    folds = detect_folds(target_ids=args.folds, rebuild=args.rebuild)
    if not folds:
        print("실행할 fold 없음. (--rebuild 로 강제 재실행 가능)")
        return

    # 기간 재계산 (args 반영)
    ds = pd.Timestamp(args.data_start)
    for fold in folds:
        fold_id    = fold["fold_id"]
        test_start = ds + pd.DateOffset(years=args.min_train) + pd.DateOffset(years=fold_id - 1)
        test_end   = test_start + pd.DateOffset(years=args.test_years)
        fold.update({
            "train_start": ds.strftime("%Y%m%d"),
            "train_end":   test_start.strftime("%Y%m%d"),
            "test_start":  test_start.strftime("%Y%m%d"),
            "test_end":    test_end.strftime("%Y%m%d"),
        })

    print(f"   실행 fold: {[f['fold_id'] for f in folds]}")
    print(f"\n{'폴드':>5}  {'테스트 기간':^21}")
    print("-" * 30)
    for f in folds:
        print(f"  {f['fold_id']:02d}   {f['test_start']} ~ {f['test_end']}")

    all_stats = []

    for fold in folds:
        fold_id = fold["fold_id"]
        print(f"\n{'='*55}")
        print(f"Fold {fold_id:02d}  테스트: {fold['test_start']} ~ {fold['test_end']}")
        print(f"{'='*55}")
        t0 = time.time()

        # 모델 로드
        print(f"   모델 로딩...")
        models = load_fold_models(fold["fold_dir"])

        # 스코어 계산
        print(f"   스코어 계산...")
        score_df = compute_scores(models, fold["test_start"], fold["test_end"])

        del models
        gc.collect()
        tf.keras.backend.clear_session()

        if score_df.empty:
            print(f"   ⚠️  스코어 없음 — 건너뜀")
            continue

        # 시뮬레이션
        print(f"   시뮬레이션...")
        history_df, trade_df = run_simulation(score_df, fold["test_start"], fold["test_end"])

        # 저장
        stats = save_results(fold, history_df, trade_df)
        all_stats.append(stats)

        elapsed = time.time() - t0
        print(f"   ✅ 완료 ({elapsed/60:.1f}분)  "
              f"수익률: {stats['수익률(%)']:+.2f}%  Sharpe: {stats['Sharpe']:.2f}  "
              f"최대낙폭: {stats['최대낙폭(%)']:.2f}%")

        save_summary(all_stats)

    print(f"\n{'='*55}")
    print("전체 완료")
    if all_stats:
        print_summary(all_stats)
        build_comparison()
        print(f"\n결과: {WF_DIR / 'summary.csv'}")


if __name__ == "__main__":
    main()
