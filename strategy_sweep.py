"""
strategy_sweep.py
=================
Exp-01 OOS(2013~2026) 스코어 캐시를 재활용하여
매매전략 파라미터 조합을 전수 탐색.

탐색 파라미터:
  buy_thresh      : 매수 최소 스코어
  alloc_per_stock : 종목당 목표 비중 (equal-weight 단순화)
  sell_thresh     : 매도 임계값 (이 미만이면 전량 청산)
  max_positions   : 최대 동시 보유 종목 수
  regime_ma       : KOSPI MA 기반 시장 레짐 필터 (0=없음)
  min_hold_days   : 최소 보유 거래일
  stop_loss_pct   : 손절선 (0=없음, 예: -0.10 = -10%)

사용:
  python -X utf8 strategy_sweep.py
"""

import warnings, sys, time, pickle, itertools
import numpy as np
import pandas as pd
from pathlib import Path

warnings.filterwarnings("ignore")
import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

ROOT = Path(r"C:\Projects\RealtimeMonitor")
sys.path.insert(0, str(ROOT))
import select_best_model as sbm

# ── 상수 ────────────────────────────────────────────────────────────────────
BUY_FEE_RATE  = 0.00015
SELL_FEE_RATE = 0.00195
INITIAL_CAPITAL = 1_000_000_000

OOS_FROM = 2013    # Exp-01 OOS 시작 (학습 후) — 명령줄에서 덮어씀
OOS_TO   = 2026    # 현재 기준 최대

# ── 파라미터 그리드 ───────────────────────────────────────────────────────────
GRID = {
    "buy_thresh":      [0.50, 0.55, 0.60, 0.65, 0.70],
    "alloc_per_stock": [0.05, 0.10, 0.15, 0.20],
    "sell_thresh":     [0.20, 0.30, 0.40, 0.50],
    "max_positions":   [5, 10, 20, 9999],        # 9999 = 무제한
    "regime_ma":       [0, 60, 120],             # 0=필터없음, N=KOSPI N일 MA 하회시만 매수
    "min_hold_days":   [0, 5, 10],
    "stop_loss_pct":   [0.0, -0.10, -0.15],      # 0=없음
}

TOP_N = 30   # 결과 상위 N개 출력


# ── 포트폴리오 (파라미터 주입형) ──────────────────────────────────────────────
class PortfolioP:
    def __init__(self):
        self.cash        = float(INITIAL_CAPITAL)
        self.positions   = {}   # code -> {qty, avg_price, buy_date}
        self._last_prices: dict = {}

    def update_prices(self, prices: dict):
        self._last_prices.update({k: v for k, v in prices.items() if v and v > 0})

    def _price(self, code, prices):
        p = prices.get(code)
        if p and p > 0: return p
        p = self._last_prices.get(code)
        if p and p > 0: return p
        pos = self.positions.get(code)
        return pos["avg_price"] if pos else 0.0

    def market_value(self, code, prices) -> float:
        pos = self.positions.get(code)
        return pos["qty"] * self._price(code, prices) if pos else 0.0

    def total_assets(self, prices) -> float:
        return self.cash + sum(self.market_value(c, prices) for c in self.positions)

    def buy(self, code, amount, price, date):
        qty  = int(amount / price)
        cost = qty * price * (1 + BUY_FEE_RATE)
        if qty == 0 or cost > self.cash: return False
        self.cash -= cost
        if code in self.positions:
            pos = self.positions[code]
            new_qty = pos["qty"] + qty
            pos["avg_price"] = (pos["qty"]*pos["avg_price"] + qty*price) / new_qty
            pos["qty"] = new_qty
        else:
            self.positions[code] = {"qty": qty, "avg_price": price, "buy_date": date}
        return True

    def sell(self, code, qty, price):
        if code not in self.positions: return
        pos = self.positions[code]
        sell_qty = min(qty, pos["qty"])
        proceeds = sell_qty * price * (1 - SELL_FEE_RATE)
        self.cash += proceeds
        pos["qty"] -= sell_qty
        if pos["qty"] == 0:
            del self.positions[code]

    def sell_all(self, code, price):
        if code not in self.positions: return
        pos = self.positions[code]
        self.sell(code, pos["qty"], price)


# ── 단일 파라미터 조합 시뮬레이션 ──────────────────────────────────────────────
def simulate_params(scores_by_date: dict, trading_days: list,
                    prices: dict,         # date -> {code: price}
                    kospi_ma: dict,       # date -> bool (True=상승추세)
                    params: dict) -> dict:

    buy_thresh      = params["buy_thresh"]
    alloc_per_stock = params["alloc_per_stock"]
    sell_thresh     = params["sell_thresh"]
    max_pos         = params["max_positions"]
    use_regime      = params["regime_ma"] > 0
    min_hold        = params["min_hold_days"]
    stop_loss       = params["stop_loss_pct"]

    portfolio = PortfolioP()
    assets    = []
    date_idx  = {d: i for i, d in enumerate(trading_days)}

    for date in trading_days:
        prices_today = prices.get(date, {})
        today_scores = scores_by_date.get(date, {})
        portfolio.update_prices(prices_today)
        total = portfolio.total_assets(prices_today)

        # ── 매도 ──────────────────────────────────────────────────────────────
        for code in list(portfolio.positions.keys()):
            pos   = portfolio.positions.get(code)
            if pos is None: continue
            price = prices_today.get(code)
            if not price or price <= 0: continue

            # 최소 보유기간 체크
            if min_hold > 0:
                buy_idx  = date_idx.get(pos.get("buy_date"), 0)
                cur_idx  = date_idx.get(date, 0)
                if (cur_idx - buy_idx) < min_hold:
                    # 손절만 허용
                    if stop_loss != 0.0:
                        loss_pct = (price - pos["avg_price"]) / pos["avg_price"]
                        if loss_pct <= stop_loss:
                            portfolio.sell_all(code, price)
                    continue

            # 손절 체크
            if stop_loss != 0.0:
                loss_pct = (price - pos["avg_price"]) / pos["avg_price"]
                if loss_pct <= stop_loss:
                    portfolio.sell_all(code, price)
                    continue

            # 스코어 기반 청산
            score = today_scores.get(code)
            if score is not None and score < sell_thresh:
                portfolio.sell_all(code, price)

        # ── 매수 ──────────────────────────────────────────────────────────────
        # 레짐 필터: KOSPI가 MA 하회 중(하락추세)일 때만 매수
        if use_regime and kospi_ma.get(date, True):
            assets.append(portfolio.total_assets(prices_today))
            continue

        total = portfolio.total_assets(prices_today)
        n_pos = len(portfolio.positions)

        candidates = [
            (code, score)
            for code, score in today_scores.items()
            if score >= buy_thresh
            and prices_today.get(code, 0) > 0
        ]
        candidates.sort(key=lambda x: x[1], reverse=True)

        for code, score in candidates:
            if n_pos >= max_pos and code not in portfolio.positions:
                continue
            price = prices_today[code]
            cur_mv    = portfolio.market_value(code, prices_today)
            total_now = portfolio.total_assets(prices_today)
            cur_alloc = cur_mv / total_now if total_now > 0 else 0.0
            if alloc_per_stock > cur_alloc + 0.01:
                buy_amt = (alloc_per_stock - cur_alloc) * total_now
                if portfolio.buy(code, buy_amt, price, date):
                    n_pos = len(portfolio.positions)

        assets.append(portfolio.total_assets(prices_today))

    if len(assets) < 10:
        return {}

    ta        = pd.Series(assets)
    ret       = (ta.iloc[-1] / ta.iloc[0] - 1) * 100
    daily_ret = ta.pct_change().dropna()
    sharpe    = (daily_ret.mean() / daily_ret.std() * np.sqrt(252)
                 if daily_ret.std() > 0 else 0.0)
    max_dd    = ((ta - ta.cummax()) / ta.cummax()).min() * 100
    return {"ret": round(ret, 2), "sharpe": round(sharpe, 2), "mdd": round(max_dd, 2)}


# ── 연도별 성과를 합산하는 시뮬레이션 래퍼 ────────────────────────────────────
def simulate_years(scores_by_date: dict, price_pivot: pd.DataFrame,
                   kospi_ma_above: dict, params: dict,
                   from_year: int, to_year: int) -> dict:

    # 가격을 date->dict로 변환 (캐시)
    prices_cache = {}
    for d in price_pivot.index:
        prices_cache[d] = price_pivot.loc[d].dropna().to_dict()

    h_start = pd.Timestamp(f"{from_year}-01-01")
    h_end   = pd.Timestamp(f"{to_year+1}-01-01")
    mask    = (price_pivot.index >= h_start) & (price_pivot.index < h_end)
    trading_days = list(price_pivot.loc[mask].index)

    return simulate_params(scores_by_date, trading_days,
                           prices_cache, kospi_ma_above, params)


# ── KOSPI MA 계산 ─────────────────────────────────────────────────────────────
def build_kospi_ma(price_pivot: pd.DataFrame, ma_days: int) -> dict:
    """date -> True면 상승추세(매수 억제), False면 하락/횡보(매수 허용)"""
    pf = sbm._list_prep_files()
    if not pf: return {}
    ref = sbm._read_prep(pf[0])
    ref['date'] = pd.to_datetime(ref['date'])
    ref = ref.set_index('date').sort_index()

    # kospi_change로 KOSPI 지수 재구성
    ref['kospi_idx'] = (1 + ref['kospi_change']).cumprod()
    ref['kospi_ma']  = ref['kospi_idx'].rolling(ma_days).mean()
    ref['above_ma']  = ref['kospi_idx'] > ref['kospi_ma']

    return ref['above_ma'].to_dict()


# ── 메인 ──────────────────────────────────────────────────────────────────────
def main():
    import argparse, datetime
    today = datetime.date.today().year

    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default='exp01',
                        choices=['exp01', 'roll7y06'],
                        help='탐색할 모델 (exp01 또는 roll7y06)')
    args = parser.parse_args()

    MODEL_META = {
        'exp01':    {'label': 'Exp-01',     'oos_from': 2013, 'cache': 'cache_exp01_full_2006_2026_scores.pkl'},
        'roll7y06': {'label': 'Roll7y-06',  'oos_from': 2018, 'cache': 'cache_roll06_full_2006_2026_scores.pkl'},
    }
    meta = MODEL_META[args.model]
    oos_from = meta['oos_from']

    print("\n" + "="*72)
    print(f"🔬  전략 파라미터 전수 탐색  ({meta['label']} OOS {oos_from}~{OOS_TO})")
    print("="*72)

    # 1. 캐시된 스코어 로드 (analyze_full_range.py가 생성한 캐시 재사용)
    cache_path = ROOT / meta['cache']
    if not cache_path.exists():
        print(f"❌ 스코어 캐시 없음: {cache_path}")
        print("   먼저 analyze_full_range.py 를 실행하세요.")
        return

    print(f"\n[1] 스코어 캐시 로드...")
    score_df = pd.read_pickle(str(cache_path))
    score_df['date'] = pd.to_datetime(score_df['date'])
    # OOS 기간만 필터
    score_df = score_df[score_df['date'].dt.year >= oos_from]
    scores_by_date = (
        score_df.groupby('date')
        .apply(lambda g: dict(zip(g['code'], g['score'])))
        .to_dict()
    )
    print(f"   레코드: {len(score_df):,}건  날짜 수: {len(scores_by_date):,}")

    # 2. 가격 데이터 로드
    print(f"\n[2] 가격 데이터 로딩 (품질필터)...")
    price_pivot = sbm.load_prices(
        pd.Timestamp(f"{oos_from}-01-01"),
        pd.Timestamp(f"{OOS_TO}-12-31"),
        filter_bad=True
    )
    print(f"   종목 수: {price_pivot.shape[1]:,}  거래일: {price_pivot.shape[0]}")

    # 가격 캐시 (date -> {code: price})
    print("   가격 dict 변환 중...")
    prices_cache = {}
    for d in price_pivot.index:
        prices_cache[d] = price_pivot.loc[d].dropna().to_dict()

    h_start = pd.Timestamp(f"{oos_from}-01-01")
    h_end   = pd.Timestamp(f"{OOS_TO+1}-01-01")
    mask    = (price_pivot.index >= h_start) & (price_pivot.index < h_end)
    trading_days = list(price_pivot.loc[mask].index)
    print(f"   OOS 거래일: {len(trading_days)}")

    # 3. KOSPI MA 시리즈 사전 계산
    print(f"\n[3] KOSPI MA 시리즈 계산...")
    kospi_ma_series = {}
    for ma_days in [d for d in GRID["regime_ma"] if d > 0]:
        kospi_ma_series[ma_days] = build_kospi_ma(price_pivot, ma_days)
        print(f"   MA{ma_days} 계산 완료")

    # 4. 파라미터 전수 탐색
    keys   = list(GRID.keys())
    values = list(GRID.values())
    combos = list(itertools.product(*values))
    total  = len(combos)
    print(f"\n[4] 파라미터 탐색 시작: {total:,}개 조합")
    print(f"    예상 소요: {total * 0.5 / 60:.0f}분 내외\n")

    results = []
    t0 = time.time()

    for i, combo in enumerate(combos):
        params = dict(zip(keys, combo))

        # 레짐 MA 시리즈 선택
        ma_key   = params["regime_ma"]
        ma_above = kospi_ma_series.get(ma_key, {}) if ma_key > 0 else {}

        res = simulate_params(scores_by_date, trading_days,
                              prices_cache, ma_above, params)
        if not res:
            continue

        results.append({**params, **res})

        if (i + 1) % 500 == 0:
            elapsed = time.time() - t0
            remain  = elapsed / (i+1) * (total - i - 1)
            print(f"   [{i+1:>5}/{total}]  경과 {elapsed/60:.1f}분  "
                  f"남은 {remain/60:.1f}분")

    print(f"\n   완료: {len(results):,}개 결과  ({(time.time()-t0)/60:.1f}분)")

    # 5. 결과 분석
    df = pd.DataFrame(results)
    out_csv = ROOT / f"strategy_sweep_{args.model}_results.csv"
    df.to_csv(out_csv, index=False, encoding='utf-8-sig')
    print(f"💾 전체 결과 저장: {out_csv.name}")

    _print_top(df, "Sharpe 기준 TOP", "sharpe", ascending=False)
    _print_top(df, "수익률 기준 TOP", "ret", ascending=False)
    _print_top(df, "MDD 최소 기준 TOP (수익률 > 50%)", "mdd", ascending=True,
               filter_col="ret", filter_val=50)


def _print_top(df, title, sort_col, ascending, filter_col=None, filter_val=None):
    tmp = df.copy()
    if filter_col and filter_val is not None:
        tmp = tmp[tmp[filter_col] > filter_val]
    if tmp.empty:
        print(f"\n⚠️  {title}: 결과 없음")
        return

    top = tmp.sort_values(sort_col, ascending=ascending).head(TOP_N)

    print(f"\n{'='*100}")
    print(f" {title} {TOP_N}")
    print(f"{'='*100}")
    print(f"  {'buy_thr':>7}  {'alloc':>5}  {'sell_thr':>8}  {'max_pos':>7}  "
          f"{'regime':>6}  {'min_hold':>8}  {'stoploss':>8}  "
          f"{'ret':>8}  {'sharpe':>7}  {'mdd':>8}")
    print(f"  {'-'*95}")

    for _, r in top.iterrows():
        regime_str = f"MA{int(r['regime_ma'])}" if r['regime_ma'] > 0 else "없음"
        maxpos_str = str(int(r['max_positions'])) if r['max_positions'] < 9999 else "무제한"
        sl_str     = f"{r['stop_loss_pct']:.0%}" if r['stop_loss_pct'] != 0 else "없음"
        print(f"  {r['buy_thresh']:>7.2f}  {r['alloc_per_stock']:>5.0%}  "
              f"{r['sell_thresh']:>8.2f}  {maxpos_str:>7}  "
              f"{regime_str:>6}  {int(r['min_hold_days']):>8}일  {sl_str:>8}  "
              f"{r['ret']:>+7.1f}%  {r['sharpe']:>7.2f}  {r['mdd']:>7.1f}%")


if __name__ == "__main__":
    main()