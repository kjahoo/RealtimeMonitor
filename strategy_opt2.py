# -*- coding: utf-8 -*-
"""
strategy_opt2.py — 포지션 사이징 집중 최적화
============================================
진입선 부근에서 '동일비중 vs 점수가중(선형/티어)' 를 촘촘히 비교.
휩쏘 레버는 직전 검증 최적값으로 고정(N=3 평활, K=2 확인, 최소보유 5, -12% 손절).
대상: Roll-03(현프로덕션), roll_01(필터).  데이터: 2000~2006(train)+2026(valid) OOS.
산출: analysis_strategy_opt/sizing_<tag>.csv
"""
import importlib.util, itertools
import numpy as np, pandas as pd
from pathlib import Path

spec = importlib.util.spec_from_file_location("ah", r"C:\Projects\RealtimeMonitor\analyze_holdout.py")
ah = importlib.util.module_from_spec(spec); spec.loader.exec_module(ah)

ROOT  = Path(r"C:\Projects\RealtimeMonitor")
CACHE = ROOT / "holdout_caches"; BK = CACHE / "_prefilter_backup_20260615"
OUT   = ROOT / "analysis_strategy_opt"; OUT.mkdir(exist_ok=True)
CUTOFF = pd.Timestamp("2026-05-29")
TRAIN = list(range(2000, 2007)); VALID = [2026]; ALL = TRAIN + VALID
BUY_FEE, SELL_FEE = 0.00015, 0.00195; CAPITAL = 1_000_000_000
WHIP_DAYS = 5
N_FIX, K_FIX, HOLD_FIX, STOP_FIX = 3, 2, 5, -0.12   # 검증된 휩쏘 레버 고정

# ── 사이징 스킴: (label, type, params) → alloc_target(score, buy) ──
EQUAL  = [(f"EQ_{int(e*100)}%", "eq",  e) for e in (0.06,0.08,0.10,0.12,0.15)]
LINEAR = [(f"LIN_{int(lo*100)}-{int(hi*100)}","lin",(lo,hi)) for lo,hi in
          ((0.05,0.15),(0.05,0.20),(0.05,0.25),(0.08,0.15),(0.08,0.20))]
TIERS  = [
 ("TIER_현재형(5-15)","tier",[(0.80,0.15),(0.70,0.12),(0.60,0.10),(0.55,0.07),(0.0,0.05)]),
 ("TIER_가파름",     "tier",[(0.80,0.20),(0.70,0.13),(0.60,0.09),(0.0,0.06)]),
 ("TIER_완만",       "tier",[(0.70,0.13),(0.55,0.09),(0.0,0.07)]),
 ("TIER_평탄=EQ10",  "tier",[(0.0,0.10)]),
]
SCHEMES = EQUAL + LINEAR + TIERS

def alloc_target(scheme, score, buy):
    typ, prm = scheme[1], scheme[2]
    if typ == "eq":  return prm
    if typ == "lin":
        lo, hi = prm; f = (score - buy)/(1.0 - buy) if buy < 1 else 0
        return lo + (hi-lo)*min(max(f,0.0),1.0)
    for thr, w in prm:   # tier: 내림차순
        if score >= thr: return w
    return prm[-1][1]

GRID = {"buy":[0.50,0.55,0.60,0.65], "sell":[0.10,0.20,0.30], "maxpos":[8,12,20]}
MODELS = {"Roll-03(현프로덕션)":(BK,"roll_03"), "roll_01(필터)":(CACHE,"roll_01")}

print("가격 로딩..."); price_pivot = ah.load_prices(ALL); kospi = ah.get_kospi_by_year(ALL)

def build_year(folder, tag, year):
    p = folder/f"scores_{tag}_{year}.pkl"
    if not p.exists(): return None
    sdf = pd.read_pickle(str(p)); sdf['date']=pd.to_datetime(sdf['date'])
    if year==2026: sdf=sdf[sdf['date']<=CUTOFF]
    y0=pd.Timestamp(f"{year}-01-01"); y1=pd.Timestamp(f"{year+1}-01-01")
    if year==2026: y1=min(CUTOFF+pd.Timedelta(days=1),y1)
    tdays=list(price_pivot.loc[(price_pivot.index>=y0)&(price_pivot.index<y1)].index)
    if len(tdays)<5: return None
    prices={d:price_pivot.loc[d].dropna().to_dict() for d in tdays}
    piv=sdf.pivot_table(index='date',columns='code',values='score').reindex(tdays)
    sm=piv.rolling(N_FIX,min_periods=1).mean()
    smoothed={d:row.dropna().to_dict() for d,row in sm.iterrows()}
    return {"tdays":tdays,"prices":prices,"sc":smoothed}

def simulate(yd, buy, sell, maxpos, scheme):
    tdays,prices,sc=yd["tdays"],yd["prices"],yd["sc"]
    cash=float(CAPITAL); pos={}; last={}; eq=[]; trades=[]
    for i,d in enumerate(tdays):
        pr=prices[d]; last.update({c:p for c,p in pr.items() if p>0}); today=sc.get(d,{})
        def px(c):
            p=pr.get(c) or last.get(c); return p if p and p>0 else (pos[c]["avg"] if c in pos else 0.0)
        for c in list(pos.keys()):
            p=pr.get(c)
            if not p or p<=0: continue
            P=pos[c]; held=i-P["buy_i"]; loss=(p-P["avg"])/P["avg"]
            if loss<=STOP_FIX: cash+=P["qty"]*p*(1-SELL_FEE); trades.append((held,loss)); del pos[c]; continue
            if held<HOLD_FIX: continue
            s=today.get(c)
            if s is not None and s<sell:
                P["below"]+=1
                if P["below"]>=K_FIX: cash+=P["qty"]*p*(1-SELL_FEE); trades.append((held,loss)); del pos[c]
            else: P["below"]=0
        tot=cash+sum(P["qty"]*px(c) for c,P in pos.items())
        cands=sorted([(c,s) for c,s in today.items() if s>=buy and pr.get(c,0)>0],key=lambda x:-x[1])
        for c,s in cands:
            if len(pos)>=maxpos and c not in pos: continue
            p=pr[c]; mv=pos[c]["qty"]*p if c in pos else 0.0; cur=mv/tot if tot>0 else 0.0
            tw=alloc_target(scheme,s,buy)
            if tw>cur+0.01:
                amt=(tw-cur)*tot; qty=int(amt/p); cost=qty*p*(1+BUY_FEE)
                if qty>0 and cost<=cash:
                    cash-=cost
                    if c in pos:
                        P=pos[c]; nq=P["qty"]+qty; P["avg"]=(P["qty"]*P["avg"]+qty*p)/nq; P["qty"]=nq
                    else: pos[c]={"qty":qty,"avg":p,"buy_i":i,"below":0}
        eq.append(cash+sum(P["qty"]*px(c) for c,P in pos.items()))
    ta=pd.Series(eq,index=tdays); ret=(ta.iloc[-1]/ta.iloc[0]-1)*100
    dr=ta.pct_change().dropna(); sh=dr.mean()/dr.std()*np.sqrt(252) if dr.std()>0 else 0.0
    mdd=((ta-ta.cummax())/ta.cummax()).min()*100
    n=len(trades); ah_=np.mean([h for h,_ in trades]) if trades else 0
    whip=sum(1 for h,pl in trades if pl<0 and h<=WHIP_DAYS); wr=whip/n*100 if n else 0
    return {"ret":ret,"sharpe":sh,"mdd":mdd,"n":n,"hold":ah_,"wr":wr}

combos=list(itertools.product(GRID["buy"],GRID["sell"],GRID["maxpos"],SCHEMES))
print(f"조합 {len(combos)} × 연도 {len(ALL)} × 모델 {len(MODELS)}")
for mname,(folder,tag) in MODELS.items():
    print(f"\n=== {mname} ===")
    yds={y:build_year(folder,tag,y) for y in ALL}; yds={y:v for y,v in yds.items() if v}
    rows=[]
    for buy,sell,mp,scheme in combos:
        per={y:simulate(yds[y],buy,sell,mp,scheme) for y in yds}
        tr=[per[y] for y in TRAIN if y in per]; va=per.get(2026)
        if not tr: continue
        tsh=[x["sharpe"] for x in tr]; trt=[x["ret"] for x in tr]
        rows.append({"사이징":scheme[0],"유형":scheme[1],"buy":buy,"sell":sell,"maxpos":mp,
            "T_Sh중앙":round(np.median(tsh),2),"T_Sh최악":round(np.min(tsh),2),"T_Sh분산":round(np.std(tsh),2),
            "T_수익":round(np.mean(trt),1),"T_누적":round((np.prod([1+r/100 for r in trt])-1)*100,1),
            "T_보유일":round(np.mean([x["hold"] for x in tr]),1),"T_휩쏘%":round(np.mean([x["wr"] for x in tr]),1),
            "V2026_Sh":round(va["sharpe"],2) if va else None,"V2026_수익":round(va["ret"],1) if va else None})
    df=pd.DataFrame(rows)
    df["강건"]=(df["T_Sh중앙"]+0.5*df["T_Sh최악"]-0.3*df["T_Sh분산"]).round(2)
    df=df.sort_values("강건",ascending=False).reset_index(drop=True)
    df.to_csv(OUT/f"sizing_{tag}.csv",index=False,encoding="utf-8-sig")
    cols=["사이징","buy","sell","maxpos","강건","T_Sh중앙","T_Sh최악","T_수익","T_누적","T_보유일","T_휩쏘%","V2026_Sh","V2026_수익"]
    print(f"\n■ {mname} 강건 상위 10")
    print(df[cols].head(10).to_string(index=False))
    print(f"\n  유형별 최고 강건점수: " + " | ".join(f"{t}:{df[df['유형']==t]['강건'].max():.2f}" for t in ["eq","lin","tier"]))
    print(f"💾 sizing_{tag}.csv")
print(f"\n완료: {OUT}")
