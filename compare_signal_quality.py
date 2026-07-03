"""
compare_signal_quality.py
=========================
2013~2020 (양 모델 모두 학습 미사용) 구간에서
Exp-01(신규) vs 구 프로덕션의 매수 신호 품질 비교.

성공 조건 (train_v3.py 기준):
  target1  : 신호일 이후 1일  내 최고 종가 수익률 >= +3%
  target5  : 신호일 이후 5일  내 최고 종가 수익률 >= +7%
  target20 : 신호일 이후 20일 내 최고 종가 수익률 >= +30%

출력:
  - 각 모델 월별 신호 수 및 성공률
  - 신호 후 1d/5d/20d 평균 최고 수익률
  - 5개년 요약 비교표

사용:
  python -X utf8 compare_signal_quality.py
"""

import warnings, sys
import numpy as np
import pandas as pd
from pathlib import Path

warnings.filterwarnings("ignore")
import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

ROOT     = Path(r"C:\Projects\RealtimeMonitor")
DATA_DIR = ROOT / "Data" / "Stock"

SIM_FROM = pd.Timestamp("2013-01-01")
SIM_TO   = pd.Timestamp("2020-12-31")

# 성공 임계값 (train_v3.py와 동일)
THRESH_1D  = 0.03   # +3%
THRESH_5D  = 0.07   # +7%
THRESH_20D = 0.30   # +30%

STRATEGIES = {
    "Exp-01 (신규, buy≥0.55)": {
        "cache":      ROOT / "cache_exp01_full_2006_2026_scores.pkl",
        "buy_thresh": 0.55,
    },
    "구 프로덕션 (buy≥0.50)": {
        "cache":      ROOT / "cache_prod_full_2006_2026_scores.pkl",
        "buy_thresh": 0.50,
    },
}


# ── 가격 데이터 로드 ─────────────────────────────────────────────────────────
def load_price_pivot(from_date: pd.Timestamp, to_date: pd.Timestamp,
                     extra_days: int = 30) -> pd.DataFrame:
    """종가 pivot (date × code). extra_days만큼 뒤까지 포함해 forward 계산 지원."""
    load_to = to_date + pd.DateOffset(days=extra_days * 2)
    frames  = []
    for fpath in DATA_DIR.glob("A*.csv"):
        code = fpath.stem[1:]
        try:
            df = pd.read_csv(fpath, encoding='utf-8-sig',
                             usecols=lambda c: c in ['date', 'close'])
            df['date'] = pd.to_datetime(df['date'])
            df = df[(df['date'] >= from_date) & (df['date'] <= load_to)]
            if not df.empty:
                frames.append(df.assign(code=code))
        except Exception:
            pass
    if not frames:
        return pd.DataFrame()
    all_df = pd.concat(frames, ignore_index=True)
    return all_df.pivot_table(index='date', columns='code', values='close').sort_index()


# ── 신호 품질 평가 ───────────────────────────────────────────────────────────
def evaluate_signals(score_df: pd.DataFrame, buy_thresh: float,
                     price_pivot: pd.DataFrame) -> pd.DataFrame:
    """
    매수 신호별 forward 수익률 계산.
    반환: DataFrame with columns
      [date, code, score, ret_1d_max, ret_5d_max, ret_20d_max,
       hit_1d, hit_5d, hit_20d, any_hit]
    """
    # 분석 기간 신호만 필터
    signals = score_df[
        (score_df['date'] >= SIM_FROM) &
        (score_df['date'] <= SIM_TO) &
        (score_df['score'] >= buy_thresh)
    ].copy()

    if signals.empty:
        return pd.DataFrame()

    all_dates = price_pivot.index  # 실제 거래일 목록

    records = []
    for _, row in signals.iterrows():
        code  = row['code']
        date  = row['date']
        score = row['score']

        if code not in price_pivot.columns:
            continue

        # 신호일 종가
        if date not in price_pivot.index:
            continue
        base_price = price_pivot.at[date, code]
        if pd.isna(base_price) or base_price <= 0:
            continue

        # 신호일 이후 거래일 인덱스
        pos = all_dates.get_loc(date)

        def fwd_max_ret(n_days):
            end_pos = pos + n_days
            if end_pos >= len(all_dates):
                return np.nan
            fut = price_pivot.iloc[pos + 1: end_pos + 1][code].dropna()
            if fut.empty:
                return np.nan
            return float(fut.max() / base_price - 1)

        r1  = fwd_max_ret(1)
        r5  = fwd_max_ret(5)
        r20 = fwd_max_ret(20)

        records.append({
            'date':        date,
            'code':        code,
            'score':       score,
            'ret_1d_max':  r1,
            'ret_5d_max':  r5,
            'ret_20d_max': r20,
            'hit_1d':      (r1  >= THRESH_1D)  if pd.notna(r1)  else False,
            'hit_5d':      (r5  >= THRESH_5D)  if pd.notna(r5)  else False,
            'hit_20d':     (r20 >= THRESH_20D) if pd.notna(r20) else False,
        })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df['any_hit'] = df['hit_1d'] | df['hit_5d'] | df['hit_20d']
    return df


# ── 연도별 집계 ──────────────────────────────────────────────────────────────
def yearly_summary(eval_df: pd.DataFrame) -> pd.DataFrame:
    eval_df = eval_df.copy()
    eval_df['year'] = eval_df['date'].dt.year

    rows = []
    for year, g in eval_df.groupby('year'):
        n = len(g)
        valid_20 = g['ret_20d_max'].notna()
        rows.append({
            '연도':         year,
            '신호수':       n,
            '성공(1건이상)': f"{g['any_hit'].mean()*100:.1f}%",
            'hit_1d':       f"{g['hit_1d'].mean()*100:.1f}%",
            'hit_5d':       f"{g['hit_5d'].mean()*100:.1f}%",
            'hit_20d':      f"{g['hit_20d'].mean()*100:.1f}%",
            '평균최고_1d':  f"{g['ret_1d_max'].mean()*100:+.2f}%",
            '평균최고_5d':  f"{g['ret_5d_max'].mean()*100:+.2f}%",
            '평균최고_20d': f"{g[valid_20]['ret_20d_max'].mean()*100:+.2f}%" if valid_20.any() else "N/A",
        })
    return pd.DataFrame(rows)


# ── 메인 ────────────────────────────────────────────────────────────────────
def main():
    print("\n" + "="*72)
    print("📊  매수 신호 품질 비교  2013 ~ 2020  (양 모델 공통 OOS 구간)")
    print("="*72)
    print(f"  성공 조건: 1d ≥+3%  /  5d ≥+7%  /  20d ≥+30%  (1건 이상 = 성공)")

    print("\n[공통] 가격 데이터 로딩 (2013~2021년 초)...")
    price_pivot = load_price_pivot(SIM_FROM, SIM_TO, extra_days=30)
    print(f"   종목 수: {price_pivot.shape[1]:,}  날짜 수: {price_pivot.shape[0]:,}")

    results = {}
    for label, cfg in STRATEGIES.items():
        cache_path = cfg['cache']
        if not cache_path.exists():
            print(f"\n⚠️  [{label}] 캐시 없음: {cache_path.name}")
            continue

        print(f"\n[{label}]")
        score_df = pd.read_pickle(str(cache_path))
        score_df['date'] = pd.to_datetime(score_df['date'])

        in_range = score_df[(score_df['date'] >= SIM_FROM) & (score_df['date'] <= SIM_TO)]
        print(f"   전체 스코어: {len(in_range):,}건")

        signals = in_range[in_range['score'] >= cfg['buy_thresh']]
        print(f"   매수 신호:   {len(signals):,}건  (score ≥ {cfg['buy_thresh']})")

        print("   forward 수익률 계산 중...")
        eval_df = evaluate_signals(score_df, cfg['buy_thresh'], price_pivot)

        if eval_df.empty:
            print("   신호 없음")
            continue

        results[label] = eval_df

        # 연도별 출력
        yr = yearly_summary(eval_df)
        print(f"\n  {'연도':>6}  {'신호수':>6}  {'성공률':>8}  "
              f"{'hit_1d':>7}  {'hit_5d':>7}  {'hit_20d':>8}  "
              f"{'avg_1d':>8}  {'avg_5d':>8}  {'avg_20d':>9}")
        print("  " + "─"*74)
        for _, r in yr.iterrows():
            print(f"  {int(r['연도']):>6}  {r['신호수']:>6,}  {r['성공(1건이상)']:>8}  "
                  f"{r['hit_1d']:>7}  {r['hit_5d']:>7}  {r['hit_20d']:>8}  "
                  f"{r['평균최고_1d']:>8}  {r['평균최고_5d']:>8}  {r['평균최고_20d']:>9}")

        # 전체 합계
        n   = len(eval_df)
        print("  " + "─"*74)
        print(f"  {'합계':>6}  {n:>6,}  "
              f"{eval_df['any_hit'].mean()*100:>7.1f}%  "
              f"{eval_df['hit_1d'].mean()*100:>6.1f}%  "
              f"{eval_df['hit_5d'].mean()*100:>6.1f}%  "
              f"{eval_df['hit_20d'].mean()*100:>7.1f}%  "
              f"{eval_df['ret_1d_max'].mean()*100:>+7.2f}%  "
              f"{eval_df['ret_5d_max'].mean()*100:>+7.2f}%  "
              f"{eval_df['ret_20d_max'].mean()*100:>+8.2f}%")

    # ── 최종 비교표 ──────────────────────────────────────────────────────────
    if len(results) == 2:
        labels = list(results.keys())
        e1, e2 = results[labels[0]], results[labels[1]]

        def stats(df):
            return {
                'n':        len(df),
                'any_hit':  df['any_hit'].mean() * 100,
                'hit_1d':   df['hit_1d'].mean() * 100,
                'hit_5d':   df['hit_5d'].mean() * 100,
                'hit_20d':  df['hit_20d'].mean() * 100,
                'avg_1d':   df['ret_1d_max'].mean() * 100,
                'avg_5d':   df['ret_5d_max'].mean() * 100,
                'avg_20d':  df['ret_20d_max'].mean() * 100,
            }

        s1, s2 = stats(e1), stats(e2)

        print(f"\n\n{'='*72}")
        print(" 최종 비교  (2013~2020 합산)")
        print(f"{'='*72}")
        lbl1 = "Exp-01 (신규)"
        lbl2 = "구 프로덕션"
        print(f"  {'항목':<22}  {lbl1:>16}  {lbl2:>14}  {'차이':>8}")
        print("  " + "─"*64)

        rows = [
            ("총 매수 신호수",      f"{s1['n']:,}건",          f"{s2['n']:,}건",          ""),
            ("성공률 (1건이상)",    f"{s1['any_hit']:.1f}%",   f"{s2['any_hit']:.1f}%",   f"{s1['any_hit']-s2['any_hit']:+.1f}%p"),
            ("  hit_1d  (≥+3%)",  f"{s1['hit_1d']:.1f}%",    f"{s2['hit_1d']:.1f}%",    f"{s1['hit_1d']-s2['hit_1d']:+.1f}%p"),
            ("  hit_5d  (≥+7%)",  f"{s1['hit_5d']:.1f}%",    f"{s2['hit_5d']:.1f}%",    f"{s1['hit_5d']-s2['hit_5d']:+.1f}%p"),
            ("  hit_20d (≥+30%)", f"{s1['hit_20d']:.1f}%",   f"{s2['hit_20d']:.1f}%",   f"{s1['hit_20d']-s2['hit_20d']:+.1f}%p"),
            ("평균 최고수익_1d",   f"{s1['avg_1d']:+.2f}%",   f"{s2['avg_1d']:+.2f}%",   f"{s1['avg_1d']-s2['avg_1d']:+.2f}%p"),
            ("평균 최고수익_5d",   f"{s1['avg_5d']:+.2f}%",   f"{s2['avg_5d']:+.2f}%",   f"{s1['avg_5d']-s2['avg_5d']:+.2f}%p"),
            ("평균 최고수익_20d",  f"{s1['avg_20d']:+.2f}%",  f"{s2['avg_20d']:+.2f}%",  f"{s1['avg_20d']-s2['avg_20d']:+.2f}%p"),
        ]
        for item, v1, v2, diff in rows:
            print(f"  {item:<22}  {v1:>16}  {v2:>14}  {diff:>8}")


if __name__ == "__main__":
    main()
