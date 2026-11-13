# -*- coding: utf-8 -*-
"""
kis_api/trade_switches.py — 매매 운영 스위치 (2026-11-09)
─────────────────────────────────────────────────────────────────────
코드 수정·프로그램 재시작 없이 켜고 끄는 스위치. 파일 logs/trading_switches.json 에 저장되고,
읽는 쪽이 매 판정마다(2초 캐시) 다시 읽으므로 바꾸면 곧바로 반영된다.

  credit_buy : 신용매수 우선 — 신용가능 종목은 신용(kt10006)·비중 2배(10~50%), 신용불가는 현금·당초 비중.
               False = 전 종목 현금·당초 비중(5~25%) — 2026-11-09 이전과 같은 매수.
               읽는 곳: auto_buy(매 plan 빌드) · execution_monitor(매 틱, 끄면 즉시 현금·1배 목표로 전환)
  nxt_buy    : NXT 애프터마켓 신용매수 — 거래일 15:35~19:55, 오늘 매수 plan 의 신용가능 종목 중 NXT 매도호가가
               NXT 시장 평균(전 NXT 종목 시총가중, 오늘 KRX 종가 대비)이 마이너스일 때 그 평균보다 3%p 이상 더 빠진 물량을 신용·2배 비중 목표까지 매수(credit_buy 도 켜져 있어야 동작).
               False = 장후 매수 안 함(정규장 매수는 그대로). 읽는 곳: execution_monitor(매 틱)
  stop_loss  : 가격 손절 — 평단 대비 -14% 이하(현재 점수 60점 이상이면 면제) 전량 매도.
               False = 가격 손절 일시 중지(점수 청산 규칙은 그대로) — 2026-11-06 부터의 상태.
               읽는 곳: sell_strategy_b.is_stop_loss(Update_Promising_Stocks 매도 판정·텔레그램 /help)
               ※ paper_trader(가상매매 비교용)는 자체 수식이라 이 스위치와 무관하게 손절 적용.

바꾸는 방법
  · 텔레그램(소유자): /스위치 · /신용 켜기|끄기 · /nxt 켜기|끄기 · /손절 켜기|끄기
                      (영문 /switch · /credit on|off · /nxt on|off · /stoploss on|off)
  · 명령줄: python kis_api/trade_switches.py                  ← 현재 상태
            python kis_api/trade_switches.py credit on|off
            python kis_api/trade_switches.py nxt on|off
            python kis_api/trade_switches.py stop on|off
파일이 없거나 깨졌으면 DEFAULTS(2026-11-09 지시 기준 운영값)를 쓴다. 변경 이력: logs/trading_switches.log
"""
import os
import sys
import json
import time
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR     = os.path.join(PROJECT_DIR, "logs")
PATH        = os.path.join(LOG_DIR, "trading_switches.json")
HIST_PATH   = os.path.join(LOG_DIR, "trading_switches.log")

DEFAULTS = {"credit_buy": True, "nxt_buy": True, "stop_loss": False}   # 파일 없을 때 값(2026-11-09 운영 상태)
LABELS   = {"credit_buy": "신용매수 우선(신용가능 종목 비중 2배)",
            "nxt_buy":    "NXT 애프터 신용매수(시장평균 −3%p 이하)",
            "stop_loss":  "가격 손절(평단 -14%)"}
ALIASES  = {"credit": "credit_buy", "신용": "credit_buy", "credit_buy": "credit_buy",
            "nxt": "nxt_buy", "넥스트": "nxt_buy", "nxt_buy": "nxt_buy",
            "stop": "stop_loss", "stoploss": "stop_loss", "손절": "stop_loss", "stop_loss": "stop_loss"}
ON_WORDS  = {"on", "켜기", "켜", "켬", "켜줘", "재개", "1", "true", "yes"}
OFF_WORDS = {"off", "끄기", "꺼", "끔", "꺼줘", "중지", "0", "false", "no"}
RECHECK_SEC = 2.0     # 파일 변경 확인 주기(초) — 매 판정마다 stat 하지 않도록

_cache = {"mtime": None, "data": None, "checked": 0.0}


def _load_file():
    """파일 → {키: bool}. 없으면 {}, 깨졌으면 None."""
    try:
        with open(PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    return {k: raw[k] for k in DEFAULTS if isinstance(raw.get(k), bool)}


def _read():
    now = time.time()
    if _cache["data"] is not None and now - _cache["checked"] < RECHECK_SEC:
        return _cache["data"]
    try:
        mt = os.path.getmtime(PATH)
    except OSError:
        mt = None
    if _cache["data"] is None or mt != _cache["mtime"]:
        vals = _load_file()
        if vals is None:                       # 깨진 파일 → 직전 정상값 유지(없으면 기본값)
            print(f"   ⚠️ 매매 스위치 파일 손상({PATH}) — 직전 값 유지")
            data = _cache["data"] or dict(DEFAULTS)
        else:
            data = {**DEFAULTS, **vals}
        _cache.update(mtime=mt, data=data)
    _cache["checked"] = now
    return _cache["data"]


def get(name):
    """스위치 값(bool). name: credit_buy | stop_loss (별칭 credit/stop/신용/손절 허용)."""
    key = ALIASES.get(str(name).lower(), name)
    return bool(_read().get(key, DEFAULTS.get(key, False)))


def get_all():
    return dict(_read())


def parse_onoff(word):
    """'켜기'/'on' → True, '끄기'/'off' → False, 그 외 None."""
    w = str(word or "").strip().lower()
    if w in ON_WORDS:
        return True
    if w in OFF_WORDS:
        return False
    return None


def set_switch(name, value, by=""):
    """스위치 저장(원자적 교체) + 이력 기록. 반환 (key, 이전값, 새값)."""
    key = ALIASES.get(str(name).lower(), name)
    if key not in DEFAULTS:
        raise KeyError(f"알 수 없는 스위치: {name}")
    cur = _load_file()
    data = {**DEFAULTS, **(cur or {})}
    if cur is None and _cache["data"]:
        data = {**data, **{k: v for k, v in _cache["data"].items() if k in DEFAULTS}}
    prev = bool(data.get(key))
    data[key] = bool(value)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = {**{k: data[k] for k in DEFAULTS}, "updated": now, "updated_by": by or "manual",
           "_doc": "credit_buy=신용매수 우선(비중 2배) / nxt_buy=NXT 애프터 신용매수(시장평균 −3%p↓) / stop_loss=가격 손절(-14%). "
                   "텔레그램 /스위치 /신용 /nxt /손절 로 변경"}
    os.makedirs(LOG_DIR, exist_ok=True)
    tmp = f"{PATH}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PATH)
    try:
        with open(HIST_PATH, "a", encoding="utf-8") as f:
            f.write(f"{now}\t{key}\t{prev} -> {bool(value)}\t{by or 'manual'}\n")
    except Exception:
        pass
    _cache.update(mtime=None, data=None, checked=0.0)     # 다음 get 에서 즉시 재로딩
    return key, prev, bool(value)


def status_text():
    d = get_all()
    return "\n".join(f"  · {LABELS[k]}: {'✅ 켜짐' if d[k] else '⏸️ 꺼짐'}" for k in DEFAULTS)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    a = sys.argv[1:]
    if len(a) >= 2:
        v = parse_onoff(a[1])
        if v is None:
            print("사용법: python kis_api/trade_switches.py [credit|nxt|stop] [on|off]")
            sys.exit(1)
        k, p, n = set_switch(a[0], v, by="cli")
        print(f"{LABELS[k]}: {'켜짐' if p else '꺼짐'} → {'켜짐' if n else '꺼짐'}")
    print("현재 매매 스위치\n" + status_text())
