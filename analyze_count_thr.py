# -*- coding: utf-8 -*-
"""임계 50/60/70/80점별: 종목수 구간 → 향후 1/3/5일 KOSPI/KOSDAQ 하락확률·평균·MDD + 순위상관."""
import glob, os
import pandas as pd, numpy as np
BK="C:\\Projects\\RealtimeMonitor\\holdout_caches\\_prefilter_backup_20260615"
IDX="C:\\Projects\\RealtimeMonitor\\Data\\Stock\\A005930.csv"
THRS=[0.50,0.60,0.70,0.80]; HZ=[1,3,5]
try:
    from scipy import stats as st; HAVE=True
except Exception: HAVE=False

idx=pd.read_csv(IDX,encoding='utf-8-sig',usecols=['date','kospi_change','kosdaq_change'])
idx['date']=pd.to_datetime(idx['date'],errors='coerce').dt.normalize()
idx=idx.dropna(subset=['date']).drop_duplicates('date').sort_values('date').reset_index(drop=True)
kc=idx['kospi_change'].values; qc=idx['kosdaq_change'].values
pos={d:i for i,d in enumerate(idx['date'])}

def fwd(i,arr,H):
    w=arr[i+1:i+1+H]
    if len(w)<H or np.isnan(w).any(): return None
    cur=1.0; path=[]
    for x in w: cur*=(1+x); path.append(cur-1)
    peak=0.0; mdd=0.0
    for c in path: peak=max(peak,c); mdd=min(mdd,c-peak)
    return path[-1],mdd

# 종목별 점수 로드 (한 번)
allscores=[]
for f in glob.glob(os.path.join(BK,"scores_roll_03_*.pkl")):
    s=pd.read_pickle(f); s['date']=pd.to_datetime(s['date']).dt.normalize()
    allscores.append(s[['date','score']])
S=pd.concat(allscores,ignore_index=True)

def cbin(n):
    if n==0: return "0"
    if n<=2: return "1-2"
    if n<=5: return "3-5"
    if n<=10: return "6-10"
    if n<=20: return "11-20"
    return "21+"
ORDER=["0","1-2","3-5","6-10","11-20","21+"]

for THR in THRS:
    cby=S[S['score']>=THR].groupby('date').size().to_dict()
    # 점수 로드된 모든 날짜(카운트 0 포함)
    alldays=S['date'].unique()
    recs=[]
    for d in alldays:
        if d not in pos: continue
        i=pos[d]; n=int(cby.get(d,0)); row={'n':n,'bin':cbin(n)}
        ok=True
        for H in HZ:
            rk=fwd(i,kc,H); rq=fwd(i,qc,H)
            if rk is None or rq is None: ok=False; break
            row[f'k{H}']=rk[0]; row[f'kmdd{H}']=rk[1]; row[f'q{H}']=rq[0]; row[f'qmdd{H}']=rq[1]
        if ok: recs.append(row)
    df=pd.DataFrame(recs)
    z=(df['n']==0).mean()*100
    print(f"\n{'#'*80}\n[{int(THR*100)}점 이상]  유효 {len(df)}일 | 종목수 중앙 {int(df.n.median())} 평균 {df.n.mean():.1f} 최대 {df.n.max()} | 0개인날 {(df.n==0).sum()}일({z:.0f}%)")
    if HAVE:
        line="  순위상관 ρ(수↔수익): "
        for H in HZ:
            rk,pk=st.spearmanr(df.n,df[f'k{H}']); rq,pq=st.spearmanr(df.n,df[f'q{H}'])
            line+=f"[{H}일 K{rk:+.3f}{'*' if pk<0.05 else ' '} Q{rq:+.3f}{'*' if pq<0.05 else ' '}] "
        print(line)
    # 5일 구간 테이블
    H=5
    print(f"  [5일누적] {'구간':<7}{'일수':>5}{'K하락%':>8}{'K평균':>7}{'K_MDD':>7}{'Q하락%':>9}{'Q평균':>7}{'Q_MDD':>7}")
    for b in ORDER:
        s=df[df['bin']==b]; m=len(s)
        if m==0: continue
        print(f"           {b:<7}{m:>5}{(s[f'k{H}']<0).mean()*100:>7.0f}%{s[f'k{H}'].mean()*100:>7.2f}{s[f'kmdd{H}'].mean()*100:>7.2f}"
              f"{(s[f'q{H}']<0).mean()*100:>8.0f}%{s[f'q{H}'].mean()*100:>7.2f}{s[f'qmdd{H}'].mean()*100:>7.2f}")
    s=df
    print(f"           {'전체':<7}{len(s):>5}{(s[f'k{H}']<0).mean()*100:>7.0f}%{s[f'k{H}'].mean()*100:>7.2f}{s[f'kmdd{H}'].mean()*100:>7.2f}"
          f"{(s[f'q{H}']<0).mean()*100:>8.0f}%{s[f'q{H}'].mean()*100:>7.2f}{s[f'qmdd{H}'].mean()*100:>7.2f}")
