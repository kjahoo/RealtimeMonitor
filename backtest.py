"""
V3 모델 기반 전략 백테스팅
==========================
Phase 1 : 전 종목 스코어 사전 계산 (parquet 캐시, 최초 1회)
Phase 2 : 일별 매매 시뮬레이션
Phase 3 : 6개월 구간별 성과 분석

실행 예시:
    python backtest.py                             # 기본 (2015-01-01 ~ 오늘)
    python backtest.py --start 20200101 --end 20260101
    python backtest.py --rebuild-cache             # 스코어 강제 재계산
"""

import os, sys, argparse, pickle, warnings, time
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
warnings.filterwarnings("ignore")

# ─── GPU 설정 — TF 임포트 전에 환경변수, 임포트 직후 memory_growth 설정 ──────────
#   set_memory_growth 는 TF가 GPU를 초기화하기 전(첫 TF 연산 전)에 호출해야 한다.
#   모듈 최상단(임포트 직후)에서 호출하는 것이 유일하게 안전한 위치.
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
        # 이미 초기화된 경우(재진입 등) — 에러 없이 GPU 사용 시도
        if "cannot be modified after being initialized" in str(e):
            return True, 4096   # 이미 초기화됐어도 GPU는 사용 가능
        return False, 1024

# 모듈 로드 시점에 즉시 실행 (load_model 등 TF 연산보다 반드시 먼저)
_USE_GPU, _BATCH_SZ = _setup_gpu()
if _USE_GPU:
    print(f"   ✅ GPU 사용 (RTX 5060 Ti)  배치: {_BATCH_SZ}")
    print("   ⚠️  최초 실행 시 PTX JIT 컴파일 약 30분 소요 (이후 캐시 적용)")
else:
    print(f"   ℹ️  CPU 모드  배치: {_BATCH_SZ}")

# ─── 경로 설정 ─────────────────────────────────────────────────────────────────
ROOT         = Path(r"C:\Projects\RealtimeMonitor")
DATA_DIR     = ROOT / "Data" / "Stock"
MODEL_DIR    = ROOT / "Model"
LOG_DIR      = ROOT / "logs"
CACHE_FILE   = LOG_DIR / "backtest_score_cache.pkl"
RESULT_FILE  = LOG_DIR / "backtest_results.xlsx"

# ─── 전략 파라미터 ──────────────────────────────────────────────────────────────
INITIAL_CAPITAL = 1_000_000_000   # 10억

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

# ─── 모델 설정 (Update_Promising_Stocks 와 동일) ──────────────────────────────
V3_FEATURES = [
    'change_pct', 'volume_ratio', 'vol_power',
    'prog_net_ratio', 'prog_ratio_vol',
    'disparity_5', 'disparity_20',
    'rsi', 'bb_p', 'bb_w', 'adx',
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
MAX_LOOKBACK = max(s["lb"] for s in MODEL_SETTINGS.values())   # 98


# ══════════════════════════════════════════════════════════════════════════════
# Phase 1 : 스코어 캐시 빌드
# ══════════════════════════════════════════════════════════════════════════════

def load_models():
    models  = {}
    best_f1 = {}
    print("📂 모델 로딩 중...")
    for m_name, cfg in MODEL_SETTINGS.items():
        h5  = MODEL_DIR / f"{m_name}_lstm_v3.h5"
        scl = MODEL_DIR / f"{m_name}_lstm_v3.scaler"
        log = MODEL_DIR / f"log_{m_name}_v3_v3_unified.csv"
        if h5.exists() and scl.exists():
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
            print(f"   ✅ {m_name} (lb={actual_lb}  thr={threshold:.4f})")
        else:
            print(f"   ⚠️  {m_name} 파일 없음")

    # F1 비례 가중치: weight_Nd = f1_Nd / sum(f1 in group)
    for group in ("surge", "drop"):
        names = [n for n, c in MODEL_SETTINGS.items() if c["type"] == group and n in models]
        f1s   = [best_f1.get(n) for n in names]
        if all(f is not None for f in f1s):
            total = sum(f1s)
            for n, f in zip(names, f1s):
                models[n]["weight"] = f / total
                print(f"   ↳ {n} weight={models[n]['weight']:.4f} (f1={f:.4f})")

    return models


_USE_GPU  = False   # build_score_cache 진입 시 세팅
_BATCH_SZ = 1024    # CPU 기본값; GPU 시 4096으로 변경


def _compute_stock_scores(feat_data, models):
    """
    feat_data : np.ndarray (n_days, n_features) — 이미 fillna(0) 처리된 값
    반환      : dict {day_idx: total_score}
    """
    n         = len(feat_data)
    n_feat    = feat_data.shape[1]
    day_probs = {}

    for m_name, info in models.items():
        lb = info["lookback"]
        if n < lb:
            continue
        n_win = n - lb + 1

        # 슬라이딩 윈도우 (n_win, lb, n_feat)
        idx  = np.arange(n_win)[:, None] + np.arange(lb)[None, :]
        wins = feat_data[idx].astype(np.float32)

        # 배치 스케일링
        flat = info["scaler"].transform(wins.reshape(-1, n_feat)).astype(np.float32)
        wins = tf.constant(flat.reshape(n_win, lb, n_feat))

        # 배치 추론
        preds_list = []
        for s in range(0, n_win, _BATCH_SZ):
            preds_list.append(
                info["model"](wins[s: s + _BATCH_SZ], training=False).numpy().flatten()
            )
        preds = np.concatenate(preds_list)

        for i, prob in enumerate(preds):
            day_probs.setdefault(lb - 1 + i, {})[m_name] = float(prob)

        del wins, flat, preds, preds_list

    # 합산 → total_score
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


def build_score_cache(models, start_date=None, end_date=None, sample=None):
    """
    전 종목 rolling 스코어를 계산해 pkl 캐시로 저장.
    start_date: 이 날짜 이전 스코어는 저장하지 않음
    end_date  : 이 날짜 이후 스코어는 저장하지 않음 (확장 모드 전용)
    sample    : 지정 시 N개 종목만 처리 (테스트용)
    """
    csv_files = sorted(DATA_DIR.glob("A*.csv"))
    if sample:
        csv_files = csv_files[:sample]
        print(f"\n🔢 [샘플 모드] {sample}개 종목만 계산")
    else:
        print(f"\n🔢 스코어 계산 시작: {len(csv_files)}개 종목")
    if start_date:
        end_label = f" ~ {pd.Timestamp(end_date).date()}" if end_date else ""
        print(f"   캐시 저장 기준일: {pd.Timestamp(start_date).date()}{end_label}")
    t0 = time.time()

    records = []   # [{date, code, score}, ...]

    for i, fpath in enumerate(csv_files):
        code = fpath.stem[1:]   # "A005930" → "005930"
        try:
            df = pd.read_csv(fpath, encoding='utf-8-sig', usecols=['date'] + V3_FEATURES)
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date').reset_index(drop=True)
            df[V3_FEATURES] = df[V3_FEATURES].fillna(0)

            feat_data = df[V3_FEATURES].values
            scores    = _compute_stock_scores(feat_data, models)

            for di, score in scores.items():
                date = df['date'].iloc[di]
                if start_date and date < pd.Timestamp(start_date):
                    continue
                if end_date and date >= pd.Timestamp(end_date):
                    continue
                records.append({"date": date, "code": code, "score": score})

        except Exception as e:
            pass   # 데이터 부족·포맷 오류 종목 조용히 건너뜀

        # 진행 상황 출력
        if (i + 1) % 100 == 0 or i == len(csv_files) - 1:
            elapsed = time.time() - t0
            rate    = (i + 1) / elapsed
            remain  = (len(csv_files) - i - 1) / rate if rate > 0 else 0
            print(f"   [{i+1:4d}/{len(csv_files)}] "
                  f"경과: {elapsed/60:.1f}분  남은시간: {remain/60:.1f}분  "
                  f"누적 레코드: {len(records):,}")

    print(f"\n💾 캐시 저장 중... ({len(records):,} rows)")
    cache_df = pd.DataFrame(records)
    cache_df['date'] = pd.to_datetime(cache_df['date'])
    LOG_DIR.mkdir(exist_ok=True)
    cache_df.to_pickle(str(CACHE_FILE))
    print(f"   ✅ 저장 완료 → {CACHE_FILE}")
    return cache_df


def load_score_cache():
    print(f"📂 스코어 캐시 로딩: {CACHE_FILE}")
    df = pd.read_pickle(str(CACHE_FILE))
    df['date'] = pd.to_datetime(df['date'])
    print(f"   ✅ {len(df):,} rows  "
          f"({df['date'].min().date()} ~ {df['date'].max().date()})")
    return df


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 : 포트폴리오 시뮬레이션
# ══════════════════════════════════════════════════════════════════════════════

class Portfolio:
    def __init__(self, initial_capital):
        self.cash      = float(initial_capital)
        self.positions = {}   # code → {qty, avg_price, alloc_ratio}
        self._trade_log = []

    def total_assets(self, prices):
        mv = sum(
            pos["qty"] * prices.get(code, pos["avg_price"])
            for code, pos in self.positions.items()
        )
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
        self._trade_log.append(
            {"date": date, "action": "BUY", "code": code, "qty": qty, "price": price})
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
        self._trade_log.append(
            {"date": date, "action": "SELL", "code": code, "qty": qty, "price": price})

    def sell_all(self, code, price, date):
        if code in self.positions:
            self.sell(code, self.positions[code]["qty"], price, date)


def load_close_prices(start_date, end_date):
    """전 종목 close price 로드 → pivot DataFrame (date × code)"""
    print("\n📈 종가 데이터 로딩 중...")
    frames = []
    for fpath in DATA_DIR.glob("A*.csv"):
        code = fpath.stem[1:]
        try:
            df = pd.read_csv(fpath, encoding='utf-8-sig', usecols=['date', 'close'])
            df['date']  = pd.to_datetime(df['date'])
            df['code']  = code
            df = df[(df['date'] >= start_date) & (df['date'] <= end_date)]
            frames.append(df)
        except Exception:
            pass
    all_df = pd.concat(frames, ignore_index=True)
    pivot  = all_df.pivot_table(index='date', columns='code', values='close')
    pivot  = pivot.sort_index()
    print(f"   ✅ {pivot.shape[1]}개 종목 × {len(pivot)}개 거래일")
    return pivot


def run_simulation(score_cache, price_pivot, start_date, end_date):
    """
    일별 매매 시뮬레이션.
    score_cache : DataFrame [date, code, score]
    price_pivot : DataFrame (date × code) with close prices
    """
    # 스코어 캐시를 빠른 조회용 dict으로 변환
    # {날짜: {code: score}}
    print("\n🔄 시뮬레이션 전처리...")
    score_cache = score_cache[
        (score_cache['date'] >= start_date) &
        (score_cache['date'] <= end_date)
    ]
    scores_by_date = (
        score_cache
        .groupby('date')
        .apply(lambda g: dict(zip(g['code'], g['score'])))
        .to_dict()
    )

    trading_days = price_pivot.loc[start_date:end_date].index.tolist()
    print(f"   거래일: {len(trading_days)}일 ({trading_days[0].date()} ~ {trading_days[-1].date()})")

    portfolio = Portfolio(INITIAL_CAPITAL)
    history   = []

    for date in trading_days:
        if date not in price_pivot.index:
            continue

        prices_today = price_pivot.loc[date].dropna().to_dict()
        today_scores = scores_by_date.get(date, {})
        total        = portfolio.total_assets(prices_today)

        # ── 1. 매도 (먼저 실행) ────────────────────────────────────────────────
        for code in list(portfolio.positions.keys()):
            score = today_scores.get(code)
            price = prices_today.get(code)
            if score is None or price is None or price <= 0:
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

        # ── 2. 매수 (매도 후 실행, 점수 높은 순) ──────────────────────────────
        # 신규·기존 포지션 모두 포함: 현재 alloc_ratio보다 목표가 높을 때만 추가매수
        total = portfolio.total_assets(prices_today)

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

        # ── 일별 기록 ─────────────────────────────────────────────────────────
        total_eod = portfolio.total_assets(prices_today)
        history.append({
            "date":         date,
            "total_assets": total_eod,
            "cash":         portfolio.cash,
            "n_positions":  len(portfolio.positions),
            "cash_ratio":   portfolio.cash / total_eod if total_eod > 0 else 0,
        })

    return pd.DataFrame(history), pd.DataFrame(portfolio._trade_log)


# ══════════════════════════════════════════════════════════════════════════════
# Phase 3 : 성과 분석
# ══════════════════════════════════════════════════════════════════════════════

def _period_stats(period_df, label):
    ta = period_df["total_assets"]
    if len(ta) < 2:
        return None
    ret        = (ta.iloc[-1] / ta.iloc[0] - 1) * 100
    daily_ret  = ta.pct_change().dropna()
    sharpe     = (daily_ret.mean() / daily_ret.std() * np.sqrt(252)
                  if daily_ret.std() > 0 else 0)
    cummax     = ta.cummax()
    max_dd     = ((ta - cummax) / cummax).min() * 100
    win_days   = (daily_ret > 0).sum()
    win_rate   = win_days / len(daily_ret) * 100 if len(daily_ret) > 0 else 0
    return {
        "구간":        label,
        "시작자산(억)": round(ta.iloc[0] / 1e8, 2),
        "종료자산(억)": round(ta.iloc[-1] / 1e8, 2),
        "수익률(%)":   round(ret, 2),
        "Sharpe":      round(sharpe, 2),
        "최대낙폭(%)": round(max_dd, 2),
        "일승률(%)":   round(win_rate, 1),
        "거래일수":    len(ta),
    }


def analyze_periods(history_df, months=6):
    history_df = history_df.set_index("date").sort_index()
    start = history_df.index[0]
    end   = history_df.index[-1]

    rows = []
    cur  = start
    while cur <= end:
        next_cur = cur + pd.DateOffset(months=months)
        period   = history_df.loc[cur: next_cur - pd.Timedelta(days=1)]
        if len(period) >= 10:
            label = f"{cur.strftime('%Y-%m')} ~ {(next_cur - pd.Timedelta(days=1)).strftime('%Y-%m')}"
            s = _period_stats(period.reset_index(), label)
            if s:
                rows.append(s)
        cur = next_cur

    # 전체 구간 추가
    total = _period_stats(history_df.reset_index(), "전체")
    if total:
        rows.append(total)

    return pd.DataFrame(rows)


def print_report(period_df, trade_df):
    print("\n" + "=" * 72)
    print("📊 6개월 구간별 성과")
    print("=" * 72)
    pd.set_option('display.float_format', '{:,.2f}'.format)
    pd.set_option('display.max_rows', 100)
    print(period_df.to_string(index=False))

    print("\n" + "=" * 72)
    print("📋 거래 요약")
    print("=" * 72)
    if len(trade_df):
        buy_cnt  = (trade_df['action'] == 'BUY').sum()
        sell_cnt = (trade_df['action'] == 'SELL').sum()
        print(f"  총 매수: {buy_cnt:,}회  |  총 매도: {sell_cnt:,}회")
        print(f"  거래 종목 수: {trade_df['code'].nunique():,}개")
    print("=" * 72)


# ══════════════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="V3 전략 백테스팅")
    parser.add_argument("--start",         default="20150101",
                        help="백테스팅 시작일 YYYYMMDD (기본: 20150101)")
    parser.add_argument("--end",           default=datetime.now().strftime("%Y%m%d"),
                        help="백테스팅 종료일 YYYYMMDD (기본: 오늘)")
    parser.add_argument("--rebuild-cache", action="store_true",
                        help="스코어 캐시 강제 재계산")
    parser.add_argument("--extend-cache",  action="store_true",
                        help="기존 캐시에 누락 구간만 추가 계산 후 병합")
    parser.add_argument("--cache-from",    default="20070101",
                        help="캐시 빌드 기준 시작일 (기본: 20070101, 룩백 여유 포함)")
    parser.add_argument("--cache-to",      default=None,
                        help="캐시 빌드 종료일 YYYYMMDD (--extend-cache 에서 추가할 구간 끝, 기본: 기존 캐시 시작일)")
    parser.add_argument("--sample",        type=int, default=None,
                        help="N개 종목만 처리 (테스트용, 예: --sample 50)")
    args = parser.parse_args()

    start_date = pd.Timestamp(args.start)
    end_date   = pd.Timestamp(args.end)
    cache_from = pd.Timestamp(args.cache_from)

    print(f"\n🚀 백테스팅 시작")
    print(f"   기간: {start_date.date()} ~ {end_date.date()}")
    print(f"   초기 자산: {INITIAL_CAPITAL:,}원")

    # ── Phase 1: 스코어 캐시 ─────────────────────────────────────────────────
    if args.rebuild_cache or not CACHE_FILE.exists():
        print("\n" + "=" * 60)
        print("Phase 1: 스코어 사전 계산")
        print("=" * 60)
        models      = load_models()
        score_cache = build_score_cache(models, start_date=cache_from, sample=args.sample)
        del models
        tf.keras.backend.clear_session()

    elif args.extend_cache:
        # 기존 캐시 로드 → 빠진 앞쪽 구간만 계산 → 병합
        print("\n" + "=" * 60)
        print("Phase 1: 캐시 확장 (누락 구간 추가)")
        print("=" * 60)
        existing = load_score_cache()
        existing_min = existing['date'].min()

        # 추가 계산 끝 날짜: --cache-to 또는 기존 캐시 시작일
        if args.cache_to:
            extend_end = pd.Timestamp(args.cache_to)
        else:
            extend_end = existing_min

        if cache_from >= extend_end:
            print(f"   ℹ️  --cache-from({cache_from.date()}) ≥ 기존 캐시 시작일({extend_end.date()})")
            print("      추가할 구간이 없습니다.")
            score_cache = existing
        else:
            print(f"   추가 계산 구간: {cache_from.date()} ~ {extend_end.date()}")
            models   = load_models()
            new_part = build_score_cache(models, start_date=cache_from, end_date=extend_end, sample=args.sample)
            del models
            tf.keras.backend.clear_session()

            score_cache = pd.concat([new_part, existing], ignore_index=True)
            score_cache = score_cache.drop_duplicates(subset=['date', 'code']).sort_values(['date', 'code'])
            score_cache['date'] = pd.to_datetime(score_cache['date'])
            score_cache.to_pickle(str(CACHE_FILE))
            print(f"   ✅ 병합 완료: {len(score_cache):,} rows  "
                  f"({score_cache['date'].min().date()} ~ {score_cache['date'].max().date()})")

    else:
        score_cache = load_score_cache()

    # 캐시 범위 확인
    cache_min = score_cache['date'].min()
    if cache_min > start_date:
        print(f"\n⚠️  캐시 시작일({cache_min.date()})이 요청 시작일({start_date.date()})보다 늦습니다.")
        print("   --rebuild-cache --cache-from YYYYMMDD 로 재빌드하세요 (기본: 20070101).")

    # ── Phase 2: 시뮬레이션 ──────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("Phase 2: 매매 시뮬레이션")
    print("=" * 60)
    price_pivot = load_close_prices(start_date, end_date)
    history_df, trade_df = run_simulation(score_cache, price_pivot, start_date, end_date)

    # ── Phase 3: 성과 분석 ───────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("Phase 3: 성과 분석")
    print("=" * 60)
    period_df = analyze_periods(history_df)
    print_report(period_df, trade_df)

    # ── 결과 저장 ────────────────────────────────────────────────────────────
    print(f"\n💾 결과 저장 중...")
    LOG_DIR.mkdir(exist_ok=True)
    stem = str(RESULT_FILE).replace(".xlsx", "")
    period_df.to_csv(f"{stem}_periods.csv",  index=False, encoding='utf-8-sig')
    history_df.to_csv(f"{stem}_history.csv", index=False, encoding='utf-8-sig')
    trade_df.to_csv(f"{stem}_trades.csv",    index=False, encoding='utf-8-sig')
    print(f"   ✅ {stem}_periods.csv")
    print(f"   ✅ {stem}_history.csv")
    print(f"   ✅ {stem}_trades.csv")


if __name__ == "__main__":
    main()