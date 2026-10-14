# -*- coding: utf-8 -*-
"""
execution_monitor.py — 실시간 호가 sweep 매수 집행 데몬 (키움 계좌, 실거래)
─────────────────────────────────────────────────────────────────────
auto_buy.py 가 매 30분 세션마다 산출한 매수 계획(logs/{날짜}_autobuy_plan.json)을
읽어, 정규장(09:00~15:30)에 4초 간격으로 종목별 매도호가를 실시간 감시하고
'promising 지정가 이하로 내려온 매도호가 잔량'만큼만 즉시체결성 sweep 매수한다.

설계 (사용자 확정):
  1) 목표수량은 plan 에서 받는다(한 번에 promising 가로 전량 발주하지 않음).
  2) 매도호가(ka10004)를 실시간 조회 → promising 지정가 이하(포함)에 쌓인
     호가 잔량 = 즉시 체결될 수량만큼만 promising 지정가로 주문.
  3) 잔여 = target_qty − 현재보유. 모두 채울 때까지 매 틱 반복.
     (보유잔고 = 체결의 진실. 별도 체결수량 누적 없이 보유 증가로 판정)
  4) 30분 세션마다 plan 이 새 promising 가로 갱신 → 목표수량·기준가 자동 반영.

집행 원칙:
  - 현금 배분(동시 집행 + top-down 예산 예약): AI 상위부터 각 종목의 '잔여목표금액'을 현금에서
    예약하고, 각 종목은 자기 예산 안에서 '동시에' 매수한다. 상위 종목 몫은 하위가 못 쓴다.
    예) 현금 1천5백만·Top1 목표 1천만·Top2 목표 1천만 → Top1 1천만·Top2 5백만·Top3 0.
    상위가 일시적으로 못 사도(가격상승 등) 그 예산은 예약된 채 유지되고, 하위는 잔여 예산으로 매수.
    한 종목은 동시에 1개 주문만(체결 반영 1틱 대기). 미수 방지는 별도 하드한도로 보장.
  - '걸어두지 않음': 직전 틱에 낸 미체결 잔량이 남아 있으면 취소하고 다시 sweep.
  - 우리(이 데몬)가 낸 주문만 추적·취소. '직접 낸 주문'은 절대 건드리지 않음.
  - 호가 lag/보유 lag 안전장치: 한 종목에 동시 1개 주문만. 주문 해소(체결/취소)된
    틱에는 그 종목 재주문을 건너뛰어 보유잔고가 갱신될 시간을 준다(중복매수 방지).
  - 매매는 KRX 정규장(09:00~15:30)에만. 매수 직전 '정규장 점수'가 SCORE_MIN(60)+ 인지 재확인한다.
    ▶ NXT(장전/장후) 점수는 매수/매도에 관여하지 않는다(참고용). 정규장 시각 행만 사용.
  - 콘솔(모니터 창)에 매 틱 감시 현황·체결 현황을 출력한다(무엇을 보고 왜 대기/집행하는지).
  - (2026-10-07) 매수 순서 = plan 순서(신규종목 → 기존 보유). 신규종목(target.new) 매수 예산이 모자라면
    '순위교체 매도': 보유비중(평가금액) 순위대로 '수수료·세금 차감 후 이익'(매수최우선 > 손익분기) 종목을 부족분만큼만 매도해
    자금을 마련한다(_rotate_tick). 기존 보유 추가매수를 위해서는 팔지 않는다. 교체매도 종목은 당일 재매수 금지.
    제외: 오늘 우리가 매수체결한 종목·매도 plan 종목·신규 plan 종목·수기영역(ETF/마스터 미등재)·상한가.
    끄기: ROTATE_ENABLED=False. 롤백: logs/execution_monitor.py.bak_20261007_rotate

상태파일 (logs/):
  {날짜}_autobuy_plan.json   (읽기) auto_buy 가 쓴 종목별 목표수량·기준가
  {날짜}_autobuy_exec.json   (쓰기) 종목별 체결추적·미체결주문 상태
  {날짜}_rotate_exec.json    (쓰기) 순위교체 매도 체결추적·미체결주문 상태
"""

import os
import sys
import json
import math
import time
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import secrets
from kis_api import auth, kiwoom_trading as kt
import buy_gate

LOG_DIR = secrets.LOCAL_DATA_PATH
OWNER_ID = str(secrets.TELEGRAM_CHAT_ID)

POLL_SEC  = float(getattr(secrets, "MONITOR_POLL_SEC", 4))     # 호가 폴링 주기(초)
MAX_CODES = int(getattr(secrets, "MONITOR_MAX_CODES", 15))     # 동시 감시 종목 상한(rate-limit)
SCORE_MIN = 60.0                                              # 매수 직전 재확인 최소 점수(점) — auto_buy plan 진입기준(1안: 60점)과 통일
SMOOTHED_SELL_THRESH = 0.10                                   # 평활 하락(청산 예정) 매수 금지 임계 — sell_strategy_b.SELL_THRESH(0.10)와 동일하게 유지(변경 시 함께 수정)
UPL_GUARD_SEC  = 1.0                                          # 상한가 잠김 보류 종목 감시 주기(초)
UPL_BREAK_DROP = 0.03                                         # 상한가 해제 시 매도가 = 상한가 × (1 − 3%), 호가단위 내림
ROTATE_ENABLED = True                                         # (2026-10-07) 신규종목 자금 부족 시 순위교체 매도
ROT_SETTLE_SEC = 12                                           # 교체매도 주문 해소·체결 후 현금(d2) 반영 대기 — 중복 매도 방지
ROT_MAX_DROP   = 0.01                                         # 교체매도 지정가 하한 = 매수최우선 × (1 − 1%) (평단 초과 유지)
BUY_FEE_EST, SELL_FEE_EST = 0.00015, 0.00195                  # 필요금액 추정용 수수료(매수 0.015% / 매도 수수료+세금)
ROT_SELL_COST  = 0.00215                                      # 교체매도 손익분기용 매도비용 = 수수료 0.015% + 증권거래세 0.20%(2026, 보수적)
ROT_MIN_PROFIT = 0.0                                          # 손익분기 위 최소 순이익률(0 = 수수료·세금 차감 후 손실 매도만 금지)
ROT_BLOCK_ON_LOAN = False                                     # 담보대출·융자 잔고만으로는 막지 않음(팔면 상환 후 일부라도 매수여력 생김) — 미수만 차단
ROT_CASH_MIN_RATIO = 0.5                                      # 교체매도 체결 후 매수현금이 체결대금의 이 비율 미만으로만 늘면 당일 교체매도 중지
ROT_BLOCK_RECHECK_SEC = 60                                    # 교체매도 보류 중 현금·미수 재확인 간격(초)
ROT_KEEP_SCORE = 60.0                                         # (2026-10-14) 현재 정규장 점수 이 값 이상인 보유종목은 교체매도 제외
REBUY_DISCOUNT = 0.02                                         # (2026-10-14) 당일 매도 종목 재매수: 매도 순수령 평균 대비 2% 이상 싸야 매수
SELL_FILL_TTL  = 20                                           # 당일 매도체결 집계(ka10076) 캐시 유효시간(초) — 유량 보호
ETF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Data", "ETF")

_rot_need = []      # 이번 틱 _tick 이 기록한 '예산 부족 신규종목' (plan 순서) → _rotate_tick 이 소비
_hold_names = {}    # {code: 종목명} — 이번 틱 잔고 조회 결과(교체매도 알림용)


def _fmt(x):
    s = str(x).strip().split(".")[0]
    return s.zfill(6) if s.isdigit() else s


def _p(name):
    return os.path.join(LOG_DIR, name)


def _plan_path(d):  return _p(f"{d}_autobuy_plan.json")
def _exec_path(d):  return _p(f"{d}_autobuy_exec.json")
def _sell_plan_path(d):  return _p(f"{d}_autosell_plan.json")
def _sell_exec_path(d):  return _p(f"{d}_autosell_exec.json")
def _rot_path(d):        return _p(f"{d}_rotate_exec.json")


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _save_json(path, obj):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        print(f"   ⚠️ 저장 실패({path}): {e}")


def _send_owner(msg):
    if not secrets.TELEGRAM_BOT_TOKEN:
        print(msg)
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{secrets.TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": OWNER_ID, "text": msg}, timeout=5,
        )
    except Exception as e:
        print(f"   ⚠️ 텔레그램 전송 실패: {e}")


def _log(msg):
    """콘솔(모니터 창) 출력 — 감시/체결 현황 가시화용."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


_last_console = {}   # key -> 마지막으로 출력한 줄(동일 내용 반복 출력 억제용)


def _log_once(key, msg):
    """직전과 '동일한 내용'이면 출력하지 않는다. 상태가 바뀔 때만 콘솔에 찍는다.
       (예: '목표달성' 라인이 매 틱 같은 내용이면 최초 1회만 출력)"""
    if _last_console.get(key) != msg:
        _log(msg)
        _last_console[key] = msg


def _new_cst(held):
    """종목별 실행상태 초기값. held_seen 은 현재 보유로 베이스라인(과거 보유 오보고 방지)."""
    return {"held_seen": int(held), "open_order_no": "", "open_order_qty": 0,
            "open_order_price": 0, "bought": 0}


def _is_closing_auction():
    """장마감 동시호가(15:20~15:30) — 호가 접수만 되고 체결이 없는 시간대.
       이 시간에는 sweep(취소·재주문 반복)이 무의미하므로 '1회 주문 후 유지' 모드로 전환."""
    now = datetime.now()
    sec = now.hour * 3600 + now.minute * 60 + now.second
    return 15 * 3600 + 20 * 60 <= sec < 15 * 3600 + 30 * 60


def _load_live_scores(today_str):
    """코드별 '정규장 재평가' 점수(×100) — 매수 직전 '현재도 임계(60/보조 40)+인가' 재확인용.
       buy_gate 공용 규칙: 오늘 Stock_V3 행 중 KRX 봇 기동 경계(since) 이후 계산된 행만 인정.
       재평가 전 종목은 목록에 없음(None) → 매수 보류(재평가 대기).
       ※ Stock_V3 전 행을 시각 필터 없이 쓰던 폴백이 장전 77점을 통과시켜 09:01 매수가 났다
         (2026-09-29 STX엔진). Search_History 는 자동등록·평가후처리가 장전 점수에 현재시각을
         찍을 수 있어 '재평가' 증거가 못 되므로 쓰지 않는다."""
    since = buy_gate.krx_since(LOG_DIR, today_str)
    rows = buy_gate.rescored_rows(buy_gate.load_v3_rows(LOG_DIR, today_str), since)
    return {c: r["score100"] for c, r in rows.items()}


_sell_fill_cache = {"ts": 0.0, "date": "", "data": None}


def _today_sell_fills(today_str):
    """당일 매도체결 집계(kt.fetch_today_sell_fills) — SELL_FILL_TTL 초 캐시. 조회 실패 시 None(호출부 fail-closed)."""
    now = time.time()
    c = _sell_fill_cache
    if c["date"] == today_str and c["data"] is not None and now - c["ts"] < SELL_FILL_TTL:
        return c["data"]
    data = kt.fetch_today_sell_fills()
    if data is not None:
        c.update(ts=now, date=today_str, data=data)
    return data


def _rebuy_cap(f):
    """당일 매도 종목 재매수 상한가(KRX 호가 내림). 조건(사용자 확정 2026-10-14):
         재매수 1주 원가(가격×(1+매수수수료)) ≤ 당일 매도 1주 순수령액(체결가 − 실제 수수료·세금) × (1 − 2%)
       반환 (매도평균 체결가, 1주 순수령액, 상한가). 집계 없으면 상한가 0."""
    qty, amt, cost = int(f.get("qty", 0) or 0), float(f.get("amt", 0) or 0), float(f.get("cost", 0) or 0)
    if qty < 1 or amt <= 0:
        return 0.0, 0.0, 0
    if cost <= 0:
        cost = amt * ROT_SELL_COST              # 수수료·세금 미집계 시 추정치(0.215%)
    avg, net = amt / qty, (amt - cost) / qty
    cap = kt.normalize_krx_price(int(net * (1 - REBUY_DISCOUNT) / (1 + BUY_FEE_EST)))
    return avg, net, cap


# ── 1 틱: plan 을 읽어 호가 sweep 집행 ────────────────────────────────────
def _tick(today_str):
    global _rot_need
    _rot_need = []
    if not getattr(secrets, "AUTO_BUY_ENABLED", False):
        return
    if not kt.is_market_open():        # 평일 09:00~15:30 외에는 무동작
        return

    plan = _load_json(_plan_path(today_str), None)
    if not isinstance(plan, dict) or plan.get("date") != today_str:
        return                          # 오늘자 plan 아직 없음
    targets = plan.get("targets") or {}
    if not targets:
        return

    # 보유잔고·현금 1회 조회(전 종목 공용)
    holdings = kt.fetch_all_holdings()
    if holdings is None:                # 조회 실패 → 깜깜이 매수 금지
        return
    held_map = {_fmt(h.get("code", "")): int(h.get("qty", 0) or 0) for h in holdings}
    _hold_names.clear()
    _hold_names.update({_fmt(h.get("code", "")): h.get("name", "") for h in holdings})

    cash = kt.fetch_order_cash()
    if cash is None:
        return
    # 현금 0 이어도 진행(2026-10-07): 신규종목 예산 부족을 _rot_need 에 기록해야 순위교체 매도가 돈다.
    # (현금부족 알림은 아래 cash_short 마커로 현금값이 바뀔 때만 1회)

    live_scores = _load_live_scores(today_str)   # 매수 직전 SCORE_MIN(60)+ 재확인용(전 종목 공용, 1회 로드)

    exec_state = _load_json(_exec_path(today_str), None)
    if not isinstance(exec_state, dict) or exec_state.get("date") != today_str:
        exec_state = {"date": today_str, "codes": {}}
    codes_state = exec_state["codes"]

    # 오늘 매도 체결된 종목(손절/청산·순위교체·수기) → 조건부 재매수(2026-10-14, 아래 루프):
    #   당일 매도 순수령 평균 × (1−2%) 이하 가격에서만 매수. 체결집계 조회 실패 시 매수 금지(fail-closed).
    sell_exec = _load_json(_sell_exec_path(today_str), None)
    sold_today = set()
    if isinstance(sell_exec, dict) and sell_exec.get("date") == today_str:
        sold_today = {_fmt(c) for c, v in (sell_exec.get("codes") or {}).items()
                      if int((v or {}).get("sold", 0) or 0) > 0}
    rot_exec = _load_json(_rot_path(today_str), None)          # 순위교체 매도 종목
    if isinstance(rot_exec, dict) and rot_exec.get("date") == today_str:
        sold_today |= {_fmt(c) for c, v in (rot_exec.get("codes") or {}).items()
                       if int((v or {}).get("sold", 0) or 0) > 0}
    sell_fills = _today_sell_fills(today_str)                  # 실제 매도체결(수기 매도 포함)
    if sell_fills:
        sold_today |= {_fmt(c) for c in sell_fills}

    # 평활 하락(청산 예정) 종목 → 매수 금지. sell_state_b.json(=Update_Promising 이 갱신)에서
    # last_smoothed < SMOOTHED_SELL_THRESH 인 종목은 곧 팔릴/약해지는 종목이므로, raw 가 잠깐 60+로 튀어도 사지 않음.
    sm_state  = _load_json(_p("sell_state_b.json"), None)
    sell_warn = {}   # {code(6): 평활점수}
    if isinstance(sm_state, dict):
        for c, v in sm_state.items():
            try:
                ls = (v or {}).get("last_smoothed")
                if ls is not None and float(ls) < SMOOTHED_SELL_THRESH:
                    sell_warn[_fmt(c)] = float(ls)
            except Exception:
                pass

    report, changed = [], False
    _log_once("buyhdr", f"🛒 매수감시 {min(len(targets), MAX_CODES)}종목 · 주문가능현금 {cash:,}원")

    # ── 동시 집행 + top-down 예산 예약 ──────────────────────────────────────
    #   현금을 AI 상위부터 '잔여목표금액'만큼 예약(committed)해, 하위 종목이 상위 몫을 못 쓰게 한다.
    #   Top1 목표 1천만·Top2 목표 1천만·현금 1천5백만 → Top1 예산 1천만, Top2 예산 5백만, Top3 0.
    #   내 미체결(in-flight) 주문이 이미 예약한 현금은 usable(주문가능현금 cash)에서 빠져 있으므로,
    #   예산 배분 기준 base_cash 에 되더한다(안 그러면 상위가 주문 중일 때 하위 예산이 과소평가).
    #   미수 방지는 별도 하드 한도(cash − local_spent, 실시간 재조회)로 보장한다.
    my_inflight = sum(int((v or {}).get("open_order_qty", 0) or 0) *
                      int((v or {}).get("open_order_price", 0) or 0)
                      for v in codes_state.values() if (v or {}).get("open_order_no"))
    base_cash   = cash + my_inflight
    committed   = 0      # 상위 AI 유효종목이 예약한 현금(각자 잔여목표금액)
    local_spent = 0      # 이번 틱에 새로 발주한 금액(미수 방지 로컬 차감)

    # plan 삽입순 = AI점수 상위순. 상한까지만 감시(rate-limit 보호).
    for code in list(targets.keys())[:MAX_CODES]:
        t = targets[code]
        code = _fmt(code)
        name = t.get("name", "")
        target_qty = int(t.get("target_qty", 0) or 0)
        price = int(t.get("promising_price", 0) or 0)
        if target_qty < 1 or price <= 0:
            continue

        held = held_map.get(code, 0)
        cst = codes_state.get(code)
        if cst is None:
            cst = codes_state[code] = _new_cst(held)
            changed = True

        # ── 체결 판정: 보유 증가분 = 체결(우리 sweep) → 보고
        if held > cst["held_seen"]:
            delta = held - cst["held_seen"]
            cst["bought"] += delta
            report.append(f"✅ 체결 {name}({code}) +{delta}주 → 보유 {held}/{target_qty}주")
            _log_once(f"buy:{code}", f"  ✅ 체결 {name}({code}) +{delta}주 → 보유 {held}/{target_qty}")
        if held != cst["held_seen"]:
            cst["held_seen"] = held
            changed = True

        remaining = target_qty - held
        remaining_amt = remaining * price

        # ── 직전 틱에 낸 주문 해소: 미체결 잔량 남았으면 취소(걸어두지 않음).
        #    이 종목은 이번 틱 재주문 안 함(보유 갱신 1틱 대기). 단, 잔여목표는 현금을 계속 '예약'한다
        #    (주문 중인 상위 종목의 몫을 하위가 뺏지 않도록).
        if cst.get("open_order_no"):
            if _is_closing_auction():
                # 동시호가(15:20~15:30): 체결 없는 호가접수 시간 — 취소/재주문 반복 금지(사용자 확정).
                # 낸 주문 1건을 그대로 유지해 종가(15:30) 단일가 체결을 기다린다. 결과는 장마감 후 1회 알림.
                if remaining >= 1:
                    committed += remaining_amt
                if not cst.get("close_buy_hold"):
                    cst["close_buy_hold"] = True
                    changed = True
                    report.append(f"🕒 동시호가 — {name}({code}) 매수주문 유지 "
                                  f"{cst['open_order_qty']}주×{cst['open_order_price']:,}원 · 종가 체결 대기(장마감 후 결과 알림)")
                _log_once(f"buy:{code}", f"  🕒 {name}({code}) 동시호가 — 주문 유지, 종가 체결 대기")
                continue
            opens = kt.fetch_open_buy_orders(code)
            mine = next((x for x in opens if x["order_no"] == cst["open_order_no"]), None)
            if mine is not None:
                rem = mine.get("remaining_qty", 0)
                if rem and kt.cancel_order(cst["open_order_no"], code, rem):
                    report.append(f"🚫 미체결취소 {name}({code}) 잔량 {rem}주×{price:,}원")
                # 취소 실패면 다음 틱 재시도(중복주문 방지 위해 그대로 둠)
            cst["open_order_no"] = ""; cst["open_order_qty"] = 0; cst["open_order_price"] = 0
            changed = True
            if remaining >= 1:
                committed += remaining_amt      # 예산 예약 유지 → 하위가 이 종목 몫 못 씀
                _log_once(f"buy:{code}", f"  🔄 {name}({code}) 직전주문 해소 → 계속 채움(잔여 {remaining}주, 예약)")
            else:
                _log_once(f"buy:{code}", f"  🔄 {name}({code}) 주문 해소·목표달성")
            continue

        if remaining < 1:               # 목표 달성(완료) → 예약 없음, 다음 종목
            _log_once(f"buy:{code}", f"  ✔️ {name}({code}) 목표달성 보유{held}/목표{target_qty}")
            continue

        # ── 당일 매도체결 종목: 조건부 재매수(2026-10-14). 지정가를 '매도 순수령 평균×98%'(매수수수료 포함) 이하로
        #    낮춰 그 가격 이하 매도호가만 sweep. 체결집계를 못 구하면 종전대로 매수 금지(실격 → 예약 없음).
        if code in sold_today:
            avg_s, net_s, cap = _rebuy_cap((sell_fills or {}).get(code) or {})
            if cap <= 0:
                _log_once(f"buy:{code}", f"  ⛔ {name}({code}) 오늘 매도체결 · 매도평균 확인 불가 → 재매수 보류")
                if not cst.get("buy_locked"):
                    cst["buy_locked"] = True
                    changed = True
                    report.append(f"⛔ {name}({code}) 오늘 매도체결 · 매도평균 확인 불가 → 재매수 보류")
                continue
            if cst.get("buy_locked"):
                cst["buy_locked"] = False
                changed = True
            if cap < price:
                price = cap
                remaining_amt = remaining * price
            if cst.get("rebuy_cap") != cap:            # 상한가가 바뀔 때만 알림(추가 매도체결 시 갱신)
                cst["rebuy_cap"] = cap
                changed = True
                report.append(f"🔁 {name}({code}) 당일 매도분 재매수 조건 — 매도평균 {avg_s:,.0f}원"
                              f"(수수료·세금 차감 {net_s:,.0f}원) −{REBUY_DISCOUNT*100:.0f}% → {cap:,}원 이하에서만 매수")

        # ── 평활 하락(청산 예정) 종목 매수 금지(실격 → 예약 없음).
        if code in sell_warn:
            _log_once(f"buy:{code}", f"  ⏭️ {name}({code}) 평활 {sell_warn[code]*100:.1f}점<{SMOOTHED_SELL_THRESH*100:.0f} 하락구간 — 매수 보류")
            if not cst.get("smwarn"):
                cst["smwarn"] = True
                changed = True
                report.append(f"⏭️ {name}({code}) 평활 {sell_warn[code]*100:.1f}점<{SMOOTHED_SELL_THRESH*100:.0f}(청산예정) — 매수 보류")
            continue
        elif cst.get("smwarn"):
            cst["smwarn"] = False        # 평활 회복(≥임계) → 다음 하락 시 재알림
            changed = True

        # ── 매수 직전 정규장 재평가 점수 재확인(장전 NXT 점수·순간 스파이크 매수 방지)(실격 → 예약 없음).
        live_sc = live_scores.get(code)
        min_sc = float(t.get("min_score", SCORE_MIN) or SCORE_MIN)   # plan 기록 임계(보조매수 40, 기본 60)
        if live_sc is None or live_sc < min_sc:
            shown = "재평가대기" if live_sc is None else f"{live_sc:.0f}"
            why = ("정규장 재평가 전 — 매수 보류" if live_sc is None
                   else f"정규장 점수 {live_sc:.0f}<{min_sc:.0f} — 매수 보류")
            _log_once(f"buy:{code}", f"  ⏭️ {name}({code}) 잔여{remaining} · {why}")
            if cst.get("last_skip_score") != shown:      # 같은 사유 반복 알림 방지
                report.append(f"⏭️ {name}({code}) {why}")
                cst["last_skip_score"] = shown
                changed = True
            continue
        if cst.get("last_skip_score") is not None:
            cst["last_skip_score"] = None                # 점수 회복 → 다음 보류 시 재알림
            changed = True

        # ── 예산 배분: 이 종목 몫 = base_cash − (상위 유효종목 예약분). 그 뒤 이 종목도 예약한다.
        is_top_valid = (committed == 0)          # 아직 상위 유효 미완료 종목이 없음 = 이 종목이 최우선
        budget = max(0, base_cash - committed)
        if t.get("new") and budget < remaining_amt and not _is_closing_auction():
            # 신규종목 예산 부족 → 순위교체 매도 후보로 기록(plan 순서 유지). 기존 보유 추가매수는 기록 안 함.
            _rot_need.append({"code": code, "name": name, "remaining": remaining,
                              "price": price, "budget": budget})
        committed += remaining_amt                # 이 종목 잔여목표를 하위 위해 예약

        if budget < price:                        # 예산으로 1주도 못 삼(상위가 현금 다 가져감) → 대기
            _log_once(f"buy:{code}", f"  ⏸️ {name}({code}) 예산 {budget:,}원<{price:,}원/주 — 대기(상위 우선 배정)")
            if is_top_valid and exec_state.get("cash_short") != cash:   # 최우선도 못 사면 1회 알림
                report.append(f"💸 현금부족 — 최우선 종목도 매수 불가(주문가능 {cash:,}원)")
                exec_state["cash_short"] = cash
                changed = True
            continue
        if is_top_valid:
            exec_state["cash_short"] = None        # 최우선 종목은 예산 확보됨 → 부족 마커 해제

        # ── 동시호가(15:20~15:30): sweep 불가(체결 없음) — 잔여목표를 지정가 1회 주문으로 내고
        #    종가 체결(15:30)을 기다린다. 취소/재주문 없음. 체결 여부는 장마감 후 1회 알림.
        if _is_closing_auction():
            live_cash = kt.fetch_order_cash()
            if live_cash is None:
                continue
            cash_room = min(cash - local_spent, live_cash)
            qty = min(remaining, budget // price, cash_room // price)
            if qty < 1:
                _log_once(f"buy:{code}", f"  💸 {name}({code}) 동시호가 — 예산/현금 부족, 주문 없음")
                continue
            res = kt.place_buy_order(code, qty, price)
            if res and res.get("return_code") == 0:
                ono = res.get("ord_no", "?")
                local_spent += qty * price
                cst["last_fail"] = None
                cst["open_order_no"] = ono
                cst["open_order_qty"] = qty
                cst["open_order_price"] = price
                cst["close_buy_hold"] = True
                changed = True
                report.append(f"🕒 동시호가 지정가매수 {name}({code}) {qty}주×{price:,}원 = {qty*price:,}원 "
                              f"— 종가 체결 대기(장마감 후 결과 알림, 주문 {ono})")
                _log_once(f"buy:{code}", f"  🕒 동시호가 지정가매수 {name}({code}) {qty}주×{price:,}원 (주문 {ono})")
            else:
                err = (res or {}).get("return_msg", "응답 없음")
                if cst.get("last_fail") != err:
                    report.append(f"❌ 동시호가 매수실패 {name}({code}): {err}")
                    cst["last_fail"] = err
                    changed = True
                _log_once(f"buy:{code}", f"  ❌ 동시호가 매수실패 {name}({code}): {err}")
            continue

        # ── 매도호가 sweep: promising 이하 잔량만큼. 없으면(가격상승 등) 예약 유지한 채 다음 종목.
        avail, best_ask = kt.ask_qty_at_or_below(code, price)
        if avail < 1:
            _log_once(f"buy:{code}", f"  ⏳ {name}({code}) 잔여{remaining} · 점수{live_sc:.0f} · 지정가{price:,} "
                      f"매도최우선 {best_ask:,} — 호가대기(예산 {budget:,}원 예약 유지)")
            continue

        # ── 주문 직전 현금 실시간 재조회(미수 방지 하드 한도).
        #    cash−local_spent(내 이번 틱 발주 반영) 과 실시간값 중 작은 쪽. 예산과도 min.
        live_cash = kt.fetch_order_cash()
        if live_cash is None:
            continue                     # 현금 조회 실패 → 이 종목만 이번 틱 건너뜀(하위는 진행)
        cash_room = min(cash - local_spent, live_cash)
        qty = min(remaining, avail, budget // price, cash_room // price)
        if qty < 1:
            _log_once(f"buy:{code}", f"  💸 {name}({code}) 매수불가(예산 {budget:,}·현금여유 {cash_room:,} < {price:,}/주)")
            continue

        res = kt.place_buy_order(code, qty, price, stex="SOR")   # 정규장 sweep 은 SOR(KRX/NXT 최선 라우팅)
        if res and res.get("return_code") == 0:
            ono = res.get("ord_no", "?")
            amt = qty * price
            local_spent += amt
            cst["last_fail"] = None
            cst["open_order_no"] = ono
            cst["open_order_qty"] = qty
            cst["open_order_price"] = price
            changed = True
            want = min(remaining, avail)
            tag = (" ·예산한도" if qty < want and (budget // price) <= (cash_room // price)
                   else " ·현금한도" if qty < want
                   else " ·부분(호가한도)" if qty < remaining else "")
            report.append(f"🟢 sweep매수 {name}({code}) {qty}주×{price:,}원 = {amt:,}원 "
                          f"(잔여 {remaining}·호가 {avail}·최우선 {best_ask:,}{tag}, 주문 {ono})")
            _log_once(f"buy:{code}", f"  🟢 sweep매수 {name}({code}) {qty}주×{price:,}원 = {amt:,}원 "
                      f"(잔여{remaining}·호가{avail}{tag}, 주문 {ono})")
            # 동시 집행: 이 종목 주문 후에도 하위 종목이 '자기 예산' 안에서 매수하도록 계속 진행.
        else:
            err = (res or {}).get("return_msg", "응답 없음")
            _log_once(f"buy:{code}", f"  ❌ 매수실패 {name}({code}): {err}")
            if cst.get("last_fail") != err:              # 실패 알림 도배 방지(같은 에러 1회만)
                report.append(f"❌ 매수실패 {name}({code}): {err}")
                cst["last_fail"] = err
                changed = True
            # 실패는 '초점 유지'의 예외 — 특정 종목 오류로 전체 매수가 얼지 않도록 다음 종목으로 진행.
            continue

    if changed:
        _save_json(_exec_path(today_str), exec_state)
    if report:
        _send_owner("🛒 실시간 sweep 집행\n" + "\n".join(report))


# ── 순위교체 매도 (2026-10-07) ────────────────────────────────────────────
#   신규종목(plan target.new) 매수 예산이 모자라면, 보유비중(평가금액) 1위부터 순서대로
#   '수수료·세금 차감 후 이익'(매수최우선 > 손익분기 = 평단×(1+0.015%)÷(1−0.215%)) 종목만 부족분만큼 매도. 그 외 건너뜀.
#   (2026-10-14) 현재 정규장 재평가 점수 60점(ROT_KEEP_SCORE)+ 보유종목은 교체매도 제외(점수 미산출 종목은 대상 유지).
#   백테스트(addbuy_analysis/rotate_test.py '최종안') 규칙을 장중 sweep 으로 옮긴 것.
#   · 매도 수량 = 부족금액 ÷ 매수최우선 (트랜치별 발주, 신용·담보 먼저). 지정가 = max(매수최우선×(1−1%), 손익분기 초과 최소호가)
#   · '걸어두지 않음': 다음 틱에 미체결 잔량 취소 → ROT_SETTLE_SEC 동안 현금 반영을 기다린 뒤 부족분 재계산
#   · 부족분 = 신규종목들이 지금 지정가 이하 호가로 살 수 있는 금액 합 − 첫 부족 신규종목의 예산
#     (호가가 지정가 위로 달아난 신규종목 때문에 미리 팔지 않음)
_master_cache = {"ts": 0.0, "set": None}


def _rot_eligible(code):
    """교체매도 가능 종목 = V3 주식 마스터(logs/stock_master.json) 등재 & ETF 아님.
       수기 매수 영역(ETF·ETN·우선주 등)은 건드리지 않는다. 마스터 로드 실패 시 False(fail-closed)."""
    if os.path.exists(os.path.join(ETF_DIR, f"A{code}.csv")):
        return False
    now = time.time()
    if _master_cache["set"] is None or now - _master_cache["ts"] > 3600:
        try:
            with open(_p("stock_master.json"), encoding="utf-8") as f:
                _master_cache["set"] = set(json.load(f).get("stocks", {}).keys())
            _master_cache["ts"] = now
        except Exception:
            return False
    return code in _master_cache["set"]


def _rot_breakeven(avg):
    """교체매도 손익분기 매도가 = 평단 × (1+매수수수료) × (1+최소이익) ÷ (1−매도수수료−거래세).
       이 가격 이하로 팔면 수수료·세금 차감 후 손실(2026-10-14: '평단 초과'만 보다 손실 매도 발생)."""
    return avg * (1 + BUY_FEE_EST) * (1 + ROT_MIN_PROFIT) / (1 - ROT_SELL_COST)


def _price_above(x):
    """x 보다 큰 가장 낮은 KRX 호가(호가단위 정렬)."""
    p = kt.normalize_krx_price(int(x))
    while p <= x:
        p += kt.krx_tick_size(p)
    return p


def _rotate_place(today_str, need, tranche_map, codes, report):
    """부족금액을 계산해 보유비중 순위대로 교체매도 주문. 주문을 냈으면 True."""
    want_amt = 0.0
    for n in need:
        avail, _ = kt.ask_qty_at_or_below(n["code"], n["price"])
        want_amt += min(n["remaining"], avail) * n["price"] * (1 + BUY_FEE_EST)
    need_amt = want_amt - need[0]["budget"]
    min_px = min(n["price"] for n in need)
    if need_amt < min_px:
        _log_once("rot", f"  🔁 교체매도 불필요(신규종목 즉시매수 가능분 {want_amt:,.0f}원 ≤ 예산 {need[0]['budget']:,}원)")
        return False

    plan = _load_json(_plan_path(today_str), {}) or {}
    new_codes = {_fmt(c) for c, t in (plan.get("targets") or {}).items() if (t or {}).get("new")}
    sp = _load_json(_sell_plan_path(today_str), {}) or {}
    selling = {_fmt(c) for c in (sp.get("targets") or {})} if sp.get("date") == today_str else set()
    ex = _load_json(_exec_path(today_str), {}) or {}
    bought = ({_fmt(c) for c, v in (ex.get("codes") or {}).items() if int((v or {}).get("bought", 0) or 0) > 0}
              if ex.get("date") == today_str else set())
    live = _load_live_scores(today_str)      # (2026-10-14) 현재 정규장 점수 ROT_KEEP_SCORE(60)+ 보유종목은 팔지 않음

    srcs = []
    for code, poss in tranche_map.items():
        code = _fmt(code)
        if code in new_codes or code in selling or code in bought or not _rot_eligible(code):
            continue
        sc = live.get(code)
        if sc is not None and sc >= ROT_KEEP_SCORE:
            _log_once(f"rot:{code}", f"  🔁 {_hold_names.get(code, '')}({code}) 정규장 점수 {sc:.0f}≥{ROT_KEEP_SCORE:.0f} — 교체매도 제외")
            continue
        qty = sum(p["qty"] for p in poss)
        sellable = sum(p.get("sell_possible_qty", 0) for p in poss)
        if qty < 1 or sellable < 1:
            continue
        avg = sum(p["qty"] * p["avg_buy_price"] for p in poss) / qty
        cur = max((p.get("cur_price", 0) or 0) for p in poss) or avg
        srcs.append((qty * cur, code, poss, qty, sellable, avg))
    srcs.sort(key=lambda x: -x[0])

    for_names = ", ".join(f"{n['name']}({n['code']})" for n in need[:3]) + (" 외" if len(need) > 3 else "")
    placed = False
    for rank, (_, code, poss, qty, sellable, avg) in enumerate(srcs, 1):
        if need_amt < min_px:
            break
        name = _hold_names.get(code, "")
        levels = kt.fetch_bid_book(code + "_AL") or kt.fetch_bid_book(code)
        if not levels:
            continue
        best_bid = levels[0][0]
        be = _rot_breakeven(avg)
        if best_bid <= be:
            _log_once(f"rot:{code}", f"  🔁 {name}({code}) 비중{rank}위 수수료·세금 차감 시 손실(매수최우선 {best_bid:,}"
                                     f"≤손익분기 {be:,.0f}·평단 {avg:,.0f}) — 교체매도 제외")
            continue
        upl = _upper_limit(code)
        if upl and best_bid >= upl:
            _log_once(f"rot:{code}", f"  🔁 {name}({code}) 상한가 — 교체매도 제외")
            continue
        # 지정가 하한 = 손익분기 초과 최소호가 → 어느 가격에 체결돼도 수수료·세금 차감 후 이익
        floor = _price_above(max(best_bid * (1 - ROT_MAX_DROP), be))
        if floor > best_bid:
            continue
        avail = sum(q for px, q in levels if px >= floor)
        q_total = min(int(math.ceil(need_amt / (best_bid * (1 - ROT_SELL_COST)))), sellable, avail)
        if q_total < 1:
            continue
        rem, orders = q_total, []
        for pos in sorted(poss, key=lambda h: (not bool(h["loan_dt"]), h["loan_dt"])):   # 담보·신용 먼저(이자 절감)
            sq = min(pos.get("sell_possible_qty", 0), rem)
            if sq < 1:
                continue
            res = kt.place_sell_order(code, sq, floor, pos["loan_dt"], pos.get("crd_type", "00"), stex="SOR")
            if res and res.get("return_code") == 0:
                orders.append({"no": res.get("ord_no", "?"), "qty": sq, "price": floor, "loan_dt": pos["loan_dt"]})
                rem -= sq
            else:
                err = (res or {}).get("return_msg", "응답 없음")
                report.append(f"❌ 교체매도 실패 {name}({code}) {pos['order_type']} {sq}주: {err}")
                _log_once(f"rot:{code}", f"  ❌ 교체매도 실패 {name}({code}): {err}")
            if rem < 1:
                break
        if not orders:
            continue
        q_ord = q_total - rem
        c = codes.get(code)
        if c is None:
            c = codes[code] = {"name": name, "held_seen": qty, "sold": 0, "open_orders": [], "events": []}
        c["open_orders"] = orders
        c["events"].append({"time": datetime.now().strftime("%H:%M:%S"), "qty": q_ord, "price": floor,
                            "cash_qty": sum(o["qty"] for o in orders if not o.get("loan_dt")),   # 현금 트랜치분(안전망 기준)
                            "best_bid": best_bid, "avg": round(avg, 1), "rank": rank,
                            "for": [n["code"] for n in need]})
        need_amt -= q_ord * best_bid * (1 - ROT_SELL_COST)
        placed = True
        net_min = (floor * (1 - ROT_SELL_COST)) / (avg * (1 + BUY_FEE_EST)) - 1   # 하한가 체결 시 비용 차감 순수익률
        report.append(f"🔁 교체매도 {name}({code}) {q_ord}주×{floor:,}원↑ (보유비중 {rank}위 · 평단 {avg:,.0f} → "
                      f"매수최우선 {best_bid:,} · 비용차감 순익 ≥{net_min * 100:+.2f}%) → 신규 {for_names} 매수자금")
        _log_once(f"rot:{code}", f"  🔁 교체매도 {name}({code}) {q_ord}주×{floor:,}원 (비중{rank}위, 순익≥{net_min*100:+.2f}%)")
    if need_amt >= min_px and not placed:
        _log_once("rot", f"  🔁 교체매도 대상 없음(정규장 {ROT_KEEP_SCORE:.0f}점 미만 중 수수료·세금 차감 후 이익 나는 보유종목 없음) — 부족 {need_amt:,.0f}원")
    return placed


def _rot_cash_gate(rs):
    """교체매도해도 매수현금이 나오는 상태인지 판정. 막아야 하면 사유 문자열, 괜찮으면 None.
       ① 미수(D+2 예수금 음수 또는 현금미수금 발생) → 매도대금이 미수 상환에 먼저 쓰임
       ② 담보대출·융자 잔고(ROT_BLOCK_ON_LOAN, 기본 꺼짐 — 담보·신용 잔고는 팔면 상환 후 일부라도 여력이 생김)
       ③ 직전 교체매도 '현금 트랜치' 체결분만큼 매수현금이 실제로 늘지 않았으면 당일 중지(halt) — 원인 불문 안전망.
          (오늘 매수 지출 d2_buy 를 되더해 '매수로 현금이 줄어든 것'은 미반영으로 오판하지 않게 함)"""
    if rs.get("halt"):
        return rs["halt"]
    st = kt.fetch_cash_state()
    if st is None:
        return "현금·미수 조회 실패"
    base = st["d2"] + st["d2_buy"]
    sold_amt = float(rs.get("sold_amt", 0) or 0)
    if sold_amt > 0 and rs.get("cash_ref") is not None:
        rise = base - float(rs["cash_ref"])
        if rise < ROT_CASH_MIN_RATIO * sold_amt * (1 - ROT_SELL_COST):
            rs["halt"] = (f"교체매도 {sold_amt:,.0f}원 체결 후 매수현금 증가 {rise:,.0f}원뿐 "
                          f"(매도대금이 미수·대출 상환에 쓰인 것으로 판단) — 당일 교체매도 중지")
            return rs["halt"]
    rs["cash_ref"], rs["sold_amt"] = base, 0.0
    # 매수가능 현금 = max(0, D+2 예수금). D+2 가 음수면 매도대금이 그 구멍(미수)부터 메워 현금이 안 나온다.
    # D+2 가 플러스면 판 만큼 그대로 매수현금이 되므로 허용(D+1 결제예정·미수변제소요금만으로는 막지 않음).
    if st["d2"] < 0 or st.get("uncla", 0) > 0:
        return (f"미수 상태(D+2 예수금 {st['d2']:,}원 · 현금미수 {st.get('uncla', 0):,}원) "
                f"— 매도대금이 미수 상환에 먼저 쓰여 매수현금이 안 나옴")
    if ROT_BLOCK_ON_LOAN and st["loan"] > 0:
        return f"담보대출·융자 잔고 {st['loan']:,}원 — 매도대금이 대출상환에 쓰일 수 있음"
    return None


def _rotate_tick(today_str):
    """매 틱: ① 교체매도 체결 판정 ② 직전 주문 미체결 잔량 취소(1틱 대기) ③ 현금 반영 대기 후 부족분 교체매도."""
    if not (ROTATE_ENABLED and getattr(secrets, "AUTO_BUY_ENABLED", False)
            and getattr(secrets, "AUTO_SELL_ENABLED", False)):
        return
    if not kt.is_market_open():
        return
    need = list(_rot_need)
    rs = _load_json(_rot_path(today_str), None)
    if not isinstance(rs, dict) or rs.get("date") != today_str:
        rs = {"date": today_str, "codes": {}, "last_action": 0.0}
    codes = rs["codes"]
    open_any = any(c.get("open_orders") for c in codes.values())
    if not need and not open_any:
        return

    tranche_map = kt.fetch_holdings_tranche_map()
    if tranche_map is None:
        return
    report, changed, now = [], False, time.time()

    for code, c in codes.items():                     # ① 체결 판정: 보유 감소 = 교체매도 체결
        held = sum(p["qty"] for p in tranche_map.get(code, []))
        if held < c["held_seen"]:
            d = c["held_seen"] - held
            c["sold"] += d
            ev = (c.get("events") or [{}])[-1]
            # 안전망 기준 금액은 '현금 트랜치' 체결분만 — 담보·신용분은 대출상환 후 일부만 현금이 되므로 제외
            cash_d = min(d, int(ev.get("cash_qty", ev.get("qty", d)) or 0))
            rs["sold_amt"] = float(rs.get("sold_amt", 0) or 0) + cash_d * float(ev.get("price", 0) or 0)  # 지정가 기준(보수적)
            rs["last_action"] = now
            report.append(f"✅ 교체매도 체결 {c.get('name', '')}({code}) -{d}주 → 잔여보유 {held}주")
        if held != c["held_seen"]:
            c["held_seen"] = held
            changed = True

    if open_any:                                      # ② 직전 주문 해소(걸어두지 않음) → 이번 틱 대기
        for code, c in codes.items():
            if not c.get("open_orders"):
                continue
            opens = kt.fetch_open_sell_orders(code)
            for od in c["open_orders"]:
                mine = next((x for x in opens if x["order_no"] == od.get("no")), None)
                if mine is not None and mine.get("remaining_qty", 0) > 0:
                    kt.cancel_order(od["no"], code, mine["remaining_qty"], od.get("loan_dt", ""))
            c["open_orders"] = []
        rs["last_action"] = now
        changed = True
    elif need and not _is_closing_auction():          # ③ 부족분 교체매도
        if now < float(rs.get("block_until", 0) or 0):
            pass                                      # 보류 중 — 계좌 재조회는 ROT_BLOCK_RECHECK_SEC 마다
        elif now - float(rs.get("last_action", 0) or 0) >= ROT_SETTLE_SEC:
            block = _rot_cash_gate(rs)                # 팔아도 매수현금이 안 나오면 교체매도 안 함
            changed = True
            if block:
                rs["block_until"] = now + ROT_BLOCK_RECHECK_SEC
                _log_once("rot", f"  🔁 교체매도 보류 — {block}")
                kind = block.split("(")[0]
                if rs.get("block_note") != kind:      # 같은 사유 알림은 하루 1회
                    rs["block_note"] = kind
                    report.append(f"⛔ 교체매도 보류 — {block}")
            else:
                rs["block_note"] = None
                if _rotate_place(today_str, need, tranche_map, codes, report):
                    rs["last_action"] = now
        else:
            _log_once("rot", "  🔁 교체매도 현금 반영 대기")

    if changed:
        _save_json(_rot_path(today_str), rs)
    if report:
        _send_owner("🔁 순위교체 매도 (신규종목 매수자금)\n" + "\n".join(report))


# ── 상한가 잠김 가드 ──────────────────────────────────────────────────────
# 매도 대상이 상한가에 잠겨 있으면(매수최우선=상한가 & 매도호가 없음) 매도하지 않고 보류,
# 그 종목만 1초 간격으로 감시하다가 잠김이 풀리는 순간 상한가 -3% 지정가로 전량 매도한다.
# (2026-09-11 아모텍: 상한가 잠김 중 raw<0 청산 sweep 이 상한가 매수잔량에 전량 체결된 사례)
# 잠긴 채 동시호가(15:20~)에 들어가면 종가매도도 보류 → 보유 유지(다음 날 plan 재판정).
_upl_guard = {}   # {code: {"name", "upl"}} — 잠김 보류 중인 매도 대상(_sell_tick 이 매 틱 재구성)
_upl_cache = {}   # {code: (YYYYMMDD, 상한가)} — 상한가는 당일 고정이라 하루 1회 조회


def _upper_limit(code):
    """당일 상한가(원). 조회 실패 시 None(캐시 안 함 → 다음 틱 재조회)."""
    today = datetime.now().strftime("%Y%m%d")
    hit = _upl_cache.get(code)
    if hit and hit[0] == today:
        return hit[1]
    upl = kt.fetch_upper_limit(code)
    if upl > 0:
        _upl_cache[code] = (today, upl)
        return upl
    return None


def _is_upl_locked(book, upl):
    """상한가 잠김 = 매수최우선호가가 상한가 & 매도호가 잔량 없음."""
    bids, asks = book
    return bool(bids) and bids[0][0] >= upl and not any(q > 0 for _, q in asks)


def _upl_break_price(upl):
    return kt.normalize_krx_price(int(upl * (1 - UPL_BREAK_DROP)))


def _fire_upl_break(code, name, cst, positions, report):
    """잠김 해제 → 트랜치별 상한가-3% 지정가 매도(SOR). 해제가 이상 매수호가는 높은 가격부터 체결되고,
       미체결 잔량은 다음 매도 틱이 취소 후 해제가 이하 기준으로 sweep 을 이어간다(upl_break_price)."""
    upl = int((cst.pop("upl_lock", None) or {}).get("upl") or 0) or (_upper_limit(code) or 0)
    if upl <= 0:
        return
    price = _upl_break_price(upl)
    cst["upl_break_price"] = price
    report.append(f"🔓 상한가 해제 {name}({code}) {upl:,}원 → {price:,}원(-{UPL_BREAK_DROP*100:.0f}%) 지정가 매도")
    positions = sorted(positions, key=lambda h: (not bool(h["loan_dt"]), h["loan_dt"]))
    new_orders = []
    for pos in positions:
        qty = pos.get("sell_possible_qty", 0)
        if qty < 1:
            continue
        _tkey = pos["loan_dt"] or "CASH"
        res = kt.place_sell_order(code, qty, price, pos["loan_dt"], pos.get("crd_type", "00"), stex="SOR")
        if res and res.get("return_code") == 0:
            ono = res.get("ord_no", "?")
            new_orders.append({"no": ono, "qty": qty, "price": price, "loan_dt": pos["loan_dt"]})
            report.append(f"🔴 해제매도 {name}({code}) {pos['order_type']} {qty}주×{price:,}원 (주문 {ono})")
            _log_once(f"sell:{code}:{_tkey}", f"  🔴 상한가 해제매도 {name}({code}) {qty}주×{price:,}원 (주문 {ono})")
        else:
            err = (res or {}).get("return_msg", "응답 없음")
            report.append(f"❌ 해제매도 실패 {name}({code}) {pos['order_type']} {qty}주: {err}")
            _log_once(f"sell:{code}:{_tkey}", f"  ❌ 상한가 해제매도 실패 {name}({code}): {err}")
    cst["open_orders"] = list(cst.get("open_orders") or []) + new_orders


def _upl_guard_tick(today_str):
    """잠김 보류 종목만 1초 간격 감시 — 잠김이 풀리면(매도호가 등장 or 매수최우선<상한가) 즉시 해제매도.
       호가 조회 실패는 '해제'로 보지 않는다(다음 초 재확인)."""
    if not kt.is_market_open():
        _upl_guard.clear()
        return
    if not getattr(secrets, "AUTO_SELL_ENABLED", False) or _is_closing_auction():
        return
    broken = []
    for code, g in list(_upl_guard.items()):
        book = kt.fetch_book(code)
        if book is not None and not _is_upl_locked(book, g["upl"]):
            broken.append(code)
    if not broken:
        return

    exec_state = _load_json(_sell_exec_path(today_str), None)
    if not isinstance(exec_state, dict) or exec_state.get("date") != today_str:
        return
    tranche_map = kt.fetch_holdings_tranche_map()
    if tranche_map is None:             # 잔고 조회 실패 → 다음 초 재시도(잠김 상태 유지)
        return
    report = []
    for code in broken:
        g = _upl_guard.pop(code)
        cst = exec_state["codes"].get(code)
        if not cst or not cst.get("upl_lock"):
            continue
        _fire_upl_break(code, g["name"], cst, tranche_map.get(code, []), report)
    _save_json(_sell_exec_path(today_str), exec_state)
    if report:
        _send_owner("📤 상한가 해제 매도\n" + "\n".join(report))


# ── 매도 sweep ────────────────────────────────────────────────────────────
def _new_sell_cst(held):
    """종목별 매도 실행상태 초기값. held_seen = 베이스라인(체결=보유 감소로 판정).
       open_orders: 직전 틱에 낸 트랜치별 매도주문 [{no,qty,price,loan_dt}] (복수)."""
    return {"held_seen": int(held), "open_orders": [], "sold": 0}


# ── 1 틱: autosell_plan 을 읽어 '매수호가 sweep' 으로 전량청산 집행 ──────────
def _sell_tick(today_str):
    if not getattr(secrets, "AUTO_SELL_ENABLED", False):
        return
    if not kt.is_market_open():         # 평일 09:00~15:30 외에는 무동작
        return

    plan = _load_json(_sell_plan_path(today_str), None)
    if not isinstance(plan, dict) or plan.get("date") != today_str:
        _upl_guard.clear()
        return                          # 오늘자 매도 plan 아직 없음
    targets = plan.get("targets") or {}
    if not targets:
        _upl_guard.clear()              # 매도 대상 없음 → 상한가 감시도 중단
        return

    exec_state = _load_json(_sell_exec_path(today_str), None)
    if not isinstance(exec_state, dict) or exec_state.get("date") != today_str:
        exec_state = {"date": today_str, "codes": {}}
    codes_state = exec_state["codes"]

    report, changed = [], False
    _log_once("sellhdr", f"📤 매도감시 {min(len(targets), MAX_CODES)}종목")

    # 잔고는 틱당 1회 통합조회(kt00018 전 페이지 1세트) — 종목별 개별조회는
    # 종목수×페이지수 연사로 유량(5req/s) 429 를 유발했음(2026-08-05).
    tranche_map = kt.fetch_holdings_tranche_map()   # None = 조회실패
    if tranche_map is None:
        # 실패를 '잔고 0'으로 오판하면 가짜 체결보고·'청산완료' 조기종료가 남 → 틱 스킵.
        _log_once("sellhdr-fail", "  ⚠️ 잔고 통합조회 실패 — 이번 매도 틱 대기")
        return

    for code in list(targets.keys())[:MAX_CODES]:
        t = targets[code]
        code = _fmt(code)
        name = t.get("name", "")
        sell_price = int(t.get("sell_price", 0) or 0)
        if sell_price <= 0:
            continue

        positions = tranche_map.get(code, [])   # [] = 보유 없음
        held = sum(p["qty"] for p in positions)

        cst = codes_state.get(code)
        if cst is None:
            cst = codes_state[code] = _new_sell_cst(held)
            changed = True

        # ── 체결 판정: 보유 감소분 = 매도체결(우리 sweep) → 보고
        if held < cst["held_seen"]:
            delta = cst["held_seen"] - held
            cst["sold"] += delta
            report.append(f"✅ 매도체결 {name}({code}) -{delta}주 → 잔여보유 {held}주")
            _log_once(f"sell:{code}", f"  ✅ 매도체결 {name}({code}) -{delta}주 → 잔여 {held}주")
        if held != cst["held_seen"]:
            cst["held_seen"] = held
            changed = True

        # 상한가 해제매도 이후: plan 기준가(잠김 당시 상한가일 수 있음) 대신 해제매도가(상한가-3%) 이하로 추격
        if cst.get("upl_break_price"):
            sell_price = min(sell_price, int(cst["upl_break_price"]))
        # 상한가 잠김 보류 중 동시호가 진입 → 종가매도(지정가·시장가)도 보류 = 잠긴 채 마감이면 보유 유지
        if cst.get("upl_lock") and _is_closing_auction():
            _log_once(f"sell:{code}", f"  🔒 {name}({code}) 상한가 잠김 유지 — 동시호가 종가매도 보류")
            continue

        # ── 장마감 동시호가(15:20~) raw<0: 시장가 전량청산. 호가 sweep 불가 시간대라 시장가로 던진다.
        #    잔고가 현금/신용/담보 트랜치로 쪼개져 있으면 트랜치마다 별도 주문이 필요
        #    (신용·담보 kt10007 은 대출일자 지정) → 전 트랜치를 한 틱에 루프 발주하고,
        #    대출일 키별 발주완료(close_ordered_keys)를 기록해 실패 트랜치만 다음 틱 재시도.
        #    발주된 주문은 종가 체결(15:30)까지 유지(취소/재발주 안 함).
        if t.get("reason") == "raw_neg_close":
            if held < 1 or not positions:
                continue
            done_keys = set(cst.get("close_ordered_keys") or [])
            if not done_keys:   # 장중 sweep 미체결(트랜치별 복수) → 취소 후 시장가 전환(최초 1회)
                _prev = list(cst.get("open_orders") or [])
                if cst.get("open_order_no"):   # 구버전 단일필드 호환
                    _prev.append({"no": cst["open_order_no"], "loan_dt": cst.get("open_loan_dt", "")})
                    cst["open_order_no"] = ""; cst["open_order_qty"] = 0
                    cst["open_order_price"] = 0; cst["open_loan_dt"] = ""
                if _prev:
                    opens = kt.fetch_open_sell_orders(code)
                    for od in _prev:
                        mine = next((x for x in opens if x["order_no"] == od.get("no")), None)
                        if mine and mine.get("remaining_qty", 0) > 0:
                            kt.cancel_order(od["no"], code, mine["remaining_qty"], od.get("loan_dt", ""))
                    cst["open_orders"] = []
                    changed = True
            positions.sort(key=lambda h: (not bool(h["loan_dt"]), h["loan_dt"]))
            pending = [p for p in positions
                       if p.get("sell_possible_qty", 0) > 0
                       and (p["loan_dt"] or "CASH") not in done_keys]
            if not pending:
                _log_once(f"sell:{code}", f"  ⏳ {name}({code}) 종가 시장가 주문 대기(전 트랜치 발주완료, 체결 15:30)")
                continue
            for pos in pending:
                key = pos["loan_dt"] or "CASH"
                qty = pos["sell_possible_qty"]
                res = kt.place_sell_order(code, qty, 0, pos["loan_dt"], pos.get("crd_type", "00"), market=True)
                if res and res.get("return_code") == 0:
                    ono = res.get("ord_no", "?")
                    done_keys.add(key)
                    changed = True
                    report.append(f"🔴 종가 시장가매도 {name}({code}) {pos['order_type']} {qty}주 (동시호가 raw<0, 주문 {ono})")
                    _log_once(f"sell:{code}:{key}", f"  🔴 종가 시장가매도 {name}({code}) {pos['order_type']} {qty}주 (주문 {ono})")
                else:
                    err = (res or {}).get("return_msg", "응답 없음")
                    report.append(f"❌ 종가 시장가매도 실패 {name}({code}) {pos['order_type']} {qty}주: {err}")
                    _log_once(f"sell:{code}:{key}", f"  ❌ 종가 시장가매도 실패 {name}({code}) {pos['order_type']}: {err}")
            cst["close_ordered_keys"] = sorted(done_keys)
            continue

        # ── 직전 틱 주문 해소(트랜치별 복수): 미체결 잔량 취소(걸어두지 않음), 1틱 대기
        prev_orders = list(cst.get("open_orders") or [])
        if cst.get("open_order_no"):     # 구버전 단일필드 호환(코드 교체 재시작 직후 잔존 상태)
            prev_orders.append({"no": cst["open_order_no"], "qty": cst.get("open_order_qty", 0),
                                "price": cst.get("open_order_price", 0), "loan_dt": cst.get("open_loan_dt", "")})
            cst["open_order_no"] = ""; cst["open_order_qty"] = 0
            cst["open_order_price"] = 0; cst["open_loan_dt"] = ""
        if prev_orders and _is_closing_auction():
            # 동시호가(15:20~15:30): 매도주문도 취소/재주문 없이 그대로 유지 — 종가 체결 대기.
            cst["open_orders"] = prev_orders
            if not cst.get("close_sell_hold"):
                cst["close_sell_hold"] = True
                report.append(f"🕒 동시호가 — {name}({code}) 매도주문 유지 · 종가 체결 대기(장마감 후 결과 알림)")
            changed = True
            _log_once(f"sell:{code}", f"  🕒 {name}({code}) 동시호가 — 매도주문 유지, 종가 체결 대기")
            continue
        if prev_orders:
            opens = kt.fetch_open_sell_orders(code)
            for od in prev_orders:
                mine = next((x for x in opens if x["order_no"] == od.get("no")), None)
                if mine is not None and mine.get("remaining_qty", 0) > 0:
                    if kt.cancel_order(od["no"], code, mine["remaining_qty"], od.get("loan_dt", "")):
                        report.append(f"🚫 미체결취소 {name}({code}) 잔량 {mine['remaining_qty']}주×{od.get('price', 0):,}원")
            cst["open_orders"] = []
            changed = True
            _log_once(f"sell:{code}", f"  🔄 {name}({code}) 직전 매도주문 해소 → 이번 틱 대기")
            continue

        if held < 1 or not positions:   # 전량청산 완료 → 더 팔 것 없음
            _log_once(f"sell:{code}", f"  ✔️ {name}({code}) 청산완료(잔여 {held}주) — 매도 없음")
            continue

        # ── 동시호가(15:20~15:30): sweep 불가 — 트랜치별 지정가 1회 주문 후 종가 체결 대기.
        if _is_closing_auction():
            positions.sort(key=lambda h: (not bool(h["loan_dt"]), h["loan_dt"]))
            sellable = [p for p in positions if p.get("sell_possible_qty", 0) > 0]
            if not sellable:
                continue
            new_orders = []
            for pos in sellable:
                qty = pos["sell_possible_qty"]
                res = kt.place_sell_order(code, qty, sell_price, pos["loan_dt"], pos.get("crd_type", "00"))
                if res and res.get("return_code") == 0:
                    ono = res.get("ord_no", "?")
                    new_orders.append({"no": ono, "qty": qty, "price": sell_price, "loan_dt": pos["loan_dt"]})
                    report.append(f"🕒 동시호가 지정가매도 {name}({code}) {pos['order_type']} {qty}주×{sell_price:,}원 "
                                  f"— 종가 체결 대기(장마감 후 결과 알림, 주문 {ono})")
                    _log_once(f"sell:{code}:{pos['loan_dt'] or 'CASH'}",
                              f"  🕒 동시호가 지정가매도 {name}({code}) {qty}주×{sell_price:,}원 (주문 {ono})")
                else:
                    err = (res or {}).get("return_msg", "응답 없음")
                    report.append(f"❌ 동시호가 매도실패 {name}({code}) {pos['order_type']}: {err}")
                    _log_once(f"sell:{code}:{pos['loan_dt'] or 'CASH'}", f"  ❌ 동시호가 매도실패 {name}({code}): {err}")
            if new_orders:
                cst["open_orders"] = new_orders
                cst["close_sell_hold"] = True
                changed = True
            continue

        # ── 매수호가 sweep: sell_price 이상(포함)에 쌓인 잔량만큼만(=즉시 체결 수량)
        avail, best_bid = kt.bid_qty_at_or_above(code, sell_price)

        # ── 상한가 잠김 가드: 매수최우선=상한가 & 매도호가 없음 → 매도 보류, 1초 감시(_upl_guard_tick)로 전환.
        #    이미 잠김 보류 중이면 호가를 직접 재확인해 풀렸을 때 해제매도(1초 감시가 놓친 경우 보완).
        #    호가 조회 실패는 이번 틱 대기(실패를 '해제'나 '비잠김'으로 오판해 상한가에 던지지 않음).
        upl = _upper_limit(code)
        if upl and (cst.get("upl_lock") or best_bid >= upl):
            book = kt.fetch_book(code)
            if book is None:
                _log_once(f"sell:{code}", f"  ⚠️ {name}({code}) 상한가 부근 호가 조회 실패 — 이번 틱 대기")
                continue
            if _is_upl_locked(book, upl):
                if not cst.get("upl_lock"):
                    cst["upl_lock"] = {"upl": upl, "since": datetime.now().strftime("%H:%M:%S")}
                    changed = True
                    report.append(f"🔒 상한가 잠김 {name}({code}) {upl:,}원 — 매도 보류, 1초 감시 "
                                  f"(해제 시 {_upl_break_price(upl):,}원 매도)")
                _log_once(f"sell:{code}", f"  🔒 {name}({code}) 상한가 {upl:,}원 잠김 — 매도 보류(1초 감시)")
                continue
            if cst.get("upl_lock"):
                _fire_upl_break(code, name, cst, positions, report)
                changed = True
                continue

        if avail < 1:                   # 기준가 이상 매수호가 없음 → 대기
            _log_once(f"sell:{code}", f"  ⏳ {name}({code}) 보유{held} · 기준가{sell_price:,} 매수최우선 {best_bid:,} — 호가대기")
            continue

        # 담보/신용 포지션 먼저 매도(이자 절감), 그다음 현금.
        # 트랜치 분할 잔고는 '한 틱에' 전 트랜치를 순차 발주(트랜치마다 별도 주문 —
        # 신용·담보 kt10007 은 대출일자 지정 필요). 호가 잔량(avail)을 발주분만큼 차감
        # 배분해, 매수호가가 충분할 때 다음 틱을 기다리다 기회를 놓치지 않는다.
        # 각 주문은 해당 트랜치 매매가능수량 상한이라 과매도 없음. 잔량 소진 시 중단.
        positions.sort(key=lambda h: (not bool(h["loan_dt"]), h["loan_dt"]))
        sellable = [p for p in positions if p.get("sell_possible_qty", 0) > 0]
        if not sellable:                # 매매가능수량 없음 → 대기
            _log_once(f"sell:{code}", f"  ⏳ {name}({code}) 매매가능수량 0 — 대기")
            continue

        rem_avail = avail
        new_orders = []
        for pos in sellable:
            qty = min(pos["sell_possible_qty"], rem_avail)
            if qty < 1:
                break                   # 호가 잔량 소진 → 남은 트랜치는 다음 틱
            _tkey = pos["loan_dt"] or "CASH"
            # 정규장 sweep 은 SOR(KRX/NXT 최선 라우팅). 거부 시 함수 내부에서 KRX 폴백.
            res = kt.place_sell_order(code, qty, sell_price, pos["loan_dt"], pos.get("crd_type", "00"), stex="SOR")
            if res and res.get("return_code") == 0:
                ono = res.get("ord_no", "?")
                new_orders.append({"no": ono, "qty": qty, "price": sell_price, "loan_dt": pos["loan_dt"]})
                rem_avail -= qty
                report.append(f"🔴 sweep매도 {name}({code}) {pos['order_type']} {qty}주×{sell_price:,}원 "
                              f"(보유 {held}·호가 {avail}·최우선 {best_bid:,}, 주문 {ono})")
                _log_once(f"sell:{code}:{_tkey}", f"  🔴 sweep매도 {name}({code}) {pos['order_type']} {qty}주×{sell_price:,}원 (주문 {ono})")
            else:
                err = (res or {}).get("return_msg", "응답 없음")
                report.append(f"❌ 매도실패 {name}({code}) {pos['order_type']} {qty}주: {err}")
                _log_once(f"sell:{code}:{_tkey}", f"  ❌ 매도실패 {name}({code}) {pos['order_type']}: {err}")
        if new_orders:
            cst["open_orders"] = new_orders
            changed = True

    # 상한가 잠김 보류 종목 → 1초 감시 대상 재구성(plan 에서 빠진 종목은 감시 중단)
    _names = {_fmt(c): (v or {}).get("name", "") for c, v in targets.items()}
    _upl_guard.clear()
    for c, s in codes_state.items():
        if s.get("upl_lock") and c in _names:
            _upl_guard[c] = {"name": _names[c], "upl": int(s["upl_lock"]["upl"])}

    if changed:
        _save_json(_sell_exec_path(today_str), exec_state)
    if report:
        _send_owner("📤 실시간 sweep 매도\n" + "\n".join(report))


# ── 장마감 후: 동시호가 주문 체결 확인·알림(1회) ────────────────────────────
def _maybe_post_close_report(today_str):
    """15:30 종가 단일가 체결 확인. 동시호가에 유지한 매수/매도 주문(+종가 시장가 매도)의
       체결 여부를 보유잔고 변화로 판정해 장마감 후 1회만 텔레그램 알림(15:30:30~17:00 사이 실행)."""
    now = datetime.now()
    sec = now.hour * 3600 + now.minute * 60 + now.second
    if now.weekday() >= 5 or not (15 * 3600 + 30 * 60 + 30 <= sec <= 17 * 3600):
        return

    exec_state = _load_json(_exec_path(today_str), None)
    sell_exec  = _load_json(_sell_exec_path(today_str), None)
    if not isinstance(exec_state, dict) or exec_state.get("date") != today_str:
        exec_state = None
    if not isinstance(sell_exec, dict) or sell_exec.get("date") != today_str:
        sell_exec = None
    if (exec_state or {}).get("close_report_sent") or (sell_exec or {}).get("close_report_sent"):
        return                                  # 오늘 이미 보냄

    buy_holds = {c: v for c, v in ((exec_state or {}).get("codes") or {}).items()
                 if (v or {}).get("close_buy_hold")}
    sell_holds = {c: v for c, v in ((sell_exec or {}).get("codes") or {}).items()
                  if (v or {}).get("close_sell_hold") or (v or {}).get("close_ordered_keys")}
    if not buy_holds and not sell_holds:
        return                                  # 오늘 동시호가 주문 없음

    holdings = kt.fetch_all_holdings()
    if holdings is None:
        return                                  # 조회 실패 → 다음 폴링에 재시도
    held_map = {_fmt(h.get("code", "")): int(h.get("qty", 0) or 0) for h in holdings}

    bt = (_load_json(_plan_path(today_str), {}) or {}).get("targets") or {}
    st = (_load_json(_sell_plan_path(today_str), {}) or {}).get("targets") or {}

    def _tname(targets, code6):
        for k, v in targets.items():
            if _fmt(k) == code6:
                return (v or {}).get("name", "")
        return ""

    lines = []
    for code, cst in buy_holds.items():
        code6 = _fmt(code)
        name = _tname(bt, code6)
        qty = int(cst.get("open_order_qty", 0) or 0)
        price = int(cst.get("open_order_price", 0) or 0)
        held = held_map.get(code6, 0)
        delta = held - int(cst.get("held_seen", 0) or 0)
        if qty > 0 and delta >= qty:
            lines.append(f"✅ 매수 종가체결 {name}({code6}) {qty}주×{price:,}원 → 보유 {held}주")
        elif delta > 0:
            lines.append(f"◑ 매수 일부체결 {name}({code6}) {delta}/{qty}주×{price:,}원 → 보유 {held}주 (잔여 주문 자동만료)")
        else:
            lines.append(f"❌ 매수 미체결 {name}({code6}) {qty}주×{price:,}원 — 주문 자동만료(종가 > 지정가)")
    for code, cst in sell_holds.items():
        code6 = _fmt(code)
        name = _tname(st, code6)
        held = held_map.get(code6, 0)
        before = int(cst.get("held_seen", 0) or 0)
        delta = before - held
        if before > 0 and held < 1:
            lines.append(f"✅ 매도 종가체결 {name}({code6}) 전량 {before}주 청산 완료")
        elif delta > 0:
            lines.append(f"◑ 매도 일부체결 {name}({code6}) {delta}주 → 잔여보유 {held}주 (잔여 주문 자동만료)")
        else:
            lines.append(f"❌ 매도 미체결 {name}({code6}) 보유 {held}주 그대로 — 주문 자동만료")

    if lines:
        _send_owner("🔔 장마감(15:30) 종가 체결 결과\n" + "\n".join(lines))
    if exec_state is not None:
        exec_state["close_report_sent"] = True
        _save_json(_exec_path(today_str), exec_state)
    if sell_exec is not None:
        sell_exec["close_report_sent"] = True
        _save_json(_sell_exec_path(today_str), sell_exec)


def run_forever():
    if not getattr(secrets, "AUTO_BUY_ENABLED", False):
        print("ℹ️ AUTO_BUY_ENABLED=False — execution_monitor 대기만 함")
    print(f"🚀 execution_monitor 시작 (폴링 {POLL_SEC}s · 최대 {MAX_CODES}종목)")
    print(f"   · 매수/매도는 KRX 정규장(09:00~15:30) + 정규장 점수 {SCORE_MIN:.0f}+ 에만 집행")
    print("   · 장전/장후 NXT 점수는 매매에 관여하지 않음(참고용)")
    try:
        auth.get_access_token()        # 토큰 워밍업
    except Exception as e:
        print(f"   ⚠️ 토큰 준비 실패: {e}")
    last_beat = 0.0
    while True:
        try:
            today = datetime.now().strftime("%Y%m%d")
            now = time.time()
            if now - last_beat > 300:      # 5분마다 생존/상태 하트비트(감시 중이 아닐 때도 표시)
                st = "정규장(집행 ON)" if kt.is_market_open() else "정규장 외(NXT 매매 안 함·참고만)"
                _log(f"💓 execution_monitor 대기/감시 중 · {st}")
                last_beat = now
            _tick(today)          # 매수 sweep (KRX 정규장에만 동작)
            _rotate_tick(today)   # 순위교체 매도 (신규종목 매수자금 부족 시, 정규장에만)
            _sell_tick(today)     # 매도 sweep (KRX 정규장에만 동작)
            _maybe_post_close_report(today)   # 장마감 후 동시호가 주문 체결 결과 1회 알림
        except Exception as e:
            print(f"   ⚠️ tick 오류: {e}")
        # 다음 틱까지 대기 — 상한가 잠김 보류 종목이 있으면 그 종목만 1초 간격으로 감시
        deadline = time.time() + POLL_SEC
        while True:
            rem = deadline - time.time()
            if rem <= 0:
                break
            time.sleep(min(UPL_GUARD_SEC, rem) if _upl_guard else rem)
            if _upl_guard:
                try:
                    _upl_guard_tick(datetime.now().strftime("%Y%m%d"))
                except Exception as e:
                    print(f"   ⚠️ 상한가 감시 오류: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        # 디버그: 1틱만 실행
        run_today = sys.argv[2] if len(sys.argv) > 2 else datetime.now().strftime("%Y%m%d")
        _tick(run_today)
        _rotate_tick(run_today)
        _sell_tick(run_today)
    else:
        run_forever()
