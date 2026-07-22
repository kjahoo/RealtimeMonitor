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
  - 매매는 KRX 정규장(09:00~15:30)에만. 매수 직전 '정규장 점수'가 SCORE_MIN(50)+ 인지 재확인한다.
    ▶ NXT(장전/장후) 점수는 매수/매도에 관여하지 않는다(참고용). 정규장 시각 행만 사용.
  - 콘솔(모니터 창)에 매 틱 감시 현황·체결 현황을 출력한다(무엇을 보고 왜 대기/집행하는지).

상태파일 (logs/):
  {날짜}_autobuy_plan.json   (읽기) auto_buy 가 쓴 종목별 목표수량·기준가
  {날짜}_autobuy_exec.json   (쓰기) 종목별 체결추적·미체결주문 상태
"""

import os
import sys
import json
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

LOG_DIR = secrets.LOCAL_DATA_PATH
OWNER_ID = str(secrets.TELEGRAM_CHAT_ID)

POLL_SEC  = float(getattr(secrets, "MONITOR_POLL_SEC", 4))     # 호가 폴링 주기(초)
MAX_CODES = int(getattr(secrets, "MONITOR_MAX_CODES", 15))     # 동시 감시 종목 상한(rate-limit)
SCORE_MIN = 50.0                                              # 매수 직전 재확인 최소 점수(점) — auto_buy plan 기준(2안 확대: 진입 50점)과 통일
SMOOTHED_SELL_THRESH = 0.20                                   # 평활 하락(청산 예정) 매수 금지 임계 — sell_strategy_b.SELL_THRESH 와 동일


def _fmt(x):
    s = str(x).strip().split(".")[0]
    return s.zfill(6) if s.isdigit() else s


def _p(name):
    return os.path.join(LOG_DIR, name)


def _plan_path(d):  return _p(f"{d}_autobuy_plan.json")
def _exec_path(d):  return _p(f"{d}_autobuy_exec.json")
def _sell_plan_path(d):  return _p(f"{d}_autosell_plan.json")
def _sell_exec_path(d):  return _p(f"{d}_autosell_exec.json")


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


def _is_krx_session_ts(ts):
    """'YYYY-MM-DD HH:MM:SS' 가 KRX 정규장(09:00~15:30) 시각인가.
       장전/장후 NXT 세션 행을 매매 판단에서 배제하기 위한 필터. 파싱 실패 시 제외(False)."""
    try:
        h, m, s = (int(x) for x in ts.split(" ")[1].split(":"))
        sec = h * 3600 + m * 60 + s
        return 9 * 3600 <= sec <= 15 * 3600 + 30 * 60
    except Exception:
        return False


def _load_live_scores(today_str):
    """코드별 '정규장' 최신 점수(×100). Stock_V3(폴백) → promising(Search_History, 우선)으로 덮어씀.
       매수 직전 '현재도 SCORE_MIN(50)+인가' 재확인용.
       ※ NXT 점수 배제: Search_History 는 KRX 정규장(09:00~15:30) 시각 행만 사용한다.
         장전/장후 NXT 세션의 순간 점수는 매매에 관여시키지 않는다(참고용일 뿐)."""
    import csv
    scores = {}
    v3 = _p(f"{today_str}_Stock_V3.csv")
    if os.path.exists(v3):
        try:
            with open(v3, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    c = _fmt(row.get("code", ""))
                    if not c:
                        continue
                    try:
                        scores[c] = float(row.get("score_total", 0) or 0) * 100.0
                    except Exception:
                        pass
        except Exception as e:
            print(f"   ⚠️ Stock_V3 점수 로드 실패: {e}")
    hist = _p(f"{today_str}_Search_History.csv")
    if os.path.exists(hist):
        try:
            latest_ts = {}
            with open(hist, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    c = _fmt(row.get("code", ""))
                    ts = (row.get("timestamp") or "").strip()
                    if not c or not _is_krx_session_ts(ts):   # NXT/장외 시각 행 제외
                        continue
                    if c not in latest_ts or ts >= latest_ts[c]:
                        latest_ts[c] = ts
                        try:
                            scores[c] = float(row.get("total_score", 0) or 0) * 100.0
                        except Exception:
                            pass
        except Exception as e:
            print(f"   ⚠️ Search_History 점수 로드 실패: {e}")
    return scores


# ── 1 틱: plan 을 읽어 호가 sweep 집행 ────────────────────────────────────
def _tick(today_str):
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

    cash = kt.fetch_order_cash()
    if cash is None:
        return
    if cash < 1:
        return          # 미수 없이 매수 가능한 현금(D+2) 0 → 조용히 대기(매 틱 알림 방지)

    live_scores = _load_live_scores(today_str)   # 매수 직전 SCORE_MIN(50)+ 재확인용(전 종목 공용, 1회 로드)

    exec_state = _load_json(_exec_path(today_str), None)
    if not isinstance(exec_state, dict) or exec_state.get("date") != today_str:
        exec_state = {"date": today_str, "codes": {}}
    codes_state = exec_state["codes"]

    # 오늘 이미 매도 체결(손절/청산)된 종목 → 당일 재매수 금지(sell→buy churn 방지)
    sell_exec = _load_json(_sell_exec_path(today_str), None)
    sold_today = set()
    if isinstance(sell_exec, dict) and sell_exec.get("date") == today_str:
        sold_today = {_fmt(c) for c, v in (sell_exec.get("codes") or {}).items()
                      if int((v or {}).get("sold", 0) or 0) > 0}

    # 평활 하락(청산 예정) 종목 → 매수 금지. sell_state_b.json(=Update_Promising 이 갱신)에서
    # last_smoothed < 0.20 인 종목은 곧 팔릴/약해지는 종목이므로, raw 가 잠깐 50+로 튀어도 사지 않음.
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

        # ── 당일 매도체결 종목 재매수 금지(실격 → 예약 없음: 현금은 하위 유효종목으로).
        if code in sold_today:
            _log_once(f"buy:{code}", f"  ⛔ {name}({code}) 오늘 매도체결 → 당일 재매수 금지")
            if not cst.get("buy_locked"):
                cst["buy_locked"] = True
                changed = True
                report.append(f"⛔ {name}({code}) 오늘 매도체결 → 당일 재매수 금지(churn 방지)")
            continue

        # ── 평활 하락(청산 예정) 종목 매수 금지(실격 → 예약 없음).
        if code in sell_warn:
            _log_once(f"buy:{code}", f"  ⏭️ {name}({code}) 평활 {sell_warn[code]*100:.1f}점<20 하락구간 — 매수 보류")
            if not cst.get("smwarn"):
                cst["smwarn"] = True
                changed = True
                report.append(f"⏭️ {name}({code}) 평활 {sell_warn[code]*100:.1f}점<20(청산예정) — 매수 보류")
            continue
        elif cst.get("smwarn"):
            cst["smwarn"] = False        # 평활 회복(≥20) → 다음 하락 시 재알림
            changed = True

        # ── 매수 직전 실시간 점수 재확인(순간 스파이크 매수 방지)(실격 → 예약 없음).
        live_sc = live_scores.get(code)
        if live_sc is None or live_sc < SCORE_MIN:
            shown = "미확인(정규장 점수없음)" if live_sc is None else f"{live_sc:.0f}"
            _log_once(f"buy:{code}", f"  ⏭️ {name}({code}) 잔여{remaining} · 현재점수 {shown}<{SCORE_MIN:.0f} — 매수 보류")
            if cst.get("last_skip_score") != shown:      # 같은 사유 반복 알림 방지
                report.append(f"⏭️ {name}({code}) 현재점수 {shown}<{SCORE_MIN:.0f} — 매수 보류(스파이크 방지)")
                cst["last_skip_score"] = shown
                changed = True
            continue
        if cst.get("last_skip_score") is not None:
            cst["last_skip_score"] = None                # 점수 회복 → 다음 보류 시 재알림
            changed = True

        # ── 예산 배분: 이 종목 몫 = base_cash − (상위 유효종목 예약분). 그 뒤 이 종목도 예약한다.
        is_top_valid = (committed == 0)          # 아직 상위 유효 미완료 종목이 없음 = 이 종목이 최우선
        budget = max(0, base_cash - committed)
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

        res = kt.place_buy_order(code, qty, price)
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


# ── 매도 sweep ────────────────────────────────────────────────────────────
def _new_sell_cst(held):
    """종목별 매도 실행상태 초기값. held_seen = 베이스라인(체결=보유 감소로 판정)."""
    return {"held_seen": int(held), "open_order_no": "", "open_order_qty": 0,
            "open_order_price": 0, "open_loan_dt": "", "sold": 0}


# ── 1 틱: autosell_plan 을 읽어 '매수호가 sweep' 으로 전량청산 집행 ──────────
def _sell_tick(today_str):
    if not getattr(secrets, "AUTO_SELL_ENABLED", False):
        return
    if not kt.is_market_open():         # 평일 09:00~15:30 외에는 무동작
        return

    plan = _load_json(_sell_plan_path(today_str), None)
    if not isinstance(plan, dict) or plan.get("date") != today_str:
        return                          # 오늘자 매도 plan 아직 없음
    targets = plan.get("targets") or {}
    if not targets:
        return

    exec_state = _load_json(_sell_exec_path(today_str), None)
    if not isinstance(exec_state, dict) or exec_state.get("date") != today_str:
        exec_state = {"date": today_str, "codes": {}}
    codes_state = exec_state["codes"]

    report, changed = [], False
    _log_once("sellhdr", f"📤 매도감시 {min(len(targets), MAX_CODES)}종목")

    for code in list(targets.keys())[:MAX_CODES]:
        t = targets[code]
        code = _fmt(code)
        name = t.get("name", "")
        sell_price = int(t.get("sell_price", 0) or 0)
        if sell_price <= 0:
            continue

        positions = kt.fetch_stock_holdings(code)   # None = 보유 없음/조회실패
        held = sum(p["qty"] for p in positions) if positions else 0

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

        # ── 직전 틱 주문 해소: 미체결 잔량 남았으면 취소(걸어두지 않음), 1틱 대기
        if cst.get("open_order_no"):
            opens = kt.fetch_open_sell_orders(code)
            mine = next((x for x in opens if x["order_no"] == cst["open_order_no"]), None)
            if mine is not None and mine.get("remaining_qty", 0) > 0:
                if kt.cancel_order(cst["open_order_no"], code, mine["remaining_qty"], cst.get("open_loan_dt", "")):
                    report.append(f"🚫 미체결취소 {name}({code}) 잔량 {mine['remaining_qty']}주×{sell_price:,}원")
            cst["open_order_no"] = ""; cst["open_order_qty"] = 0
            cst["open_order_price"] = 0; cst["open_loan_dt"] = ""
            changed = True
            _log_once(f"sell:{code}", f"  🔄 {name}({code}) 직전 매도주문 해소 → 이번 틱 대기")
            continue

        if held < 1 or not positions:   # 전량청산 완료 → 더 팔 것 없음
            _log_once(f"sell:{code}", f"  ✔️ {name}({code}) 청산완료(잔여 {held}주) — 매도 없음")
            continue

        # ── 매수호가 sweep: sell_price 이상(포함)에 쌓인 잔량만큼만(=즉시 체결 수량)
        avail, best_bid = kt.bid_qty_at_or_above(code, sell_price)
        if avail < 1:                   # 기준가 이상 매수호가 없음 → 대기
            _log_once(f"sell:{code}", f"  ⏳ {name}({code}) 보유{held} · 기준가{sell_price:,} 매수최우선 {best_bid:,} — 호가대기")
            continue

        # 담보/신용 포지션 먼저 매도(이자 절감), 그다음 현금
        positions.sort(key=lambda h: (not bool(h["loan_dt"]), h["loan_dt"]))
        pos = next((p for p in positions if p.get("sell_possible_qty", 0) > 0), None)
        if pos is None:                 # 매매가능수량 없음 → 대기
            _log_once(f"sell:{code}", f"  ⏳ {name}({code}) 매매가능수량 0 — 대기")
            continue

        qty = min(held, avail, pos["sell_possible_qty"])
        if qty < 1:
            continue

        res = kt.place_sell_order(code, qty, sell_price, pos["loan_dt"], pos.get("crd_type", "00"))
        if res and res.get("return_code") == 0:
            ono = res.get("ord_no", "?")
            cst["open_order_no"] = ono
            cst["open_order_qty"] = qty
            cst["open_order_price"] = sell_price
            cst["open_loan_dt"] = pos["loan_dt"]
            changed = True
            report.append(f"🔴 sweep매도 {name}({code}) {qty}주×{sell_price:,}원 "
                          f"(보유 {held}·호가 {avail}·최우선 {best_bid:,}, 주문 {ono})")
            _log_once(f"sell:{code}", f"  🔴 sweep매도 {name}({code}) {qty}주×{sell_price:,}원 (보유{held}·호가{avail}, 주문 {ono})")
        else:
            err = (res or {}).get("return_msg", "응답 없음")
            report.append(f"❌ 매도실패 {name}({code}): {err}")
            _log_once(f"sell:{code}", f"  ❌ 매도실패 {name}({code}): {err}")

    if changed:
        _save_json(_sell_exec_path(today_str), exec_state)
    if report:
        _send_owner("📤 실시간 sweep 매도\n" + "\n".join(report))


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
            _sell_tick(today)     # 매도 sweep (KRX 정규장에만 동작)
        except Exception as e:
            print(f"   ⚠️ tick 오류: {e}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        # 디버그: 1틱만 실행
        run_today = sys.argv[2] if len(sys.argv) > 2 else datetime.now().strftime("%Y%m%d")
        _tick(run_today)
        _sell_tick(run_today)
    else:
        run_forever()
