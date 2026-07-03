"""
analyze_turnover_2026.py
========================
Exp-01(최적 전략)과 Production(현행 전략)의 2026년 1~5월
월별 회전율(turnover) 및 거래비용 추정.

  Exp-01     : buy=0.55  alloc=10%  sell=0.50  (sweep 최적)
  Production : BUY_TIERS/SELL_TIERS 현행 그대로

사용:
  python -X utf8 analyze_turnover_2026.py
"""

import warnings, sys, time, pickle
import numpy as np
import pandas as pd
from pathlib import Path
from collections import defaultdict

warnings.filterwarnings("ignore")
import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

ROOT = Path(r"C:\Projects\RealtimeMonitor")
sys.path.insert(0, str(ROOT))
import select_best_model as sbm

PROD_DIR = ROOT / "Model"

SIM_FROM = pd.Timestamp("2026-01-01")
SIM_TO   = pd.Timestamp("2026-05-31")

BUY_FEE_RATE  = 0.00015
SELL_FEE_RATE = 0.00195   # 매도세 + 수수료 합산


# ── 전략 정의 ─────────────────────────────────────────────────────────────────
STRATEGIES = {
    "Exp-01 (최적)": {
        "cache":       ROOT / "cache_exp01_full_2006_2026_scores.pkl",
        "buy_thresh":  0.55,
        "alloc":       0.10,
        "sell_thresh": 0.50,
        "tiered":      False,   # 단순 임계값 방식
    },
    "Production (현행)": {
        "cache":       ROOT / "cache_prod_full_2006_2026_scores.pkl",
        "buy_thresh":  None,    # BUY_TIERS 사용
        "alloc":       None,
        "sell_thresh": None,    # SELL_TIERS 사용
        "tiered":      True,
    },
}

# 현행 Production 매수/매도 티어 (backtest.py 와 동일)
BUY_TIERS  = [(0.8, 0.20), (0.7, 0.15), (0.6, 0.10), (0.5, 0.05)]
SELL_TIERS = [(0.25, 0.00), (0.30, 0.05), (0.35, 0.10), (0.40, 0.15)]


def _buy_ratio_tiered(score):
    for min_s, ratio in BUY_TIERS:
        if score >= min_s: return ratio
    return None

def _sell_ratio_tiered(score):
    for max_s, ratio in SELL_TIERS:
        if score < max_s: return ratio
    return None


# ── 포트폴리오 (거래 로그 포함) ───────────────────────────────────────────────
class Portfolio:
    INITIAL = 1_000_000_000

    def __init__(self):
        self.cash         = float(self.INITIAL)
        self.positions    = {}
        self._last_prices = {}
        self.trade_log    = []   # {date, action, code, qty, price, amount}

    def update_prices(self, prices):
        self._last_prices.update({k: v for k, v in prices.items() if v and v > 0})

    def _price(self, code, prices):
        p = prices.get(code)
        if p and p > 0: return p
        p = self._last_prices.get(code)
        if p and p > 0: return p
        pos = self.positions.get(code)
        return pos["avg_price"] if pos else 0.0

    def market_value(self, code, prices):
        pos = self.positions.get(code)
        return pos["qty"] * self._price(code, prices) if pos else 0.0

    def total_assets(self, prices):
        return self.cash + sum(self.market_value(c, prices) for c in self.positions)

    def buy(self, code, amount, price, date, alloc_ratio=0.0):
        qty  = int(amount / price)
        cost = qty * price * (1 + BUY_FEE_RATE)
        if qty == 0 or cost > self.cash: return False
        self.cash -= cost
        if code in self.positions:
            pos = self.positions[code]
            new_qty = pos["qty"] + qty
            pos["avg_price"] = (pos["qty"]*pos["avg_price"] + qty*price) / new_qty
            pos["qty"] = new_qty
            pos["alloc_ratio"] = alloc_ratio
        else:
            self.positions[code] = {"qty": qty, "avg_price": price, "alloc_ratio": alloc_ratio}
        self.trade_log.append({"date": date, "action": "BUY",
                               "code": code, "qty": qty, "price": price,
                               "amount": qty * price})
        return True

    def sell(self, code, qty, price, date, alloc_ratio=None):
        if code not in self.positions: return
        pos = self.positions[code]
        sell_qty = min(qty, pos["qty"])
        proceeds = sell_qty * price * (1 - SELL_FEE_RATE)
        self.cash += proceeds
        pos["qty"] -= sell_qty
        if pos["qty"] == 0:
            del self.positions[code]
        elif alloc_ratio is not None:
            pos["alloc_ratio"] = alloc_ratio
        self.trade_log.append({"date": date, "action": "SELL",
                               "code": code, "qty": sell_qty, "price": price,
                               "amount": sell_qty * price})

    def sell_all(self, code, price, date):
        if code not in self.positions: return
        pos = self.positions[code]
        self.sell(code, pos["qty"], price, date)


# ── 시뮬레이션 ────────────────────────────────────────────────────────────────
def run_simulation(scores_by_date, trading_days, prices_cache, strategy):
    tiered      = strategy["tiered"]
    buy_thresh  = strategy["buy_thresh"]
    alloc       = strategy["alloc"]
    sell_thresh = strategy["sell_thresh"]

    portfolio = Portfolio()
    daily     = []   # {date, total, n_pos}

    for date in trading_days:
        prices_today = prices_cache.get(date, {})
        today_scores = scores_by_date.get(date, {})
        portfolio.update_prices(prices_today)
        total = portfolio.total_assets(prices_today)

        # ── 매도 ──────────────────────────────────────────────────────────────
        for code in list(portfolio.positions.keys()):
            score = today_scores.get(code)
            price = prices_today.get(code)
            if score is None or not price or price <= 0: continue

            if tiered:
                sell_target = _sell_ratio_tiered(score)
                if sell_target is None: continue
                if sell_target == 0.0:
                    portfolio.sell_all(code, price, date)
                else:
                    target_amt = sell_target * total
                    cur_mv     = portfolio.market_value(code, prices_today)
                    if cur_mv > target_amt:
                        sell_qty = int((cur_mv - target_amt) / price)
                        if sell_qty > 0:
                            portfolio.sell(code, sell_qty, price, date, sell_target)
            else:
                if score < sell_thresh:
                    portfolio.sell_all(code, price, date)

        # ── 매수 ──────────────────────────────────────────────────────────────
        total = portfolio.total_assets(prices_today)

        if tiered:
            candidates = []
            for code, score in today_scores.items():
                price = prices_today.get(code)
                if not price or price <= 0: continue
                target_ratio = _buy_ratio_tiered(score)
                if target_ratio is None: continue
                cur_mv    = portfolio.market_value(code, prices_today)
                cur_alloc = cur_mv / total if total > 0 else 0.0
                if target_ratio > cur_alloc + 0.01:
                    candidates.append((code, score, target_ratio, target_ratio - cur_alloc))
            for code, score, target_ratio, incr in sorted(candidates, key=lambda x: x[1], reverse=True):
                buy_amt = incr * total
                price   = prices_today[code]
                if portfolio.cash >= buy_amt:
                    portfolio.buy(code, buy_amt, price, date, target_ratio)
        else:
            candidates = [
                (code, score)
                for code, score in today_scores.items()
                if score >= buy_thresh and prices_today.get(code, 0) > 0
            ]
            candidates.sort(key=lambda x: x[1], reverse=True)
            for code, score in candidates:
                price     = prices_today[code]
                cur_mv    = portfolio.market_value(code, prices_today)
                cur_alloc = cur_mv / total if total > 0 else 0.0
                if alloc > cur_alloc + 0.01:
                    buy_amt = (alloc - cur_alloc) * total
                    if portfolio.cash >= buy_amt:
                        portfolio.buy(code, buy_amt, price, date, alloc)

        daily.append({"date": date,
                      "total": portfolio.total_assets(prices_today),
                      "n_pos": len(portfolio.positions)})

    return portfolio, pd.DataFrame(daily)


# ── 월별 회전율 계산 ──────────────────────────────────────────────────────────
def calc_monthly_turnover(portfolio: Portfolio, daily_df: pd.DataFrame):
    if not portfolio.trade_log:
        return pd.DataFrame()

    trades = pd.DataFrame(portfolio.trade_log)
    trades["date"]  = pd.to_datetime(trades["date"])
    trades["month"] = trades["date"].dt.to_period("M")

    daily_df["date"]  = pd.to_datetime(daily_df["date"])
    daily_df["month"] = daily_df["date"].dt.to_period("M")
    avg_total = daily_df.groupby("month")["total"].mean()

    rows = []
    for month, grp in trades.groupby("month"):
        buys  = grp[grp.action == "BUY"]
        sells = grp[grp.action == "SELL"]
        buy_amt  = buys["amount"].sum()
        sell_amt = sells["amount"].sum()
        avg_val  = avg_total.get(month, portfolio.INITIAL)

        # 편도 회전율 = max(매수액, 매도액) / 평균 자산
        one_way = (buy_amt + sell_amt) / 2 / avg_val * 100
        # 거래비용 추정
        fee_cost = (buy_amt * BUY_FEE_RATE + sell_amt * SELL_FEE_RATE) / avg_val * 100

        rows.append({
            "월":        str(month),
            "매수종목":  len(buys["code"].unique()),
            "매도종목":  len(sells["code"].unique()),
            "매수금액":  buy_amt,
            "매도금액":  sell_amt,
            "평균자산":  avg_val,
            "회전율(%)": round(one_way, 2),
            "거래비용(%)": round(fee_cost, 4),
        })
    return pd.DataFrame(rows)


# ── 메인 ──────────────────────────────────────────────────────────────────────
def main():
    print("\n" + "="*72)
    print(f"📊  월별 회전율 분석  2026년 1월 ~ 5월")
    print("="*72)

    # 가격 데이터 로드 (1회 공유)
    print("\n[공통] 가격 데이터 로딩...")
    price_pivot = sbm.load_prices(SIM_FROM, SIM_TO, filter_bad=True)
    print(f"   종목 수: {price_pivot.shape[1]:,}  거래일: {price_pivot.shape[0]}")

    prices_cache = {d: price_pivot.loc[d].dropna().to_dict() for d in price_pivot.index}
    mask         = (price_pivot.index >= SIM_FROM) & (price_pivot.index <= SIM_TO)
    trading_days = list(price_pivot.loc[mask].index)

    results = {}
    for label, strategy in STRATEGIES.items():
        cache_path = strategy["cache"]
        if not cache_path.exists():
            print(f"\n⚠️  [{label}] 캐시 없음: {cache_path.name}")
            print("   먼저 analyze_full_range.py 를 실행하세요.")
            continue

        print(f"\n[{label}] 스코어 캐시 로드...")
        score_df = pd.read_pickle(str(cache_path))
        score_df["date"] = pd.to_datetime(score_df["date"])
        score_df = score_df[
            (score_df["date"] >= SIM_FROM) & (score_df["date"] <= SIM_TO)
        ]
        scores_by_date = (
            score_df.groupby("date")
            .apply(lambda g: dict(zip(g["code"], g["score"])))
            .to_dict()
        )
        print(f"   {len(score_df):,}건  ({len(scores_by_date)}거래일)")

        portfolio, daily_df = run_simulation(scores_by_date, trading_days,
                                             prices_cache, strategy)
        turnover_df = calc_monthly_turnover(portfolio, daily_df)
        results[label] = (portfolio, daily_df, turnover_df)

        # 개별 결과 출력
        print(f"\n  {'월':<8}  {'매수종목':>6}  {'매도종목':>6}  "
              f"{'매수금액(억)':>10}  {'매도금액(억)':>10}  "
              f"{'평균자산(억)':>10}  {'회전율':>7}  {'비용':>7}")
        print(f"  {'─'*73}")
        for _, r in turnover_df.iterrows():
            print(f"  {r['월']:<8}  {r['매수종목']:>6}  {r['매도종목']:>6}  "
                  f"  {r['매수금액']/1e8:>9.1f}  {r['매도금액']/1e8:>9.1f}  "
                  f"  {r['평균자산']/1e8:>9.1f}  {r['회전율(%)']:>6.1f}%  "
                  f"{r['거래비용(%)']:>6.3f}%")
        print(f"  {'─'*73}")

        total_buy  = turnover_df["매수금액"].sum()
        total_sell = turnover_df["매도금액"].sum()
        avg_val    = turnover_df["평균자산"].mean()
        total_fee  = turnover_df["거래비용(%)"].sum()
        total_turn = (total_buy + total_sell) / 2 / avg_val * 100

        ta = daily_df.set_index("date")["total"]
        period_ret = (ta.iloc[-1] / ta.iloc[0] - 1) * 100

        print(f"  5개월 합계 회전율: {total_turn:.1f}%  |  "
              f"거래비용 누적: {total_fee:.3f}%  |  "
              f"기간 수익률: {period_ret:+.2f}%")

    # ── 최종 비교표 ────────────────────────────────────────────────────────────
    if len(results) == 2:
        print(f"\n\n{'='*72}")
        print(" 최종 비교")
        print(f"{'='*72}")
        print(f"  {'항목':<20}  {'Exp-01 (최적)':>16}  {'Production (현행)':>18}")
        print(f"  {'─'*58}")

        labels = list(results.keys())
        def summary(label):
            _, daily_df, turnover_df = results[label]
            ta = daily_df.set_index("date")["total"]
            ret  = (ta.iloc[-1] / ta.iloc[0] - 1) * 100
            avg  = turnover_df["평균자산"].mean()
            turn = (turnover_df["매수금액"].sum() + turnover_df["매도금액"].sum()) / 2 / avg * 100
            fee  = turnover_df["거래비용(%)"].sum()
            n_buy_trades  = turnover_df["매수종목"].sum()
            n_sell_trades = turnover_df["매도종목"].sum()
            return ret, turn, fee, n_buy_trades, n_sell_trades

        r1 = summary(labels[0])
        r2 = summary(labels[1])

        rows = [
            ("기간 수익률", f"{r1[0]:+.2f}%", f"{r2[0]:+.2f}%"),
            ("5개월 총 회전율", f"{r1[1]:.1f}%", f"{r2[1]:.1f}%"),
            ("월 평균 회전율", f"{r1[1]/5:.1f}%", f"{r2[1]/5:.1f}%"),
            ("누적 거래비용", f"{r1[2]:.3f}%", f"{r2[2]:.3f}%"),
            ("총 매수 종목수(연)", f"{int(r1[3])}건", f"{int(r2[3])}건"),
            ("총 매도 종목수(연)", f"{int(r1[4])}건", f"{int(r2[4])}건"),
        ]
        for item, v1, v2 in rows:
            print(f"  {item:<20}  {v1:>16}  {v2:>18}")


if __name__ == "__main__":
    main()