# -*- coding: utf-8 -*-
"""
analyze_breadth_nextday.py
당일 60점(score_total>=0.60)+ 종목 수(0~5) 별 → 다음 거래일 KOSPI/KOSDAQ 하락 확률.
데이터: A) 라이브 일별 Stock_V3 로그(2026), B) 홀드아웃 Roll-03(2000~2006+2026)
지수 변화: Data/Stock/A005930.csv 의 kospi_change / kosdaq_change (다음날 시프트)
"""
import glob, os, re
import pandas as pd, numpy as np

LOGDIR="C:\\Projects\\RealtimeMonitor\\logs"
BK="C:\\Projects\\RealtimeMonitor\\holdout_caches\\_prefilter_backup_20260615"
IDX="C:\\Projects\\RealtimeMonitor\\Data\\Stock\\A005930.csv"
THR=0.60

# 다음날 지수 변화 맵
idx=pd.read_csv(IDX,encoding='utf-8-sig',usecols=['date','kospi_change','kosdaq_change'])
idx['date']=pd.to_datetime(idx['date'],errors='coerce').dt.normalize()
idx=idx.dropna(subset=['date']).drop_duplicates('date').sort_values('date').reset_index(drop=True)
idx['nk']=idx['kospi_change'].shift(-1); idx['nq']=idx['kosdaq_change'].shift(-1)
NK=dict(zip(idx['date'],idx['nk'])); NQ=dict(zip(idx['date'],idx['nq']))

def report(cby,label):
    recs=[]
    for d,n in cby.items():
        nk=NK.get(d); nq=NQ.get(d)
        if nk is None or nq is None or (isinstance(nk,float) and np.isnan(nk)) or (isinstance(nq,float) and np.isnan(nq)):
            continue
        recs.append((n,nk<0,nq<0))
    df=pd.DataFrame(recs,columns=['n','k','q'])
    print(f"\n{'='*64}\n{label}   (다음날 매칭된 유효 {len(df)}일)\n{'='*64}")
    print(f"{'60+개수':<8}{'일수':>6}{'KOSPI 하락':>16}{'KOSDAQ 하락':>16}")
    for n in range(0,6):
        s=df[df['n']==n]
        if len(s)==0:
            print(f"{n:<8}{0:>6}{'(표본없음)':>16}{'':>16}"); continue
        kk=int(s['k'].sum()); qq=int(s['q'].sum()); m=len(s)
        print(f"{n:<8}{m:>6}{f'{kk}/{m} ({kk/m*100:.0f}%)':>16}{f'{qq}/{m} ({qq/m*100:.0f}%)':>16}")
    s=df[df['n']>=6]
    if len(s):
        kk=int(s['k'].sum()); qq=int(s['q'].sum()); m=len(s)
        print(f"{'6+':<8}{m:>6}{f'{kk}/{m} ({kk/m*100:.0f}%)':>16}{f'{qq}/{m} ({qq/m*100:.0f}%)':>16}")
    kk=int(df['k'].sum()); qq=int(df['q'].sum()); m=len(df)
    print(f"{'전체(기준선)':<8}{m:>6}{f'{kk}/{m} ({kk/m*100:.0f}%)':>16}{f'{qq}/{m} ({qq/m*100:.0f}%)':>16}")
    # 0~5개(소수) 구간 합산
    lo=df[df['n']<=5]
    if len(lo):
        kk=int(lo['k'].sum()); qq=int(lo['q'].sum()); m=len(lo)
        print(f"{'0~5합':<8}{m:>6}{f'{kk}/{m} ({kk/m*100:.0f}%)':>16}{f'{qq}/{m} ({qq/m*100:.0f}%)':>16}")

# A) 라이브 일별 로그
paths=glob.glob(os.path.join(LOGDIR,"20????","*_Stock_V3.csv"))+glob.glob(os.path.join(LOGDIR,"*_Stock_V3.csv"))
cby={}
for p in paths:
    m=re.search(r"(\d{8})_Stock_V3\.csv",os.path.basename(p))
    if not m: continue
    try:
        df=pd.read_csv(p,encoding='utf-8-sig',usecols=['score_total'],on_bad_lines='skip')
        cby[pd.Timestamp(m.group(1))]=int((df['score_total']>=THR).sum())
    except Exception: pass
report(cby,"A) 라이브 일별로그 2026 (3~7월)")

# B) 홀드아웃 Roll-03
cby2={}
for f in glob.glob(os.path.join(BK,"scores_roll_03_*.pkl")):
    s=pd.read_pickle(f); s['date']=pd.to_datetime(s['date']).dt.normalize()
    for d,g in s.groupby('date'):
        cby2[d]=int((g['score']>=THR).sum())
report(cby2,"B) 홀드아웃 Roll-03 (2000~2006 + 2026)")
