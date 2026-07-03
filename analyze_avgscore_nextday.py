# -*- coding: utf-8 -*-
"""일별 전체종목 평균 score_total(점) 10점 구간별 → 다음날 KOSPI/KOSDAQ (대량표본 Roll-03)."""
import glob, os
import pandas as pd, numpy as np
BK="C:\\Projects\\RealtimeMonitor\\holdout_caches\\_prefilter_backup_20260615"
IDX="C:\\Projects\\RealtimeMonitor\\Data\\Stock\\A005930.csv"
try:
    from scipy import stats as st; HAVE=True
except Exception: HAVE=False

idx=pd.read_csv(IDX,encoding='utf-8-sig',usecols=['date','kospi_change','kosdaq_change'])
idx['date']=pd.to_datetime(idx['date'],errors='coerce').dt.normalize()
idx=idx.dropna(subset=['date']).drop_duplicates('date').sort_values('date').reset_index(drop=True)
idx['nk']=idx['kospi_change'].shift(-1); idx['nq']=idx['kosdaq_change'].shift(-1)
NK=dict(zip(idx['date'],idx['nk'])); NQ=dict(zip(idx['date'],idx['nq']))

# 일별 전체종목 평균 점수
avg={}
for f in glob.glob(os.path.join(BK,"scores_roll_03_*.pkl")):
    s=pd.read_pickle(f); s['date']=pd.to_datetime(s['date']).dt.normalize()
    for d,g in s.groupby('date'): avg[d]=g['score'].mean()

rows=[]
for d,a in avg.items():
    nk=NK.get(d); nq=NQ.get(d)
    if nk is None or nq is None or np.isnan(nk) or np.isnan(nq): continue
    rows.append((a,nk,nq))
df=pd.DataFrame(rows,columns=['avg','nk','nq'])
df['pts']=df['avg']*100
print(f"유효 {len(df)}일 | 평균점수 분포: 최소 {df.pts.min():.1f} / 중앙 {df.pts.median():.1f} / 평균 {df.pts.mean():.1f} / 최대 {df.pts.max():.1f}")
print(f"기준선: KOSPI하락 {(df.nk<0).mean()*100:.1f}%  KOSDAQ하락 {(df.nq<0).mean()*100:.1f}%")

def binlabel(p):
    if p< -50: return "<-50"
    if p>=50: return ">=50"
    lo=int(np.floor(p/10)*10)
    return f"[{lo},{lo+10})"
df['bin']=df['pts'].apply(binlabel)
order=["<-50"]+[f"[{lo},{lo+10})" for lo in range(-50,50,10)]+[">=50"]

def ci(k,m):
    if m==0: return (float('nan'),)*2
    p=k/m; se=(p*(1-p)/m)**.5; return (max(0,p-1.96*se)*100,min(1,p+1.96*se)*100)
print(f"\n{'평균점수구간':<12}{'일수':>6}{'KOSPI하락%[CI]':>22}{'K평균%':>9}{'KOSDAQ하락%[CI]':>22}{'Q평균%':>9}")
for b in order:
    s=df[df['bin']==b]; m=len(s)
    if m==0: continue
    kk=int((s.nk<0).sum()); qq=int((s.nq<0).sum()); kl,kh=ci(kk,m); ql,qh=ci(qq,m)
    print(f"{b:<12}{m:>6}{f'{kk/m*100:.0f}% [{kl:.0f}-{kh:.0f}]':>22}{s.nk.mean()*100:>9.2f}{f'{qq/m*100:.0f}% [{ql:.0f}-{qh:.0f}]':>22}{s.nq.mean()*100:>9.2f}")

if HAVE:
    print("\n[상관·검정] 평균점수 ↔ 다음날 지수")
    for nm,c in [("KOSPI",'nk'),("KOSDAQ",'nq')]:
        rho,pr=st.spearmanr(df.avg,df[c]); r,pp=st.pearsonr(df.avg,df[c])
        ct=pd.crosstab(df['bin'],df[c]<0); chi2,pc,_,_=st.chi2_contingency(ct)
        print(f"  {nm}: 순위상관 ρ={rho:+.3f}(p={pr:.3f}) | 피어슨 r={r:+.3f}(p={pp:.3f}) | 카이제곱 p={pc:.3f} ({'유의' if pc<0.05 else '독립'})")
