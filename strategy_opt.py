# -*- coding: utf-8 -*-
"""
strategy_opt.py
===============
스코어 기반 매매전략 최적화 (휩쏘 억제 + 진입/청산 점수대 최적화).

- 대상 모델: Roll-03(현 프로덕션 = _prefilter_backup/roll_03) 와 roll_01(필터, holdout_caches).
- 평가 데이터: 2000~2006(학습 前, train) + 2026(OOS 後, validation).  ← 깨끗한 out-of-sample만.
- 모델은 고정, '전략'만 최적화. 점수 캐시 재활용 → GPU 불필요.

탐색 파라미터:
  buy   : 진입 점수 임계 (평활점수 >= buy 매수)
  sell  : 청산 점수 임계 (평활점수 < sell 가 K일 연속이면 청산)
  N     : 스코어 평활 기간(이동평균). 1=평활 없음
  K     : 청산 확인일수. 1=즉시
  hold  : 최소 보유 거래일
  stop  : %손절 (0=없음)
고정: 종목당 비중 10%, 최대 10종목.

과적합 방지:
  - 2000~2006 연도별 Sharpe → 중앙값/최악연도/표준편차로 '강건' 파라미터 선정(봉우리 아닌 평지).
  - 2026 은 검증용으로 분리 표기.
  - 휩쏘 지표(평균보유일/왕복손실/회전율) 동시 보고.

실행:
  python -X utf8 strategy_opt.py
산출: analysis_strategy_opt/  (모델별 랭킹 CSV)
"""
import importlib.util, itertools
import numpy as np, pandas as pd
from pathlib import Path

spec = importlib.util.spec_from_file_location("ah", r"C:\Projects\RealtimeMonitor\analyze_holdout.py")
ah = importlib.util.module_from_spec(spec); spec.loader.exec_module(ah)

ROOT  = Path(r"C:\Projects\RealtimeMonitor")
CACHE = ROOT / "holdout_caches"
BK    = CACHE / "_prefilter_backup_20260615"
OUT   = ROOT / "analysis_strategy_opt"; OUT.mkdir(exist_ok=True)
CUTOFF = pd.Timestamp("2026-05-29")

TRAIN_YEARS = list(range(2000, 2007))   # 학습 前
VALID_YEARS = [2026]                     # OOS 後
ALL_YEARS   = TRAIN_YEARS + VALID_YEARS

BUY_FEE, SELL_FEE = 0.00015, 0.00195
CAPITAL = 1_000_000_000
ALLOC, MAXPOS = 0.10, 10
WHIP_DAYS = 5     # 매수 후 N거래일 이내 손실 청산 = 휩쏘

GRID = {
    "buy":  [0.45, 0.55, 0.65, 0.75, 0.85],
    "sell": [0.00, 0.10, 0.20, 0.30, 0.40],
    "N":    [1, 3, 5],
    "K":    [1, 2, 3],
    "hold": [0, 5],
    "stop": [0.0, -0.12],
}
MODELS = {"Roll-03(현프로덕션)": (BK, "roll_03"), "roll_01(필터)": (CACHE, "roll_01")}

# ── 데이터 로딩 ────────────────────────────────────────────────────────────────
print("가격 로딩...")
price_pivot = ah.load_prices(ALL_YEARS)
kospi = ah.get_kospi_by_year(ALL_YEARS)

def load_scores(folder, tag, year):
    p = folder / f"scores_{tag}_{year}.pkl"
    if not p.exists(): return None
    df = pd.read_pickle(str(p)); df['date'] = pd.to_datetime(df['date'])
    if year == 2026: df = df[df['date'] <= CUTOFF]
    return df

def build_year(folder, tag, year):
    """연도별: trading_days, prices(date->code->px), 그리고 N별 평활 점수(date->code->score)."""
    sdf = load_scores(folder, tag, year)
    if sdf is None or sdf.empty: return None
    y0 = pd.Timestamp(f"{year}-01-01"); y1 = pd.Timestamp(f"{year+1}-01-01")
    if year == 2026: y1 = min(CUTOFF + pd.Timedelta(days=1), y1)
    mask = (price_pivot.index >= y0) & (price_pivot.index < y1)
    tdays = list(price_pivot.loc[mask].index)
    if len(tdays) < 5: return None
    prices = {d: price_pivot.loc[d].dropna().to_dict() for d in tdays}
    # 코드별 점수 시계열 → N별 평활
    piv = sdf.pivot_table(index='date', columns='code', values='score').reindex(tdays)
    smoothed = {}
    for N in set(GRID["N"]):
        sm = piv if N == 1 else piv.rolling(N, min_periods=1).mean()
        # date -> {code: score}
        smoothed[N] = {d: row.dropna().to_dict() for d, row in sm.iterrows()}
    return {"tdays": tdays, "prices": prices, "smoothed": smoothed,
            "didx": {d: i for i, d in enumerate(tdays)}}

# ── 시뮬 (한 연도, 한 파라미터) ────────────────────────────────────────────────
def simulate(yd, buy, sell, N, K, hold, stop):
    tdays, prices, didx = yd["tdays"], yd["prices"], yd["didx"]
    sc = yd["smoothed"][N]
    cash = float(CAPITAL); pos = {}   # code->{qty,avg,buy_i,below}
    last_px = {}; equity = []; trades = []   # trade: (hold_days, pnl_pct)
    for i, d in enumerate(tdays):
        pr = prices[d]; last_px.update({c: p for c, p in pr.items() if p > 0})
        today = sc.get(d, {})
        def px(c):
            p = pr.get(c) or last_px.get(c)
            return p if p and p > 0 else (pos[c]["avg"] if c in pos else 0.0)
        # 매도
        for c in list(pos.keys()):
            p = pr.get(c)
            if not p or p <= 0: continue
            P = pos[c]
            held = i - P["buy_i"]
            loss = (p - P["avg"]) / P["avg"]
            # 손절 (보유기간 무관)
            if stop != 0.0 and loss <= stop:
                cash += P["qty"]*p*(1-SELL_FEE); trades.append((held, loss)); del pos[c]; continue
            if hold > 0 and held < hold:  # 최소보유 중엔 신호청산 보류
                continue
            s = today.get(c)
            if s is not None and s < sell:
                P["below"] += 1
                if P["below"] >= K:
                    cash += P["qty"]*p*(1-SELL_FEE); trades.append((held, loss)); del pos[c]
            else:
                P["below"] = 0
        # 매수
        tot = cash + sum(P["qty"]*px(c) for c, P in pos.items())
        cands = sorted([(c, s) for c, s in today.items()
                        if s >= buy and pr.get(c, 0) > 0], key=lambda x: -x[1])
        for c, s in cands:
            if len(pos) >= MAXPOS and c not in pos: continue
            p = pr[c]; mv = pos[c]["qty"]*p if c in pos else 0.0
            cur = mv/tot if tot > 0 else 0.0
            if ALLOC > cur + 0.01:
                amt = (ALLOC - cur)*tot; qty = int(amt/p); cost = qty*p*(1+BUY_FEE)
                if qty > 0 and cost <= cash:
                    cash -= cost
                    if c in pos:
                        P = pos[c]; nq = P["qty"]+qty
                        P["avg"] = (P["qty"]*P["avg"]+qty*p)/nq; P["qty"] = nq
                    else:
                        pos[c] = {"qty": qty, "avg": p, "buy_i": i, "below": 0}
        equity.append(cash + sum(P["qty"]*px(c) for c, P in pos.items()))
    # 지표
    ta = pd.Series(equity, index=tdays)
    ret = (ta.iloc[-1]/ta.iloc[0]-1)*100
    dr = ta.pct_change().dropna()
    sharpe = dr.mean()/dr.std()*np.sqrt(252) if dr.std() > 0 else 0.0
    mdd = ((ta-ta.cummax())/ta.cummax()).min()*100
    n_tr = len(trades)
    avg_hold = np.mean([h for h, _ in trades]) if trades else 0.0
    whip = sum(1 for h, pl in trades if pl < 0 and h <= WHIP_DAYS)
    whip_rate = whip/n_tr*100 if n_tr else 0.0
    return {"ret": ret, "sharpe": sharpe, "mdd": mdd, "n_tr": n_tr,
            "avg_hold": avg_hold, "whip": whip, "whip_rate": whip_rate}

# ── 모델별 그리드 실행 ─────────────────────────────────────────────────────────
keys = list(GRID.keys())
combos = list(itertools.product(*[GRID[k] for k in keys]))
print(f"파라미터 조합: {len(combos)}개 × 연도 {len(ALL_YEARS)} × 모델 {len(MODELS)}")

for mname, (folder, tag) in MODELS.items():
    print(f"\n=== {mname} 연도 데이터 준비 ===")
    yds = {y: build_year(folder, tag, y) for y in ALL_YEARS}
    yds = {y: v for y, v in yds.items() if v is not None}
    rows = []
    for ci, vals in enumerate(combos):
        p = dict(zip(keys, vals))
        per = {y: simulate(yds[y], p["buy"], p["sell"], p["N"], p["K"], p["hold"], p["stop"])
               for y in yds}
        tr = [per[y] for y in TRAIN_YEARS if y in per]
        va = per.get(2026)
        if not tr: continue
        tsh = [x["sharpe"] for x in tr]; trt = [x["ret"] for x in tr]
        rows.append({
            **{k: p[k] for k in keys},
            "T_Sharpe중앙": round(float(np.median(tsh)), 2),
            "T_Sharpe최악": round(float(np.min(tsh)), 2),
            "T_Sharpe표준": round(float(np.std(tsh)), 2),
            "T_수익평균": round(float(np.mean(trt)), 1),
            "T_누적": round(float((np.prod([1+r/100 for r in trt])-1)*100), 1),
            "T_평균보유일": round(float(np.mean([x["avg_hold"] for x in tr])), 1),
            "T_휩쏘율%": round(float(np.mean([x["whip_rate"] for x in tr])), 1),
            "T_연거래": round(float(np.mean([x["n_tr"] for x in tr])), 0),
            "V2026_Sharpe": round(va["sharpe"], 2) if va else None,
            "V2026_수익": round(va["ret"], 1) if va else None,
            "V2026_보유일": round(va["avg_hold"], 1) if va else None,
        })
        if (ci+1) % 200 == 0: print(f"  {ci+1}/{len(combos)}")
    df = pd.DataFrame(rows)
    # 강건 랭킹: train 중앙 Sharpe 우선, 최악연도 floor 가산, 분산 패널티
    df["강건점수"] = (df["T_Sharpe중앙"] + 0.5*df["T_Sharpe최악"] - 0.3*df["T_Sharpe표준"]).round(2)
    df = df.sort_values("강건점수", ascending=False).reset_index(drop=True)
    fn = OUT / f"strategy_{tag}.csv"
    df.to_csv(fn, index=False, encoding="utf-8-sig")
    print(f"\n■ {mname} — 강건 상위 12 (train 2000~2006 / valid 2026)")
    cols = ["buy","sell","N","K","hold","stop","강건점수","T_Sharpe중앙","T_Sharpe최악",
            "T_수익평균","T_평균보유일","T_휩쏘율%","T_연거래","V2026_Sharpe","V2026_수익"]
    print(df[cols].head(12).to_string(index=False))
    print(f"💾 {fn}")
print(f"\n완료: {OUT}")
