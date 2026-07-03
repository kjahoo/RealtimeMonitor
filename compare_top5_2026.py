"""
compare_top5_2026.py
====================
상위 N 모델을 2026년 각 월마다 독립 시뮬레이션(초기자본 10억 리셋)으로 비교.
매월 순수 alpha를 측정하여 모델 간 공정 비교가 가능하다.

사용:
  python -X utf8 compare_top5_2026.py
  python -X utf8 compare_top5_2026.py --top 5
  python -X utf8 compare_top5_2026.py --no-cache
  python -X utf8 compare_top5_2026.py --start 2026-01-01 --end 2026-05-23
"""

import os, sys, argparse, pickle, warnings, gc, time
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

INITIAL_CAPITAL = 1_000_000_000
BUY_TIERS       = [(0.8, 0.20), (0.7, 0.15), (0.6, 0.10), (0.5, 0.05)]
SELL_TIERS      = [(0.25, 0.00), (0.30, 0.05), (0.35, 0.10), (0.40, 0.15)]
BUY_FEE_RATE    = 0.00015
SELL_FEE_RATE   = 0.00195

WF_FEATURES = [
    'change_pct', 'volume_ratio', 'vol_power',
    'prog_net_ratio', 'prog_ratio_vol',
    'disparity_5', 'disparity_20',
    'rsi_v3', 'bb_p_v3', 'bb_w_v3', 'adx_v3',
    'kospi_change', 'kosdaq_change',
]
MODEL_SETTINGS = {
    "target1":  {"lb": 65, "thr": 0.5256, "weight": 0.1775, "type": "surge"},
    "target5":  {"lb": 55, "thr": 0.6484, "weight": 0.3639, "type": "surge"},
    "target20": {"lb": 95, "thr": 0.9197, "weight": 0.4586, "type": "surge"},
    "drop1":    {"lb": 80, "thr": 0.4018, "weight": 0.2544, "type": "drop"},
    "drop5":    {"lb": 85, "thr": 0.5041, "weight": 0.3376, "type": "drop"},
    "drop20":   {"lb": 85, "thr": 0.5723, "weight": 0.4079, "type": "drop"},
}


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
print(f"   {'GPU' if _USE_GPU else 'CPU'}  batch={_BATCH_SZ}")


# ══════════════════════════════════════════════════════════════════════════════
# 모델 메타 / 경로
# ══════════════════════════════════════════════════════════════════════════════

def load_top_models_info(csv_path: Path, top_n: int):
    if not csv_path.exists():
        raise FileNotFoundError(
            f"CSV 없음: {csv_path}\n"
            "먼저 python -X utf8 select_best_model.py 를 실행하세요."
        )
    df = pd.read_csv(csv_path, encoding='utf-8-sig')
    col_is = 'in_sample'
    df_oos = df[df[col_is] == False] if col_is in df.columns else df
    sort_col = ('연환산vsKOSPI(%p)' if '연환산vsKOSPI(%p)' in df_oos.columns
                else 'vs_KOSPI(%p)')
    return df_oos.sort_values([sort_col, 'Sharpe'], ascending=False).head(top_n).to_dict('records')


def get_fold_dir(model_info: dict) -> Path:
    method  = model_info['method']
    fold_id = int(model_info['fold_id'])
    wf_root = ROOT / ("walk_forward" if method == "Expanding"
                      else "walk_forward_rolling7y")
    return wf_root / f"fold_{fold_id:02d}"


def model_label(model_info: dict) -> str:
    prefix = "Exp" if model_info['method'] == "Expanding" else "Roll7y"
    return f"{prefix}-{int(model_info['fold_id']):02d}"


# ══════════════════════════════════════════════════════════════════════════════
# 모델 로딩 / 스코어 계산
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
            "model": model, "scaler": scaler,
            "lookback": actual_lb, "threshold": threshold,
            "weight": cfg["weight"], "type": cfg["type"],
        }
    for group in ("surge", "drop"):
        names = [n for n, c in MODEL_SETTINGS.items() if c["type"] == group and n in models]
        f1s   = [best_f1.get(n) for n in names]
        if all(f is not None for f in f1s):
            total = sum(f1s)
            for n, f in zip(names, f1s):
                models[n]["weight"] = f / total
    for m_name, info in models.items():
        print(f"     {m_name:<10} lb={info['lookback']:>3}  thr={info['threshold']:.4f}  "
              f"w={info['weight']:.4f}")
    return models


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
                if info["type"] == "surge":
                    s += prob * info["weight"]
                else:
                    d += prob * info["weight"]
        result[di] = round(s - d, 4)
    return result


def compute_scores(models: dict, h_start: pd.Timestamp, h_end: pd.Timestamp,
                   cache_path: Path, no_cache: bool) -> pd.DataFrame:
    """h_start ~ h_end 전 종목 스코어 계산 (캐시 지원)."""
    if cache_path.exists() and not no_cache:
        print(f"   캐시 로드: {cache_path.name}")
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
            rate    = (i + 1) / elapsed if elapsed > 0 else 1
            remain  = (len(prep_files) - i - 1) / rate
            print(f"   [{i+1:4d}/{len(prep_files)}]  {elapsed/60:.1f}분  "
                  f"남은: {remain/60:.1f}분  레코드: {len(records):,}")

    score_df = pd.DataFrame(records)
    if not score_df.empty:
        score_df['date'] = pd.to_datetime(score_df['date'])
        pd.to_pickle(score_df, str(cache_path))
    return score_df


# ══════════════════════════════════════════════════════════════════════════════
# 포트폴리오 / 시뮬레이션
# ══════════════════════════════════════════════════════════════════════════════

def _buy_target_ratio(score):
    for min_s, ratio in BUY_TIERS:
        if score >= min_s:
            return ratio
    return None


def _sell_target_ratio(score):
    for max_s, ratio in SELL_TIERS:
        if score < max_s:
            return ratio
    return None


class Portfolio:
    def __init__(self):
        self.cash      = float(INITIAL_CAPITAL)
        self.positions = {}

    def total_assets(self, prices: dict) -> float:
        mv = sum(p["qty"] * prices.get(c, p["avg_price"])
                 for c, p in self.positions.items())
        return self.cash + mv

    def market_value(self, code: str, prices: dict) -> float:
        pos = self.positions.get(code)
        return pos["qty"] * prices.get(code, pos["avg_price"]) if pos else 0.0

    def buy(self, code, amount, price, date, alloc_ratio):
        qty  = int(amount / price)
        cost = qty * price * (1 + BUY_FEE_RATE)
        if qty == 0 or cost > self.cash:
            return False
        self.cash -= cost
        if code in self.positions:
            pos     = self.positions[code]
            new_qty = pos["qty"] + qty
            pos["avg_price"]   = (pos["qty"] * pos["avg_price"] + qty * price) / new_qty
            pos["qty"]         = new_qty
            pos["alloc_ratio"] = alloc_ratio
        else:
            self.positions[code] = {"qty": qty, "avg_price": price, "alloc_ratio": alloc_ratio}
        return True

    def sell(self, code, qty, price, date, alloc_ratio=None):
        if code not in self.positions:
            return
        pos = self.positions[code]
        qty = min(qty, pos["qty"])
        self.cash     += qty * price * (1 - SELL_FEE_RATE)
        pos["qty"]    -= qty
        if pos["qty"] == 0:
            del self.positions[code]
        elif alloc_ratio is not None:
            pos["alloc_ratio"] = alloc_ratio

    def sell_all(self, code, price, date):
        if code in self.positions:
            self.sell(code, self.positions[code]["qty"], price, date)


def load_prices(h_start: pd.Timestamp, h_end: pd.Timestamp) -> pd.DataFrame:
    frames = []
    for fpath in DATA_DIR.glob("A*.csv"):
        code = fpath.stem[1:]
        try:
            df = pd.read_csv(fpath, encoding='utf-8-sig', usecols=['date', 'close'])
            df['date'] = pd.to_datetime(df['date'])
            df['code'] = code
            df = df[(df['date'] >= h_start) & (df['date'] <= h_end)]
            if not df.empty:
                frames.append(df)
        except Exception:
            pass
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).pivot_table(
        index='date', columns='code', values='close').sort_index()


def run_simulation_one_month(score_df: pd.DataFrame,
                             m_start: pd.Timestamp, m_end_excl: pd.Timestamp,
                             price_pivot: pd.DataFrame) -> float:
    """
    m_start ~ m_end_excl(exclusive) 기간 독립 시뮬레이션.
    초기자본 10억으로 시작, 기간 수익률(%) 반환.
    """
    scores_by_date = (
        score_df.groupby('date')
        .apply(lambda g: dict(zip(g['code'], g['score'])))
        .to_dict()
    )
    mask         = (price_pivot.index >= m_start) & (price_pivot.index < m_end_excl)
    trading_days = price_pivot.loc[mask].index.tolist()

    if not trading_days:
        return float('nan')

    portfolio = Portfolio()
    final_val = INITIAL_CAPITAL

    for date in trading_days:
        prices_today = price_pivot.loc[date].dropna().to_dict()
        today_scores = scores_by_date.get(date, {})
        total        = portfolio.total_assets(prices_today)

        for code in list(portfolio.positions.keys()):
            score = today_scores.get(code)
            price = prices_today.get(code)
            if score is None or not price or price <= 0:
                continue
            sell_target = _sell_target_ratio(score)
            if sell_target is None:
                continue
            if sell_target == 0.0:
                portfolio.sell_all(code, price, date)
            else:
                target_amount = sell_target * total
                cur_mv = portfolio.market_value(code, prices_today)
                if cur_mv > target_amount:
                    sell_qty = int((cur_mv - target_amount) / price)
                    if sell_qty > 0:
                        portfolio.sell(code, sell_qty, price, date, alloc_ratio=sell_target)

        total      = portfolio.total_assets(prices_today)
        candidates = []
        for code, score in today_scores.items():
            price = prices_today.get(code)
            if not price or price <= 0:
                continue
            target_ratio = _buy_target_ratio(score)
            if target_ratio is None:
                continue
            pos       = portfolio.positions.get(code)
            cur_alloc = pos["alloc_ratio"] if pos else 0.0
            if target_ratio > cur_alloc:
                candidates.append((code, score, target_ratio, target_ratio - cur_alloc))

        for code, score, target_ratio, incr_ratio in sorted(candidates, key=lambda x: x[1], reverse=True):
            buy_amount = incr_ratio * total
            price      = prices_today[code]
            if portfolio.cash < buy_amount:
                continue
            portfolio.buy(code, buy_amount, price, date, alloc_ratio=target_ratio)

        final_val = portfolio.total_assets(prices_today)

    return round((final_val / INITIAL_CAPITAL - 1) * 100, 2)


# ══════════════════════════════════════════════════════════════════════════════
# 월별 구간 / 벤치마크
# ══════════════════════════════════════════════════════════════════════════════

def get_monthly_ranges(price_pivot: pd.DataFrame,
                       h_start: pd.Timestamp, h_end: pd.Timestamp):
    """
    price_pivot의 실제 거래일 기준으로 월별 (월라벨, 시작, 종료exclusive) 반환.
    """
    mask         = (price_pivot.index >= h_start) & (price_pivot.index < h_end)
    trading_days = price_pivot.loc[mask].index

    if len(trading_days) == 0:
        return []

    periods  = trading_days.to_period('M')
    unique_m = sorted(periods.unique())
    ranges   = []
    for p in unique_m:
        days_in_m = trading_days[periods == p]
        m_start   = days_in_m[0]
        m_end_ex  = days_in_m[-1] + pd.Timedelta(days=1)
        ranges.append((str(p), m_start, m_end_ex))
    return ranges


def get_benchmark_per_month(monthly_ranges, prep_ref: pd.DataFrame) -> dict:
    """월별 KOSPI / KOSDAQ 수익률 반환: {YYYY-MM: {KOSPI:%, KOSDAQ:%}}"""
    bm = {}
    for label, m_start, m_end_ex in monthly_ranges:
        period = prep_ref[(prep_ref.index >= m_start) & (prep_ref.index < m_end_ex)]
        if period.empty:
            bm[label] = {"KOSPI": float('nan'), "KOSDAQ": float('nan')}
            continue
        bm[label] = {
            "KOSPI":  round(((1 + period['kospi_change']).prod()  - 1) * 100, 2),
            "KOSDAQ": round(((1 + period['kosdaq_change']).prod() - 1) * 100, 2),
        }
    return bm


def _fetch_daily_returns_external(start_dt: pd.Timestamp, end_dt: pd.Timestamp):
    """pykrx → yfinance → FinanceDataReader 순으로 KOSPI/KOSDAQ 일별 수익률 취득."""
    start_str = start_dt.strftime('%Y%m%d')
    end_str   = end_dt.strftime('%Y%m%d')
    start_ymd = start_dt.strftime('%Y-%m-%d')
    end_ymd   = end_dt.strftime('%Y-%m-%d')

    # ── 1. pykrx ──────────────────────────────────────────────────────────────
    try:
        from pykrx import stock
        print("   📡 pykrx로 KOSPI/KOSDAQ 취득 중...")
        df_k = stock.get_index_ohlcv_by_date(start_str, end_str, "1001")
        df_q = stock.get_index_ohlcv_by_date(start_str, end_str, "2001")
        # 버전에 따라 컬럼명이 다를 수 있어 '종가' 우선, 없으면 4번째 컬럼(index=3) 사용
        def _close(df):
            if '종가' in df.columns:
                return df['종가']
            return df.iloc[:, 3]
        kp = _close(df_k).sort_index().pct_change().dropna()
        kq = _close(df_q).sort_index().pct_change().dropna()
        kp.index = pd.to_datetime(kp.index)
        kq.index = pd.to_datetime(kq.index)
        if len(kp) > 0:
            print(f"   ✅ pykrx: {len(kp)}일")
            return kp, kq
        print("   ⚠️  pykrx 데이터 비어있음")
    except ImportError:
        print("   ℹ️  pykrx 미설치")
    except Exception as e:
        print(f"   ⚠️  pykrx 오류: {e}")

    # ── 2. yfinance (Ticker.history — database lock 우회) ──────────────────────
    try:
        import yfinance as yf
        print("   📡 yfinance로 KOSPI 취득 중...")
        hist_k = yf.Ticker("^KS11").history(start=start_ymd, end=end_ymd)
        if len(hist_k) > 0:
            kp = hist_k['Close'].pct_change().dropna()
            # timezone-aware index → naive
            if kp.index.tz is not None:
                kp.index = kp.index.tz_localize(None)

            # KOSDAQ: ^KQ11 시도, 실패 시 kp 복사(0배)로 대체
            try:
                hist_q = yf.Ticker("^KQ11").history(start=start_ymd, end=end_ymd)
                if len(hist_q) > 0:
                    kq = hist_q['Close'].pct_change().dropna()
                    if kq.index.tz is not None:
                        kq.index = kq.index.tz_localize(None)
                else:
                    raise ValueError("empty")
            except Exception:
                print("   ℹ️  KOSDAQ 데이터 없음 — KOSPI로 대체")
                kq = kp.copy()

            print(f"   ✅ yfinance: {len(kp)}일")
            return kp, kq
        print("   ⚠️  yfinance KOSPI 데이터 비어있음")
    except ImportError:
        print("   ℹ️  yfinance 미설치")
    except Exception as e:
        print(f"   ⚠️  yfinance 오류: {e}")

    # ── 3. FinanceDataReader ───────────────────────────────────────────────────
    try:
        import FinanceDataReader as fdr
        print("   📡 FinanceDataReader로 KOSPI/KOSDAQ 취득 중...")
        df_k = fdr.DataReader('KS11', start_ymd, end_ymd)
        df_q = fdr.DataReader('KQ11', start_ymd, end_ymd)
        kp = df_k['Close'].pct_change().dropna()
        kq = df_q['Close'].pct_change().dropna()
        kp.index = pd.to_datetime(kp.index)
        kq.index = pd.to_datetime(kq.index)
        if len(kp) > 0:
            print(f"   ✅ FinanceDataReader: {len(kp)}일")
            return kp, kq
    except ImportError:
        print("   ℹ️  FinanceDataReader 미설치  (pip install finance-datareader)")
    except Exception as e:
        print(f"   ⚠️  FinanceDataReader 오류: {e}")

    return None, None


def get_benchmark_per_month_external(monthly_ranges) -> dict:
    """외부 API(pykrx/yfinance)로 월별 KOSPI/KOSDAQ 수익률 계산."""
    all_starts = [r[1] for r in monthly_ranges]
    all_ends   = [r[2] for r in monthly_ranges]
    fetch_start = min(all_starts) - pd.Timedelta(days=5)
    fetch_end   = max(all_ends)

    kp_daily, kq_daily = _fetch_daily_returns_external(fetch_start, fetch_end)
    if kp_daily is None:
        return {}

    bm = {}
    for label, m_start, m_end_ex in monthly_ranges:
        kp_m = kp_daily[(kp_daily.index >= m_start) & (kp_daily.index < m_end_ex)]
        kq_m = kq_daily[(kq_daily.index >= m_start) & (kq_daily.index < m_end_ex)]
        bm[label] = {
            "KOSPI":  round(((1 + kp_m).prod() - 1) * 100, 2) if len(kp_m) > 0 else float('nan'),
            "KOSDAQ": round(((1 + kq_m).prod() - 1) * 100, 2) if len(kq_m) > 0 else float('nan'),
        }
    return bm


# ══════════════════════════════════════════════════════════════════════════════
# 출력 헬퍼
# ══════════════════════════════════════════════════════════════════════════════

def _fmt(val, suffix="%", width=9):
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return f"{'N/A':>{width}}"
    return f"{val:>+{width-len(suffix)}.2f}{suffix}"


def print_table(title: str, months: list, labels: list,
                results: dict, bm: dict, show_vs: bool = False):
    """
    results: {label: {month_label: ret%}}
    bm:      {month_label: {KOSPI:%, KOSDAQ:%}}
    show_vs: True면 vsKOSPI 표시, False면 raw 수익률 표시
    """
    col_w  = 10
    n_bm   = 2  # KOSPI, KOSDAQ
    n_col  = len(labels) + (n_bm if not show_vs else 0)
    width  = 9 + (col_w + 2) * n_col

    print(f"\n{title}")
    # 헤더
    hdr = f"{'월':<9}"
    for lb in labels:
        hdr += f"  {lb:>{col_w}}"
    if not show_vs:
        hdr += f"  {'KOSPI':>{col_w}}  {'KOSDAQ':>{col_w}}"
    print(hdr)
    print("─" * width)

    for m_label in months:
        row = f"{m_label:<9}"
        for lb in labels:
            val = results.get(lb, {}).get(m_label)
            if show_vs:
                kp  = bm.get(m_label, {}).get('KOSPI', float('nan'))
                val = round(val - kp, 2) if val is not None and not np.isnan(kp) else None
                row += f"  {_fmt(val, '%p', col_w):>{col_w}}"
            else:
                row += f"  {_fmt(val, '%', col_w):>{col_w}}"
        if not show_vs:
            kp = bm.get(m_label, {}).get('KOSPI',  float('nan'))
            kd = bm.get(m_label, {}).get('KOSDAQ', float('nan'))
            row += f"  {_fmt(kp, '%', col_w):>{col_w}}"
            row += f"  {_fmt(kd, '%', col_w):>{col_w}}"
        print(row)

    # 합계 행
    print("─" * width)
    cum_row = f"{'합계':<9}"
    def _cum(vals):
        v = [x for x in vals if x is not None and not np.isnan(x)]
        if not v:
            return float('nan')
        return round((np.prod([1 + x / 100 for x in v]) - 1) * 100, 2)

    for lb in labels:
        vals = [results.get(lb, {}).get(m) for m in months]
        if show_vs:
            kps  = [bm.get(m, {}).get('KOSPI', float('nan')) for m in months]
            vals = [round(v - k, 2) if v is not None and not np.isnan(k) else None
                    for v, k in zip(vals, kps)]
            cum_row += f"  {_fmt(_cum(vals), '%p', col_w):>{col_w}}"
        else:
            cum_row += f"  {_fmt(_cum(vals), '%', col_w):>{col_w}}"
    if not show_vs:
        kp_vals = [bm.get(m, {}).get('KOSPI',  float('nan')) for m in months]
        kd_vals = [bm.get(m, {}).get('KOSDAQ', float('nan')) for m in months]
        cum_row += f"  {_fmt(_cum(kp_vals), '%', col_w):>{col_w}}"
        cum_row += f"  {_fmt(_cum(kd_vals), '%', col_w):>{col_w}}"
    print(cum_row)
    print("=" * width)


# ══════════════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="2026년 상위 모델 월별 독립 비교")
    parser.add_argument("--top",      type=int, default=5,
                        help="비교할 상위 모델 수 (기본: 5)")
    parser.add_argument("--start",    default="2026-01-01",
                        help="시작일 (기본: 2026-01-01)")
    parser.add_argument("--end",      default="2026-05-23",
                        help="종료일 exclusive (기본: 2026-05-23 → 5/22까지)")
    parser.add_argument("--no-cache", action="store_true",
                        help="스코어 캐시 무시하고 재계산")
    args = parser.parse_args()

    h_start = pd.Timestamp(args.start)
    h_end   = pd.Timestamp(args.end)

    print(f"\n{'='*70}")
    print(f"📅  2026년 월별 독립 시뮬레이션  "
          f"({h_start.date()} ~ exclusive {h_end.date()})")
    print(f"   매월 초기자본 {INITIAL_CAPITAL:,}원으로 리셋")
    print(f"{'='*70}")

    # ── 상위 모델 로드 ─────────────────────────────────────────────────────────
    csv_path   = ROOT / "best_model_selection.csv"
    top_models = load_top_models_info(csv_path, args.top)
    labels     = [model_label(m) for m in top_models]

    print(f"\n비교 대상 모델 ({len(top_models)}개):")
    for m in top_models:
        ann_col = m.get('연환산vsKOSPI(%p)', m.get('vs_KOSPI(%p)', 'N/A'))
        print(f"  {model_label(m):<12}  train_end={m['train_end']}  "
              f"연환산vsKOSPI={ann_col}  신뢰도={m.get('신뢰도','?')}")

    # ── 가격 데이터 ────────────────────────────────────────────────────────────
    print(f"\n가격 데이터 로딩...")
    price_pivot = load_prices(h_start, h_end)
    if price_pivot.empty:
        print("❌ 가격 데이터 없음.")
        return
    td = price_pivot.index.tolist()
    print(f"   거래일: {len(td)}일  ({td[0].date()} ~ {td[-1].date()})")

    # ── 월별 구간 ─────────────────────────────────────────────────────────────
    monthly_ranges = get_monthly_ranges(price_pivot, h_start, h_end)
    if not monthly_ranges:
        print("❌ 거래일 없음.")
        return
    months = [r[0] for r in monthly_ranges]
    print(f"   월별 구간: {months}")

    # ── 벤치마크 ──────────────────────────────────────────────────────────────
    prep_files = _list_prep_files()
    prep_ref   = None
    if prep_files:
        prep_ref = _read_prep(prep_files[0])
        prep_ref['date'] = pd.to_datetime(prep_ref['date'])
        prep_ref = prep_ref.set_index('date').sort_index()

    bm_monthly: dict = {}
    has_bm = False

    if prep_ref is not None:
        bm_monthly = get_benchmark_per_month(monthly_ranges, prep_ref)
        has_bm = any(not np.isnan(v.get('KOSPI', float('nan')))
                     for v in bm_monthly.values())

    if not has_bm:
        print("   prep 캐시에 2026년 KOSPI 데이터 없음 → pykrx/yfinance로 취득 시도...")
        bm_monthly = get_benchmark_per_month_external(monthly_ranges)
        has_bm = any(not np.isnan(v.get('KOSPI', float('nan')))
                     for v in bm_monthly.values())
        if not has_bm:
            print("   ⚠️  외부 API 취득 실패 — KOSPI 비교 없이 진행")
            print("      pip install pykrx  또는  pip install yfinance")

    # ── 모델별 스코어 계산 → 월별 독립 시뮬레이션 ─────────────────────────────
    results: dict = {lb: {} for lb in labels}   # {label: {month: ret%}}

    for model_info in top_models:
        label    = model_label(model_info)
        fold_dir = get_fold_dir(model_info)
        tag        = f"{h_start.strftime('%Y%m%d')}_{h_end.strftime('%Y%m%d')}"
        cache_path = fold_dir / f"holdout_{tag}_scores.pkl"

        print(f"\n{'─'*70}")
        print(f"[ {label} ]  {fold_dir.name}")
        t0 = time.time()

        try:
            print(f"   모델 로딩...")
            models   = load_fold_models(fold_dir)
            print(f"   스코어 계산 (전체 기간)...")
            score_df = compute_scores(models, h_start, h_end, cache_path, args.no_cache)
            del models
            gc.collect()
            tf.keras.backend.clear_session()

            if score_df.empty:
                print(f"   ⚠️  스코어 없음 — prep 캐시에 2026년 데이터 확인 필요")
                continue

            score_df['date'] = pd.to_datetime(score_df['date'])
            print(f"   스코어 날짜: {score_df['date'].nunique()}일  "
                  f"({score_df['date'].min().date()} ~ {score_df['date'].max().date()})")

            # 월별 독립 시뮬레이션
            for m_label, m_start, m_end_ex in monthly_ranges:
                month_scores = score_df[
                    (score_df['date'] >= m_start) & (score_df['date'] < m_end_ex)
                ]
                ret = run_simulation_one_month(month_scores, m_start, m_end_ex, price_pivot)
                results[label][m_label] = ret

            elapsed = time.time() - t0
            line = "  ".join(f"{m}:{results[label].get(m, float('nan')):>+.2f}%"
                             for m in months)
            print(f"   ✅ {elapsed:.0f}초  {line}")

        except Exception as e:
            import traceback
            print(f"   ❌ 오류: {e}")
            traceback.print_exc()

    if all(not v for v in results.values()):
        print("\n❌ 결과 없음. prep 캐시를 2026년 데이터로 업데이트하세요.")
        return

    # ── 출력 ──────────────────────────────────────────────────────────────────
    print_table(
        "\n📊 월별 수익률 (독립 시뮬레이션, 초기자본 리셋)",
        months, labels, results, bm_monthly, show_vs=False
    )
    print_table(
        "\n📈 월별 초과수익률 vs KOSPI",
        months, labels, results, bm_monthly, show_vs=True
    )

    # ── 승패 카운트 ───────────────────────────────────────────────────────────
    print(f"\n🏅 KOSPI 초과 월 수 ({len(months)}개월 중)")
    beat_counts = {}
    for lb in labels:
        count = 0
        for m in months:
            val = results.get(lb, {}).get(m)
            kp  = bm_monthly.get(m, {}).get('KOSPI', float('nan'))
            if val is not None and not np.isnan(kp) and val > kp:
                count += 1
        beat_counts[lb] = count

    for lb, cnt in sorted(beat_counts.items(), key=lambda x: x[1], reverse=True):
        bar = "█" * cnt + "░" * (len(months) - cnt)
        print(f"  {lb:<12}  {bar}  {cnt}/{len(months)}")

    # ── CSV 저장 ──────────────────────────────────────────────────────────────
    save_rows = []
    for m in months:
        row = {"월": m}
        for lb in labels:
            row[lb] = results.get(lb, {}).get(m)
        if bm_monthly:
            row["KOSPI"]  = bm_monthly.get(m, {}).get("KOSPI")
            row["KOSDAQ"] = bm_monthly.get(m, {}).get("KOSDAQ")
        save_rows.append(row)

    # 합계 행
    def _cum_list(vals):
        v = [x for x in vals if x is not None and not np.isnan(x)]
        return round((np.prod([1 + x / 100 for x in v]) - 1) * 100, 2) if v else None

    cum_row = {"월": "누적"}
    for lb in labels:
        cum_row[lb] = _cum_list([results.get(lb, {}).get(m) for m in months])
    if bm_monthly:
        cum_row["KOSPI"]  = _cum_list([bm_monthly.get(m, {}).get("KOSPI")  for m in months])
        cum_row["KOSDAQ"] = _cum_list([bm_monthly.get(m, {}).get("KOSDAQ") for m in months])
    save_rows.append(cum_row)

    out_path = ROOT / "compare_top5_2026.csv"
    pd.DataFrame(save_rows).to_csv(out_path, index=False, encoding='utf-8-sig')
    print(f"\n💾 저장: {out_path}")


if __name__ == "__main__":
    main()