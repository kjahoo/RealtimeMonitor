"""
strategy_sweep_bear.py
======================
Exp-01 OOS(2013~2026) 스코어로 매매 파라미터를 전수 탐색하되,
연도별 성과(Bull/Sideways/Bear)를 분리해 보고.

KOSPI 연간 수익률 기준 시장 분류:
  Bull    : >= +10%
  Sideways: -5% ~ +10%
  Bear    :  < -5%

레짐 필터 (표준): KOSPI > MA_N 일 때만 신규 매수 허용
  → 하락장에서 신규 진입 차단, 기존 포지션은 매도 조건만 적용

그리드 (960 조합):
  buy_thresh   : [0.50, 0.55, 0.60, 0.65]
  alloc        : [0.05, 0.10, 0.15, 0.20]
  sell_thresh  : [0.35, 0.40, 0.45, 0.50, 0.55]
  regime_ma    : [0, 60, 120]
  stop_loss    : [0.0, -0.05, -0.08, -0.10]

사용:
  python -X utf8 strategy_sweep_bear.py
"""

import warnings, sys, time
import numpy as np
import pandas as pd
from pathlib import Path
from itertools import product

warnings.filterwarnings("ignore")
import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

ROOT = Path(r"C:\Projects\RealtimeMonitor")
sys.path.insert(0, str(ROOT))
import select_best_model as sbm

CACHE_PATH = ROOT / "cache_exp01_full_2006_2026_scores.pkl"
BUY_FEE    = 0.00015
SELL_FEE   = 0.00195
INITIAL    = 1_000_000_000

OOS_FROM = pd.Timestamp("2013-01-01")
OOS_TO   = pd.Timestamp("2026-05-31")

GRID = {
    "buy_thresh":  [0.50, 0.55, 0.60, 0.65],
    "alloc":       [0.05, 0.10, 0.15, 0.20],
    "sell_thresh": [0.35, 0.40, 0.45, 0.50, 0.55],
    "regime_ma":   [0, 60, 120],
    "stop_loss":   [0.0, -0.05, -0.08, -0.10],
}


# ══════════════════════════════════════════════════════════════════════════════
# 포트폴리오
# ══════════════════════════════════════════════════════════════════════════════
class Portfolio:
    def __init__(self):
        self.cash = float(INITIAL)
        self.pos  = {}          # code -> {qty, avg_price}
        self._lpx = {}

    def upd(self, px):
        self._lpx.update({k: v for k, v in px.items() if v and v > 0})

    def _p(self, code, px):
        p = px.get(code)
        if p and p > 0: return p
        p = self._lpx.get(code)
        if p and p > 0: return p
        return self.pos[code]["avg_price"] if code in self.pos else 0.0

    def mv(self, code, px):
        p = self.pos.get(code)
        return p["qty"] * self._p(code, px) if p else 0.0

    def total(self, px):
        return self.cash + sum(self.mv(c, px) for c in self.pos)

    def buy(self, code, amount, price):
        qty  = int(amount / price)
        cost = qty * price * (1 + BUY_FEE)
        if qty == 0 or cost > self.cash: return
        self.cash -= cost
        if code in self.pos:
            p  = self.pos[code]
            nq = p["qty"] + qty
            p["avg_price"] = (p["qty"] * p["avg_price"] + qty * price) / nq
            p["qty"] = nq
        else:
            self.pos[code] = {"qty": qty, "avg_price": price}

    def sell_all(self, code, price):
        p = self.pos.pop(code, None)
        if p:
            self.cash += p["qty"] * price * (1 - SELL_FEE)


# ══════════════════════════════════════════════════════════════════════════════
# 단일 파라미터 조합 시뮬레이션 → (date, total) 리스트 반환
# ══════════════════════════════════════════════════════════════════════════════
def simulate(scores_by_date, trading_days, prices_cache,
             kospi_above,        # {date: bool}  True=KOSPI>MA → 매수 허용
             buy_thresh, alloc, sell_thresh, regime_ma, stop_loss):

    pf    = Portfolio()
    daily = []

    for date in trading_days:
        px = prices_cache.get(date, {})
        sc = scores_by_date.get(date, {})
        pf.upd(px)

        # 매도: 손절 먼저, 이후 스코어 기반 청산
        for code in list(pf.pos.keys()):
            price = px.get(code)
            if not price or price <= 0: continue
            pos = pf.pos.get(code)
            if pos is None: continue

            if stop_loss != 0.0:
                if (price - pos["avg_price"]) / pos["avg_price"] <= stop_loss:
                    pf.sell_all(code, price)
                    continue

            score = sc.get(code)
            if score is not None and score < sell_thresh:
                pf.sell_all(code, price)

        # 매수: 레짐 필터 (0=없음, 또는 KOSPI > MA일 때만 허용)
        if regime_ma == 0 or kospi_above.get(date, True):
            tot = pf.total(px)
            for code, score in sorted(sc.items(), key=lambda x: x[1], reverse=True):
                if score < buy_thresh: break
                price = px.get(code, 0)
                if price <= 0: continue
                cur = pf.mv(code, px) / tot if tot > 0 else 0.0
                if alloc > cur + 0.01:
                    pf.buy(code, (alloc - cur) * tot, price)

        daily.append((date, pf.total(px)))

    return daily


# ══════════════════════════════════════════════════════════════════════════════
# 지표 계산
# ══════════════════════════════════════════════════════════════════════════════
def metrics(daily):
    ta  = pd.Series([v for _, v in daily])
    ret = (ta.iloc[-1] / ta.iloc[0] - 1) * 100
    dr  = ta.pct_change().dropna()
    sh  = (dr.mean() / dr.std() * (252 ** 0.5)) if dr.std() > 0 else 0.0
    mdd = ((ta - ta.cummax()) / ta.cummax()).min() * 100
    return round(ret, 1), round(sh, 3), round(mdd, 1)


def yearly_rets(daily):
    df = pd.DataFrame(daily, columns=["date", "total"])
    df["year"] = df["date"].dt.year
    out = {}
    for yr, g in df.groupby("year"):
        out[yr] = round((g["total"].iloc[-1] / g["total"].iloc[0] - 1) * 100, 1)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════════════
def main():
    print("\n" + "="*72)
    print("🐻  Exp-01 매매 파라미터 탐색 (횡보/하락장 성과 포함)")
    print("="*72)

    # ── 데이터 로드 ──────────────────────────────────────────────────────────
    print("\n[1] 스코어 캐시 로드...")
    score_df = pd.read_pickle(str(CACHE_PATH))
    score_df["date"] = pd.to_datetime(score_df["date"])
    score_df = score_df[(score_df["date"] >= OOS_FROM) & (score_df["date"] <= OOS_TO)]
    scores_by_date = (
        score_df.groupby("date")
        .apply(lambda g: dict(zip(g["code"], g["score"])))
        .to_dict()
    )
    print(f"   {len(score_df):,}건")

    print("\n[2] 가격 데이터 로딩...")
    price_pivot = sbm.load_prices(OOS_FROM, OOS_TO, filter_bad=True)
    mask         = (price_pivot.index >= OOS_FROM) & (price_pivot.index <= OOS_TO)
    trading_days = list(price_pivot.loc[mask].index)
    prices_cache = {d: price_pivot.loc[d].dropna().to_dict() for d in price_pivot.index}
    print(f"   종목: {price_pivot.shape[1]:,}  거래일: {len(trading_days)}")

    print("\n[3] KOSPI 연간 수익률 & MA 시리즈 계산...")
    pf_files = sbm._list_prep_files()
    ref = sbm._read_prep(pf_files[0])
    ref["date"] = pd.to_datetime(ref["date"])
    ref = ref.set_index("date").sort_index()
    ref["kidx"] = (1 + ref["kospi_change"]).cumprod()

    # KOSPI 연간 수익률
    kospi_annual = {}
    for yr, g in ref.groupby(ref.index.year):
        kospi_annual[yr] = round((g["kidx"].iloc[-1] / g["kidx"].iloc[0] - 1) * 100, 1)

    # MA 시리즈: True = KOSPI > MA (매수 허용)
    kospi_ma = {}
    for ma in [60, 120]:
        s = ref["kidx"].rolling(ma).mean()
        kospi_ma[ma] = (ref["kidx"] > s).to_dict()

    # 연도별 시장 분류
    def cls(r):
        return "Bull" if r >= 10 else ("Bear" if r < -5 else "Side")

    oos_yrs = [y for y in sorted(kospi_annual) if 2013 <= y <= 2026]
    bull_yrs = [y for y in oos_yrs if cls(kospi_annual[y]) == "Bull"]
    side_yrs = [y for y in oos_yrs if cls(kospi_annual[y]) == "Side"]
    bear_yrs = [y for y in oos_yrs if cls(kospi_annual[y]) == "Bear"]

    print(f"\n  KOSPI 연간 수익률:")
    for yr in oos_yrs:
        r = kospi_annual[yr]
        print(f"    {yr}  {r:>+6.1f}%  [{cls(r)}]")
    print(f"\n  Bull    : {bull_yrs}")
    print(f"  Sideways: {side_yrs}")
    print(f"  Bear    : {bear_yrs}")

    # ── 파라미터 탐색 ────────────────────────────────────────────────────────
    combos = list(product(
        GRID["buy_thresh"], GRID["alloc"],
        GRID["sell_thresh"], GRID["regime_ma"], GRID["stop_loss"]
    ))
    print(f"\n[4] 파라미터 탐색: {len(combos):,}개 조합")

    rows, t0 = [], time.time()
    for i, (bt, al, st, rm, sl) in enumerate(combos):
        ka = kospi_ma.get(rm, {}) if rm > 0 else {}
        daily = simulate(scores_by_date, trading_days, prices_cache,
                         ka, bt, al, st, rm, sl)
        if not daily:
            continue

        ret, sh, mdd = metrics(daily)
        yr = yearly_rets(daily)

        bull_r = [yr.get(y, 0) for y in bull_yrs]
        side_r = [yr.get(y, 0) for y in side_yrs]
        bear_r = [yr.get(y, 0) for y in bear_yrs]

        rows.append({
            "buy":      bt, "alloc": al, "sell": st,
            "ma":       rm, "sl":    sl,
            "ret":      ret, "sharpe": sh, "mdd": mdd,
            "bull_avg": round(np.mean(bull_r), 1) if bull_r else 0,
            "side_avg": round(np.mean(side_r), 1) if side_r else 0,
            "bear_avg": round(np.mean(bear_r), 1) if bear_r else 0,
            "bear_min": round(min(bear_r),      1) if bear_r else 0,
        })

        if (i + 1) % 100 == 0 or i == len(combos) - 1:
            el = time.time() - t0
            print(f"   [{i+1:4d}/{len(combos)}]  {el/60:.1f}분 경과  "
                  f"남은 {(len(combos)-i-1)/((i+1)/el)/60:.1f}분")

    df = pd.DataFrame(rows)
    df.to_csv(ROOT / "strategy_sweep_bear_results.csv", index=False, encoding="utf-8-sig")
    print(f"\n💾 저장: strategy_sweep_bear_results.csv")

    # ── 출력 함수 ─────────────────────────────────────────────────────────────
    H = (f"  {'buy':>5} {'alloc':>6} {'sell':>5} {'MA':>5} {'SL':>6}  "
         f"{'ret':>8} {'sharpe':>7} {'mdd':>7}  "
         f"{'bull':>7} {'side':>7} {'bear평균':>8} {'bear최악':>8}")
    SEP = "  " + "─" * 86

    def tbl(subset, title, n=20):
        print(f"\n{'='*90}")
        print(f" {title}")
        print(f"{'='*90}")
        print(H); print(SEP)
        for r in subset.head(n).to_dict("records"):
            ma = f"MA{r['ma']}" if r['ma'] else "없음"
            sl = f"{r['sl']*100:.0f}%" if r['sl'] else "없음"
            print(f"  {r['buy']:>5.2f} {r['alloc']*100:>5.0f}%"
                  f" {r['sell']:>5.2f} {ma:>5} {sl:>6}  "
                  f"{r['ret']:>+7.1f}% {r['sharpe']:>7.2f} {r['mdd']:>6.1f}%  "
                  f"{r['bull_avg']:>+6.1f}% {r['side_avg']:>+6.1f}%"
                  f" {r['bear_avg']:>+7.1f}% {r['bear_min']:>+7.1f}%")

    # TOP 20 by Sharpe (전체)
    tbl(df.nlargest(20, "sharpe"), "▶ Sharpe 기준 TOP 20 (전체 기간)")

    # 하락장 평균 > -5% 조건 → Sharpe TOP 20
    bear_ok = df[df["bear_avg"] >= -5]
    tbl(bear_ok.nlargest(20, "sharpe"),
        f"▶ 하락장 평균손실 -5% 이내 조건 → Sharpe TOP 20  (해당: {len(bear_ok):,}개)")

    # 하락장 최악 > -10% 조건 → Sharpe TOP 20
    bear_ok2 = df[df["bear_min"] >= -10]
    tbl(bear_ok2.nlargest(20, "sharpe"),
        f"▶ 하락장 최악연도 -10% 이내 조건 → Sharpe TOP 20  (해당: {len(bear_ok2):,}개)")

    # ── 최적 추천 (하락장 -5% 이내 + 최고 Sharpe) ───────────────────────────
    pick = (bear_ok if not bear_ok.empty else df).nlargest(1, "sharpe").iloc[0]
    ma_s = f"MA{int(pick['ma'])}" if pick['ma'] else "없음"
    sl_s = f"{pick['sl']*100:.0f}%" if pick['sl'] else "없음"

    print(f"\n\n{'='*72}")
    print(" ★ 최적 추천 파라미터  (하락장 평균손실 -5% 이내 + 최고 Sharpe)")
    print(f"{'='*72}")
    print(f"  매수 임계값    : {pick['buy']:.2f}  (score >= {pick['buy']})")
    print(f"  종목당 비중    : {pick['alloc']*100:.0f}%")
    print(f"  매도 임계값    : {pick['sell']:.2f}  (score <  {pick['sell']})")
    print(f"  레짐 필터      : {ma_s}  (KOSPI > MA 일 때만 신규 매수)")
    print(f"  손절선         : {sl_s}")
    print(f"\n  전체 수익률    : {pick['ret']:+.1f}%")
    print(f"  Sharpe         : {pick['sharpe']:.2f}")
    print(f"  MDD            : {pick['mdd']:.1f}%")
    print(f"  Bull 평균      : {pick['bull_avg']:+.1f}%")
    print(f"  Sideways 평균  : {pick['side_avg']:+.1f}%")
    print(f"  Bear 평균      : {pick['bear_avg']:+.1f}%  (최악: {pick['bear_min']:+.1f}%)")


if __name__ == "__main__":
    main()