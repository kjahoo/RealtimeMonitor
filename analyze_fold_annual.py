"""
analyze_fold_annual.py
======================
특정 폴드를 학습종료 이후 각 연도별로 시뮬레이션하여 KOSPI와 비교.
데이터 품질 필터(거래정지/가격급변/거래량0) 적용.

사용:
  python -X utf8 analyze_fold_annual.py
  python -X utf8 analyze_fold_annual.py --method Expanding --fold 1 --from-year 2013 --to-year 2025
"""

import argparse, gc, warnings, sys, time
import numpy as np
import pandas as pd
from pathlib import Path

warnings.filterwarnings("ignore")
import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

ROOT = Path(r"C:\Projects\RealtimeMonitor")
sys.path.insert(0, str(ROOT))
import select_best_model as sbm

PREP_DIR = ROOT / "Data" / "_prep_wf_v3"


def get_kospi_annual(from_year: int, to_year: int) -> dict:
    """prep 캐시에서 연도별 KOSPI 수익률 딕셔너리 반환."""
    try:
        pf = sbm._list_prep_files()
        if not pf:
            return {}
        ref = sbm._read_prep(pf[0])
        ref['date'] = pd.to_datetime(ref['date'])
        ref = ref.set_index('date').sort_index()
        result = {}
        for year in range(from_year, to_year + 1):
            period = ref[ref.index.year == year]
            if not period.empty:
                result[year] = round(((1 + period['kospi_change']).prod() - 1) * 100, 2)
        return result
    except Exception:
        return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--method',    default='Expanding', choices=['Expanding', 'Rolling7y'])
    parser.add_argument('--fold',      type=int, default=1)
    parser.add_argument('--from-year', type=int, default=2013)
    parser.add_argument('--to-year',   type=int, default=2025)
    parser.add_argument('--no-cache',  action='store_true')
    args = parser.parse_args()

    wf_root  = ROOT / ("walk_forward" if args.method == "Expanding" else "walk_forward_rolling7y")
    fold_dir = wf_root / f"fold_{args.fold:02d}"

    print(f"\n{'='*70}")
    print(f"📊  {args.method} fold_{args.fold:02d}  연간 분석  "
          f"({args.from_year} ~ {args.to_year})  품질필터 ON")
    print(f"{'='*70}")

    # ── 1. 모델 로드 ────────────────────────────────────────────────────────────
    print(f"\n[1] 모델 로딩...")
    models = sbm.load_fold_models(fold_dir)

    # ── 2. 전체 기간 스코어 1회 계산 ────────────────────────────────────────────
    full_start = pd.Timestamp(f"{args.from_year}-01-01")
    full_end   = pd.Timestamp(f"{args.to_year}-12-31")

    # 별도 캐시 태그 (연간 전체용)
    cache_tag  = f"annual_{full_start.strftime('%Y%m%d')}_{full_end.strftime('%Y%m%d')}"
    cache_path = fold_dir / f"holdout_{cache_tag}_scores.pkl"

    print(f"\n[2] 전체 스코어 계산 ({args.from_year}~{args.to_year})...")
    t0 = time.time()
    score_df_all = sbm.compute_scores(
        models, full_start, full_end, cache_path, no_cache=args.no_cache
    )
    del models
    gc.collect()

    if score_df_all.empty:
        print("❌ 스코어 없음")
        return

    score_df_all['date'] = pd.to_datetime(score_df_all['date'])
    print(f"   총 레코드: {len(score_df_all):,}건  ({time.time()-t0:.0f}초)")

    # ── 3. 가격 데이터 전체 로드 (품질 필터 적용) ────────────────────────────────
    print(f"\n[3] 가격 데이터 로딩 (품질 필터 적용)...")
    price_pivot = sbm.load_prices(full_start, full_end, filter_bad=True)
    if price_pivot.empty:
        print("❌ 가격 데이터 없음")
        return
    print(f"   종목 수: {price_pivot.shape[1]:,}  전체 거래일: {price_pivot.shape[0]}")

    # ── 4. KOSPI 연간 수익률 ────────────────────────────────────────────────────
    kospi_dict = get_kospi_annual(args.from_year, args.to_year)

    # ── 5. 연도별 시뮬레이션 ───────────────────────────────────────────────────
    print(f"\n[4] 연도별 시뮬레이션 실행...\n")
    print(f"  {'연도':>4}  {'전략':>8}  {'KOSPI':>7}  {'초과':>8}  "
          f"{'Sharpe':>7}  {'MDD':>8}  {'일승률':>6}  {'거래일':>4}  {'판정':>2}")
    print(f"  {'─'*66}")

    rows = []
    for year in range(args.from_year, args.to_year + 1):
        h_start = pd.Timestamp(f"{year}-01-01")
        h_end   = pd.Timestamp(f"{year+1}-01-01")   # exclusive end

        # 연도별 스코어 슬라이스
        score_year = score_df_all[score_df_all['date'].dt.year == year].copy()
        if score_year.empty:
            continue

        # 해당 연도 거래일 확인
        yr_mask = (price_pivot.index >= h_start) & (price_pivot.index < h_end)
        if yr_mask.sum() == 0:
            continue

        # 시뮬레이션
        history_df = sbm.run_simulation(score_year, h_start, h_end, price_pivot)
        if history_df.empty or len(history_df) < 5:
            continue

        stats  = sbm.analyze_history(history_df)
        ret    = stats['수익률(%)']
        kospi  = kospi_dict.get(year, float('nan'))
        excess = round(ret - kospi, 2) if not np.isnan(kospi) else float('nan')
        beat   = "✅" if (not np.isnan(excess) and excess > 0) else "❌"

        kospi_s  = f"{kospi:+.2f}%" if not np.isnan(kospi) else "   N/A"
        excess_s = f"{excess:+.2f}%p" if not np.isnan(excess) else "    N/A"

        print(f"  {year:>4}  {ret:>+7.2f}%  {kospi_s:>7}  "
              f"{excess_s:>8}  {stats['Sharpe']:>7.2f}  "
              f"{stats['최대낙폭(%)']:>7.2f}%  {stats['일승률(%)']:>5.1f}%  "
              f"{stats['거래일수']:>4}  {beat}")

        rows.append({
            "연도":       year,
            "전략(%)":    ret,
            "KOSPI(%)":   kospi,
            "초과(%p)":   excess,
            "Sharpe":     stats['Sharpe'],
            "MDD(%)":     stats['최대낙폭(%)'],
            "일승률(%)":  stats['일승률(%)'],
            "거래일수":   stats['거래일수'],
        })

    if not rows:
        print("  결과 없음")
        return

    print(f"  {'─'*66}")

    # ── 6. 요약 통계 ────────────────────────────────────────────────────────────
    df = pd.DataFrame(rows)
    valid = df.dropna(subset=['KOSPI(%)'])

    n_beat    = int((valid['초과(%p)'] > 0).sum())
    avg_ret   = df['전략(%)'].mean()
    avg_kospi = valid['KOSPI(%)'].mean()
    avg_exc   = valid['초과(%p)'].mean()
    avg_sh    = df['Sharpe'].mean()
    avg_mdd   = df['MDD(%)'].mean()

    # 전체 복리 누적 수익률
    cum_strat = ((1 + df['전략(%)'] / 100).prod() - 1) * 100
    cum_kospi = ((1 + valid['KOSPI(%)'] / 100).prod() - 1) * 100

    print(f"\n  ▶ 요약 ({len(df)}개년, {args.from_year}~{args.to_year})")
    print(f"    KOSPI 초과 : {n_beat}/{len(valid)}년 "
          f"({n_beat/len(valid)*100:.0f}%)")
    print(f"    평균 수익률: {avg_ret:+.2f}%  vs  KOSPI 평균: {avg_kospi:+.2f}%")
    print(f"    평균 초과  : {avg_exc:+.2f}%p")
    print(f"    평균 Sharpe: {avg_sh:.2f}  |  평균 MDD: {avg_mdd:.2f}%")
    print(f"    누적 수익률: 전략 {cum_strat:+.1f}%  vs  KOSPI {cum_kospi:+.1f}%")

    # 연도별 KOSPI 초과/미달 막대
    print(f"\n  ▶ 연도별 초과수익 시각화 (■ = 1%p)")
    print(f"  {'─'*66}")
    for _, r in df.iterrows():
        if np.isnan(r['초과(%p)']):
            continue
        exc = r['초과(%p)']
        bar_len = min(int(abs(exc) / 1), 40)
        bar = '■' * bar_len
        direction = '+' if exc >= 0 else '-'
        print(f"  {int(r['연도'])}  {'✅' if exc >= 0 else '❌'}  "
              f"{direction}{bar:<40}  {exc:+.1f}%p")

    # ── 저장 ────────────────────────────────────────────────────────────────────
    out = ROOT / f"{args.method.lower()}_fold{args.fold:02d}_annual.csv"
    df.to_csv(out, index=False, encoding='utf-8-sig')
    print(f"\n💾 저장: {out}")


if __name__ == "__main__":
    main()