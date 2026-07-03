"""
strategy_sweep_prod.py
======================
프로덕션 모델(cache_prod_full_2006_2026_scores.pkl) OOS(2013~2026) 구간에서
티어형 매수/매도 파라미터를 전수 탐색.

── 매수 티어 구조 ──────────────────────────────────────────────────────────────
  고정 임계점: 0.50 / 0.60 / 0.70 / 0.80 (10점 단위)
  buy_min     : 최저 매수 발동 임계 (0.50~0.80, 4값)
  buy_base_alloc: 최저 티어 목표 비중 (0.05~0.25, 5값)
  상위 티어마다 +5%씩 가산, 상한 0.30

  예) buy_min=0.50, buy_base_alloc=0.05
    → 0.50→5%, 0.60→10%, 0.70→15%, 0.80→20%  (현재 프로덕션)

── 매도 티어 구조 ──────────────────────────────────────────────────────────────
  sell_top     : 최고 매도 발동 임계 (0.25~0.50, 6값)
  sell_max_keep: 최고 티어 보유비중 (0.00~0.20, 5값)
  sell_top 이하를 5점 단위로 나눠 keep = 0% → sell_max_keep 까지 선형

  예) sell_top=0.40, sell_max_keep=0.15
    → 0.25→0%, 0.30→5%, 0.35→10%, 0.40→15%  (현재 프로덕션)

  sell_top ~ buy_min 사이(데드존): 아무 행동 없이 포지션 유지

그리드 (600 조합):
  buy_min      : [0.50, 0.60, 0.70, 0.80]        (4)
  buy_base_alloc: [0.05, 0.10, 0.15, 0.20, 0.25]  (5)
  sell_top     : [0.25, 0.30, 0.35, 0.40, 0.45, 0.50]  (6)
  sell_max_keep: [0.00, 0.05, 0.10, 0.15, 0.20]  (5)

사용:
  python -X utf8 strategy_sweep_prod.py
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

CACHE_PATH = ROOT / "cache_prod_full_2006_2026_scores.pkl"
BUY_FEE    = 0.00015
SELL_FEE   = 0.00195
INITIAL    = 1_000_000_000

OOS_FROM = pd.Timestamp("2013-01-01")
OOS_TO   = pd.Timestamp("2026-05-31")

BUY_MIN_LIST       = [0.50, 0.60, 0.70, 0.80]
BUY_BASE_ALLOC_LIST = [0.05, 0.10, 0.15, 0.20, 0.25]
SELL_TOP_LIST      = [0.25, 0.30, 0.35, 0.40, 0.45, 0.50]
SELL_MAX_KEEP_LIST  = [0.00, 0.05, 0.10, 0.15, 0.20]


# ══════════════════════════════════════════════════════════════════════════════
# 티어 생성
# ══════════════════════════════════════════════════════════════════════════════
def make_buy_tiers(buy_min, base_alloc):
    """
    0.80부터 buy_min까지 10점 단위 티어 구성.
    각 티어: alloc = base_alloc + (tier_index * 0.05), 상한 0.30
    반환: [(score_thresh, alloc), ...] — 높은 점수 먼저 (lookup용)
    """
    tiers = []
    thresholds = [t for t in [0.80, 0.70, 0.60, 0.50] if t >= buy_min - 1e-9]
    n = len(thresholds)
    for i, thr in enumerate(sorted(thresholds)):           # 낮은 쪽부터 번호 부여
        alloc = round(base_alloc + i * 0.05, 2)
        alloc = min(alloc, 0.30)
        tiers.append((thr, alloc))
    return sorted(tiers, reverse=True)  # 높은 threshold 먼저


def make_sell_tiers(sell_top, sell_max_keep):
    """
    sell_top 이하를 5점 단위로 나눠 keep 0% ~ sell_max_keep 선형.
    반환: [(score_thresh, keep_ratio), ...] — 낮은 threshold 먼저 (lookup용)
    """
    n = int(round(sell_max_keep / 0.05)) + 1   # 티어 개수
    tiers = []
    for i in range(n):
        thr  = round(sell_top - (n - 1 - i) * 0.05, 2)
        keep = round(i * 0.05, 2)
        if thr > 0:
            tiers.append((thr, keep))
    return sorted(tiers)   # 낮은 threshold 먼저


def buy_target(score, buy_tiers):
    """score → 매수 목표 비중. 매수 불필요이면 None."""
    for thr, alloc in buy_tiers:   # 높은 threshold부터
        if score >= thr:
            return alloc
    return None


def sell_target(score, sell_tiers):
    """score → 매도 후 보유 목표 비중. 매도 불필요이면 None."""
    for thr, keep in sell_tiers:   # 낮은 threshold부터
        if score < thr:
            return keep
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 포트폴리오 — 실제 비중(market_value/total) 기반 리밸런싱
# ══════════════════════════════════════════════════════════════════════════════
class Portfolio:
    def __init__(self):
        self.cash = float(INITIAL)
        self.pos  = {}      # code → {qty, avg_price, target_ratio}
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

    def buy(self, code, amount, price, target_ratio):
        qty  = int(amount / price)
        cost = qty * price * (1 + BUY_FEE)
        if qty == 0 or cost > self.cash: return
        self.cash -= cost
        if code in self.pos:
            p  = self.pos[code]
            nq = p["qty"] + qty
            p["avg_price"]   = (p["qty"] * p["avg_price"] + qty * price) / nq
            p["qty"]         = nq
            p["target_ratio"] = target_ratio
        else:
            self.pos[code] = {"qty": qty, "avg_price": price, "target_ratio": target_ratio}

    def trim_to(self, code, target_ratio, price, tot):
        """실제 비중이 target_ratio 초과 시 매도로 조정."""
        p = self.pos.get(code)
        if p is None: return
        cur_mv = p["qty"] * price
        tgt_mv = target_ratio * tot
        if cur_mv > tgt_mv + price:
            sell_q = int((cur_mv - tgt_mv) / price)
            if sell_q > 0:
                sell_q = min(sell_q, p["qty"])
                self.cash += sell_q * price * (1 - SELL_FEE)
                p["qty"] -= sell_q
                if p["qty"] == 0:
                    del self.pos[code]
                else:
                    p["target_ratio"] = target_ratio

    def sell_all(self, code, price):
        p = self.pos.pop(code, None)
        if p:
            self.cash += p["qty"] * price * (1 - SELL_FEE)


# ══════════════════════════════════════════════════════════════════════════════
# 단일 조합 시뮬레이션 — 실제 비중 기반
# ══════════════════════════════════════════════════════════════════════════════
def simulate(scores_by_date, trading_days, prices_cache, buy_tiers, sell_tiers):
    pf    = Portfolio()
    daily = []

    for date in trading_days:
        px = prices_cache.get(date, {})
        sc = scores_by_date.get(date, {})
        pf.upd(px)
        tot = pf.total(px)

        # ── 1. 매도 / 트림 ─────────────────────────────────────────────────────
        for code in list(pf.pos.keys()):
            price = px.get(code)
            if not price or price <= 0: continue
            score = sc.get(code)

            p = pf.pos.get(code)
            if p is None: continue

            if score is not None:
                s_tgt = sell_target(score, sell_tiers)
                b_tgt = buy_target(score, buy_tiers)
                if s_tgt is not None:
                    p["target_ratio"] = s_tgt       # 매도존: 목표 갱신
                elif b_tgt is not None:
                    p["target_ratio"] = b_tgt       # 매수존: 목표 갱신
                # 데드존: target_ratio 유지

            target = p.get("target_ratio", 0.0)
            if target == 0.0:
                pf.sell_all(code, price)
            else:
                pf.trim_to(code, target, price, tot)   # 실제 비중 > 목표 시 트림

        # ── 2. 매수 ────────────────────────────────────────────────────────────
        tot = pf.total(px)
        candidates = []
        for code, score in sc.items():
            price = px.get(code, 0)
            if price <= 0: continue
            tgt = buy_target(score, buy_tiers)
            if tgt is None: continue
            cur_ratio = pf.mv(code, px) / tot if tot > 0 else 0.0
            if tgt > cur_ratio + 0.01:
                candidates.append((code, score, tgt, tgt - cur_ratio))

        for code, score, tgt, incr in sorted(candidates, key=lambda x: x[1], reverse=True):
            price = px.get(code, 0)
            if price <= 0: continue
            buy_amt = incr * tot
            if pf.cash >= buy_amt:
                pf.buy(code, buy_amt, price, target_ratio=tgt)

        daily.append((date, pf.total(px)))

    return daily


# ══════════════════════════════════════════════════════════════════════════════
# 지표
# ══════════════════════════════════════════════════════════════════════════════
def metrics(daily):
    ta  = pd.Series([v for _, v in daily])
    ret = (ta.iloc[-1] / ta.iloc[0] - 1) * 100
    dr  = ta.pct_change().dropna()
    sh  = (dr.mean() / dr.std() * 252 ** 0.5) if dr.std() > 0 else 0.0
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
    combos = list(product(BUY_MIN_LIST, BUY_BASE_ALLOC_LIST,
                          SELL_TOP_LIST, SELL_MAX_KEEP_LIST))

    print("\n" + "=" * 72)
    print("📊  프로덕션 모델 매수/매도 티어 파라미터 탐색")
    print(f"    OOS: 2013~2026  |  조합: {len(combos)}개")
    print("=" * 72)

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
    print(f"   {len(score_df):,}건  |  {score_df['date'].min().date()} ~ {score_df['date'].max().date()}")

    print("\n[2] 가격 데이터 로딩...")
    price_pivot  = sbm.load_prices(OOS_FROM, OOS_TO, filter_bad=True)
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

    def cls(r): return "Bull" if r >= 10 else ("Bear" if r < -5 else "Side")

    oos_yrs  = [y for y in sorted(kospi_annual) if 2013 <= y <= 2026]
    bull_yrs = [y for y in oos_yrs if cls(kospi_annual[y]) == "Bull"]
    side_yrs = [y for y in oos_yrs if cls(kospi_annual[y]) == "Side"]
    bear_yrs = [y for y in oos_yrs if cls(kospi_annual[y]) == "Bear"]

    for yr in oos_yrs:
        r = kospi_annual[yr]
        print(f"    {yr}  {r:>+6.1f}%  [{cls(r)}]")

    # ── 현재 프로덕션 기본값 표시 ─────────────────────────────────────────────
    def_bt = make_buy_tiers(0.50, 0.05)
    def_st = make_sell_tiers(0.40, 0.15)
    print(f"\n  현재 프로덕션 기본값:")
    print(f"    매수 티어: {def_bt}")
    print(f"    매도 티어: {def_st}")

    # ── 탐색 ─────────────────────────────────────────────────────────────────
    print(f"\n[4] 탐색 시작: {len(combos)}개 조합")
    rows, t0 = [], time.time()

    for i, (bm, ba, st, sk) in enumerate(combos):
        buy_t  = make_buy_tiers(bm, ba)
        sell_t = make_sell_tiers(st, sk)

        daily = simulate(scores_by_date, trading_days, prices_cache, buy_t, sell_t)
        if not daily:
            continue

        ret, sh, mdd = metrics(daily)
        yr_ret = yearly_rets(daily)

        ann_vals  = [yr_ret.get(y, 0) for y in oos_yrs]
        pos_ratio = round(sum(1 for v in ann_vals if v > 0) / len(ann_vals) * 100, 0)
        ann_std   = round(float(np.std(ann_vals)), 1)

        bull_r = [yr_ret.get(y, 0) for y in bull_yrs]
        side_r = [yr_ret.get(y, 0) for y in side_yrs]
        bear_r = [yr_ret.get(y, 0) for y in bear_yrs]

        row = {
            "buy_min": bm, "buy_base": ba, "sell_top": st, "sell_keep": sk,
            "buy_tiers": str(buy_t), "sell_tiers": str(sell_t),
            "ret": ret, "sharpe": sh, "mdd": mdd,
            "pos_ratio": pos_ratio, "ann_std": ann_std,
            "bull_avg": round(np.mean(bull_r), 1) if bull_r else 0,
            "side_avg": round(np.mean(side_r), 1) if side_r else 0,
            "bear_avg": round(np.mean(bear_r), 1) if bear_r else 0,
            "bear_min": round(min(bear_r), 1) if bear_r else 0,
        }
        for yr in oos_yrs:
            row[yr] = yr_ret.get(yr, 0)
        rows.append(row)

        el   = time.time() - t0
        done = i + 1
        eta  = (len(combos) - done) / (done / el) if el > 0 else 0
        if done % 50 == 0 or done == len(combos):
            print(f"   [{done:3d}/{len(combos)}]  {el/60:.1f}분 경과  "
                  f"잔여 {eta/60:.1f}분")

    df = pd.DataFrame(rows)
    df.to_csv(ROOT / "strategy_sweep_prod_results.csv", index=False, encoding="utf-8-sig")
    print(f"\n💾 저장: strategy_sweep_prod_results.csv")

    valid = df.dropna(subset=["sharpe"])

    # ══════════════════════════════════════════════════════════════════════════
    # Sharpe × buy_min 히트맵 (sell_top/sell_keep 평균으로 집약)
    # ══════════════════════════════════════════════════════════════════════════
    print(f"\n\n{'='*90}")
    print(" 📊 Sharpe 히트맵  (행=buy_min, 열=buy_base_alloc)  — sell 파라미터 평균")
    print(f"{'='*90}")
    col_hdr = "buy\\base"
    hdr = f"  {col_hdr:>8}" + "".join(f"  ba={ba:.2f}" for ba in BUY_BASE_ALLOC_LIST)
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    for bm in BUY_MIN_LIST:
        row_str = f"  bm={bm:.2f} "
        for ba in BUY_BASE_ALLOC_LIST:
            sub = valid[(valid["buy_min"] == bm) & (valid["buy_base"] == ba)]
            sh  = sub["sharpe"].mean() if not sub.empty else float("nan")
            row_str += f"   {sh:>5.2f}" if not np.isnan(sh) else "    ----"
        print(row_str)

    print(f"\n\n{'='*90}")
    print(" 📊 Sharpe 히트맵  (행=sell_top, 열=sell_max_keep)  — buy 파라미터 평균")
    print(f"{'='*90}")
    col_hdr2 = "st\\keep"
    hdr2 = f"  {col_hdr2:>8}" + "".join(f"  sk={sk:.2f}" for sk in SELL_MAX_KEEP_LIST)
    print(hdr2)
    print("  " + "─" * (len(hdr2) - 2))
    for st in SELL_TOP_LIST:
        row_str = f"  st={st:.2f} "
        for sk in SELL_MAX_KEEP_LIST:
            sub = valid[(valid["sell_top"] == st) & (valid["sell_keep"] == sk)]
            sh  = sub["sharpe"].mean() if not sub.empty else float("nan")
            row_str += f"   {sh:>5.2f}" if not np.isnan(sh) else "    ----"
        print(row_str)

    # ══════════════════════════════════════════════════════════════════════════
    # Sharpe TOP-10 연도별
    # ══════════════════════════════════════════════════════════════════════════
    yr_cols = "".join(f"  {y}" for y in oos_yrs)
    print(f"\n\n{'='*130}")
    print(" 🏆 Sharpe TOP-10 연도별 수익률")
    print(f"{'='*130}")
    print(f"  {'bm':>4} {'ba':>5} {'st':>5} {'sk':>5}  "
          f"{'ret':>7} {'sh':>6} {'mdd':>6}  "
          f"{'pos%':>5} {'std':>5}  {yr_cols}")
    print("  " + "─" * 125)
    for _, r in valid.nlargest(10, "sharpe").iterrows():
        yr_str = "".join(f" {r.get(y, 0):>+5.0f}" for y in oos_yrs)
        print(f"  {r['buy_min']:>4.2f} {r['buy_base']:>5.2f} {r['sell_top']:>5.2f} {r['sell_keep']:>5.2f}  "
              f"{r['ret']:>+6.1f}% {r['sharpe']:>6.2f} {r['mdd']:>+6.1f}%  "
              f"{r['pos_ratio']:>4.0f}% {r['ann_std']:>5.1f}  {yr_str}")

    # ══════════════════════════════════════════════════════════════════════════
    # 안정 조합 (양수 연도 ≥70% + 하락장 평균 > -10%)
    # ══════════════════════════════════════════════════════════════════════════
    stable = valid[(valid["pos_ratio"] >= 70) & (valid["bear_avg"] >= -10)]
    print(f"\n\n{'='*130}")
    print(f" 🛡️  안정 조합 (양수연도≥70% & 하락장평균≥-10%)  TOP-10 by Sharpe  ({len(stable)}개)")
    print(f"{'='*130}")
    print(f"  {'bm':>4} {'ba':>5} {'st':>5} {'sk':>5}  "
          f"{'ret':>7} {'sh':>6} {'mdd':>6}  "
          f"{'pos%':>5} {'std':>5}  "
          f"{'Bull평균':>8} {'Side평균':>8} {'Bear평균':>8} {'Bear최악':>8}  {yr_cols}")
    print("  " + "─" * 125)
    for _, r in stable.nlargest(10, "sharpe").iterrows():
        yr_str = "".join(f" {r.get(y, 0):>+5.0f}" for y in oos_yrs)
        print(f"  {r['buy_min']:>4.2f} {r['buy_base']:>5.2f} {r['sell_top']:>5.2f} {r['sell_keep']:>5.2f}  "
              f"{r['ret']:>+6.1f}% {r['sharpe']:>6.2f} {r['mdd']:>+6.1f}%  "
              f"{r['pos_ratio']:>4.0f}% {r['ann_std']:>5.1f}  "
              f"{r['bull_avg']:>+7.1f}% {r['side_avg']:>+7.1f}% {r['bear_avg']:>+7.1f}% {r['bear_min']:>+7.1f}%  "
              f"{yr_str}")

    # ══════════════════════════════════════════════════════════════════════════
    # 현재 프로덕션 설정 위치
    # ══════════════════════════════════════════════════════════════════════════
    prod_row = valid[(valid["buy_min"] == 0.50) & (valid["buy_base"] == 0.05) &
                     (valid["sell_top"] == 0.40) & (valid["sell_keep"] == 0.15)]
    if not prod_row.empty:
        r = prod_row.iloc[0]
        yr_str = "".join(f" {r.get(y, 0):>+5.0f}" for y in oos_yrs)
        print(f"\n\n{'='*72}")
        print(" 📌 현재 프로덕션 설정 (buy_min=0.50, base=0.05, sell_top=0.40, keep=0.15)")
        print(f"{'='*72}")
        print(f"  수익률: {r['ret']:+.1f}%  Sharpe: {r['sharpe']:.2f}  MDD: {r['mdd']:.1f}%")
        print(f"  양수연도: {r['pos_ratio']:.0f}%  표준편차: {r['ann_std']:.1f}%")
        print(f"  Bull: {r['bull_avg']:+.1f}%  Side: {r['side_avg']:+.1f}%  Bear평균: {r['bear_avg']:+.1f}%  Bear최악: {r['bear_min']:+.1f}%")
        print(f"  연도별: {yr_str}")

    # ══════════════════════════════════════════════════════════════════════════
    # 최적 추천
    # ══════════════════════════════════════════════════════════════════════════
    candidate = stable if not stable.empty else valid
    pick = candidate.nlargest(1, "sharpe").iloc[0]
    bt_str = make_buy_tiers(pick["buy_min"], pick["buy_base"])
    st_str = make_sell_tiers(pick["sell_top"], pick["sell_keep"])

    print(f"\n\n{'='*72}")
    print(" ★ 최적 추천  (양수비율≥70% & 하락장≥-10% 중 최고 Sharpe)")
    print(f"{'='*72}")
    print(f"  매수 티어: {bt_str}")
    print(f"  매도 티어: {st_str}")
    print(f"\n  전체 수익률: {pick['ret']:+.1f}%")
    print(f"  Sharpe     : {pick['sharpe']:.2f}")
    print(f"  MDD        : {pick['mdd']:.1f}%")
    print(f"  양수 연도  : {pick['pos_ratio']:.0f}%")
    print(f"  연수익 표준편차: {pick['ann_std']:.1f}%")
    print(f"  Bull 평균  : {pick['bull_avg']:+.1f}%")
    print(f"  Sideways   : {pick['side_avg']:+.1f}%")
    print(f"  Bear 평균  : {pick['bear_avg']:+.1f}%  (최악: {pick['bear_min']:+.1f}%)")
    print()
    for yr in oos_yrs:
        rv = pick.get(yr, 0)
        print(f"    {yr}: {rv:>+6.1f}%  [{cls(kospi_annual[yr])}  KOSPI {kospi_annual[yr]:+.1f}%]")


if __name__ == "__main__":
    main()