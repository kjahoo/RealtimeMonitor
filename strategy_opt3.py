# -*- coding: utf-8 -*-
"""
strategy_opt3.py — 부분청산(scale-out) + 절충 사이징 정밀화
==========================================================
청산 방식(전량 vs 부분 2단/3단), 사이징(동일/선형), 진입, 평활을 함께 탐색.
고정: 확인 K=2, 최소보유 5, -12% 손절, maxpos 12.
대상: Roll-03, roll_01.  데이터: 2000~2006(train)+2026(valid) OOS.
산출: analysis_strategy_opt/refine_<tag>.csv
"""
import importlib.util, itertools
import numpy as np, pandas as pd
from pathlib import Path

spec = importlib.util.spec_from_file_location("ah", r"C:\Projects\RealtimeMonitor\analyze_holdout.py")
ah = importlib.util.module_from_spec(spec); spec.loader.exec_module(ah)

ROOT=Path(r"C:\Projects\RealtimeMonitor"); CACHE=ROOT/"holdout_caches"; BK=CACHE/"_prefilter_backup_20260615"
OUT=ROOT/"analysis_strategy_opt"; OUT.mkdir(exist_ok=True)
CUTOFF=pd.Timestamp("2026-05-29"); TRAIN=list(range(2000,2007)); VALID=[2026]; ALLY=TRAIN+VALID
BUY_FEE,SELL_FEE=0.00015,0.00195; CAPITAL=1_000_000_000; WHIP_DAYS=5
K_FIX,HOLD_FIX,STOP_FIX,MAXPOS=2,5,-0.12,12

SIZING=[("EQ_10","eq",0.10),("EQ_12","eq",0.12),
        ("LIN_5-15","lin",(0.05,0.15)),("LIN_5-18","lin",(0.05,0.18)),
        ("LIN_5-20","lin",(0.05,0.20)),("LIN_5-25","lin",(0.05,0.25)),("LIN_8-18","lin",(0.08,0.18))]
def alloc_target(sz,s,buy):
    t,p=sz[1],sz[2]
    if t=="eq": return p
    lo,hi=p; f=(s-buy)/(1-buy) if buy<1 else 0; return lo+(hi-lo)*min(max(f,0),1)

# 청산 방식: sell_zone(s<sell, K일확인) 진입 후 s에 따른 '잔여 목표비율' 함수
def remain_frac(style, s, sell):
    if style=="full": return 0.0
    if style=="P2":   return 0.0 if s < sell-0.10 else 0.5
    if style=="P3":
        if s < sell-0.14: return 0.0
        if s < sell-0.07: return 0.33
        return 0.66
EXIT=["full","P2","P3"]

GRID={"buy":[0.55,0.60],"sell":[0.10,0.20],"N":[3,5],"exit":EXIT}
MODELS={"Roll-03(현프로덕션)":(BK,"roll_03"),"roll_01(필터)":(CACHE,"roll_01")}

print("가격 로딩..."); price_pivot=ah.load_prices(ALLY); kospi=ah.get_kospi_by_year(ALLY)

def build_year(folder,tag,year):
    p=folder/f"scores_{tag}_{year}.pkl"
    if not p.exists(): return None
    sdf=pd.read_pickle(str(p)); sdf['date']=pd.to_datetime(sdf['date'])
    if year==2026: sdf=sdf[sdf['date']<=CUTOFF]
    y0=pd.Timestamp(f"{year}-01-01"); y1=pd.Timestamp(f"{year+1}-01-01")
    if year==2026: y1=min(CUTOFF+pd.Timedelta(days=1),y1)
    td=list(price_pivot.loc[(price_pivot.index>=y0)&(price_pivot.index<y1)].index)
    if len(td)<5: return None
    prices={d:price_pivot.loc[d].dropna().to_dict() for d in td}
    piv=sdf.pivot_table(index='date',columns='code',values='score').reindex(td)
    sm={N:({d:r.dropna().to_dict() for d,r in piv.rolling(N,min_periods=1).mean().iterrows()}) for N in set(GRID["N"])}
    return {"td":td,"prices":prices,"sm":sm}

def simulate(yd,buy,sell,N,exit_style,sz):
    td,prices,sc=yd["td"],yd["prices"],yd["sm"][N]
    cash=float(CAPITAL); pos={}; last={}; eq=[]; trades=[]
    for i,d in enumerate(td):
        pr=prices[d]; last.update({c:p for c,p in pr.items() if p>0}); today=sc.get(d,{})
        def px(c):
            p=pr.get(c) or last.get(c); return p if p and p>0 else (pos[c]["avg"] if c in pos else 0.0)
        for c in list(pos.keys()):
            p=pr.get(c)
            if not p or p<=0: continue
            P=pos[c]; held=i-P["buy_i"]; loss=(p-P["avg"])/P["avg"]
            if loss<=STOP_FIX:  # 하드손절 전량
                cash+=P["qty"]*p*(1-SELL_FEE); trades.append((held,loss)); del pos[c]; continue
            if held<HOLD_FIX: continue
            s=today.get(c)
            if s is not None and s<sell:
                P["below"]+=1
                if P["below"]>=K_FIX:
                    tgt=int(remain_frac(exit_style,s,sell)*P["maxq"])
                    if P["qty"]>tgt:
                        sq=P["qty"]-tgt; cash+=sq*p*(1-SELL_FEE); trades.append((held,loss)); P["qty"]=tgt
                        if P["qty"]<=0: del pos[c]
            else: P["below"]=0
        tot=cash+sum(P["qty"]*px(c) for c,P in pos.items())
        cands=sorted([(c,s) for c,s in today.items() if s>=buy and pr.get(c,0)>0],key=lambda x:-x[1])
        for c,s in cands:
            if len(pos)>=MAXPOS and c not in pos: continue
            p=pr[c]; mv=pos[c]["qty"]*p if c in pos else 0.0; cur=mv/tot if tot>0 else 0.0
            tw=alloc_target(sz,s,buy)
            if tw>cur+0.01:
                amt=(tw-cur)*tot; qty=int(amt/p); cost=qty*p*(1+BUY_FEE)
                if qty>0 and cost<=cash:
                    cash-=cost
                    if c in pos:
                        P=pos[c]; nq=P["qty"]+qty; P["avg"]=(P["qty"]*P["avg"]+qty*p)/nq; P["qty"]=nq; P["maxq"]=max(P["maxq"],nq)
                    else: pos[c]={"qty":qty,"avg":p,"buy_i":i,"below":0,"maxq":qty}
        eq.append(cash+sum(P["qty"]*px(c) for c,P in pos.items()))
    ta=pd.Series(eq,index=td); ret=(ta.iloc[-1]/ta.iloc[0]-1)*100
    dr=ta.pct_change().dropna(); sh=dr.mean()/dr.std()*np.sqrt(252) if dr.std()>0 else 0.0
    n=len(trades); hold=np.mean([h for h,_ in trades]) if trades else 0
    whip=sum(1 for h,pl in trades if pl<0 and h<=WHIP_DAYS); wr=whip/n*100 if n else 0
    return {"ret":ret,"sharpe":sh,"hold":hold,"wr":wr,"n":n}

combos=list(itertools.product(GRID["buy"],GRID["sell"],GRID["N"],GRID["exit"],SIZING))
print(f"조합 {len(combos)} × 연도 {len(ALLY)} × 모델 {len(MODELS)}")
for mname,(folder,tag) in MODELS.items():
    print(f"\n=== {mname} ===")
    yds={y:build_year(folder,tag,y) for y in ALLY}; yds={y:v for y,v in yds.items() if v}
    rows=[]
    for buy,sell,N,ex,sz in combos:
        per={y:simulate(yds[y],buy,sell,N,ex,sz) for y in yds}
        tr=[per[y] for y in TRAIN if y in per]; va=per.get(2026)
        if not tr: continue
        tsh=[x["sharpe"] for x in tr]; trt=[x["ret"] for x in tr]
        rows.append({"청산":ex,"사이징":sz[0],"buy":buy,"sell":sell,"N":N,
            "T_Sh중앙":round(np.median(tsh),2),"T_Sh최악":round(np.min(tsh),2),"T_Sh분산":round(np.std(tsh),2),
            "T_누적":round((np.prod([1+r/100 for r in trt])-1)*100,1),
            "T_보유일":round(np.mean([x["hold"] for x in tr]),1),"T_휩쏘%":round(np.mean([x["wr"] for x in tr]),1),
            "V2026_Sh":round(va["sharpe"],2) if va else None,"V2026수익":round(va["ret"],1) if va else None})
    df=pd.DataFrame(rows); df["강건"]=(df["T_Sh중앙"]+0.5*df["T_Sh최악"]-0.3*df["T_Sh분산"]).round(2)
    df=df.sort_values("강건",ascending=False).reset_index(drop=True)
    df.to_csv(OUT/f"refine_{tag}.csv",index=False,encoding="utf-8-sig")
    cols=["청산","사이징","buy","sell","N","강건","T_Sh중앙","T_Sh최악","T_누적","T_보유일","T_휩쏘%","V2026_Sh","V2026수익"]
    print(f"\n■ {mname} 강건 상위 10"); print(df[cols].head(10).to_string(index=False))
    print(f"  청산방식별 최고 강건: " + " | ".join(f"{e}:{df[df['청산']==e]['강건'].max():.2f}" for e in EXIT))
    print(f"💾 refine_{tag}.csv")
print(f"\n완료: {OUT}")
