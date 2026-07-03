"""
analyze_full_range.py
=====================
각 모델을 전체 데이터 범위(2006~2026)에서 시뮬레이션하여
학습 전·중·후 구간 성과를 비교.

  Exp-01     : IS 2006~2012  /  OOS後 2013~2025
  Roll7y-06  : OOS前 2006~2010  /  IS 2011~2017  /  OOS後 2018~2025
  Production : OOS前 2006~2020  /  IS 2021~2025  /  OOS後 2026~

사용:
  python -X utf8 analyze_full_range.py
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

PROD_DIR  = ROOT / "Model"
FROM_YEAR = 2006
TO_YEAR   = 2026


# ── 모델별 구간 정의 ──────────────────────────────────────────────────────────
MODEL_CONFIGS = {
    "Exp-01": {
        "fold_dir":    ROOT / "walk_forward" / "fold_01",
        "loader":      "fold",
        "train_start": 2006,   # Expanding: 데이터 시작부터 학습
        "train_end":   2012,   # train_end = 2013-01-01 → 2012년까지 IS
        "cache_tag":   "exp01_full",
    },
    "Roll7y-06": {
        "fold_dir":    ROOT / "walk_forward_rolling7y" / "fold_06",
        "loader":      "fold",
        "train_start": 2011,   # 7y rolling: 2011~2017
        "train_end":   2017,
        "cache_tag":   "roll06_full",
    },
    "Production": {
        "fold_dir":    PROD_DIR,
        "loader":      "prod",
        "train_start": 2021,   # START_DT = 2021-01-04
        "train_end":   2025,   # END_DT   = 2025-12-31
        "cache_tag":   "prod_full",
    },
}


def period_label(year: int, train_start: int, train_end: int) -> str:
    if year < train_start:  return "OOS前"
    if year <= train_end:   return "IS"
    return "OOS後"


# ── 프로덕션 모델 로드 ────────────────────────────────────────────────────────
def load_production_models() -> dict:
    from tensorflow.keras.models import load_model as keras_load
    models  = {}
    best_f1 = {}
    for m_name, cfg in sbm.MODEL_SETTINGS.items():
        h5  = PROD_DIR / f"{m_name}_lstm_v3.h5"
        scl = PROD_DIR / f"{m_name}_lstm_v3.scaler"
        log = PROD_DIR / f"log_{m_name}_v3_v3_unified.csv"
        if not h5.exists() or not scl.exists():
            continue
        with open(scl, 'rb') as f:
            scaler = pickle.load(f)
        model     = keras_load(str(h5), compile=False)
        actual_lb = model.input_shape[1]
        if log.exists():
            ldf             = pd.read_csv(log)
            best_row        = ldf.loc[ldf['f1'].idxmax()]
            threshold       = float(best_row['threshold'])
            best_f1[m_name] = float(best_row['f1'])
        else:
            threshold = cfg["thr"]
        models[m_name] = {"model": model, "scaler": scaler,
                          "lookback": actual_lb, "threshold": threshold,
                          "weight": cfg["weight"], "type": cfg["type"]}
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


# ── 단일 모델 전체 기간 시뮬레이션 ───────────────────────────────────────────
def run_model(name: str, cfg: dict, price_pivot, kospi_dict: dict) -> pd.DataFrame:
    train_s, train_e = cfg['train_start'], cfg['train_end']
    print(f"\n{'='*72}")
    print(f"📊  {name}  |  OOS前 ~{train_s-1}  /  IS {train_s}~{train_e}  /  OOS後 {train_e+1}~")
    print(f"{'='*72}")

    print("\n[1] 모델 로딩...")
    models = load_production_models() if cfg["loader"] == "prod" else sbm.load_fold_models(cfg["fold_dir"])
    if not models:
        print("  ❌ 모델 로드 실패"); return pd.DataFrame()

    cache_path = ROOT / f"cache_{cfg['cache_tag']}_{FROM_YEAR}_{TO_YEAR}_scores.pkl"
    full_start = pd.Timestamp(f"{FROM_YEAR}-01-01")
    full_end   = pd.Timestamp(f"{TO_YEAR}-12-31")

    print(f"\n[2] 스코어 계산 ({FROM_YEAR}~{TO_YEAR})...")
    t0 = time.time()
    score_df_all = sbm.compute_scores(models, full_start, full_end, cache_path, no_cache=False)
    del models; gc.collect()
    if score_df_all.empty:
        print("  ❌ 스코어 없음"); return pd.DataFrame()
    score_df_all['date'] = pd.to_datetime(score_df_all['date'])
    print(f"   총 레코드: {len(score_df_all):,}건  ({time.time()-t0:.0f}초)")

    print(f"\n[3] 연도별 시뮬레이션...\n")
    print(f"  {'구간':>5}  {'연도':>4}  {'전략':>8}  {'KOSPI':>7}  {'초과':>9}  "
          f"{'Sharpe':>7}  {'MDD':>8}  {'거래일':>4}  {'판정':>2}")
    print(f"  {'─'*73}")

    rows = []
    for year in range(FROM_YEAR, TO_YEAR + 1):
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
        plabel = period_label(year, train_s, train_e)

        kospi_s  = f"{kospi:+.2f}%" if not np.isnan(kospi) else "  N/A "
        excess_s = f"{excess:+.2f}%p" if not np.isnan(excess) else "   N/A "

        print(f"  {plabel:>5}  {year:>4}  {ret:>+7.2f}%  {kospi_s:>7}  "
              f"{excess_s:>9}  {stats['Sharpe']:>7.2f}  "
              f"{stats['최대낙폭(%)']:>7.2f}%  {stats['거래일수']:>4}  {beat}")

        rows.append({
            "구간":     plabel,
            "연도":     year,
            "전략(%)":  ret,
            "KOSPI(%)": kospi,
            "초과(%p)": excess,
            "Sharpe":   stats['Sharpe'],
            "MDD(%)":   stats['최대낙폭(%)'],
            "거래일수": stats['거래일수'],
        })

    print(f"  {'─'*73}")
    return pd.DataFrame(rows)


# ── 구간별 요약 ───────────────────────────────────────────────────────────────
def print_segment_summary(df: pd.DataFrame, model_name: str):
    if df.empty: return
    print(f"\n  ▶ [{model_name}] 구간별 요약")
    hdr = (f"  {'구간':>5}  {'연수':>4}  {'평균수익':>9}  {'평균초과':>9}  "
           f"{'KOSPI초과':>9}  {'평균Sh':>7}  {'평균MDD':>8}  {'누적수익':>9}  {'누적KOSPI':>10}")
    print(hdr)
    print(f"  {'─'*87}")

    for seg in ["OOS前", "IS", "OOS後"]:
        part = df[df['구간'] == seg]
        if part.empty: continue
        valid    = part.dropna(subset=['KOSPI(%)'])
        n_beat   = int((valid['초과(%p)'] > 0).sum()) if not valid.empty else 0
        n        = len(part)
        avg_ret  = part['전략(%)'].mean()
        avg_exc  = valid['초과(%p)'].mean() if not valid.empty else float('nan')
        avg_sh   = part['Sharpe'].mean()
        avg_mdd  = part['MDD(%)'].mean()
        cum_s    = ((1 + part['전략(%)'] / 100).prod() - 1) * 100
        cum_k    = ((1 + valid['KOSPI(%)'] / 100).prod() - 1) * 100 if not valid.empty else float('nan')
        beat_str = f"{n_beat}/{len(valid)}"
        cum_k_s  = f"{cum_k:>+9.1f}%" if not np.isnan(cum_k) else "      N/A"
        print(f"  {seg:>5}  {n:>4}년  {avg_ret:>+8.1f}%  {avg_exc:>+8.1f}p  "
              f"{beat_str:>9}  {avg_sh:>7.2f}  {avg_mdd:>7.1f}%  "
              f"{cum_s:>+8.1f}%  {cum_k_s}")


# ── 메인 ──────────────────────────────────────────────────────────────────────
def main():
    print("\n" + "="*72)
    print("🔍  3개 모델 학습 전·중·후 전체 기간 시뮬레이션")
    print(f"    대상 기간: {FROM_YEAR} ~ {TO_YEAR}  |  품질필터 ON")
    print("="*72)

    print(f"\n[공통] 가격 데이터 로딩 ({FROM_YEAR}~{TO_YEAR}, 품질필터)...")
    price_pivot = sbm.load_prices(
        pd.Timestamp(f"{FROM_YEAR}-01-01"),
        pd.Timestamp(f"{TO_YEAR}-12-31"),
        filter_bad=True
    )
    if price_pivot.empty:
        print("❌ 가격 데이터 없음"); return
    print(f"   종목 수: {price_pivot.shape[1]:,}  전체 거래일: {price_pivot.shape[0]}")

    kospi_dict = get_kospi_annual(FROM_YEAR, TO_YEAR)

    results = {}
    for name, cfg in MODEL_CONFIGS.items():
        df = run_model(name, cfg, price_pivot, kospi_dict)
        if not df.empty:
            results[name] = df
            print_segment_summary(df, name)
            out = ROOT / f"fullrange_{name.lower().replace(' ','_').replace('-','').replace('7','7')}_annual.csv"
            df.to_csv(out, index=False, encoding='utf-8-sig')
            print(f"\n  💾 저장: {out}")

    # ── 최종 통합 비교표 ─────────────────────────────────────────────────────
    print(f"\n\n{'='*72}")
    print(" 최종 통합 요약  (IS=학습기간  OOS前=학습전  OOS後=학습후)")
    print(f"{'='*72}")
    print(f"  {'모델':<14}  {'구간':>5}  {'연수':>4}  {'평균수익':>9}  {'평균초과':>9}  "
          f"{'KOSPI초과':>9}  {'평균Sh':>7}  {'평균MDD':>8}  {'누적수익':>9}")
    print(f"  {'─'*92}")

    for name, df in results.items():
        first = True
        for seg in ["OOS前", "IS", "OOS後"]:
            part = df[df['구간'] == seg]
            if part.empty: continue
            valid    = part.dropna(subset=['KOSPI(%)'])
            n_beat   = int((valid['초과(%p)'] > 0).sum()) if not valid.empty else 0
            n        = len(part)
            avg_ret  = part['전략(%)'].mean()
            avg_exc  = valid['초과(%p)'].mean() if not valid.empty else float('nan')
            avg_sh   = part['Sharpe'].mean()
            avg_mdd  = part['MDD(%)'].mean()
            cum_s    = ((1 + part['전략(%)'] / 100).prod() - 1) * 100
            beat_str = f"{n_beat}/{len(valid)}"
            mname    = name if first else ""
            first    = False
            print(f"  {mname:<14}  {seg:>5}  {n:>4}년  {avg_ret:>+8.1f}%  "
                  f"{avg_exc:>+8.1f}p  {beat_str:>9}  {avg_sh:>7.2f}  "
                  f"{avg_mdd:>7.1f}%  {cum_s:>+8.1f}%")
        print(f"  {'─'*92}")


if __name__ == "__main__":
    main()