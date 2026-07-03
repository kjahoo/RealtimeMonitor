"""
analyze_three_models.py
=======================
Exp-01, Roll7y-06, 프로덕션(unified) 3개 모델을 각각 학습기간 제외 후 비교.

  python -X utf8 analyze_three_models.py
"""

import gc, warnings, sys, time, pickle
import numpy as np
import pandas as pd
from pathlib import Path

warnings.filterwarnings("ignore")
import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

ROOT = Path(r"C:\Projects\RealtimeMonitor")
sys.path.insert(0, str(ROOT))
import select_best_model as sbm

PROD_DIR = ROOT / "Model"


# ── 프로덕션 모델 로드 (Model/ 디렉토리 직접 사용) ───────────────────────────
def load_production_models() -> dict:
    from tensorflow.keras.models import load_model as keras_load

    models  = {}
    best_f1 = {}

    for m_name, cfg in sbm.MODEL_SETTINGS.items():
        h5  = PROD_DIR / f"{m_name}_lstm_v3.h5"
        scl = PROD_DIR / f"{m_name}_lstm_v3.scaler"
        log = PROD_DIR / f"log_{m_name}_v3_v3_unified.csv"

        if not h5.exists() or not scl.exists():
            print(f"  ❌ {m_name}: 모델 파일 없음")
            continue

        with open(scl, 'rb') as f:
            scaler = pickle.load(f)
        model     = keras_load(str(h5), compile=False)
        actual_lb = model.input_shape[1]

        if log.exists():
            log_df          = pd.read_csv(log)
            best_row        = log_df.loc[log_df['f1'].idxmax()]
            threshold       = float(best_row['threshold'])
            best_f1[m_name] = float(best_row['f1'])
        else:
            threshold = cfg["thr"]

        models[m_name] = {
            "model":     model,
            "scaler":    scaler,
            "lookback":  actual_lb,
            "threshold": threshold,
            "weight":    cfg["weight"],
            "type":      cfg["type"],
        }

    # F1 비례 가중치
    for group in ("surge", "drop"):
        names = [n for n, c in sbm.MODEL_SETTINGS.items() if c["type"] == group and n in models]
        f1s   = [best_f1.get(n) for n in names]
        if all(f is not None for f in f1s):
            total = sum(f1s)
            for n, f in zip(names, f1s):
                models[n]["weight"] = f / total

    for m_name, info in models.items():
        print(f"     {m_name:<10} lb={info['lookback']:>3}  thr={info['threshold']:.4f}  w={info['weight']:.4f}")

    return models


# ── KOSPI 연간 수익률 ─────────────────────────────────────────────────────────
def get_kospi_annual(from_year: int, to_year: int) -> dict:
    try:
        pf  = sbm._list_prep_files()
        if not pf: return {}
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


# ── 연도별 시뮬레이션 공통 함수 ───────────────────────────────────────────────
def simulate_annual(models, from_year: int, to_year: int,
                    price_pivot, kospi_dict, label: str) -> pd.DataFrame:
    score_df_all = None

    print(f"\n{'='*70}")
    print(f"📊  {label}  ({from_year} ~ {to_year})")
    print(f"{'='*70}")

    full_start = pd.Timestamp(f"{from_year}-01-01")
    full_end   = pd.Timestamp(f"{to_year}-12-31")

    safe_label = label.replace(" ", "_").replace("(", "").replace(")", "")
    cache_path = ROOT / f"cache_{safe_label}_{from_year}_{to_year}_scores.pkl"
    print(f"\n[스코어 계산 중...]")
    t0 = time.time()
    score_df_all = sbm.compute_scores(models, full_start, full_end, cache_path, no_cache=False)
    print(f"   완료: {len(score_df_all):,}건  ({time.time()-t0:.0f}초)")

    if score_df_all.empty:
        print("❌ 스코어 없음")
        return pd.DataFrame()

    score_df_all['date'] = pd.to_datetime(score_df_all['date'])

    print(f"\n{'연도':>4}  {'전략':>8}  {'KOSPI':>7}  {'초과':>8}  "
          f"{'Sharpe':>7}  {'MDD':>8}  {'거래일':>4}  {'판정':>2}")
    print(f"  {'─'*66}")

    rows = []
    for year in range(from_year, to_year + 1):
        h_start = pd.Timestamp(f"{year}-01-01")
        h_end   = pd.Timestamp(f"{year+1}-01-01")

        score_year = score_df_all[score_df_all['date'].dt.year == year].copy()
        if score_year.empty: continue

        yr_mask = (price_pivot.index >= h_start) & (price_pivot.index < h_end)
        if yr_mask.sum() == 0: continue

        history_df = sbm.run_simulation(score_year, h_start, h_end, price_pivot)
        if history_df.empty or len(history_df) < 5: continue

        stats  = sbm.analyze_history(history_df)
        ret    = stats['수익률(%)']
        kospi  = kospi_dict.get(year, float('nan'))
        excess = round(ret - kospi, 2) if not np.isnan(kospi) else float('nan')
        beat   = "✅" if (not np.isnan(excess) and excess > 0) else "❌"

        kospi_s  = f"{kospi:+.2f}%" if not np.isnan(kospi) else "  N/A"
        excess_s = f"{excess:+.2f}%p" if not np.isnan(excess) else "  N/A"

        print(f"{year:>4}  {ret:>+7.2f}%  {kospi_s:>7}  "
              f"{excess_s:>8}  {stats['Sharpe']:>7.2f}  "
              f"{stats['최대낙폭(%)']:>7.2f}%  {stats['거래일수']:>4}  {beat}")

        rows.append({
            "연도":      year,
            "전략(%)":   ret,
            "KOSPI(%)":  kospi,
            "초과(%p)":  excess,
            "Sharpe":    stats['Sharpe'],
            "MDD(%)":    stats['최대낙폭(%)'],
            "거래일수":  stats['거래일수'],
        })

    print(f"  {'─'*66}")
    return pd.DataFrame(rows)


# ── 요약 출력 ─────────────────────────────────────────────────────────────────
def print_summary(df: pd.DataFrame, label: str, fy: int, ty: int):
    if df.empty: return
    valid   = df.dropna(subset=['KOSPI(%)'])
    n_beat  = int((valid['초과(%p)'] > 0).sum())
    avg_ret = df['전략(%)'].mean()
    avg_exc = valid['초과(%p)'].mean() if not valid.empty else float('nan')
    avg_sh  = df['Sharpe'].mean()
    avg_mdd = df['MDD(%)'].mean()
    cum_s   = ((1 + df['전략(%)'] / 100).prod() - 1) * 100
    cum_k   = ((1 + valid['KOSPI(%)'] / 100).prod() - 1) * 100 if not valid.empty else float('nan')

    print(f"\n  ▶ [{label}] 요약 ({len(df)}개년, {fy}~{ty})")
    print(f"    KOSPI 초과: {n_beat}/{len(valid)}년  "
          f"({n_beat/len(valid)*100:.0f}%)" if len(valid) > 0 else "")
    print(f"    평균 수익률: {avg_ret:+.2f}%  |  평균 초과: {avg_exc:+.2f}%p")
    print(f"    평균 Sharpe: {avg_sh:.2f}  |  평균 MDD: {avg_mdd:.2f}%")
    print(f"    누적 수익률: 전략 {cum_s:+.1f}%  vs  KOSPI {cum_k:+.1f}%")


# ── 메인 ──────────────────────────────────────────────────────────────────────
def main():
    import datetime
    today_year = datetime.date.today().year

    # ── [1] Exp-01, Roll7y-06: 기존 CSV 읽기 ────────────────────────────────
    exp01_csv  = ROOT / "expanding_fold01_annual.csv"
    roll06_csv = ROOT / "rolling7y_fold06_annual.csv"

    print("\n" + "="*70)
    print("📋  3개 모델 연간 성과 비교 (학습기간 제외 OOS)")
    print("="*70)

    # ── [2] Exp-01 기존 결과 출력 ───────────────────────────────────────────
    if exp01_csv.exists():
        df01 = pd.read_csv(exp01_csv)
        print(f"\n{'='*70}")
        print(f"📊  Exp-01 (Expanding fold_01)  OOS: 2013 ~ 2025  [학습: 2006~2012]")
        print(f"{'='*70}")
        print(f"{'연도':>4}  {'전략':>8}  {'KOSPI':>7}  {'초과':>8}  "
              f"{'Sharpe':>7}  {'MDD':>8}  {'거래일':>4}  {'판정':>2}")
        print(f"  {'─'*66}")
        for _, r in df01.iterrows():
            kospi  = r['KOSPI(%)']
            excess = r['초과(%p)']
            beat   = "✅" if excess > 0 else "❌"
            kospi_s  = f"{kospi:+.2f}%" if not np.isnan(kospi) else "  N/A"
            excess_s = f"{excess:+.2f}%p" if not np.isnan(excess) else "  N/A"
            print(f"{int(r['연도']):>4}  {r['전략(%)']:>+7.2f}%  {kospi_s:>7}  "
                  f"{excess_s:>8}  {r['Sharpe']:>7.2f}  "
                  f"{r['MDD(%)']:>7.2f}%  {int(r['거래일수']):>4}  {beat}")
        print(f"  {'─'*66}")
        print_summary(df01, "Exp-01", 2013, 2025)
    else:
        print("⚠️  expanding_fold01_annual.csv 없음 — analyze_fold_annual.py 먼저 실행")

    # ── [3] Roll7y-06 기존 결과 출력 ────────────────────────────────────────
    if roll06_csv.exists():
        df06 = pd.read_csv(roll06_csv)
        print(f"\n{'='*70}")
        print(f"📊  Roll7y-06 (Rolling7y fold_06)  OOS: 2018 ~ 2025  [학습: 2011~2017]")
        print(f"{'='*70}")
        print(f"{'연도':>4}  {'전략':>8}  {'KOSPI':>7}  {'초과':>8}  "
              f"{'Sharpe':>7}  {'MDD':>8}  {'거래일':>4}  {'판정':>2}")
        print(f"  {'─'*66}")
        for _, r in df06.iterrows():
            kospi  = r['KOSPI(%)']
            excess = r['초과(%p)']
            beat   = "✅" if excess > 0 else "❌"
            kospi_s  = f"{kospi:+.2f}%" if not np.isnan(kospi) else "  N/A"
            excess_s = f"{excess:+.2f}%p" if not np.isnan(excess) else "  N/A"
            print(f"{int(r['연도']):>4}  {r['전략(%)']:>+7.2f}%  {kospi_s:>7}  "
                  f"{excess_s:>8}  {r['Sharpe']:>7.2f}  "
                  f"{r['MDD(%)']:>7.2f}%  {int(r['거래일수']):>4}  {beat}")
        print(f"  {'─'*66}")
        print_summary(df06, "Roll7y-06", 2018, 2025)
    else:
        print("⚠️  rolling7y_fold06_annual.csv 없음 — analyze_fold_annual.py 먼저 실행")

    # ── [4] 프로덕션 모델: 2026 시뮬레이션 ──────────────────────────────────
    print(f"\n{'='*70}")
    print(f"📊  Production (unified)  OOS: 2026~  [학습: ~2025-12-31]")
    print(f"{'='*70}")

    print(f"\n[모델 로딩...]")
    prod_models = load_production_models()
    if not prod_models:
        print("❌ 프로덕션 모델 로드 실패")
        return

    from_y, to_y = 2026, today_year
    kospi_dict = get_kospi_annual(from_y, to_y)

    print(f"\n[가격 데이터 로딩 (품질필터 적용)...]")
    full_start = pd.Timestamp(f"{from_y}-01-01")
    full_end   = pd.Timestamp(f"{to_y}-12-31")
    price_pivot = sbm.load_prices(full_start, full_end, filter_bad=True)
    if price_pivot.empty:
        print("❌ 가격 데이터 없음")
        return
    print(f"   종목 수: {price_pivot.shape[1]:,}  거래일: {price_pivot.shape[0]}")

    df_prod = simulate_annual(prod_models, from_y, to_y, price_pivot, kospi_dict,
                              "Production (unified)")
    del prod_models; gc.collect()

    if not df_prod.empty:
        print_summary(df_prod, "Production", from_y, to_y)
        out = ROOT / "production_annual.csv"
        df_prod.to_csv(out, index=False, encoding='utf-8-sig')
        print(f"\n💾 저장: {out}")

    # ── [5] 3개 모델 통합 요약 비교 ─────────────────────────────────────────
    print(f"\n{'='*70}")
    print(" 3개 모델 요약 비교")
    print(f"{'='*70}")
    print(f"{'모델':<16}  {'OOS기간':<12}  {'연수':>4}  {'평균수익':>9}  "
          f"{'평균초과':>9}  {'KOSPI초과':>9}  {'평균Sh':>7}  {'평균MDD':>8}  "
          f"{'누적수익':>9}  {'누적KOSPI':>10}")
    print("-"*110)

    summaries = []
    if exp01_csv.exists():
        df = pd.read_csv(exp01_csv)
        summaries.append(("Exp-01", "2013~2025", df))
    if roll06_csv.exists():
        df = pd.read_csv(roll06_csv)
        summaries.append(("Roll7y-06", "2018~2025", df))
    if not df_prod.empty:
        summaries.append(("Production", f"2026~{to_y}", df_prod))

    for name, period, df in summaries:
        valid   = df.dropna(subset=['KOSPI(%)'])
        n_beat  = int((valid['초과(%p)'] > 0).sum()) if not valid.empty else 0
        n_valid = len(valid)
        n       = len(df)
        avg_ret = df['전략(%)'].mean()
        avg_exc = valid['초과(%p)'].mean() if not valid.empty else float('nan')
        avg_sh  = df['Sharpe'].mean()
        avg_mdd = df['MDD(%)'].mean()
        cum_s   = ((1 + df['전략(%)'] / 100).prod() - 1) * 100
        cum_k   = ((1 + valid['KOSPI(%)'] / 100).prod() - 1) * 100 if not valid.empty else float('nan')
        beat_str = f"{n_beat}/{n_valid}"
        print(f"{name:<16}  {period:<12}  {n:>4}년  {avg_ret:>+8.1f}%  "
              f"{avg_exc:>+8.1f}p  {beat_str:>9}  {avg_sh:>7.2f}  {avg_mdd:>7.1f}%  "
              f"{cum_s:>+8.1f}%  {cum_k:>+9.1f}%")


if __name__ == "__main__":
    main()