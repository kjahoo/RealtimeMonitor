# -*- coding: utf-8 -*-
"""
compare_filtered.py
===================
필터링 재학습 Rolling 7y 12폴드(새) vs 현 프로덕션(Roll-03) 종합 비교.
- 모든 모델을 동일 wf-path(feather)로 평가해 공정 비교.
- 현 프로덕션(Roll-03) = 옛 unfiltered fold_03 = _prefilter_backup 의 roll_03.
- 필터 효과: 새(filtered, holdout_caches) vs 옛(unfiltered, _prefilter_backup) 폴드별.
- 2026 은 05-29 공통 종료일.
산출: analysis_holdout_filtered_20260616/  (랭킹/필터효과 CSV)
"""
import importlib.util
import pandas as pd, numpy as np
from pathlib import Path

spec = importlib.util.spec_from_file_location("ah", r"C:\Projects\RealtimeMonitor\analyze_holdout.py")
ah = importlib.util.module_from_spec(spec); spec.loader.exec_module(ah)

CACHE = Path(r"C:\Projects\RealtimeMonitor\holdout_caches")
BK    = CACHE / "_prefilter_backup_20260615"
OUT   = Path(r"C:\Projects\RealtimeMonitor\analysis_holdout_filtered_20260616"); OUT.mkdir(exist_ok=True)
CUTOFF = pd.Timestamp("2026-05-29")
A_YEARS = ah.HOLDOUT_A
ALL = A_YEARS + ah.HOLDOUT_B

price = ah.load_prices(ALL)
kospi = ah.get_kospi_by_year(ALL)

def ev(folder, tag):
    out={}
    for y in ALL:
        p=folder/f"scores_{tag}_{y}.pkl"
        if not p.exists(): continue
        sdf=pd.read_pickle(str(p)); sdf['date']=pd.to_datetime(sdf['date'])
        cap=None
        if y==2026: sdf=sdf[sdf['date']<=CUTOFF]; cap=CUTOFF
        h,t=ah.run_simulation(sdf,y,price,end_cap=cap)
        m=ah.calc_metrics(h,t)
        if m: out[y]=m
    return out

def block(rows, years):
    sub={y:rows[y] for y in years if y in rows}
    rets=[v["수익률(%)"] for v in sub.values()]; shs=[v["Sharpe"] for v in sub.values()]
    mdds=[v["MDD(%)"] for v in sub.values()]
    nval=[y for y in sub if not np.isnan(kospi.get(y,float('nan')))]
    beat=sum(1 for y in nval if sub[y]["수익률(%)"]>kospi[y])
    return {"평균수익":np.mean(rets),"누적":(np.prod([1+r/100 for r in rets])-1)*100,
            "Sharpe":np.mean(shs),"MDD":np.mean(mdds),
            "일관성":np.mean([1 if r>0 else 0 for r in rets]),
            "KOSPI승률":(beat/len(nval) if nval else float('nan'))}

# ── 평가: 현프로덕션(BK roll_03) + 새 필터 12 ──
models = {"현프로덕션(Roll-03)": ev(BK, "roll_03")}
for f in range(1,13):
    models[f"roll_{f:02d}(필터)"] = ev(CACHE, f"roll_{f:02d}")

recs=[]
for name, r in models.items():
    A=block(r, A_YEARS); B=block(r, ah.HOLDOUT_B)
    recs.append({"모델":name,
        "A_평균수익":round(A["평균수익"],1),"A_누적":round(A["누적"],1),"A_Sharpe":round(A["Sharpe"],2),
        "A_MDD":round(A["MDD"],1),"A_일관성":round(A["일관성"],2),"A_KOSPI승률":round(A["KOSPI승률"],2),
        "B2026_수익":round(B["평균수익"],1),"B2026_Sharpe":round(B["Sharpe"],2),"B2026_MDD":round(B["MDD"],1)})
df=pd.DataFrame(recs)

# ── 복합점수 (analyze_holdout 방식: 구간별 min-max 정규화 가중합) ──
W=ah.COMPOSITE_WEIGHTS
def comp(part):  # part='A' or 'B2026'
    if part=='A':
        cols={"수익률(%)":df["A_평균수익"],"Sharpe":df["A_Sharpe"],"MDD(%)":df["A_MDD"],
              "KOSPI승률":df["A_KOSPI승률"],"일관성":df["A_일관성"]}
    else:
        cons=(df["B2026_수익"]>0).astype(float)
        cols={"수익률(%)":df["B2026_수익"],"Sharpe":df["B2026_Sharpe"],"MDD(%)":df["B2026_MDD"],
              "KOSPI승률":pd.Series(0.0,index=df.index),"일관성":cons}
    s=pd.Series(0.0,index=df.index)
    for k,w in W.items():
        v=cols[k].astype(float)
        if k=="MDD(%)": v=-v
        lo,hi=v.min(),v.max()
        n=(v-lo)/(hi-lo) if hi-lo>1e-9 else pd.Series(0.5,index=df.index)
        s=s+w*n
    return (s*100).round(1)
df["A_복합"]=comp('A'); df["B_복합"]=comp('B2026'); df["종합복합"]=((df["A_복합"]+df["B_복합"])/2).round(1)
df=df.sort_values("종합복합",ascending=False).reset_index(drop=True)
df.insert(0,"순위",df.index+1)
df.to_csv(OUT/"ranking_filtered.csv",index=False,encoding="utf-8-sig")

# ── 필터 효과: 새(filtered) vs 옛(unfiltered) 폴드별 ──
fe=[]
for f in range(1,13):
    nf=ev(CACHE,f"roll_{f:02d}"); of=ev(BK,f"roll_{f:02d}")
    nA=block(nf,A_YEARS); oA=block(of,A_YEARS)
    nB=block(nf,ah.HOLDOUT_B); oB=block(of,ah.HOLDOUT_B)
    fe.append({"폴드":f"roll_{f:02d}",
        "필터A누적":round(nA["누적"],1),"옛A누적":round(oA["누적"],1),"A차이":round(nA["누적"]-oA["누적"],1),
        "필터2026":round(nB["평균수익"],1),"옛2026":round(oB["평균수익"],1),"2026차이":round(nB["평균수익"]-oB["평균수익"],1)})
fedf=pd.DataFrame(fe); fedf.to_csv(OUT/"filter_effect.csv",index=False,encoding="utf-8-sig")

# ── 출력 ──
pd.set_option('display.width',200); pd.set_option('display.max_columns',30)
print("\n"+"="*100)
print("■ 종합 복합 랭킹 (현프로덕션 + 새 필터 12폴드, 동일 wf-path)")
print("="*100)
print(df[["순위","모델","종합복합","A_복합","B_복합","A_평균수익","A_누적","A_Sharpe","A_KOSPI승률","B2026_수익","B2026_Sharpe"]].to_string(index=False))
print("\n"+"="*100)
print("■ 필터 효과 (새 filtered vs 옛 unfiltered, 폴드별)")
print("="*100)
print(fedf.to_string(index=False))
nA_win=(fedf["A차이"]>0).sum(); nB_win=(fedf["2026차이"]>0).sum()
print(f"\n필터가 도움된 폴드: Holdout-A 누적 {nA_win}/12,  2026 {nB_win}/12")
prod_rank=int(df[df['모델']=='현프로덕션(Roll-03)']['순위'].iloc[0])
print(f"현 프로덕션(Roll-03) 종합 순위: {prod_rank}/13")
best=df.iloc[0]
print(f"종합 1위: {best['모델']} (복합 {best['종합복합']})")
print(f"\n산출물: {OUT}")
