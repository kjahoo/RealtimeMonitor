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
#   · (2026-11-09) 아래 시각 상수는 '평소' 기준. 수능일 등 KRX 지연 개장일(kiwoom_trading.KRX_LATE_DAYS,
#     정규장 10:00~16:30)은 _krx_hms() 로 +1시간 순연해 쓴다 — 마커가 09시대로 찍혀도(구 scheduler)
#     FLOOR(10:00:30) 가 장전 행을 막는다.
KRX_END = "15:30:59"
KRX_SINCE_DEFAULT = "09:01:00"
KRX_SINCE_FLOOR = "09:00:30"
RESCORE_READY_RATIO = 0.8   # 40점+ 보조모드 판정 전, 오늘 Stock_V3 의 이 비율 이상이 재평가돼야 함


def _krx_hms(hms, day=None):
    """평소 기준 'HH:MM:SS' → 그날 KRX 시각(수능일 등 KRX_LATE_DAYS 는 +1시간). 실패 시 그대로."""
    try:
        from datetime import datetime
        from kis_api import kiwoom_trading as kt
        return kt.krx_t(datetime.strptime(hms, "%H:%M:%S").time(), day).strftime("%H:%M:%S")
    except Exception:
        return hms


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
    """정규장 재평가 경계 시각 'HH:MM:SS' (이 시각 이후 계산된 점수만 매수에 쓴다).
       수능일 등 KRX 지연 개장일은 기본값·하한이 10:01:00·10:00:30."""
    since = _krx_hms(KRX_SINCE_DEFAULT, day)
    try:
        with open(krx_session_path(log_dir, day), encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") == day and d.get("since"):
            since = str(d["since"])
    except Exception:
        pass
    return max(since, _krx_hms(KRX_SINCE_FLOOR, day))


def krx_end(day=None):
    """그날 정규장 재평가 행의 마지막 시각 'HH:MM:SS' — 평소 15:30:59, 수능일 16:30:59."""
    return _krx_hms(KRX_END, day)


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


def rescored_rows(rows, since, day=None):
    """정규장 재평가된 행만: since ≤ time ≤ 15:30:59 (수능일 등 KRX 지연일 16:30:59). day 생략 = 오늘."""
    end = krx_end(day)
    return {c: r for c, r in rows.items() if since <= r.get("time", "") <= end}


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
    if now_hhmmss < _krx_hms(AWAIT_DEADLINE) and awaiting_primary(rows, rescored):   # 수능일 10:15:00
        return False
    if len(rescored) < RESCORE_READY_RATIO * len(rows):
        return False
    return low_mode([r["score100"] / 100.0 for r in rescored.values()])


def latest_evals(log_dir, day, include_today=False, max_files=None):
    """종목별 가장 최근 AI 평가 {code(6): {"date": YYYYMMDD, "rec": BUY|HOLD|AVOID, "score": int}} (2026-11-11).
       logs/{날짜}_claude_results_all.json(누적 평가풀)을 최신 날짜부터 훑어 종목별 첫 발견(=가장 최근)만 채택.
       include_today=False 면 day 이전 날짜만(= '직전 평가'). 같은 파일 안의 중복 코드는 뒤(나중 평가)가 우선.
       include_today=True 면 오늘 results.json(미승격 평가)도 가장 먼저 반영."""
    import glob
    out = {}

    def _take(items, d):
        seen = {}
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            c = str(it.get("code", "")).strip().split(".")[0]
            c = c.zfill(6) if c.isdigit() else c
            if c:
                seen[c] = it                     # 뒤가 우선
        for c, it in seen.items():
            if c not in out:
                try:
                    sc = int(float(it.get("claude_score", 0) or 0))
                except Exception:
                    sc = 0
                out[c] = {"date": d, "rec": str(it.get("recommendation", "")).upper(), "score": sc}

    def _load(path):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return d.get("results", d) if isinstance(d, dict) else d
        except Exception:
            return []

    if include_today:
        _take(_load(os.path.join(log_dir, f"{day}_claude_results.json")), day)
    files = sorted(glob.glob(os.path.join(log_dir, "2*_claude_results_all.json")), reverse=True)
    n = 0
    for p in files:
        d = os.path.basename(p)[:8]
        if d > day or (d == day and not include_today):
            continue
        n += 1
        if max_files and n > max_files:
            break
        _take(_load(p), d)
    return out
