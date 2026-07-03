# -*- coding: utf-8 -*-
"""
sell_strategy_b.py — B 매도전략 (전량청산 결정 엔진)  [평활 방식 + 자동 backfill]
=====================================================
규칙:
  · 3일 평활 점수(최근 3 거래일 raw 점수 이동평균) < SELL_THRESH 가
    CONFIRM_DAYS 일 연속이면 → 전량청산
  · 현재가가 평단(API 잔고 평균매입단가) 대비 STOP_PCT 이하(예: -12%)면 → 즉시 전량청산
    단, total_score(raw) 가 STOP_SCORE_KEEP(0.50) 이상이면 손절 면제(강한 종목은 홀드)
교차일(cross-day) 상태를 logs/sell_state_b.json 에 영속 (재시작/재부팅 생존).

  ※ 2026-06-30 시뮬 결과 '평활<0.2·2일연속'(①) 최고 강건 → raw→평활 전환.
  ※ 2026-07-02 startup 자동 backfill 추가: 리셋/재부팅으로 hist 가 비면
    일별 Stock_V3.csv 로그(최근 며칠)에서 평활 이력을 자동 복원.

파라미터: SELL_THRESH=0.20, CONFIRM_DAYS=2, SMOOTH_N=3, STOP_PCT=-0.12, STOP_SCORE_KEEP=0.50
"""
import json, os, re

STATE_FILE   = r"C:\Projects\RealtimeMonitor\logs\sell_state_b.json"
LOG_DIR      = r"C:\Projects\RealtimeMonitor\logs"
SELL_THRESH  = 0.20    # 청산 점수 임계 (평활 점수 기준)
CONFIRM_DAYS = 2       # 연속 청산구간 확인일수
SMOOTH_N     = 3       # 평활 기간(거래일)
STOP_PCT     = -0.12   # 가격 손절 (-12%)
STOP_SCORE_KEEP = 0.50 # 손절 면제 임계: total_score(raw)가 이 값 이상이면 -12%라도 청산 안 함
BACKFILL_DAYS = 6      # startup backfill 시 참고할 최근 일별 로그 수


def _load():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


_STATE = _load()


def _sm(vals):
    """마지막 SMOOTH_N개 평균."""
    w = vals[-SMOOTH_N:]
    return sum(w) / len(w)


def backfill_from_logs(today):
    """hist 가 비어있는 종목을 일별 Stock_V3.csv 로그(today 제외 최근일)로 복원.
    이미 hist 가 있는 종목은 건드리지 않음(라이브 누적 보존). 실패해도 조용히 무시."""
    try:
        import pandas as pd
        paths = []
        for pat in (os.path.join(LOG_DIR, "*_Stock_V3.csv"),
                    os.path.join(LOG_DIR, "??????", "*_Stock_V3.csv")):
            import glob
            paths += glob.glob(pat)
        dated = []
        for p in paths:
            m = re.search(r"(\d{8})_Stock_V3\.csv", os.path.basename(p))
            if m and m.group(1) < today:
                dated.append((m.group(1), p))
        if not dated:
            return
        dated.sort()
        recent = dated[-BACKFILL_DAYS:]
        days, day_scores = [], {}
        for d, p in recent:
            try:
                df = pd.read_csv(p, encoding='utf-8-sig', dtype={'code': str},
                                 usecols=['code', 'score_total'],
                                 on_bad_lines='skip').dropna()
                day_scores[d] = {str(c).split('.')[0].zfill(6): float(s)
                                 for c, s in zip(df['code'], df['score_total'])}
                days.append(d)
            except Exception:
                pass
        if not days:
            return
        days.sort()
        last_day = days[-1]
        codes = set()
        for d in days:
            codes |= set(day_scores[d])
        filled = 0
        for code in codes:
            e = _STATE.get(code)
            if e and e.get("hist"):        # 이미 이력 있음 → 보존
                continue
            raws = [day_scores[d][code] for d in days if code in day_scores[d]]
            if len(raws) < 2:
                continue
            prior = raws[:-1]
            bd = 0
            for i in range(len(prior), 0, -1):
                w = prior[max(0, i - SMOOTH_N):i]
                if sum(w) / len(w) < SELL_THRESH:
                    bd += 1
                else:
                    break
            _STATE[code] = {"day": last_day, "hist": raws[-SMOOTH_N:-1],
                            "below_days": bd, "last_raw": raws[-1],
                            "last_smoothed": round(_sm(raws), 6)}
            filled += 1
        if filled:
            print(f"   [sell_strategy_b] 평활 이력 backfill: {filled}종목 (기준 {last_day})")
    except Exception as ex:
        print(f"   ⚠️ sell_strategy_b backfill 실패(무시): {ex}")


# startup 시 hist 없는 종목 자동 복원
try:
    from datetime import datetime as _dt
    backfill_from_logs(_dt.now().strftime("%Y%m%d"))
except Exception:
    pass


def _roll_day(e, today):
    """날짜가 바뀌었으면: 어제 최종 평활점수로 연속일수 갱신 + 어제 raw점수 이력 이월."""
    if e.get("day") == today:
        return
    ls = e.get("last_smoothed")
    if ls is not None and ls < SELL_THRESH:
        e["below_days"] = e.get("below_days", 0) + 1
    else:
        e["below_days"] = 0
    if e.get("last_raw") is not None:
        e["hist"] = (e.get("hist", []) + [e["last_raw"]])[-(SMOOTH_N - 1):]
    e["day"] = today


def decide(code, raw_score, cur_price, avg_price, today):
    """
    전량청산 여부 결정.
    반환: (full_sell: bool, smoothed: float, reason: str)  reason ∈ {"", "score", "stop12"}
    """
    e = _STATE.setdefault(code, {"day": today, "hist": [], "below_days": 0,
                                 "last_raw": None, "last_smoothed": None})
    _roll_day(e, today)

    hist = e.get("hist", [])
    smoothed = (sum(hist) + raw_score) / (len(hist) + 1)
    e["last_raw"]      = raw_score
    e["last_smoothed"] = smoothed
    e["day"]           = today

    # 가격 손절: API 잔고 평균매입단가(avg_price) 대비 현재가가 STOP_PCT 이하일 때.
    #   단, total_score(raw_score) 가 STOP_SCORE_KEEP 이상이면 손절 면제(강한 종목 홀드).
    if (avg_price and avg_price > 0 and cur_price and cur_price > 0
            and raw_score < STOP_SCORE_KEEP):
        if (cur_price - avg_price) / avg_price <= STOP_PCT:
            return True, smoothed, "stop12"

    # 점수 청산: 평활<thr 가 오늘 포함 CONFIRM_DAYS 연속
    if smoothed < SELL_THRESH and (e.get("below_days", 0) + 1) >= CONFIRM_DAYS:
        return True, smoothed, "score"

    return False, smoothed, ""


def clear(code):
    _STATE.pop(code, None)


def persist():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(_STATE, f)
    except Exception as ex:
        print(f"   ⚠️ sell_state_b 저장 실패: {ex}")
