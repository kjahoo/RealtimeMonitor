# -*- coding: utf-8 -*-
"""60+ 종목수 구간별 → 익일/3일/5일 누적 KOSPI/KOSDAQ 하락확률·평균수익·MDD (Roll-03 대량표본)."""
import glob, os
import pandas as pd, numpy as np
BK="C:\\Projects\\RealtimeMonitor\\holdout_caches\\_prefilter_backup_20260615"
IDX="C:\\Projects\\RealtimeMonitor\\Data\\Stock\\A005930.csv"
THR=0.60; HZ=[1,3,5]
try:
    from scipy import stats as st; HAVE=True
except Exception: HAVE=False

idx=pd.read_csv(IDX,encoding='utf-8-sig',usecols=['date','kospi_change','kosdaq_change'])
idx['date']=pd.to_datetime(idx['date'],errors='coerce').dt.normalize()
idx=idx.dropna(subset=['date']).drop_duplicates('date').sort_values('date').reset_index(drop=True)
kc=idx['kospi_change'].values; qc=idx['kosdaq_change'].values
pos={d:i for i,d in enumerate(idx['date'])}

def fwd(i, arr, H):
    """다음 H거래일 누적수익, MDD (entry=0 기준 최저 낙폭)."""
    w=arr[i+1:i+1+H]
    if len(w)<H or np.isnan(w).any(): return None
    cur=1.0; path=[]
    for x in w: cur*=(1+x); path.append(cur-1)
    peak=0.0; mdd=0.0
    for c in path:
        peak=max(peak,c); mdd=min(mdd,c-peak)
    return path[-1], mdd   # 누적수익, MDD(<=0)

# 60+ 카운트
cby={}
for f in glob.glob(os.path.join(BK,"scores_roll_03_*.pkl")):
    s=pd.read_pickle(f); s['date']=pd.to_datetime(s['date']).dt.normalize()
    for d,g in s.groupby('date'): cby[d]=int((g['score']>=THR).sum())

def cbin(n):
    if n==0: return "0"
    if n<=2: return "1-2"
    if n<=5: return "3-5"
    if n<=10: return "6-10"
    if n<=15: return "11-15"
    if n<=20: return "16-20"
    return "21+"
ORDER=["0","1-2","3-5","6-10","11-15","16-20","21+"]

recs=[]
for d,n in cby.items():
    if d not in pos: continue
    i=pos[d]; row={'n':n,'bin':cbin(n)}
    ok=True
    for H in HZ:
        rk=fwd(i,kc,H); rq=fwd(i,qc,H)
        if rk is None or rq is None: ok=False; break
        row[f'k{H}']=rk[0]; row[f'kmdd{H}']=rk[1]
        row[f'q{H}']=rq[0]; row[f'qmdd{H}']=rq[1]
    if ok: recs.append(row)
df=pd.DataFrame(recs)
print(f"유효 {len(df)}일 (5일 앞 데이터 있는 날 기준)")

for H in HZ:
    print(f"\n{'='*78}\n[{H}일 누적]  구간별 (일수 / 하락% / 평균수익% / 평균MDD%)\n{'='*78}")
    print(f"{'60+수':<7}{'일수':>5}   {'KOSPI 하락%':>11}{'K평균%':>8}{'K_MDD%':>8}   {'KOSDAQ하락%':>11}{'Q평균%':>8}{'Q_MDD%':>8}")
    for b in ORDER:
        s=df[df['bin']==b]; m=len(s)
        if m==0: continue
        kd=(s[f'k{H}']<0).mean()*100; qd=(s[f'q{H}']<0).mean()*100
        print(f"{b:<7}{m:>5}   {kd:>10.0f}%{s[f'k{H}'].mean()*100:>8.2f}{s[f'kmdd{H}'].mean()*100:>8.2f}   "
              f"{qd:>10.0f}%{s[f'q{H}'].mean()*100:>8.2f}{s[f'qmdd{H}'].mean()*100:>8.2f}")
    kd=(df[f'k{H}']<0).mean()*100; qd=(df[f'q{H}']<0).mean()*100
    print(f"{'전체':<7}{len(df):>5}   {kd:>10.0f}%{df[f'k{H}'].mean()*100:>8.2f}{df[f'kmdd{H}'].mean()*100:>8.2f}   "
          f"{qd:>10.0f}%{df[f'q{H}'].mean()*100:>8.2f}{df[f'qmdd{H}'].mean()*100:>8.2f}")
    if HAVE:
        rk,pk=st.spearmanr(df['n'],df[f'k{H}']); rq,pq=st.spearmanr(df['n'],df[f'q{H}'])
        print(f"  순위상관(종목수↔{H}일수익)  KOSPI ρ={rk:+.3f}(p={pk:.3f})  KOSDAQ ρ={rq:+.3f}(p={pq:.3f})")
