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
  - 현금 배분: plan 의 AI점수 상위(삽입순) 우선·선착순. 현금부족 시 그 틱 중단.
  - '걸어두지 않음': 직전 틱에 낸 미체결 잔량이 남아 있으면 취소하고 다시 sweep.
  - 우리(이 데몬)가 낸 주문만 추적·취소. '직접 낸 주문'은 절대 건드리지 않음.
  - 호가 lag/보유 lag 안전장치: 한 종목에 동시 1개 주문만. 주문 해소(체결/취소)된
    틱에는 그 종목 재주문을 건너뛰어 보유잔고가 갱신될 시간을 준다(중복매수 방지).
  - 매매는 KRX 정규장(09:00~15:30)에만. 매수 직전 '정규장 점수'가 60+ 인지 재확인한다.
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
SCORE_MIN = 60.0                                              # 매수 직전 재확인 최소 점수(점)


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
       매수 직전 '현재도 60+인가' 재확인용.
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

    live_scores = _load_live_scores(today_str)   # 매수 직전 60+ 재확인용(전 종목 공용, 1회 로드)

    exec_state = _load_json(_exec_path(today_str), None)
    if not isinstance(exec_state, dict) or exec_state.get("date") != today_str:
        exec_state = {"date": today_str, "codes": {}}
    codes_state = exec_state["codes"]

    report, changed = [], False
    _log_once("buyhdr", f"🛒 매수감시 {min(len(targets), MAX_CODES)}종목 · 주문가능현금 {cash:,}원")

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

        # ── 직전 틱에 낸 주문 해소: 미체결 잔량 남았으면 취소(걸어두지 않음).
        #    체결/취소 어느 쪽이든 이 틱엔 재주문하지 않고 보유 갱신을 1틱 기다린다.
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
            _log_once(f"buy:{code}", f"  🔄 {name}({code}) 직전주문 해소 → 이번 틱 대기(보유 갱신 대기)")
            continue

        remaining = target_qty - held
        if remaining < 1:               # 목표 달성 → 더 사지 않음
            _log_once(f"buy:{code}", f"  ✔️ {name}({code}) 목표달성 보유{held}/목표{target_qty} — 매수 없음")
            continue

        # ── 매수 직전 실시간 점수 재확인(순간 60+ 스파이크 매수 방지).
        #    plan 은 빌드 시점 점수로 담기므로, 개장 스파이크가 이미 붕괴했으면 여기서 스킵.
        #    점수 정보를 못 구하면(파일 없음 등) 안전하게 매수 보류.
        live_sc = live_scores.get(code)
        if live_sc is None or live_sc < SCORE_MIN:
            shown = "미확인(정규장 점수없음)" if live_sc is None else f"{live_sc:.0f}"
            _log_once(f"buy:{code}", f"  ⏭️ {name}({code}) 잔여{remaining} · 현재점수 {shown}<60 — 매수 보류")
            if cst.get("last_skip_score") != shown:      # 같은 사유 반복 알림 방지
                report.append(f"⏭️ {name}({code}) 현재점수 {shown}<60 — 매수 보류(스파이크 방지)")
                cst["last_skip_score"] = shown
                changed = True
            continue
        if cst.get("last_skip_score") is not None:
            cst["last_skip_score"] = None                # 점수 회복 → 다음 보류 시 재알림
            changed = True

        # ── 매도호가 sweep: promising 이하에 쌓인 잔량만큼만(=즉시 체결 수량)
        avail, best_ask = kt.ask_qty_at_or_below(code, price)
        if avail < 1:                   # 우리 가격 이하 매도호가 없음 → 대기
            _log_once(f"buy:{code}", f"  ⏳ {name}({code}) 잔여{remaining} · 점수{live_sc:.0f} · 지정가{price:,} "
                      f"매도최우선 {best_ask:,} — 호가대기")
            continue

        qty = min(remaining, avail)

        # ── 주문 직전 현금 실시간 재조회(미수 방지 최종 게이트).
        #    틱 시작 스냅샷(cash·로컬차감)과 실시간값 중 '작은 쪽'으로 수량을 최종 제한한다.
        #    - 로컬 cash: 같은 틱에서 앞서 낸 주문분이 반영됨(실시간값이 늦게 갱신돼도 방어)
        #    - live_cash: 외부(수동·타봇·체결)로 줄어든 변동을 즉시 반영
        live_cash = kt.fetch_order_cash()
        if live_cash is None:
            continue                    # 현금 조회 실패 → 깜깜이 주문 금지(이 종목 이번 틱 건너뜀)
        usable = min(cash, live_cash)
        cash_limited = qty * price > usable
        if cash_limited:
            qty = usable // price
        if qty < 1:
            _log_once(f"buy:{code}", f"  💸 {name}({code}) 현금부족(가용 {usable:,}원<{price:,}원/주) — 매수 중단")
            # 현금부족 알림 도배 방지: 가용액(usable)이 직전 통지값과 다를 때만 1회 전송.
            #   같은 부족 상태가 이어지면 침묵, 가용액이 바뀌면 재알림.
            if exec_state.get("cash_short") != usable:
                report.append(f"💸 현금부족(가용 {usable:,}원<{price:,}원/주) — 매수 보류"
                              f"(가용액 변동 시 재알림)")
                exec_state["cash_short"] = usable
                changed = True
            break

        res = kt.place_buy_order(code, qty, price)
        if res and res.get("return_code") == 0:
            ono = res.get("ord_no", "?")
            amt = qty * price
            cash -= amt
            exec_state["cash_short"] = None   # 매수 성공 → 부족 마커 해제(다음 부족 시 재알림)
            cst["open_order_no"] = ono
            cst["open_order_qty"] = qty
            cst["open_order_price"] = price
            changed = True
            tag = " ·현금한도" if cash_limited else ""
            report.append(f"🟢 sweep매수 {name}({code}) {qty}주×{price:,}원 = {amt:,}원 "
                          f"(잔여 {remaining}·호가 {avail}·최우선 {best_ask:,}{tag}, 주문 {ono})")
            _log_once(f"buy:{code}", f"  🟢 sweep매수 {name}({code}) {qty}주×{price:,}원 = {amt:,}원 "
                      f"(잔여{remaining}·호가{avail}{tag}, 주문 {ono})")
            if cash_limited:
                report.append("⏹️ 현금한도 도달 — 이번 틱 중단(다음 틱 AI 1위부터 재개)")
                break
        else:
            err = (res or {}).get("return_msg", "응답 없음")
            report.append(f"❌ 매수실패 {name}({code}): {err}")
            _log_once(f"buy:{code}", f"  ❌ 매수실패 {name}({code}): {err}")

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
    print("   · 매수/매도는 KRX 정규장(09:00~15:30) + 정규장 점수 60+ 에만 집행")
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
