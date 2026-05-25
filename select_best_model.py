"""
select_best_model.py
====================
Expanding(14폴드) + Rolling7y(13폴드) 전체를 동일 홀드아웃 기간에 평가해 최적 모델 선정.

테스트 기간이 폴드마다 다른 문제를 해소하기 위해 공통 홀드아웃을 사용한다.
  - Expanding 후반 폴드가 홀드아웃 데이터를 학습에 포함한 경우 ⚠️ in-sample 표시
  - 스코어 계산 결과는 fold_dir/holdout_YYYYMMDD_YYYYMMDD_scores.pkl 에 캐시됨
  - 가격 데이터는 전체 폴드가 공유 (중복 로딩 없음)

사용:
  python -X utf8 select_best_model.py
  python -X utf8 select_best_model.py --holdout-start 2025-01-01 --holdout-end 2025-12-31
  python -X utf8 select_best_model.py --top 5
  python -X utf8 select_best_model.py --no-cache   # 스코어 캐시 무시하고 재계산

결과:
  best_model_selection.csv  (프로젝트 루트)
"""

import os, sys, argparse, pickle, warnings, time, gc
import numpy as np
import pandas as pd
from pathlib import Path

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
warnings.filterwarnings("ignore")

import tensorflow as tf
from tensorflow.keras.models import load_model

ROOT     = Path(r"C:\Projects\RealtimeMonitor")
DATA_DIR = ROOT / "Data" / "Stock"
PREP_DIR = ROOT / "Data" / "_prep_wf_v3"
sys.path.insert(0, str(ROOT))


def _list_prep_files():
    """feather 우선, 없으면 pkl — 두 포맷 모두 지원."""
    feather = sorted(PREP_DIR.glob("*.feather"))
    return feather if feather else sorted(PREP_DIR.glob("*.pkl"))


def _read_prep(fpath: Path) -> pd.DataFrame:
    if fpath.suffix == '.feather':
        return pd.read_feather(str(fpath))
    return pd.read_pickle(str(fpath))

# ─── GPU ──────────────────────────────────────────────────────────────────────
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

# ─── 전략 상수 (walk_forward_sim.py 와 동일) ─────────────────────────────────
INITIAL_CAPITAL = 1_000_000_000
BUY_THRESH      = 0.55    # Exp-01 최적 파라미터
ALLOC_PER_STOCK = 0.10
SELL_THRESH     = 0.50
BUY_FEE_RATE    = 0.00015
SELL_FEE_RATE   = 0.00195

WF_FEATURES = [
    'change_pct', 'volume_ratio', 'vol_power',
    'prog_net_ratio', 'prog_ratio_vol',
    'disparity_5', 'disparity_20',
    'rsi_v3', 'bb_p_v3', 'bb_w_v3', 'adx_v3',
    'kospi_change', 'kosdaq_change',
]
# 기본값 — load_fold_models()가 로그 파일로 오버라이드함
MODEL_SETTINGS = {
    "target1":  {"lb": 21, "thr": 0.5108, "weight": 0.1962, "type": "surge"},
    "target5":  {"lb": 50, "thr": 0.6555, "weight": 0.4234, "type": "surge"},
    "target20": {"lb": 60, "thr": 0.3291, "weight": 0.3804, "type": "surge"},
    "drop1":    {"lb": 10, "thr": 0.4512, "weight": 0.2369, "type": "drop"},
    "drop5":    {"lb": 94, "thr": 0.3431, "weight": 0.3537, "type": "drop"},
    "drop20":   {"lb": 98, "thr": 0.5445, "weight": 0.4095, "type": "drop"},
}

DATA_START = "20060101"   # walk_forward.py 기본값과 동일
MIN_TRAIN  = 7            # walk_forward.py --min-train 기본값


# ══════════════════════════════════════════════════════════════════════════════
# 폴드 스캔
# ══════════════════════════════════════════════════════════════════════════════

def scan_folds(wf_dir: Path, method: str):
    """
    wf_dir/fold_XX 디렉터리를 스캔.
    모델 6개 모두 있는 폴드만 반환.
    train_end 계산 포함 (홀드아웃 OOS 여부 판단용).
    """
    folds = []
    ds = pd.Timestamp(DATA_START)

    for fd in sorted(wf_dir.glob("fold_*")):
        try:
            fold_id = int(fd.name.split("_")[1])
        except (IndexError, ValueError):
            continue

        model_files = [fd / "models" / f"{m}_lstm_v3.h5" for m in MODEL_SETTINGS]
        if not all(f.exists() for f in model_files):
            continue

        # Expanding·Rolling 모두 동일 공식으로 테스트 시작일 계산
        # fold 1: train_end = ds + MIN_TRAIN years  (= test 시작)
        train_end = ds + pd.DateOffset(years=MIN_TRAIN + fold_id - 1)

        folds.append({
            "method":    method,
            "fold_id":   fold_id,
            "fold_dir":  fd,
            "train_end": train_end,
        })

    return folds


# ══════════════════════════════════════════════════════════════════════════════
# 모델 로드
# ══════════════════════════════════════════════════════════════════════════════

def load_fold_models(fold_dir: Path) -> dict:
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
            "weight":    cfg["weight"],
            "type":      cfg["type"],
        }

    # F1 비례 가중치
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
# 스코어 계산 (캐시 지원)
# ══════════════════════════════════════════════════════════════════════════════

def _compute_scores_raw(feat_data: np.ndarray, models: dict) -> dict:
    n, n_feat = len(feat_data), feat_data.shape[1]
    day_probs: dict = {}

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


def compute_scores(models: dict,
                   h_start: pd.Timestamp, h_end: pd.Timestamp,
                   cache_path: Path, no_cache: bool) -> pd.DataFrame:
    if cache_path.exists() and not no_cache:
        print(f"   📦 캐시 로드: {cache_path.name}")
        return pd.read_pickle(str(cache_path))

    actual_max_lb = max(info["lookback"] for info in models.values())
    load_from     = h_start - pd.DateOffset(days=actual_max_lb * 2)

    prep_files = _list_prep_files()
    if not prep_files:
        print(f"   ❌ prep 캐시 없음: {PREP_DIR}")
        return pd.DataFrame()

    records = []
    t0 = time.time()
    for i, fpath in enumerate(prep_files):
        code = fpath.stem[1:]
        try:
            df = _read_prep(fpath)
            df['date'] = pd.to_datetime(df['date'])
            df = df[df['date'] >= load_from].sort_values('date').reset_index(drop=True)

            for col in WF_FEATURES:
                if col not in df.columns:
                    df[col] = 0.0
            df[WF_FEATURES] = df[WF_FEATURES].fillna(0)

            if len(df) < actual_max_lb:
                continue

            scores = _compute_scores_raw(df[WF_FEATURES].values, models)
            for di, score in scores.items():
                date = df['date'].iloc[di]
                if h_start <= date < h_end:
                    records.append({"date": date, "code": code, "score": score})
        except Exception:
            pass

        if (i + 1) % 300 == 0 or i == len(prep_files) - 1:
            elapsed = time.time() - t0
            rate    = (i + 1) / elapsed
            remain  = (len(prep_files) - i - 1) / rate if rate > 0 else 0
            print(f"   [{i+1:4d}/{len(prep_files)}]  {elapsed/60:.1f}분 경과  "
                  f"남은: {remain/60:.1f}분  레코드: {len(records):,}")

    score_df = pd.DataFrame(records)
    if not score_df.empty:
        score_df['date'] = pd.to_datetime(score_df['date'])
        pd.to_pickle(score_df, str(cache_path))

    return score_df


# ══════════════════════════════════════════════════════════════════════════════
# 포트폴리오 & 시뮬레이션
# ══════════════════════════════════════════════════════════════════════════════

class Portfolio:
    def __init__(self):
        self.cash        = float(INITIAL_CAPITAL)
        self.positions   = {}
        self._trade_log  = []
        self._last_prices: dict = {}   # 마지막으로 관측된 종가 — avg_price 팬텀값 방지

    def update_prices(self, prices: dict):
        self._last_prices.update({k: v for k, v in prices.items() if v and v > 0})

    def _price(self, code: str, prices: dict) -> float:
        """당일 가격 → 최근 관측 가격 → 평균단가 순으로 폴백."""
        p = prices.get(code)
        if p and p > 0:
            return p
        p = self._last_prices.get(code)
        if p and p > 0:
            return p
        return self.positions[code]["avg_price"]

    def total_assets(self, prices: dict) -> float:
        mv = sum(pos["qty"] * self._price(code, prices)
                 for code, pos in self.positions.items())
        return self.cash + mv

    def market_value(self, code: str, prices: dict) -> float:
        pos = self.positions.get(code)
        return pos["qty"] * self._price(code, prices) if pos else 0.0

    def buy(self, code, amount, price, date, alloc_ratio):
        qty        = int(amount / price)
        total_cost = qty * price * (1 + BUY_FEE_RATE)
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
        self.cash   += qty * price * (1 - SELL_FEE_RATE)
        pos["qty"]  -= qty
        if pos["qty"] == 0:
            del self.positions[code]
        elif alloc_ratio is not None:
            pos["alloc_ratio"] = alloc_ratio
        self._trade_log.append({"date": date, "action": "SELL", "code": code, "qty": qty, "price": price})

    def sell_all(self, code, price, date):
        if code in self.positions:
            self.sell(code, self.positions[code]["qty"], price, date)


def _max_consecutive(bool_series: pd.Series) -> int:
    """Boolean Series에서 연속 True 최대 구간 길이 반환."""
    if not bool_series.any():
        return 0
    grp = (bool_series != bool_series.shift()).cumsum()
    return int(bool_series.groupby(grp).sum().max())


def load_prices(h_start: pd.Timestamp, h_end: pd.Timestamp,
                filter_bad: bool = True) -> pd.DataFrame:
    """
    종가 데이터 로드. filter_bad=True 시 품질 불량 종목 자동 제외:
      - 연속 동일 종가 15일+  (거래 정지)
      - 일간 가격 변동 40%+   (액분·무증·데이터 오류, 상하한 30% 초과)
      - 연속 거래량 0  10일+  (유동성 없음)
    """
    frames = []
    excl_halt  = []   # 거래 정지
    excl_spike = []   # 가격 급변
    excl_novol = []   # 거래량 0

    for fpath in DATA_DIR.glob("A*.csv"):
        code = fpath.stem[1:]
        try:
            df = pd.read_csv(
                fpath, encoding='utf-8-sig',
                usecols=lambda c: c in ['date', 'close', 'volume']
            )
            df['date'] = pd.to_datetime(df['date'])
            df = (df[(df['date'] >= h_start) & (df['date'] <= h_end)]
                  .sort_values('date').reset_index(drop=True))
            if df.empty:
                continue

            if filter_bad and len(df) >= 2:
                close = df['close'].astype(float)

                # 필터 1: 연속 동일 종가 14회+ = 15일+ 정지
                # (diff==0이 14번 연속 → 15일 연속 동일가)
                if _max_consecutive(close.diff() == 0) >= 14:
                    excl_halt.append(code)
                    continue

                # 필터 2: 일간 변동 40% 초과 (상하한 30% 넘으면 데이터 오류)
                if (close.pct_change().abs() > 0.40).any():
                    excl_spike.append(code)
                    continue

                # 필터 3: 연속 거래량 0 10일+
                if 'volume' in df.columns:
                    vol = df['volume'].fillna(0).astype(float)
                    if _max_consecutive(vol == 0) >= 10:
                        excl_novol.append(code)
                        continue

            frames.append(df[['date', 'close']].assign(code=code))
        except Exception:
            pass

    if filter_bad:
        n = len(excl_halt) + len(excl_spike) + len(excl_novol)
        print(f"   ⚠️  품질 필터 제외: {n}개  "
              f"(거래정지 {len(excl_halt)} / 가격급변 {len(excl_spike)} / 거래량0 {len(excl_novol)})")

    if not frames:
        return pd.DataFrame()
    all_df = pd.concat(frames, ignore_index=True)
    return all_df.pivot_table(index='date', columns='code', values='close').sort_index()


def run_simulation(score_df: pd.DataFrame,
                   h_start: pd.Timestamp, h_end: pd.Timestamp,
                   price_pivot: pd.DataFrame) -> pd.DataFrame:
    scores_by_date = (
        score_df.groupby('date')
        .apply(lambda g: dict(zip(g['code'], g['score'])))
        .to_dict()
    )
    mask         = (price_pivot.index >= h_start) & (price_pivot.index < h_end)
    trading_days = price_pivot.loc[mask].index.tolist()
    portfolio    = Portfolio()
    history      = []

    for date in trading_days:
        prices_today = price_pivot.loc[date].dropna().to_dict()
        today_scores = scores_by_date.get(date, {})
        portfolio.update_prices(prices_today)   # 최근 가격 캐시 갱신
        total        = portfolio.total_assets(prices_today)

        # 매도: score < SELL_THRESH 이면 전량 매도
        for code in list(portfolio.positions.keys()):
            score = today_scores.get(code)
            price = prices_today.get(code)
            if score is None or not price or price <= 0:
                continue
            if score < SELL_THRESH:
                portfolio.sell_all(code, price, date)

        # 매수: score >= BUY_THRESH 이면 ALLOC_PER_STOCK 까지 증분 매수
        total      = portfolio.total_assets(prices_today)
        candidates = []
        for code, score in today_scores.items():
            price = prices_today.get(code)
            if not price or price <= 0 or score < BUY_THRESH:
                continue
            cur_mv    = portfolio.market_value(code, prices_today)
            cur_alloc = cur_mv / total if total > 0 else 0.0
            if ALLOC_PER_STOCK > cur_alloc + 0.01:
                candidates.append((code, score, ALLOC_PER_STOCK - cur_alloc))

        for code, score, incr_ratio in sorted(candidates, key=lambda x: x[1], reverse=True):
            buy_amount = incr_ratio * total
            price      = prices_today[code]
            if portfolio.cash < buy_amount:
                continue
            portfolio.buy(code, buy_amount, price, date, alloc_ratio=ALLOC_PER_STOCK)

        history.append({
            "date":        date,
            "total_assets": portfolio.total_assets(prices_today),
            "n_positions": len(portfolio.positions),
        })

    return pd.DataFrame(history)


def analyze_history(history_df: pd.DataFrame) -> dict:
    ta        = history_df.set_index("date")["total_assets"]
    ret       = (ta.iloc[-1] / ta.iloc[0] - 1) * 100
    daily_ret = ta.pct_change().dropna()
    sharpe    = (daily_ret.mean() / daily_ret.std() * np.sqrt(252)
                 if daily_ret.std() > 0 else 0.0)
    max_dd    = ((ta - ta.cummax()) / ta.cummax()).min() * 100
    win_rate  = (daily_ret > 0).sum() / len(daily_ret) * 100
    return {
        "수익률(%)":   round(ret, 2),
        "Sharpe":      round(sharpe, 2),
        "최대낙폭(%)": round(max_dd, 2),
        "일승률(%)":   round(win_rate, 1),
        "거래일수":    len(ta),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 벤치마크
# ══════════════════════════════════════════════════════════════════════════════

def get_benchmark(h_start: pd.Timestamp, h_end: pd.Timestamp) -> dict:
    prep_files = _list_prep_files()
    if not prep_files:
        return {"KOSPI(%)": 0.0, "KOSDAQ(%)": 0.0}
    ref = _read_prep(prep_files[0])
    ref['date'] = pd.to_datetime(ref['date'])
    ref = ref.set_index('date').sort_index()
    period = ref[(ref.index >= h_start) & (ref.index < h_end)]
    return {
        "KOSPI(%)":  round(((1 + period['kospi_change']).prod()  - 1) * 100, 2),
        "KOSDAQ(%)": round(((1 + period['kosdaq_change']).prod() - 1) * 100, 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 연환산 헬퍼
# ══════════════════════════════════════════════════════════════════════════════

def annualize(total_pct: float, trading_days: int) -> float:
    """누적 수익률(%) → 연환산 수익률(%). 거래일 기준 252일/년."""
    if trading_days <= 0:
        return 0.0
    years = trading_days / 252.0
    return round(((1 + total_pct / 100) ** (1 / years) - 1) * 100, 2)


def reliability(oos_years: float) -> str:
    if oos_years >= 4:
        return "높음 ★★★"
    elif oos_years >= 2:
        return "중간 ★★"
    elif oos_years >= 1:
        return "낮음 ★"
    else:
        return "매우낮음"


# ══════════════════════════════════════════════════════════════════════════════
# 출력 헬퍼
# ══════════════════════════════════════════════════════════════════════════════

def _print_table_common(rows, title: str, top: int):
    """공통 홀드아웃 모드 출력."""
    print(f"\n{title}")
    print(f"  {'순위':>4}  {'방법':<12}  {'폴드':>4}  {'학습종료':^10}  "
          f"{'수익률':>8}  {'vsKOSPI':>8}  {'Sharpe':>7}  {'낙폭':>8}  {'일승률':>7}")
    print("  " + "─" * 74)
    for r in rows[:top]:
        print(f"  {r['rank']:4d}  {r['method']:<12}  {r['fold_id']:4d}  "
              f"{r['train_end']:^10}  "
              f"{r['수익률(%)']:+8.2f}%  {r['vs_KOSPI(%p)']:+7.2f}%p  "
              f"{r['Sharpe']:7.2f}  {r['최대낙폭(%)']:7.2f}%  {r['일승률(%)']:6.1f}%")


def _print_table_maxoos(rows, title: str, top: int):
    """Max-OOS 모드 출력 — 연환산 기준."""
    print(f"\n{title}")
    print(f"  {'순위':>4}  {'방법':<12}  {'폴드':>4}  {'학습종료':^10}  "
          f"{'OOS(년)':>7}  {'연환산수익':>10}  {'연환산vsKOSPI':>13}  "
          f"{'Sharpe':>7}  {'낙폭':>8}  {'신뢰도'}")
    print("  " + "─" * 92)
    for r in rows[:top]:
        print(f"  {r['rank']:4d}  {r['method']:<12}  {r['fold_id']:4d}  "
              f"{r['train_end']:^10}  "
              f"{r['oos_years']:7.1f}  "
              f"{r['연환산수익률(%)']:+10.2f}%  "
              f"{r['연환산vsKOSPI(%p)']:+12.2f}%p  "
              f"{r['Sharpe']:7.2f}  {r['최대낙폭(%)']:7.2f}%  "
              f"{r['신뢰도']}")


# ══════════════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="최적 모델 선정 (기본: max-oos — 각 폴드가 가진 최대 OOS 기간 활용)"
    )
    parser.add_argument("--mode", choices=["common", "max-oos"], default="max-oos",
                        help="평가 방식: common=공통 홀드아웃, max-oos=폴드별 최대 OOS (기본)")
    parser.add_argument("--holdout-start", default="2022-01-01",
                        help="[common 전용] 공통 홀드아웃 시작일 (기본: 2022-01-01)")
    parser.add_argument("--holdout-end",   default="2025-12-31",
                        help="홀드아웃 종료일 — 모든 모드에서 공통 (기본: 2025-12-31)")
    parser.add_argument("--top",      type=int, default=5,
                        help="출력할 상위 N개 (기본: 5)")
    parser.add_argument("--no-cache", action="store_true",
                        help="스코어 캐시 무시하고 재계산")
    parser.add_argument("--expanding-max-fold", type=int, default=None,
                        help="Expanding 폴드 상한 (예: 11 → fold_01~11만 사용)")
    parser.add_argument("--rolling-max-fold", type=int, default=None,
                        help="Rolling7y 폴드 상한 (예: 10 → fold_01~10만 사용)")
    args = parser.parse_args()

    h_end   = pd.Timestamp(args.holdout_end)
    h_start = pd.Timestamp(args.holdout_start)   # common 모드에서만 사용

    print(f"\n{'='*65}")
    print(f"🎯 최적 모델 선정  —  {'Max-OOS 모드' if args.mode == 'max-oos' else '공통 홀드아웃 모드'}")
    if args.mode == "max-oos":
        print(f"   각 폴드의 train_end ~ {h_end.date()} 를 OOS로 사용")
        print(f"   비교 기준: 연환산 수익률 / 연환산 vs KOSPI / Sharpe")
    else:
        print(f"   홀드아웃: {h_start.date()} ~ {h_end.date()}")
    print(f"{'='*65}")

    # ── 폴드 스캔 ──────────────────────────────────────────────────────────────
    exp_dir  = ROOT / "walk_forward"
    roll_dir = ROOT / "walk_forward_rolling7y"
    exp_folds  = scan_folds(exp_dir,  "Expanding")
    roll_folds = scan_folds(roll_dir, "Rolling7y")

    if args.expanding_max_fold is not None:
        exp_folds = [f for f in exp_folds if f["fold_id"] <= args.expanding_max_fold]
    if args.rolling_max_fold is not None:
        roll_folds = [f for f in roll_folds if f["fold_id"] <= args.rolling_max_fold]

    all_folds  = exp_folds + roll_folds

    if not all_folds:
        print("❌ 평가할 폴드 없음")
        return

    print(f"\n   Expanding : {len(exp_folds):2d}개 폴드"
          + (f"  (fold_01~{args.expanding_max_fold:02d})" if args.expanding_max_fold else ""))
    print(f"   Rolling7y : {len(roll_folds):2d}개 폴드"
          + (f"  (fold_01~{args.rolling_max_fold:02d})" if args.rolling_max_fold else ""))
    print(f"   합계      : {len(all_folds):2d}개 폴드")

    # ── 가격 데이터 (전체 범위 한 번만 로딩) ────────────────────────────────────
    price_load_start = min(f["train_end"] for f in all_folds) if args.mode == "max-oos" else h_start
    print(f"\n   종가 데이터 로딩 ({price_load_start.date()} ~ {h_end.date()})...")
    price_pivot = load_prices(price_load_start, h_end)
    if price_pivot.empty:
        print("❌ 가격 데이터 없음")
        return
    td = price_pivot.index.tolist()
    print(f"   거래일: {len(td)}일  ({td[0].date()} ~ {td[-1].date()})")

    # ── 벤치마크 캐시 (준비) ───────────────────────────────────────────────────
    _prep_ref = None
    def _get_bm(fs, fe):
        nonlocal _prep_ref
        if _prep_ref is None:
            pf = _list_prep_files()
            if not pf:
                return {"KOSPI(%)": 0.0, "KOSDAQ(%)": 0.0}
            _prep_ref = _read_prep(pf[0])
            _prep_ref['date'] = pd.to_datetime(_prep_ref['date'])
            _prep_ref = _prep_ref.set_index('date').sort_index()
        period = _prep_ref[((_prep_ref.index >= fs) & (_prep_ref.index < fe))]
        return {
            "KOSPI(%)":  round(((1 + period['kospi_change']).prod()  - 1) * 100, 2),
            "KOSDAQ(%)": round(((1 + period['kosdaq_change']).prod() - 1) * 100, 2),
        }

    # ── 폴드별 평가 ────────────────────────────────────────────────────────────
    results = []
    total   = len(all_folds)

    for idx, fold in enumerate(all_folds, 1):
        method  = fold["method"]
        fold_id = fold["fold_id"]

        # 모드별 평가 기간 결정
        if args.mode == "max-oos":
            fold_h_start = fold["train_end"]   # 각 폴드의 학습 종료일 = OOS 시작
            fold_h_end   = h_end
            is_in_s      = False               # max-oos는 정의상 항상 OOS
        else:
            fold_h_start = h_start
            fold_h_end   = h_end
            is_in_s      = fold["train_end"] > h_start

        cache_tag  = f"{fold_h_start.strftime('%Y%m%d')}_{fold_h_end.strftime('%Y%m%d')}"
        cache_path = fold["fold_dir"] / f"holdout_{cache_tag}_scores.pkl"

        oos_days  = len(price_pivot.loc[(price_pivot.index >= fold_h_start) &
                                        (price_pivot.index < fold_h_end)].index)
        oos_years = round(oos_days / 252.0, 1)

        print(f"\n{'─'*65}")
        print(f"[{idx:02d}/{total}] {method} fold_{fold_id:02d}  "
              f"train_end={fold['train_end'].date()}  "
              f"OOS={oos_years}년({oos_days}일)"
              + ("  ⚠️ in-sample" if is_in_s else ""))
        t0 = time.time()

        try:
            print(f"   모델 로딩...")
            models = load_fold_models(fold["fold_dir"])

            print(f"   스코어 계산...")
            score_df = compute_scores(models, fold_h_start, fold_h_end,
                                      cache_path, args.no_cache)
            del models
            gc.collect()
            tf.keras.backend.clear_session()

            if score_df.empty:
                print(f"   ⚠️  스코어 없음 — 건너뜀")
                continue

            print(f"   시뮬레이션...")
            history_df = run_simulation(score_df, fold_h_start, fold_h_end, price_pivot)
            if history_df.empty:
                print(f"   ⚠️  거래일 없음 — 건너뜀")
                continue

            bm = _get_bm(fold_h_start, fold_h_end)

            stats = analyze_history(history_df)
            ann_ret   = annualize(stats["수익률(%)"],  stats["거래일수"])
            ann_kospi = annualize(bm["KOSPI(%)"],      stats["거래일수"])
            ann_kosdaq= annualize(bm["KOSDAQ(%)"],     stats["거래일수"])

            stats.update({
                "method":            method,
                "fold_id":           fold_id,
                "train_end":         fold["train_end"].strftime("%Y-%m-%d"),
                "in_sample":         is_in_s,
                "oos_years":         oos_years,
                "신뢰도":            reliability(oos_years),
                "KOSPI(%)":          bm["KOSPI(%)"],
                "KOSDAQ(%)":         bm["KOSDAQ(%)"],
                "vs_KOSPI(%p)":      round(stats["수익률(%)"] - bm["KOSPI(%)"],  2),
                "vs_KOSDAQ(%p)":     round(stats["수익률(%)"] - bm["KOSDAQ(%)"], 2),
                "연환산수익률(%)":   ann_ret,
                "연환산KOSPI(%)":    ann_kospi,
                "연환산vsKOSPI(%p)": round(ann_ret - ann_kospi,  2),
                "연환산vsKOSDAQ(%p)":round(ann_ret - ann_kosdaq, 2),
            })
            results.append(stats)

            elapsed = time.time() - t0
            print(f"   ✅ {elapsed/60:.1f}분  "
                  f"누적: {stats['수익률(%)']:+.2f}%  "
                  f"연환산: {ann_ret:+.2f}%  "
                  f"연환산vsKOSPI: {stats['연환산vsKOSPI(%p)']:+.2f}%p  "
                  f"Sharpe: {stats['Sharpe']:.2f}  [{stats['신뢰도']}]")

        except Exception as e:
            import traceback
            print(f"   ❌ 오류: {e}")
            traceback.print_exc()
            continue

    if not results:
        print("\n❌ 평가 결과 없음")
        return

    # ── 랭킹 ──────────────────────────────────────────────────────────────────
    # max-oos: 연환산vsKOSPI 내림차순 → Sharpe 내림차순
    # common : vs_KOSPI 내림차순 → Sharpe 내림차순
    df = pd.DataFrame(results)

    if args.mode == "max-oos":
        sort_key = ["연환산vsKOSPI(%p)", "Sharpe"]
    else:
        sort_key = ["vs_KOSPI(%p)", "Sharpe"]

    df_oos = df[~df["in_sample"]].sort_values(sort_key, ascending=False).reset_index(drop=True)
    df_is  = df[ df["in_sample"]].sort_values(sort_key, ascending=False).reset_index(drop=True)
    df_oos["rank"] = df_oos.index + 1
    df_is["rank"]  = df_is.index + 1

    oos_rows = df_oos.to_dict("records")
    is_rows  = df_is.to_dict("records")

    # ── 결과 출력 ──────────────────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"🏆 최적 모델 선정 결과  ({'Max-OOS 연환산' if args.mode == 'max-oos' else '공통 홀드아웃'})")
    print(f"{'='*80}")

    if args.mode == "max-oos":
        if oos_rows:
            _print_table_maxoos(oos_rows, f"▶ 전체 폴드 TOP {args.top}  (연환산 vsKOSPI 기준)", args.top)
        if is_rows:
            _print_table_maxoos(is_rows, f"\n▶ In-Sample 폴드 (참고용)", args.top)
    else:
        if oos_rows:
            _print_table_common(oos_rows, f"▶ OOS 모델 TOP {args.top}", args.top)
        if is_rows:
            _print_table_common(is_rows, f"\n▶ In-Sample 폴드 (참고용)", args.top)

    # ── 저장 ───────────────────────────────────────────────────────────────────
    all_rows  = oos_rows + is_rows
    save_cols = ["rank", "method", "fold_id", "train_end", "oos_years", "신뢰도",
                 "수익률(%)", "KOSPI(%)", "vs_KOSPI(%p)",
                 "연환산수익률(%)", "연환산KOSPI(%)", "연환산vsKOSPI(%p)",
                 "Sharpe", "최대낙폭(%)", "일승률(%)", "거래일수", "in_sample"]
    out_df    = pd.DataFrame(all_rows)
    out_df[[c for c in save_cols if c in out_df.columns]].to_csv(
        ROOT / "best_model_selection.csv", index=False, encoding='utf-8-sig'
    )
    print(f"\n💾 전체 결과 저장: {ROOT / 'best_model_selection.csv'}")

    # ── 최고 모델 안내 ─────────────────────────────────────────────────────────
    target_rows = oos_rows if oos_rows else is_rows
    if target_rows:
        best    = target_rows[0]
        wf_root = exp_dir if best["method"] == "Expanding" else roll_dir
        best_dir = wf_root / f"fold_{best['fold_id']:02d}" / "models"
        print(f"\n🥇 최적 모델")
        print(f"   방법론        : {best['method']}")
        print(f"   폴드          : fold_{best['fold_id']:02d}  (학습 종료: {best['train_end']})")
        print(f"   OOS 기간      : {best['oos_years']}년  [{best['신뢰도']}]")
        if args.mode == "max-oos":
            print(f"   연환산 수익률 : {best['연환산수익률(%)']:+.2f}%")
            print(f"   연환산 vsKOSPI: {best['연환산vsKOSPI(%p)']:+.2f}%p")
        else:
            print(f"   수익률        : {best['수익률(%)']:+.2f}%")
            print(f"   vs KOSPI      : {best['vs_KOSPI(%p)']:+.2f}%p")
        print(f"   Sharpe        : {best['Sharpe']:.2f}")
        print(f"   낙폭          : {best['최대낙폭(%)']:.2f}%")
        print(f"   모델 경로     : {best_dir}")
        print(f"\n   → secrets.V3_MODEL_DIR 에 위 경로를 설정하면 프로덕션 적용됩니다.")


if __name__ == "__main__":
    main()