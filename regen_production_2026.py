# -*- coding: utf-8 -*-
"""
regen_production_2026.py
========================
Production 모델의 2026 점수 캐시(scores_production_2026.pkl)를 WF 모델과
'동일한 기간'으로 재생성한다.

배경:
  - WF 모델(exp/roll)은 feather(_prep_wf_v3, 2026-05-29까지)를 쓰지만
    Production 은 raw CSV(Data/Stock, 더 최신: ~06-09)를 써서 2026 기간이 더 길다.
  - 공정 비교를 위해 Production 2026 도 WF 와 같은 마지막 거래일까지만 계산한다.

방법:
  - holdout_comparison.py 의 '정확히 같은' 함수(load_tf_models / compute_scores_year)를
    재사용해 계산 일관성을 보장.
  - 모듈 전역 TODAY 만 공통 종료일로 monkey-patch → compute_scores_year 의 2026
    y_end = min(TODAY+1, ...) 가 그 날짜로 잘린다.
  - 기존 캐시는 .bak_full 로 백업 후 덮어씀.

실행 (시뮬 완료 후, GPU):
  conda run -n trading_env --no-capture-output python -u regen_production_2026.py
"""
import sys
import pandas as pd
from pathlib import Path

import holdout_comparison as hc   # 주의: import 시 TF/GPU 초기화됨 (완료 후 실행할 것)


def detect_cutoff() -> pd.Timestamp:
    """WF 모델들의 2026 캐시 max date 중 최소값 = 공통 종료일."""
    maxes = []
    for p in hc.CACHE_DIR.glob("scores_*_2026.pkl"):
        tag = p.stem[len("scores_"):-5]
        if tag == "production":
            continue
        try:
            maxes.append(pd.to_datetime(pd.read_pickle(str(p))['date']).max())
        except Exception:
            pass
    if not maxes:
        raise SystemExit("❌ WF 2026 캐시가 없어 공통 종료일을 정할 수 없음")
    return min(maxes)


def main():
    prod_h5 = hc.PROD_DIR / "target1_lstm_v3.h5"
    if not prod_h5.exists():
        raise SystemExit(f"❌ Production 모델 없음: {hc.PROD_DIR}")

    cutoff = detect_cutoff()
    print(f"[regen] 공통 2026 종료일(WF 기준): {cutoff.date()}")

    cache_path = hc.CACHE_DIR / "scores_production_2026.pkl"
    if cache_path.exists():
        cur_max = pd.to_datetime(pd.read_pickle(str(cache_path))['date']).max()
        print(f"[regen] 기존 production 2026 종료일: {cur_max.date()}")
        if cur_max <= cutoff:
            print("[regen] 이미 공통 종료일 이내 — 재생성 불필요. 종료.")
            return
        bak = cache_path.with_suffix(".pkl.bak_full")
        cache_path.replace(bak)
        print(f"[regen] 기존 캐시 백업 → {bak.name}")

    # 핵심: TODAY 를 공통 종료일로 바꿔 2026 y_end 를 cutoff 로 제한
    hc.TODAY = pd.Timestamp(cutoff)

    print("[regen] Production 모델 로딩...")
    tf_models = hc.load_tf_models(hc.PROD_DIR, is_prod=True)
    if not tf_models:
        raise SystemExit("❌ Production 서브모델 로드 실패")

    print("[regen] 2026 점수 재계산 (raw CSV, cutoff 적용)...")
    score_df = hc.compute_scores_year(
        tf_models, hc.PROD_FEATURES, 2026, True, cache_path, no_cache=True
    )
    if score_df is None or score_df.empty:
        raise SystemExit("❌ 재계산 결과 비어 있음")

    print(f"[regen] ✅ 완료: {score_df['date'].min().date()} ~ "
          f"{score_df['date'].max().date()}  거래일 {score_df['date'].nunique()}  "
          f"행 {len(score_df):,}")
    print(f"[regen] 저장: {cache_path}")


if __name__ == "__main__":
    main()
