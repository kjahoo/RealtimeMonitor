"""
strategy_sweep_threshold.py
===========================
Exp-01 fold_01 OOS(2013~2026) 스코어로
매수/매도 임계값만 탐색 (alloc=10%, MA필터 없음, 손절 없음).

그리드 (66 조합):
  buy_thresh  : 0.55 ~ 0.80  (5점 단위, 6값)
  sell_thresh : 0.00 ~ 0.50  (5점 단위, 11값)
  alloc       : 0.10  (고정)
  regime_ma   : 0     (없음, 고정)
  stop_loss   : 0.0   (없음, 고정)

출력:
  1) buy × sell Sharpe 히트맵 (6×11 격자)
  2) buy × sell 연수익 히트맵
  3) 연도별 수익 TOP-10 (Sharpe 기준)
  4) 안정성 지표 (양수 연도 비율, 연수익 표준편차)

사용:
  python -X utf8 strategy_sweep_threshold.py
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

BUY_LIST  = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
SELL_LIST = [0.00, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]
ALLOC     = 0.10


# ══════════════════════════════════════════════════════════════════════════════
# 포트폴리오
# ══════════════════════════════════════════════════════════════════════════════
class Portfolio:
    def __init__(self):
        self.cash = float(INITIAL)
        self.pos  = {}
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
# 단일 조합 시뮬레이션
# ══════════════════════════════════════════════════════════════════════════════
def simulate(scores_by_date, trading_days, prices_cache, buy_thresh, sell_thresh):
    pf    = Portfolio()
    daily = []

    for date in trading_days:
        px = prices_cache.get(date, {})
        sc = scores_by_date.get(date, {})
        pf.upd(px)

        # 매도: score < sell_thresh (sell_thresh=0.00 → 사실상 매도 없음)
        if sell_thresh > 0:
            for code in list(pf.pos.keys()):
                price = px.get(code)
                if not price or price <= 0: continue
                if code in pf.pos:
                    score = sc.get(code)
                    if score is not None and score < sell_thresh:
                        pf.sell_all(code, price)

        # 매수: score >= buy_thresh
        tot = pf.total(px)
        for code, score in sorted(sc.items(), key=lambda x: x[1], reverse=True):
            if score < buy_thresh: break
            price = px.get(code, 0)
            if price <= 0: continue
            cur = pf.mv(code, px) / tot if tot > 0 else 0.0
            if ALLOC > cur + 0.01:
                pf.buy(code, (ALLOC - cur) * tot, price)

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
    print("📊  Exp-01 매수/매도 임계값 탐색  (alloc=10%, MA없음, 손절없음)")
    print(f"    OOS: 2013~2026  |  조합: {len(BUY_LIST)*len(SELL_LIST)}개")
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
    print(f"   {len(score_df):,}건  |  날짜: {score_df['date'].min().date()} ~ {score_df['date'].max().date()}")

    print("\n[2] 가격 데이터 로딩...")
    price_pivot = sbm.load_prices(OOS_FROM, OOS_TO, filter_bad=True)
    mask         = (price_pivot.index >= OOS_FROM) & (price_pivot.index <= OOS_TO)
    trading_days = list(price_pivot.loc[mask].index)
    prices_cache = {d: price_pivot.loc[d].dropna().to_dict() for d in price_pivot.index}
    print(f"   종목: {price_pivot.shape[1]:,}  거래일: {len(trading_days)}")

    print("\n[3] KOSPI 연간 수익률 확인...")
    pf_files = sbm._list_prep_files()
    ref = sbm._read_prep(pf_files[0])
    ref["date"] = pd.to_datetime(ref["date"])
    ref = ref.set_index("date").sort_index()
    ref["kidx"] = (1 + ref["kospi_change"]).cumprod()

    kospi_annual = {}
    for yr, g in ref.groupby(ref.index.year):
        kospi_annual[yr] = round((g["kidx"].iloc[-1] / g["kidx"].iloc[0] - 1) * 100, 1)

    def cls(r):
        return "Bull" if r >= 10 else ("Bear" if r < -5 else "Side")

    oos_yrs  = [y for y in sorted(kospi_annual) if 2013 <= y <= 2026]
    bull_yrs = [y for y in oos_yrs if cls(kospi_annual[y]) == "Bull"]
    side_yrs = [y for y in oos_yrs if cls(kospi_annual[y]) == "Side"]
    bear_yrs = [y for y in oos_yrs if cls(kospi_annual[y]) == "Bear"]

    for yr in oos_yrs:
        r = kospi_annual[yr]
        print(f"    {yr}  {r:>+6.1f}%  [{cls(r)}]")

    # ── 탐색 ─────────────────────────────────────────────────────────────────
    combos = list(product(BUY_LIST, SELL_LIST))
    print(f"\n[4] 탐색 시작: {len(combos)}개 조합")

    rows, t0 = [], time.time()
    for i, (bt, st) in enumerate(combos):
        # sell_thresh가 buy_thresh 이상이면 의미없음 (매도선이 매수선 이상)
        if st >= bt:
            rows.append({
                "buy": bt, "sell": st,
                "ret": float("nan"), "sharpe": float("nan"), "mdd": float("nan"),
                "pos_ratio": float("nan"), "ann_std": float("nan"),
                "bull_avg": float("nan"), "side_avg": float("nan"), "bear_avg": float("nan"),
                **{yr: float("nan") for yr in oos_yrs}
            })
            continue

        daily = simulate(scores_by_date, trading_days, prices_cache, bt, st)
        if not daily:
            continue

        ret, sh, mdd = metrics(daily)
        yr_ret = yearly_rets(daily)

        ann_vals   = [yr_ret.get(y, 0) for y in oos_yrs]
        pos_ratio  = round(sum(1 for v in ann_vals if v > 0) / len(ann_vals) * 100, 0)
        ann_std    = round(float(np.std(ann_vals)), 1)

        bull_r = [yr_ret.get(y, 0) for y in bull_yrs]
        side_r = [yr_ret.get(y, 0) for y in side_yrs]
        bear_r = [yr_ret.get(y, 0) for y in bear_yrs]

        row = {
            "buy": bt, "sell": st,
            "ret": ret, "sharpe": sh, "mdd": mdd,
            "pos_ratio": pos_ratio, "ann_std": ann_std,
            "bull_avg": round(np.mean(bull_r), 1) if bull_r else 0,
            "side_avg": round(np.mean(side_r), 1) if side_r else 0,
            "bear_avg": round(np.mean(bear_r), 1) if bear_r else 0,
        }
        for yr in oos_yrs:
            row[yr] = yr_ret.get(yr, 0)
        rows.append(row)

        el = time.time() - t0
        done = i + 1
        eta  = (len(combos) - done) / (done / el) if el > 0 else 0
        print(f"   [{done:2d}/{len(combos)}]  buy={bt:.2f} sell={st:.2f}  "
              f"ret={ret:+.1f}% sh={sh:.2f} mdd={mdd:.1f}%  "
              f"({el/60:.1f}분 경과, 잔여 {eta/60:.1f}분)")

    df = pd.DataFrame(rows)
    df.to_csv(ROOT / "strategy_sweep_threshold_results.csv", index=False, encoding="utf-8-sig")
    print(f"\n💾 저장: strategy_sweep_threshold_results.csv")

    valid = df.dropna(subset=["sharpe"])

    # ══════════════════════════════════════════════════════════════════════════
    # 히트맵 출력: Sharpe
    # ══════════════════════════════════════════════════════════════════════════
    print(f"\n\n{'='*90}")
    print(" 📊 Sharpe 히트맵  (행=매수임계, 열=매도임계)")
    print(f"{'='*90}")
    sell_cols = [s for s in SELL_LIST if s < max(BUY_LIST)]
    col_hdr = "buy\\sell"
    header = f"{col_hdr:>8}" + "".join(f"  s={s:.2f}" for s in sell_cols)
    print(header)
    print("  " + "─" * (len(header) - 2))
    for bt in BUY_LIST:
        row_str = f"  b={bt:.2f} "
        for st in sell_cols:
            r = df[(df["buy"] == bt) & (df["sell"] == st)]
            if r.empty or pd.isna(r.iloc[0]["sharpe"]):
                row_str += f"    ----"
            else:
                sh = r.iloc[0]["sharpe"]
                row_str += f"   {sh:>5.2f}"
        print(row_str)

    # ══════════════════════════════════════════════════════════════════════════
    # 히트맵 출력: 전체 수익률
    # ══════════════════════════════════════════════════════════════════════════
    print(f"\n\n{'='*90}")
    print(" 📈 전체 수익률 히트맵 (%)")
    print(f"{'='*90}")
    print(header)
    print("  " + "─" * (len(header) - 2))
    for bt in BUY_LIST:
        row_str = f"  b={bt:.2f} "
        for st in sell_cols:
            r = df[(df["buy"] == bt) & (df["sell"] == st)]
            if r.empty or pd.isna(r.iloc[0]["ret"]):
                row_str += f"    ----"
            else:
                ret = r.iloc[0]["ret"]
                row_str += f"  {ret:>+6.0f}"
        print(row_str)

    # ══════════════════════════════════════════════════════════════════════════
    # 히트맵 출력: MDD
    # ══════════════════════════════════════════════════════════════════════════
    print(f"\n\n{'='*90}")
    print(" 📉 MDD 히트맵 (%)")
    print(f"{'='*90}")
    print(header)
    print("  " + "─" * (len(header) - 2))
    for bt in BUY_LIST:
        row_str = f"  b={bt:.2f} "
        for st in sell_cols:
            r = df[(df["buy"] == bt) & (df["sell"] == st)]
            if r.empty or pd.isna(r.iloc[0]["mdd"]):
                row_str += f"    ----"
            else:
                mdd = r.iloc[0]["mdd"]
                row_str += f"  {mdd:>+6.0f}"
        print(row_str)

    # ══════════════════════════════════════════════════════════════════════════
    # 연도별 수익 테이블 (Sharpe TOP-10)
    # ══════════════════════════════════════════════════════════════════════════
    print(f"\n\n{'='*120}")
    print(" 🏆 Sharpe TOP-10 조합  연도별 수익률")
    print(f"{'='*120}")
    yr_cols = "".join(f"  {y}" for y in oos_yrs)
    print(f"  {'buy':>5} {'sell':>5}  {'ret':>7} {'sh':>6} {'mdd':>6}  "
          f"{'pos%':>5} {'std':>5}  {yr_cols}")
    print("  " + "─" * 115)
    for _, r in valid.nlargest(10, "sharpe").iterrows():
        yr_str = "".join(f" {r.get(y, 0):>+5.0f}" for y in oos_yrs)
        print(f"  {r['buy']:>5.2f} {r['sell']:>5.2f}  "
              f"{r['ret']:>+6.1f}% {r['sharpe']:>6.2f} {r['mdd']:>+6.1f}%  "
              f"{r['pos_ratio']:>4.0f}% {r['ann_std']:>5.1f}  {yr_str}")

    # ══════════════════════════════════════════════════════════════════════════
    # 안정성 기준 (양수연도 비율 >= 70% + Sharpe 정렬)
    # ══════════════════════════════════════════════════════════════════════════
    stable = valid[valid["pos_ratio"] >= 70].nlargest(10, "sharpe")
    print(f"\n\n{'='*120}")
    print(f" 🛡️  안정 조합 (양수 연도 비율 ≥70%)  TOP-10 by Sharpe  ({len(valid[valid['pos_ratio']>=70])}개)")
    print(f"{'='*120}")
    print(f"  {'buy':>5} {'sell':>5}  {'ret':>7} {'sh':>6} {'mdd':>6}  "
          f"{'pos%':>5} {'std':>5}  "
          f"{'Bull평균':>8} {'Side평균':>8} {'Bear평균':>8}  {yr_cols}")
    print("  " + "─" * 115)
    for _, r in stable.iterrows():
        yr_str = "".join(f" {r.get(y, 0):>+5.0f}" for y in oos_yrs)
        print(f"  {r['buy']:>5.2f} {r['sell']:>5.2f}  "
              f"{r['ret']:>+6.1f}% {r['sharpe']:>6.2f} {r['mdd']:>+6.1f}%  "
              f"{r['pos_ratio']:>4.0f}% {r['ann_std']:>5.1f}  "
              f"{r['bull_avg']:>+7.1f}% {r['side_avg']:>+7.1f}% {r['bear_avg']:>+7.1f}%  {yr_str}")

    # ══════════════════════════════════════════════════════════════════════════
    # 최종 추천
    # ══════════════════════════════════════════════════════════════════════════
    candidate = stable if not stable.empty else valid
    pick = candidate.nlargest(1, "sharpe").iloc[0]
    print(f"\n\n{'='*72}")
    print(" ★ 최적 추천  (양수비율≥70% 중 최고 Sharpe)")
    print(f"{'='*72}")
    print(f"  매수 임계값  : {pick['buy']:.2f}  (score ≥ {pick['buy']*100:.0f}점)")
    print(f"  매도 임계값  : {pick['sell']:.2f}  (score <  {pick['sell']*100:.0f}점)")
    print(f"  종목당 비중  : {ALLOC*100:.0f}%  (고정)")
    print(f"\n  전체 수익률  : {pick['ret']:+.1f}%")
    print(f"  Sharpe       : {pick['sharpe']:.2f}")
    print(f"  MDD          : {pick['mdd']:.1f}%")
    print(f"  양수 연도    : {pick['pos_ratio']:.0f}%")
    print(f"  연수익 표준편차: {pick['ann_std']:.1f}%")
    print(f"  Bull 평균    : {pick['bull_avg']:+.1f}%")
    print(f"  Sideways 평균: {pick['side_avg']:+.1f}%")
    print(f"  Bear 평균    : {pick['bear_avg']:+.1f}%")
    print()
    for yr in oos_yrs:
        r = pick.get(yr, 0)
        marker = f"  [{cls(kospi_annual[yr])}  KOSPI {kospi_annual[yr]:+.1f}%]"
        print(f"    {yr}: {r:>+6.1f}%{marker}")


if __name__ == "__main__":
    main()