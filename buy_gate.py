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
import csv
import json
import os

PRIMARY_MIN = 0.60
LOW_MIN = 0.40
LOW_ALLOC_PCT = 5.0
LOW_SIGNAL = "40+자동등록"

# ── 정규장 재평가 규칙 (2026-09-29) ─────────────────────────────────────
#   장전 NXT 점수는 정규장에만 있는 입력변수(프로그램매매 등)가 비어 부풀려질 수 있다
#   (2026-09-29 STX엔진: 장전 77 → 정규장 33.7 인데 09:01 매수). 그래서 매수 판단·비중은
#   '정규장 재평가 점수'로만 한다 = 오늘 Stock_V3 행 중 KRX 봇 기동 경계(since) 이후 계산된 행.
#   · Stock_V3.time 은 main_stock·Update_Promising 모두 '점수 계산 시각'이고, main_stock 병합은
#     time 이 늦은 행을 채택하므로 재기동 때 되살아난 장전 행은 time<since 로 남아 자동 제외된다.
#   · Search_History.timestamp 는 자동등록·평가후처리가 장전 점수에 현재시각을 찍을 수 있어 쓰지 않는다.
#   · since = scheduler 가 NXT 봇 종료 직후 KRX 봇 기동 시 기록(logs/{날짜}_krx_session.json).
#     마커가 없으면 09:01:00 (NXT 봇이 09:00 직후 ~20초 더 쓸 수 있어 여유).
KRX_END = "15:30:59"
KRX_SINCE_DEFAULT = "09:01:00"
KRX_SINCE_FLOOR = "09:00:30"
RESCORE_READY_RATIO = 0.8   # 40점+ 보조모드 판정 전, 오늘 Stock_V3 의 이 비율 이상이 재평가돼야 함


def primary_count(scores):
    """60점+ 종목 수."""
    return sum(1 for s in scores if s is not None and s >= PRIMARY_MIN)


def low_mode(scores):
    """60점+ 가 하나도 없으면 True. 점수 목록이 비어 있으면(아직 스캔 전 등) 판단 불가 → False."""
    vals = [s for s in scores if s is not None]
    return bool(vals) and primary_count(vals) == 0


def krx_session_path(log_dir, day):
    return os.path.join(log_dir, f"{day}_krx_session.json")


def krx_since(log_dir, day):
    """정규장 재평가 경계 시각 'HH:MM:SS' (이 시각 이후 계산된 점수만 매수에 쓴다)."""
    since = KRX_SINCE_DEFAULT
    try:
        with open(krx_session_path(log_dir, day), encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") == day and d.get("since"):
            since = str(d["since"])
    except Exception:
        pass
    return max(since, KRX_SINCE_FLOOR)


def load_v3_rows(log_dir, day):
    """오늘자 Stock_V3 → {code(6): {"score100", "price", "name", "time"}} (time = 점수 계산 시각)."""
    path = os.path.join(log_dir, f"{day}_Stock_V3.csv")
    rows = {}
    if not os.path.exists(path):
        return rows
    try:
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                c = str(r.get("code", "")).strip().split(".")[0]
                if not c:
                    continue
                c = c.zfill(6) if c.isdigit() else c
                try:
                    sc = float(r.get("score_total", 0) or 0) * 100.0
                except Exception:
                    sc = 0.0
                try:
                    px = int(float(r.get("close_price", 0) or 0))
                except Exception:
                    px = 0
                t = (r.get("time") or "").strip()
                prev = rows.get(c)
                if prev is not None and prev["time"] > t:   # 같은 코드 중복행이면 최신 계산시각 행 채택
                    continue
                rows[c] = {"score100": sc, "price": px,
                           "name": (r.get("name") or "").strip(), "time": t}
    except Exception as e:
        print(f"   ⚠️ Stock_V3 로드 실패: {e}")
    return rows


def rescored_rows(rows, since):
    """정규장 재평가된 행만: since ≤ time ≤ 15:30:59."""
    return {c: r for c, r in rows.items() if since <= r.get("time", "") <= KRX_END}


def awaiting_primary(rows, rescored):
    """장전에 60점+ 였지만 아직 정규장 재평가 전인 종목 코드 목록."""
    return [c for c, r in rows.items()
            if c not in rescored and r.get("score100", 0) >= PRIMARY_MIN * 100]


AWAIT_DEADLINE = "09:15:00"   # 이 시각 이후엔 재평가 안 된 장전 60점+ 를 무시(거래정지 등으로 영영 재계산 안 되는 종목이 보조모드를 종일 막지 않게)


def low_mode_ready(rows, rescored, now_hhmmss=None):
    """40점+ 보조모드 판정 — 아래를 모두 만족할 때만 True.
       ① 정규장 재평가 60점+ 가 0개  ② 장전 60점+ 중 재평가 대기 종목 없음(AWAIT_DEADLINE 전까지만 적용)
       ③ 오늘 Stock_V3 의 RESCORE_READY_RATIO 이상이 재평가 완료(첫 정규장 스캔이 거의 끝남)."""
    if not rows or not rescored:
        return False
    if now_hhmmss is None:
        from datetime import datetime
        now_hhmmss = datetime.now().strftime("%H:%M:%S")
    if now_hhmmss < AWAIT_DEADLINE and awaiting_primary(rows, rescored):
        return False
    if len(rescored) < RESCORE_READY_RATIO * len(rows):
        return False
    return low_mode([r["score100"] / 100.0 for r in rescored.values()])
