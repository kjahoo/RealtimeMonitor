"""
compare_strategies_models.py
============================
2가지 모델 × 2가지 전략 = 4가지 조합 비교

모델:
  A) 프로덕션 모델  (cache_prod_full_2006_2026_scores.pkl)
  B) Expanding fold_01  (walk_forward/fold_01/holdout_20130101_20251231_scores.pkl)

전략:
  1) 단순 threshold : buy >= 0.55 → 10% 비중, score < 0.50 → 전량 매도
  2) Tiered 전략    : BUY_TIERS / SELL_TIERS (walk_forward.py 와 동일)

평가 기간: 2020-01-01 ~ 2025-12-31

사용:
  python -X utf8 compare_strategies_models.py
  python -X utf8 compare_strategies_models.py --start 2020-01-01 --end 2025-12-31
"""

import argparse, warnings, time
import numpy as np
import pandas as pd
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT     = Path(r"C:\Projects\RealtimeMonitor")
DATA_DIR = ROOT / "Data" / "Stock"

# ── 경로 ──────────────────────────────────────────────────────────────────────
PROD_CACHE   = ROOT / "cache_prod_full_2006_2026_scores.pkl"
FOLD01_CACHE = ROOT / "walk_forward" / "fold_01" / "holdout_20130101_20251231_scores.pkl"
PREP_DIR     = ROOT / "Data" / "_prep_wf_v3"

# ── 거래비용 ──────────────────────────────────────────────────────────────────
INITIAL_CAPITAL = 1_000_000_000
BUY_FEE   = 0.00015
SELL_FEE  = 0.00195

# ── 전략 1: 단순 threshold ────────────────────────────────────────────────────
SIMPLE_BUY_THRESH  = 0.55
SIMPLE_SELL_THRESH = 0.50
SIMPLE_ALLOC       = 0.10

# ── 전략 2: Tiered ───────────────────────────────────────────────────────────
BUY_TIERS  = [(0.8, 0.20), (0.7, 0.15), (0.6, 0.10), (0.5, 0.05)]
SELL_TIERS = [(0.25, 0.00), (0.30, 0.05), (0.35, 0.10), (0.40, 0.15)]


def _buy_target_tiered(score):
    for min_s, ratio in BUY_TIERS:
        if score >= min_s:
            return ratio
    return None


def _sell_target_tiered(score):
    for max_s, ratio in SELL_TIERS:
        if score < max_s:
            return ratio
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 포트폴리오
# ══════════════════════════════════════════════════════════════════════════════

class Portfolio:
    def __init__(self):
        self.cash      = float(INITIAL_CAPITAL)
        self.positions = {}   # code → {qty, avg_price, alloc_ratio}
        self._last_px  = {}

    def _px(self, code, prices):
        p = prices.get(code)
        if p and p > 0:
            self._last_px[code] = p
            return p
        return self._last_px.get(code, self.positions[code]["avg_price"])

    def total(self, prices):
        mv = sum(pos["qty"] * self._px(code, prices)
                 for code, pos in self.positions.items())
        return self.cash + mv

    def mv(self, code, prices):
        pos = self.positions.get(code)
        return pos["qty"] * self._px(code, prices) if pos else 0.0

    def buy(self, code, amount, price, alloc_ratio):
        qty  = int(amount / price)
        cost = qty * price * (1 + BUY_FEE)
        if qty == 0 or cost > self.cash:
            return
        self.cash -= cost
        if code in self.positions:
            pos = self.positions[code]
            nq  = pos["qty"] + qty
            pos["avg_price"]   = (pos["qty"] * pos["avg_price"] + qty * price) / nq
            pos["qty"]         = nq
            pos["alloc_ratio"] = alloc_ratio
        else:
            self.positions[code] = {"qty": qty, "avg_price": price, "alloc_ratio": alloc_ratio}

    def sell_qty(self, code, qty, price, alloc_ratio=None):
        pos = self.positions.get(code)
        if not pos:
            return
        qty = min(qty, pos["qty"])
        self.cash    += qty * price * (1 - SELL_FEE)
        pos["qty"]   -= qty
        if pos["qty"] == 0:
            del self.positions[code]
        elif alloc_ratio is not None:
            pos["alloc_ratio"] = alloc_ratio

    def sell_all(self, code, price):
        if code in self.positions:
            self.sell_qty(code, self.positions[code]["qty"], price)


# ══════════════════════════════════════════════════════════════════════════════
# 시뮬레이션
# ══════════════════════════════════════════════════════════════════════════════

def run_simple(score_df, price_pivot, ts, te):
    """단순 threshold 전략."""
    by_date = (score_df.groupby("date")
               .apply(lambda g: dict(zip(g["code"], g["score"])))
               .to_dict())
    days = price_pivot.loc[ts:te].index.tolist()
    pf   = Portfolio()
    hist = []

    for date in days:
        prices = price_pivot.loc[date].dropna().to_dict()
        scores = by_date.get(date, {})
        tot    = pf.total(prices)

        # 매도 먼저
        for code in list(pf.positions):
            sc = scores.get(code)
            px = prices.get(code)
            if sc is None or not px:
                continue
            if sc < SIMPLE_SELL_THRESH:
                pf.sell_all(code, px)

        # 매수
        tot = pf.total(prices)
        cands = []
        for code, sc in scores.items():
            px = prices.get(code)
            if not px or sc < SIMPLE_BUY_THRESH:
                continue
            cur = pf.mv(code, prices) / tot if tot > 0 else 0
            if SIMPLE_ALLOC > cur + 0.01:
                cands.append((code, sc, SIMPLE_ALLOC - cur))

        for code, sc, incr in sorted(cands, key=lambda x: x[1], reverse=True):
            pf.buy(code, incr * tot, prices[code], SIMPLE_ALLOC)

        hist.append({"date": date, "total": pf.total(prices), "n": len(pf.positions)})

    return pd.DataFrame(hist)


def run_tiered(score_df, price_pivot, ts, te):
    """Tiered buy/sell 전략."""
    by_date = (score_df.groupby("date")
               .apply(lambda g: dict(zip(g["code"], g["score"])))
               .to_dict())
    days = price_pivot.loc[ts:te].index.tolist()
    pf   = Portfolio()
    hist = []

    for date in days:
        prices = price_pivot.loc[date].dropna().to_dict()
        scores = by_date.get(date, {})
        tot    = pf.total(prices)

        # 매도 먼저
        for code in list(pf.positions):
            sc = scores.get(code)
            px = prices.get(code)
            if sc is None or not px:
                continue
            sell_tgt = _sell_target_tiered(sc)
            if sell_tgt is None:
                continue
            if sell_tgt == 0.0:
                pf.sell_all(code, px)
            else:
                cur_mv = pf.mv(code, prices)
                tgt_mv = sell_tgt * tot
                if cur_mv > tgt_mv:
                    sq = int((cur_mv - tgt_mv) / px)
                    if sq > 0:
                        pf.sell_qty(code, sq, px, sell_tgt)

        # 매수
        tot = pf.total(prices)
        cands = []
        for code, sc in scores.items():
            px = prices.get(code)
            if not px:
                continue
            buy_tgt = _buy_target_tiered(sc)
            if buy_tgt is None:
                continue
            pos      = pf.positions.get(code)
            cur_alloc = pos["alloc_ratio"] if pos else 0.0
            if buy_tgt > cur_alloc:
                cands.append((code, sc, buy_tgt, buy_tgt - cur_alloc))

        for code, sc, tgt, incr in sorted(cands, key=lambda x: x[1], reverse=True):
            pf.buy(code, incr * tot, prices[code], tgt)

        hist.append({"date": date, "total": pf.total(prices), "n": len(pf.positions)})

    return pd.DataFrame(hist)


# ══════════════════════════════════════════════════════════════════════════════
# 성과 분석
# ══════════════════════════════════════════════════════════════════════════════

def stats(hist_df):
    ta      = hist_df.set_index("date")["total"]
    ret     = (ta.iloc[-1] / ta.iloc[0] - 1) * 100
    dr      = ta.pct_change().dropna()
    sharpe  = dr.mean() / dr.std() * np.sqrt(252) if dr.std() > 0 else 0
    mdd     = ((ta - ta.cummax()) / ta.cummax()).min() * 100
    wr      = (dr > 0).sum() / len(dr) * 100
    avg_pos = hist_df["n"].mean()
    return {"수익률(%)": round(ret, 2), "Sharpe": round(sharpe, 2),
            "MDD(%)": round(mdd, 2), "일승률(%)": round(wr, 1),
            "평균보유종목": round(avg_pos, 1)}


def yearly(hist_df):
    ta = hist_df.set_index("date")["total"]
    rows = {}
    for yr, grp in ta.groupby(ta.index.year):
        rows[yr] = round((grp.iloc[-1] / grp.iloc[0] - 1) * 100, 2)
    return rows


def benchmark(ts, te):
    files = sorted(PREP_DIR.glob("A*.feather"))
    if not files:
        return {}
    ref = pd.read_feather(str(files[0]))
    ref["date"] = pd.to_datetime(ref["date"])
    ref = ref.set_index("date").sort_index()
    period = ref.loc[ts:te]
    return {
        "KOSPI":  round(((1 + period["kospi_change"]).prod()  - 1) * 100, 2),
        "KOSDAQ": round(((1 + period["kosdaq_change"]).prod() - 1) * 100, 2),
    }


def benchmark_yearly(ts, te):
    files = sorted(PREP_DIR.glob("A*.feather"))
    if not files:
        return {}, {}
    ref = pd.read_feather(str(files[0]))
    ref["date"] = pd.to_datetime(ref["date"])
    ref = ref.set_index("date").sort_index()
    kp_yr, kq_yr = {}, {}
    for yr in range(ts.year, te.year + 1):
        ys = pd.Timestamp(f"{yr}-01-01")
        ye = pd.Timestamp(f"{yr+1}-01-01")
        p = ref.loc[ys:ye]
        if len(p) > 0:
            kp_yr[yr] = round(((1 + p["kospi_change"]).prod()  - 1) * 100, 2)
            kq_yr[yr] = round(((1 + p["kosdaq_change"]).prod() - 1) * 100, 2)
    return kp_yr, kq_yr


def load_prices(ts, te):
    frames = []
    for fp in DATA_DIR.glob("A*.csv"):
        code = fp.stem[1:]
        try:
            df = pd.read_csv(fp, encoding="utf-8-sig", usecols=["date", "close"])
            df["date"] = pd.to_datetime(df["date"])
            df["code"] = code
            df = df[(df["date"] >= ts) & (df["date"] <= te)]
            if not df.empty:
                frames.append(df)
        except Exception:
            pass
    all_df = pd.concat(frames, ignore_index=True)
    return all_df.pivot_table(index="date", columns="code", values="close").sort_index()


# ══════════════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end",   default="2025-12-31")
    args = parser.parse_args()

    ts = pd.Timestamp(args.start)
    te = pd.Timestamp(args.end)

    print(f"\n{'='*68}")
    print(f" 전략 × 모델 비교 분석  ({ts.date()} ~ {te.date()})")
    print(f"{'='*68}")
    print(f" 모델A  : 프로덕션 ({PROD_CACHE.name})")
    print(f" 모델B  : Expanding fold_01 ({FOLD01_CACHE.name})")
    print(f" 전략1  : 단순 threshold  buy≥{SIMPLE_BUY_THRESH} / sell<{SIMPLE_SELL_THRESH} / 10%")
    print(f" 전략2  : Tiered  buy {[t[0] for t in BUY_TIERS]} / sell {[t[0] for t in SELL_TIERS]}")
    print(f"{'='*68}")

    # ── 스코어 로드 ───────────────────────────────────────────────────────────
    print("\n[1] 스코어 로드...")
    prod_scores  = pd.read_pickle(str(PROD_CACHE))
    fold01_scores = pd.read_pickle(str(FOLD01_CACHE))
    for df in (prod_scores, fold01_scores):
        df["date"] = pd.to_datetime(df["date"])

    prod_scores   = prod_scores[(prod_scores["date"]  >= ts) & (prod_scores["date"]  <= te)]
    fold01_scores = fold01_scores[(fold01_scores["date"] >= ts) & (fold01_scores["date"] <= te)]

    print(f"   프로덕션  : {len(prod_scores):,}행  종목 {prod_scores['code'].nunique():,}개")
    print(f"   fold_01   : {len(fold01_scores):,}행  종목 {fold01_scores['code'].nunique():,}개")

    # ── 가격 데이터 ───────────────────────────────────────────────────────────
    print("\n[2] 종가 로드...")
    t0 = time.time()
    price_pivot = load_prices(ts, te)
    print(f"   거래일 {len(price_pivot)}일 | 종목 {price_pivot.shape[1]}개  ({time.time()-t0:.1f}초)")

    # ── 벤치마크 ─────────────────────────────────────────────────────────────
    bm       = benchmark(ts, te)
    kp_yr, kq_yr = benchmark_yearly(ts, te)

    # ── 4가지 시뮬레이션 ──────────────────────────────────────────────────────
    combos = [
        ("A1", "프로덕션+단순",   prod_scores,   run_simple),
        ("A2", "프로덕션+Tiered", prod_scores,   run_tiered),
        ("B1", "fold_01+단순",    fold01_scores, run_simple),
        ("B2", "fold_01+Tiered",  fold01_scores, run_tiered),
    ]

    results = {}
    for key, label, scores, runner in combos:
        print(f"\n[3] {label} 시뮬레이션...")
        t0 = time.time()
        hist = runner(scores, price_pivot, ts, te)
        elapsed = time.time() - t0
        results[key] = {"label": label, "hist": hist,
                        "stats": stats(hist), "yearly": yearly(hist)}
        print(f"   완료 ({elapsed:.1f}초)  수익률: {results[key]['stats']['수익률(%)']:+.2f}%  "
              f"Sharpe: {results[key]['stats']['Sharpe']:.2f}")

    # ── 결과 출력 ─────────────────────────────────────────────────────────────
    print(f"\n\n{'='*68}")
    print(f" 종합 성과 비교  ({ts.date()} ~ {te.date()})")
    print(f"{'='*68}")

    hdr = f"  {'':22}  {'수익률':>8}  {'Sharpe':>7}  {'MDD':>8}  {'일승률':>7}  {'평균종목':>7}"
    print(hdr)
    print("  " + "─" * 64)

    for key, r in results.items():
        s = r["stats"]
        print(f"  {r['label']:<22}  {s['수익률(%)']:>+8.2f}%  {s['Sharpe']:>7.2f}  "
              f"{s['MDD(%)']:>7.2f}%  {s['일승률(%)']:>6.1f}%  {s['평균보유종목']:>7.1f}개")

    print("  " + "─" * 64)
    print(f"  {'KOSPI (벤치마크)':22}  {bm.get('KOSPI',0):>+8.2f}%")
    print(f"  {'KOSDAQ (벤치마크)':22}  {bm.get('KOSDAQ',0):>+8.2f}%")

    # ── 연도별 수익률 ─────────────────────────────────────────────────────────
    years = sorted(kp_yr.keys())
    print(f"\n\n{'='*68}")
    print(f" 연도별 수익률")
    print(f"{'='*68}")

    yr_hdr = f"  {'연도':>4}  " + "  ".join(f"{r['label']:>14}" for r in results.values())
    yr_hdr += f"  {'KOSPI':>7}  {'KOSDAQ':>7}"
    print(yr_hdr)
    print("  " + "─" * (len(yr_hdr) - 2))

    for yr in years:
        row = f"  {yr:>4}  "
        for r in results.values():
            v = r["yearly"].get(yr, float("nan"))
            row += f"  {v:>+13.2f}%"
        row += f"  {kp_yr.get(yr,0):>+6.2f}%  {kq_yr.get(yr,0):>+6.2f}%"
        print(row)

    # ── vs KOSPI ─────────────────────────────────────────────────────────────
    print(f"\n\n{'='*68}")
    print(f" KOSPI 대비 초과수익 (연도별)")
    print(f"{'='*68}")

    vs_hdr = f"  {'연도':>4}  " + "  ".join(f"{r['label']:>14}" for r in results.values())
    print(vs_hdr)
    print("  " + "─" * (len(vs_hdr) - 2))

    for yr in years:
        kp = kp_yr.get(yr, 0)
        row = f"  {yr:>4}  "
        for r in results.values():
            v = r["yearly"].get(yr, float("nan"))
            row += f"  {v - kp:>+13.2f}%p"
        print(row)

    # 전체 기간 vs KOSPI
    kp_total = bm.get("KOSPI", 0)
    row = f"  {'전체':>4}  "
    for r in results.items():
        v = r[1]["stats"]["수익률(%)"]
        row += f"  {v - kp_total:>+13.2f}%p"
    print("  " + "─" * (len(vs_hdr) - 2))
    print(row)

    # ── CSV 저장 ─────────────────────────────────────────────────────────────
    rows = []
    for key, r in results.items():
        s = r["stats"]
        base = {"조합": r["label"], **s}
        for yr in years:
            base[str(yr)] = r["yearly"].get(yr, "")
        rows.append(base)

    out_df = pd.DataFrame(rows)
    out_path = ROOT / "compare_strategies_models.csv"
    out_df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\n\n💾 결과 저장: {out_path}")


if __name__ == "__main__":
    main()
