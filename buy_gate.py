# -*- coding: utf-8 -*-
"""
buy_gate.py — 매수 진입 게이트 공용 규칙
========================================
기본: 60점+(score_total ≥ 0.60) 종목, 구간 비중 5~25% (auto_buy._alloc_pct)
보조: 그 시점 60점+ 종목이 하나도 없으면 → 40점+ 종목을 종목당 5%까지 매수 (보유 여부 무관)
      Claude 평가(BUY 판정)·plan·호가 sweep 집행·매도규칙은 60점+ 와 완전히 동일.
      2026-09-14 도입. 근거: regime_analysis/low_gate_test.py L40_nocand
      (prod OOS 2016~2026: CAGR 22.6→27.0%, MDD 동일 −41.2%, 11년 중 8년 우위)

공유처
  claude_eval_pipeline.write_handoff  평가 대상(pending) · promising 자동등록(LOW_SIGNAL)
  auto_buy._build_plan                정규장 점수로 보조모드 판정 → 40점+ BUY 종목 5% 목표, 종목별 min_score 기록
  execution_monitor._tick             매수 직전 재확인 임계 = plan 의 min_score
  Update_Promising_Stocks             LOW_SIGNAL 행은 LOW_MIN 미만일 때만 정리, plan 편입 종목은 보존
"""
PRIMARY_MIN = 0.60
LOW_MIN = 0.40
LOW_ALLOC_PCT = 5.0
LOW_SIGNAL = "40+자동등록"


def primary_count(scores):
    """60점+ 종목 수."""
    return sum(1 for s in scores if s is not None and s >= PRIMARY_MIN)


def low_mode(scores):
    """60점+ 가 하나도 없으면 True. 점수 목록이 비어 있으면(아직 스캔 전 등) 판단 불가 → False."""
    vals = [s for s in scores if s is not None]
    return bool(vals) and primary_count(vals) == 0
