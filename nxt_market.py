# -*- coding: utf-8 -*-
"""
nxt_market.py — NXT 시장 평균 등락률(2026-11-11)
─────────────────────────────────────────────────────────────────────
NXT 거래 가능 전 종목(키움 ka10099 nxtEnable=Y)의 '오늘 KRX 종가 대비 NXT 현재가' 등락률을
시가총액(NXT 현재가 × 상장주식수) 가중 평균한다. execution_monitor 의 NXT 애프터 매수 기준.

  · 오늘 KRX 종가 = 종목별 1분봉 15:30 봉(kt.fetch_krx_close) — 장후 KRX 현재가는 애프터마켓 체결가라 쓰지 않음.
    603종목 × 1회 → 약 3~4분. 하루 1회 백그라운드 스레드로 받아 logs/{날짜}_krx_close_nxt.json 에 저장(재시작해도 이어받음).
  · NXT 현재가 = ka10095(관심종목정보) 100종목씩 묶음 조회(_NX) → 몇 초. market_avg() 가 max_age 초마다 갱신.
  · 종가를 받은 종목 비율이 COVERAGE_MIN 미만이면 None(아직 계산 불가).
"""
import os
import json
import time
import threading
from datetime import datetime

from config import secrets
from kis_api import kiwoom_trading as kt

LOG_DIR = secrets.LOCAL_DATA_PATH
COVERAGE_MIN = 0.95          # 종가 확보 비율이 이 이상이어야 평균 산출
CLOSE_GAP_SEC = 0.35         # 종가 조회 간격(초) — 유량(5req/s)을 execution_monitor 와 나눠 씀

_lock = threading.Lock()
_st = {"date": "", "univ": None, "closes": {}, "thread": None, "avg": None, "ts": 0.0, "n": 0, "info": ""}


def _univ_path(d):  return os.path.join(LOG_DIR, f"{d}_nxt_universe.json")
def _close_path(d): return os.path.join(LOG_DIR, f"{d}_krx_close_nxt.json")


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _load_universe(d):
    """{code6: 상장주식수} — NXT 거래 가능 종목. 하루 1회 조회 후 파일 캐시."""
    u = _load(_univ_path(d), None)
    if isinstance(u, dict) and u:
        return {k: int(v) for k, v in u.items()}
    from kis_api import kiwoom_inquiry as ki
    lst = []
    for m in ("0", "10"):
        part = []
        for _ in range(3):                    # 실패(429·시간초과 등) 시 빈/부분 목록 → 재시도
            part = ki.fetch_stock_list(m) or []
            if part:
                break
            time.sleep(1.0)
        if not part:
            print(f"   ⚠️ NXT 종목 목록 조회 실패(시장 {m}) — 저장 안 함, 재시도 예정")
            return {}                         # fail-closed: 한 시장이라도 비면 평균 계산 안 함
        lst += part
        time.sleep(0.3)
    u = {x["code"]: int(x.get("listCount") or 0) for x in lst
         if str(x.get("nxtEnable", "")).upper() == "Y" and x.get("code") and int(x.get("listCount") or 0) > 0}
    # 부분 목록 방지: 직전 거래일 목록 대비 80% 미만이면 저장하지 않음(페이지 누락 의심)
    import glob
    prev = sorted(p for p in glob.glob(os.path.join(LOG_DIR, "*_nxt_universe.json"))
                  if os.path.basename(p)[:8] < d)
    if prev:
        pu = _load(prev[-1], {}) or {}
        if pu and len(u) < len(pu) * 0.8:
            print(f"   ⚠️ NXT 종목 목록 {len(u)}개 < 직전 {len(pu)}개×80% — 부분 조회 의심, 저장 안 함")
            return {}
    if u:
        _save(_univ_path(d), u)
    return u


def _worker(d):
    try:
        univ = _load_universe(d)
        with _lock:
            _st["univ"] = univ
        closes = {k: int(v) for k, v in (_load(_close_path(d), {}) or {}).items() if int(v or 0) > 0}
        todo = [c for c in univ if c not in closes]
        for i, c in enumerate(todo, 1):
            px = kt.fetch_krx_close(c, d)
            if px > 0:
                closes[c] = px
            if i % 50 == 0 or i == len(todo):
                _save(_close_path(d), closes)
                with _lock:
                    _st["closes"] = dict(closes)
            time.sleep(CLOSE_GAP_SEC)
        with _lock:
            _st["closes"] = dict(closes)
    except Exception as e:
        print(f"   ⚠️ NXT 시장 평균 종가 수집 오류: {e}")


def ensure_started(d=None):
    """오늘 유니버스·종가 수집 스레드 시작(이미 진행/완료면 무동작). KRX 마감(+30초) 후에만."""
    d = d or datetime.now().strftime("%Y%m%d")
    now = datetime.now()
    ct = kt.krx_close_time(d)
    if now.strftime("%Y%m%d") == d and now.time() < datetime(2000, 1, 1, ct.hour, ct.minute, 30).time():
        return
    with _lock:
        if _st["date"] != d:
            _st.update(date=d, univ=None, closes={}, thread=None, avg=None, ts=0.0, n=0, info="")
        th = _st["thread"]
        if th is not None and (th.is_alive() or (_st["univ"] and len(_st["closes"]) >= len(_st["univ"]))):
            return
        if th is not None and not th.is_alive():
            # 끝났는데 목록 실패·종가 일부 실패 → 재시도(60초 간격)
            if time.time() - _st.get("retry_ts", 0) < 60:
                return
        _st["retry_ts"] = time.time()
        t = threading.Thread(target=_worker, args=(d,), daemon=True)
        _st["thread"] = t
        t.start()


def market_avg(d=None, max_age=60):
    """(평균 등락률(소수, 예 -0.0021), 계산 종목수, 설명) — 계산 불가면 (None, 0, 사유).
       max_age 초 이내 계산값은 재사용."""
    d = d or datetime.now().strftime("%Y%m%d")
    ensure_started(d)
    with _lock:
        univ, closes = _st["univ"], dict(_st["closes"])
        if _st["date"] == d and _st["avg"] is not None and time.time() - _st["ts"] < max_age:
            return _st["avg"], _st["n"], _st["info"]
    if not univ:
        return None, 0, "NXT 종목 목록 수집 중"
    cov = len(closes) / len(univ)
    if cov < COVERAGE_MIN:
        return None, 0, f"오늘 종가 수집 중 {len(closes)}/{len(univ)}"
    q = kt.fetch_quotes_multi([c for c in univ if c in closes], suffix="_NX")
    # KRX 마감(15:30, 수능일 16:30) 이후 NXT 에서 실제 체결된 종목만 — 장후 미체결 종목은 15:20 이전 NXT 가격이
    #   남아 있어 종가 단일가와의 차이가 평균을 왜곡한다(2026-11-11 검토 반영)
    close_hms = kt.krx_close_time(d).strftime("%H%M%S")
    num = den = 0.0
    n = 0
    for c, r in q.items():
        cur, cl, sh = r.get("cur", 0), closes.get(c, 0), univ.get(c, 0)
        if cur <= 0 or cl <= 0 or sh <= 0 or str(r.get("tm", "")) < close_hms:
            continue
        cap = cur * sh
        num += cap * (cur / cl - 1)
        den += cap
        n += 1
    if den <= 0 or n < max(30, len(univ) * 0.5):
        return None, n, f"장후 NXT 체결 종목 부족 {n}/{len(univ)}"
    avg = num / den
    info = f"NXT {n}종목 시총가중 오늘 종가 대비 {avg * 100:+.2f}%"
    with _lock:
        _st.update(avg=avg, ts=time.time(), n=n, info=info)
    return avg, n, info


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    d = sys.argv[1] if len(sys.argv) > 1 else None
    ensure_started(d)
    while True:
        a, n, info = market_avg(d, max_age=0)
        print(datetime.now().strftime("%H:%M:%S"), info)
        if a is not None:
            break
        time.sleep(10)
