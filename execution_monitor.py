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
    '순위교체 매도': 보유비중(평가금액) 순위대로 '수수료·세금 차감 후 순익 3% 이상'(매수최우선 > 기준가) 종목을 부족분만큼만 매도해
    자금을 마련한다(_rotate_tick). 기존 보유 추가매수를 위해서는 팔지 않는다. 교체매도 종목은 당일 재매수 금지.
    제외: 오늘 우리가 매수체결한 종목·매도 plan 종목·신규 plan 종목·수기영역(ETF/마스터 미등재)·상한가.
    (2026-10-29) 담보대출·신용 물량은 팔지 않고 현금 물량만 매도(전부 담보·신용인 종목은 제외, ROT_CASH_ONLY).
    끄기: ROTATE_ENABLED=False. 롤백: logs/execution_monitor.py.bak_20261007_rotate
  - (2026-11-09) 신용매수 우선: plan target.credit 종목은 신용(kt10006)으로 2배 목표(target_qty)까지 sweep.
    주문 직전 신용주문가능금액(kt00012)을 실시간 확인(현금의 live_cash 역할), 신용여력 부족·신용주문 거부면
    CREDIT_RETRY_SEC 동안 현금·당초 비중 목표(cash_target_qty)로 대체 후 신용 재시도. 현금 예약·예산·순위교체
    필요액은 '현금 소요' 기준(신용 = 잔여금액×보증금율, 현금 = 전액)으로 같은 top-down 규칙 적용.
    신용 미체결 주문 취소는 kt10009. 운영 스위치 credit_buy(텔레그램 /신용 켜기|끄기)를 매 틱 읽어 꺼지면 즉시
    전 종목 현금·당초 비중. 감시 슬롯(MAX_CODES)은 할 일이 남은 종목에만 배정.
    신용 종목 예산은 현금이 아니라 '신용주문가능금액'을 top-down 예약(보증금이 대용으로 충당돼 현금과 무관할 수 있음).
  - (2026-11-09) NXT 애프터 신용매수(_tick session="NXT", 15:35~19:55, 스위치 nxt_buy): 오늘 plan 을 장후 1회 재산출한 뒤
    신용 종목만 NXT 주문(kt10006, NXT)으로 신용 2배 목표까지 sweep.
    (2026-11-11 개정) 매수 조건 = NXT 시장 평균(전 NXT 종목 시총가중, 오늘 KRX 종가 대비 등락률, nxt_market)이 마이너스일 때만,
    NXT 매도호가가 오늘 종가 × (1 + 시장평균 − NXT_REL_GAP 3%p) 이하인 잔량(시장보다 3%p 이상 더 빠진 가격). 종전 '종가 −2%' 규칙 폐지.
    점수(정규장 재평가 60+/보조 40)·청산예정·당일매도 재매수 규칙 동일. 신용 불가면 현금 대체 없이 대기.
    롤백: logs/execution_monitor.py.bak_20261109_credit(신용 이전) · .bak_20261109_switch(검토 수정·스위치 이전)
  - (2026-11-09) 수능일 등 KRX 지연 개장일(kt.KRX_LATE_DAYS): 정규장 10:00~16:30 · 동시호가 16:20~16:30 ·
    장마감 체결 리포트 16:30:30~18:00 · NXT 애프터 16:35~19:55 — 모든 장 시각은 kt.krx_t()/krx_open_time()/
    krx_close_time() 로 그날 기준으로 판정. 롤백: logs/execution_monitor.py.bak_20261109_suneung

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
from datetime import datetime, time as dtime

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import secrets
from kis_api import auth, kiwoom_trading as kt, trade_switches
import buy_gate
import nxt_market

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
ROT_MIN_PROFIT = 0.03                                         # (2026-10-27) 수수료·세금 차감 후 순수익률 이 값(3%) 이상일 때만 교체매도
ROT_BLOCK_ON_LOAN = False                                     # 담보대출·융자 잔고만으로는 막지 않음(팔면 상환 후 일부라도 매수여력 생김) — 미수만 차단
ROT_CASH_ONLY  = True                                         # (2026-10-29) 교체매도는 현금 물량만 — 담보대출·신용 물량은 팔면 대출상환에 쓰여 매수현금이 거의 안 나옴
ROT_CASH_MIN_RATIO = 0.5                                      # 교체매도 체결 후 매수현금이 체결대금의 이 비율 미만으로만 늘면 당일 교체매도 중지
ROT_BLOCK_RECHECK_SEC = 60                                    # 교체매도 보류 중 현금·미수 재확인 간격(초)
ROT_KEEP_SCORE = 60.0                                         # (2026-10-14) 현재 정규장 점수 이 값 이상인 보유종목은 교체매도 제외
ROT_MIN_HOLD_TD = 6                                           # (2026-10-27) 교체매도 대상 = 마지막 매수일로부터 거래일 D+6 이상(D+6일부터) — 10-16 D+5 에서 변경
REBUY_DISCOUNT = 0.02                                         # (2026-10-14) 당일 매도 종목 재매수: 매도 순수령 평균 대비 2% 이상 싸야 매수
SELL_FILL_TTL  = 20                                           # 당일 매도체결 집계(ka10076) 캐시 유효시간(초) — 유량 보호
CREDIT_RETRY_SEC = 300                                        # (2026-11-09) 신용주문 거부·신용여력 부족 시 이 시간 동안 현금·당초 비중으로 대체 후 신용 재시도
CREDIT_RATE_DEFAULT = 0.60                                    # plan 에 신용보증금율이 없을 때 신용여력 조회 구간 기본값(보수적 — kt00012 최고 구간)
NXT_REL_GAP = 0.03                                            # (2026-11-11) NXT 애프터 매수: 시장 평균(종가 대비, 음수일 때만)보다 이 폭(3%p) 이상 더 빠진 가격 이하만 매수
NEW_MIN_QTY = 10                                              # (2026-11-10) 신규종목: 주문 후 보유가 이 수량(목표가 더 작으면 목표 전량) 미만인 자투리 주문은 안 냄
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


CLOSE_AUCTION_BASE = dtime(15, 20)    # 평소 기준 종가 단일가 시작 — 그날 시각은 kt.krx_t (수능일 16:20)


def _is_closing_auction(now=None):
    """장마감 동시호가(15:20~15:30, 수능일 16:20~16:30) — 호가 접수만 되고 체결이 없는 시간대.
       이 시간에는 sweep(취소·재주문 반복)이 무의미하므로 '1회 주문 후 유지' 모드로 전환."""
    now = now or datetime.now()
    return kt.krx_t(CLOSE_AUCTION_BASE, now) <= now.time() < kt.krx_close_time(now)


def _load_live_scores(today_str):
    """코드별 '정규장 재평가' 점수(×100) — 매수 직전 '현재도 임계(60/보조 40)+인가' 재확인용.
       buy_gate 공용 규칙: 오늘 Stock_V3 행 중 KRX 봇 기동 경계(since) 이후 계산된 행만 인정.
       재평가 전 종목은 목록에 없음(None) → 매수 보류(재평가 대기).
       ※ Stock_V3 전 행을 시각 필터 없이 쓰던 폴백이 장전 77점을 통과시켜 09:01 매수가 났다
         (2026-09-29 STX엔진). Search_History 는 자동등록·평가후처리가 장전 점수에 현재시각을
         찍을 수 있어 '재평가' 증거가 못 되므로 쓰지 않는다."""
    since = buy_gate.krx_since(LOG_DIR, today_str)
    rows = buy_gate.rescored_rows(buy_gate.load_v3_rows(LOG_DIR, today_str), since, today_str)
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


def _waterfill(members, total):
    """(2026-11-11) 비중 비례 배분. members {code: (비중, 필요금액)} · total 배분 가능 금액 → {code: 배분액(int)}.
       각 종목 몫 = 남은 금액 × 비중/비중합, 단 필요금액이 상한 — 상한에 걸린 종목의 남는 몫은 나머지에 재배분.
       total ≥ 필요금액 합이면 모두 필요금액 전부."""
    out = {c: 0.0 for c in members}
    active = {c for c, (w, need) in members.items() if w > 0 and need > 0}
    rem = float(max(0, total))
    while active and rem > 0.5:
        wsum = sum(members[c][0] for c in active)
        capped = {c for c in active if rem * members[c][0] / wsum >= members[c][1] - out[c]}
        if capped:
            for c in capped:
                rem -= members[c][1] - out[c]
                out[c] = float(members[c][1])
            active -= capped
        else:
            for c in active:
                out[c] += rem * members[c][0] / wsum
            rem = 0.0
    return {c: int(v) for c, v in out.items()}


def _new_small(t, held, qty, target_qty):
    """(2026-11-10) 신규종목(장 시작 시 미보유) 자투리 주문이면 최소 보유 수량을, 아니면 0 을 반환.
       자투리 = 이번 주문 후 보유 < min(NEW_MIN_QTY, 목표수량). 호가 잔량·예산 끝자락으로 1~2주씩 담겨
       소액 종목이 늘어나는 것 방지. 기존 보유 종목(추가매수)은 제한 없음. 이미 10주 이상이면 이후 소량 채움 허용."""
    need = min(NEW_MIN_QTY, max(1, int(target_qty)))
    return need if (t.get("new") and held + qty < need) else 0


def _close_key(p):
    """매도 트랜치 키 — 대출일(신용·담보), 현금은 'CASH'. (2026-11-09) 대출일 없는 신용 트랜치(당일 신용매수분 등)는
       'CRD{구분}' 로 따로 — 현금 트랜치와 키가 겹쳐 실패 트랜치 재시도가 막히지 않게(기존 'CASH' 키는 호환 유지)."""
    ld = str(p.get("loan_dt") or "").strip()
    if ld:
        return ld
    ct = str(p.get("crd_type") or "00").strip()
    return "CASH" if ct in ("", "00") else f"CRD{ct}"


def _nxt_cancel_open(today_str, why):
    """(2026-11-09) NXT 애프터 신용매수가 꺼졌거나(스위치) 매수창이 끝났을 때(19:55~20:00) 직전 틱에 낸 NXT 주문 잔량 취소
       — '걸어두지 않음': 꺼진 뒤 20:00 까지 체결될 수 있는 주문을 남기지 않는다. 취소 실패면 다음 틱 재시도."""
    ex = _load_json(_exec_path(today_str), None)
    if not isinstance(ex, dict) or ex.get("date") != today_str:
        return
    changed, lines = False, []
    for c, cst in (ex.get("codes") or {}).items():
        if not (cst or {}).get("open_order_no") or cst.get("open_order_stex") != "NXT":
            continue
        c6 = _fmt(c)
        mine = next((x for x in kt.fetch_open_buy_orders(c6) if x["order_no"] == cst["open_order_no"]), None)
        if mine is not None and mine.get("remaining_qty"):
            if not kt.cancel_order(cst["open_order_no"], c6, mine["remaining_qty"], credit=True, stex="NXT"):
                continue                     # 취소 실패 → 다음 틱 재시도
            lines.append(f"🚫 NXT 미체결취소 {_hold_names.get(c6, '')}({c6}) 잔량 {mine['remaining_qty']}주 — {why}")
        cst["open_order_no"] = ""; cst["open_order_qty"] = 0; cst["open_order_price"] = 0
        cst["open_order_credit"] = False; cst["open_order_stex"] = ""
        changed = True
    if changed:
        _save_json(_exec_path(today_str), ex)
    if lines:
        _send_owner("🌙 NXT 애프터 신용매수\n" + "\n".join(lines))


def _credit_off(cst, code, name, why, report, key=None, nxt=False):
    """(2026-11-09) 신용매수 불가(신용여력 부족·신용주문 거부) → CREDIT_RETRY_SEC 동안 현금·당초 비중(cash_target_qty)으로
       대체(NXT 애프터는 현금 대체 없이 대기). 같은 사유(key, 없으면 why)는 1회만 알림. 상태가 바뀌었으면 True."""
    cst["credit_off_until"] = time.time() + CREDIT_RETRY_SEC
    then = "신용 재시도 대기(NXT 는 현금 매수 안 함)" if nxt else "현금·당초 비중으로 매수, 이후 신용 재시도"
    _log_once(f"buy:{code}", f"  ⚠️ {name}({code}) 신용매수 불가({why}) — {CREDIT_RETRY_SEC // 60}분간 {then}")
    if cst.get("credit_off_why") != (key or why):
        cst["credit_off_why"] = key or why
        report.append(f"⚠️ {name}({code}) 신용매수 불가({why}) — {CREDIT_RETRY_SEC // 60}분간 {then}")
    return True


_close_cache = {"date": "", "px": {}}     # (2026-11-09) NXT 애프터용 오늘 KRX 종가 캐시 {code: 종가}
_nxt_nobook  = {}                        # {code: 마지막으로 NXT 매도호가가 없던 시각} — NXT 미거래 종목 슬롯 제외용
NXT_NOBOOK_SEC = 600
_nxt_build = {"date": "", "ts": 0.0}     # NXT 애프터 plan 재산출 시도 시각(재시도 간격 보호)


def _krx_close(today_str, code):
    """오늘 KRX 종가(15:30 이후 ka10001 KRX 현재가). 종목당 하루 1회 조회 캐시. 실패 시 0."""
    if _close_cache["date"] != today_str:
        _close_cache.update(date=today_str, px={})
    px = _close_cache["px"].get(code)
    if not px:
        px = kt.fetch_krx_close(code, today_str)   # 1분봉 15:30 종가 단일가 봉 — 장후 애프터마켓 체결가 아님
        if px > 0:
            _close_cache["px"][code] = px
    return px or 0


def _nxt_rebuild_plan(today_str):
    """NXT 애프터 창에서 오늘 plan 을 1회 재산출(auto_buy after=True) — 정규장에 만든 plan 에 신용가능·2배 목표
       (credit/cash_target_qty)가 없거나 장중 신용여력 0 으로 정렬된 경우를 위해. 실패 시 120초 후 재시도."""
    now = time.time()
    if _nxt_build["date"] == today_str and now - _nxt_build["ts"] < 120:
        return None
    _nxt_build.update(date=today_str, ts=now)
    try:
        import auto_buy
        _log("🌙 NXT 애프터 신용매수 — 오늘 plan 장후 재산출(정규장 점수·가격 그대로, 신용 2배 목표)")
        auto_buy.run(today_str, after=True)
    except Exception as e:
        _log(f"  ⚠️ NXT 애프터 plan 재산출 실패: {e}")
        return None
    p = _load_json(_plan_path(today_str), None)
    return p if isinstance(p, dict) and p.get("date") == today_str and p.get("built_after") else None


# ── 1 틱: plan 을 읽어 호가 sweep 집행 ────────────────────────────────────
#   session="KRX"(기본): 정규장 09:00~15:30 sweep — 현금·신용.
#   session="NXT"(2026-11-09): NXT 애프터 신용매수 창(15:35~19:55) — plan 의 신용 종목만, 오늘 KRX 종가 대비
#     (2026-11-11) NXT 시장 평균 등락률 M(음수일 때만) − 3%p 이상 하락한 가격(지정가 = 종가×(1+M−3%p), 호가단위 내림) 이하의 NXT 매도호가만 신용 2배 목표까지
#     sweep(dmst_stex_tp=NXT). 스위치 nxt_buy·credit_buy 둘 다 켜져 있어야 동작. 신용 불가면 현금 대체 없이 대기.
#   신용 종목 예산(2026-11-09 검토 반영): 신용 보증금은 대용(보유주식)으로도 충당되어 현금(d2)과 무관할 수 있으므로
#     (실측: 주문가능현금 3,435원인데 신용주문가능 7,986만원) 현금이 아니라 '신용주문가능금액'을 top-down 예약한다
#     — 상위 신용 종목 몫을 하위 신용 종목이 못 가져감. 현금 종목은 종전대로 현금을 top-down 예약.
def _tick(today_str, session="KRX"):
    global _rot_need
    nxt = (session == "NXT")
    if not nxt:
        _rot_need = []
    if not getattr(secrets, "AUTO_BUY_ENABLED", False):
        return
    if nxt:
        win = kt.is_nxt_after_buy_window()
        sw_on = trade_switches.get("nxt_buy") and trade_switches.get("credit_buy")
        if not (win and sw_on):
            if kt.is_post_close():           # 장후(~20:00): 꺼졌거나 창이 끝났으면 남은 NXT 주문 취소(걸어두지 않음)
                _nxt_cancel_open(today_str, "NXT 매수 꺼짐" if not sw_on else "매수창 종료(19:55)")
            if win and not sw_on:
                _log_once("nxthdr", "🌙 NXT 애프터 신용매수 꺼짐(스위치 nxt_buy/credit_buy) — 매수 안 함")
            return
    elif not kt.is_market_open():        # 평일 09:00~15:30 외에는 무동작
        return

    plan = _load_json(_plan_path(today_str), None)
    if not isinstance(plan, dict) or plan.get("date") != today_str:
        return                          # 오늘자 plan 아직 없음
    if nxt and not plan.get("built_after"):
        plan = _nxt_rebuild_plan(today_str)
        if not plan:
            return                      # 재산출 전/실패 → 다음 틱(재시도 간격 120초)
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

    if nxt:
        cash = 0                        # NXT 애프터는 신용만 — 현금 매수 없음
    else:
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
    # (2026-11-11) NXT 애프터 매수 기준 — 시장 평균(전 NXT 종목 시총가중, 오늘 KRX 종가 대비)이 음수일 때만 '평균 − 3%p'
    nxt_thr, nxt_info = None, ""
    if nxt:
        _m_avg, _m_n, nxt_info = nxt_market.market_avg(today_str)
        if _m_avg is not None and _m_avg < 0:
            nxt_thr = _m_avg - NXT_REL_GAP
    credit_on = trade_switches.get("credit_buy")    # (2026-11-09) 신용매수 우선 스위치 — 끄면 이번 틱부터 전 종목 현금·당초 비중

    # (2026-11-13 재훈님 지시) 정규장 신용 소진: 신용주문가능금액(+내 신용 미체결)으로 어떤 신용 종목도 1주를 못 사면
    #   이번 틱은 신용가능 여부와 무관하게 전 종목을 현금·당초 비중(cash_target_qty)으로 — 현금 풀에서 비중 비례로 함께 배분.
    #   (종전: 신용 종목은 신용불가 종목 배분 후 남은 현금만 받음.) 보증금율 구간별로 조회해 모두 부족할 때만. 조회 실패 = 판정 보류.
    credit_dry, credit_pre = False, {}
    if not nxt and credit_on:
        _now = time.time()
        # 판정 대상 = 이번 틱 실제로 살 수 있는 신용 종목만(배분 블록과 같은 자격: 점수·청산예정·당일매도 재매수 상한,
        #   AI 평가 대기 제외) — 못 사는 저가 종목 하나가 '아직 1주 살 여력 있음'으로 판정을 막지 않도록(11-13 검토)
        _rates, _code0 = {}, {}          # {보증금율: 최저 매수가}, {보증금율: 조회용 종목}
        for c, t in targets.items():
            c6 = _fmt(c)
            px = int(t.get("promising_price", 0) or 0)
            if (not t.get("credit") or px <= 0 or t.get("await_eval")
                    or held_map.get(c6, 0) >= int(t.get("target_qty", 0) or 0)
                    or _now < float((codes_state.get(c6) or {}).get("credit_off_until", 0) or 0)
                    or c6 in sell_warn):
                continue
            sc = live_scores.get(c6)
            if sc is None or sc < float(t.get("min_score", SCORE_MIN) or SCORE_MIN):
                continue
            if c6 in sold_today:
                cap = _rebuy_cap((sell_fills or {}).get(c6) or {})[2]
                if cap <= 0:
                    continue
                px = min(px, cap)
            r = float(t.get("credit_rate") or CREDIT_RATE_DEFAULT)
            if px < _rates.get(r, 10 ** 15):
                _rates[r] = px
            _code0.setdefault(r, c6)
        if _rates:
            _cinf = int(sum(int((v or {}).get("open_order_qty", 0) or 0) * int((v or {}).get("open_order_price", 0) or 0)
                            for v in codes_state.values()
                            if (v or {}).get("open_order_no") and (v or {}).get("open_order_credit")))
            dry_all, rooms, failed = True, [], False
            for r, mpx in _rates.items():
                room_r, why_r = kt.fetch_credit_room(_code0[r], r)
                credit_pre[r] = (room_r, why_r)
                if room_r is None:
                    dry_all = False
                    failed = failed or why_r == "조회 실패"
                elif int(room_r) + _cinf >= mpx:
                    dry_all = False
                if room_r is not None:
                    rooms.append(int(room_r))
            # 통신 실패 = 판정 보류(직전 상태 유지, 알림 없음) — 실패 한 번에 전환·복귀 알림이 오가지 않도록
            credit_dry = bool(exec_state.get("credit_dry")) if failed else dry_all
            if not failed and credit_dry != bool(exec_state.get("credit_dry")):
                exec_state["credit_dry"] = credit_dry
                changed = True
                report.append(f"💳 신용주문가능 {max(rooms or [0]):,}원 — 신용 소진 → 신용가능 종목도 현금·당초 비중으로 배분"
                              f"(주문가능현금 {cash:,}원)" if credit_dry else
                              f"💳 신용주문가능 {max(rooms or [0]):,}원 회복 → 신용가능 종목 신용매수 재개")

    def _mode(c, t):
        """(신용주문 여부, 유효 목표수량). 신용 종목이라도 스위치 꺼짐·신용 대기(credit_off_until) 중이면
           현금·당초 비중 목표(cash_target_qty). NXT 애프터는 신용만 — 신용 못 쓰면/신용불가 종목은 목표 0."""
        cs = codes_state.get(_fmt(c)) or {}
        ct = bool(t.get("credit"))
        uc = (ct and credit_on and not credit_dry
              and time.time() >= float(cs.get("credit_off_until", 0) or 0))
        tq = int(t.get("target_qty", 0) or 0)
        if not uc and (ct or nxt):
            tq = 0 if nxt else int(t.get("cash_target_qty", 0) or 0)
        return uc, tq

    # 감시 슬롯(MAX_CODES)은 '아직 할 일이 있는' 종목에만 배정(2026-11-09): 목표 미달·미체결 주문 보유·체결 미보고.
    #   목표를 다 채운 상위 종목이 슬롯을 차지해 하위(신용불가 등) 종목이 영영 감시되지 않던 문제 방지.
    def _pending(c, t):
        c6 = _fmt(c)
        cs = codes_state.get(c6) or {}
        h = held_map.get(c6, 0)
        if t.get("await_eval"):              # (2026-11-11) AI 평가 대기 — 배분만 예약, 주문·감시 안 함
            return bool(cs.get("open_order_no"))
        return (h < _mode(c, t)[1] or bool(cs.get("open_order_no"))
                or ("held_seen" in cs and h != int(cs.get("held_seen", 0) or 0)))
    watch = [c for c, t in targets.items() if _pending(c, t)]
    if nxt:   # NXT 매도호가가 없던 종목은 10분간 슬롯 제외(미체결 주문이 남은 종목은 취소 위해 유지)
        _now_ts = time.time()
        watch = [c for c in watch
                 if _now_ts - _nxt_nobook.get(_fmt(c), 0) > NXT_NOBOOK_SEC
                 or (codes_state.get(_fmt(c)) or {}).get("open_order_no")]
    # 실효 모드 기준 재정렬(2026-11-09 검토 반영): 신용 실사용 종목(신규→보유) → 현금 종목(신규→보유), 그룹 안은 AI점수순.
    #   스위치 꺼짐·신용 대기로 현금 대체된 신용 종목이 plan 의 신용 우선 자리에서 현금을 먼저 가져가지 않도록.
    watch.sort(key=lambda c: ((0 if _mode(c, targets[c])[0] else 2) + (0 if targets[c].get("new") else 1),
                              -float(targets[c].get("score100", 0) or 0)))   # (2026-11-11) 그룹 안은 V3 점수순
    if len(watch) > MAX_CODES:
        _log_once("buycap", f"  ℹ️ 미완료 {len(watch)}종목 중 상위 {MAX_CODES}종목만 감시(유량 보호)")
    if nxt:
        _log_once("nxthdr", f"🌙 NXT 애프터 신용매수 감시 {min(len(watch), MAX_CODES)}종목(plan {len(targets)}) · {nxt_info}"
                            + (f" → 종가 대비 {nxt_thr * 100:+.2f}% 이하만 매수" if nxt_thr is not None
                               else " → 시장 평균 ≥ 0 또는 계산 중 — 매수 안 함"))
    else:
        _log_once("buyhdr", f"🛒 매수감시 {min(len(watch), MAX_CODES)}종목(plan {len(targets)}) · 주문가능현금 {cash:,}원"
                            f"{'' if credit_on else ' · 신용매수 꺼짐'}")

    # ── 동시 집행 + top-down 예산 예약 ──────────────────────────────────────
    #   현금을 AI 상위부터 '잔여목표금액'만큼 예약(committed)해, 하위 종목이 상위 몫을 못 쓰게 한다.
    #   Top1 목표 1천만·Top2 목표 1천만·현금 1천5백만 → Top1 예산 1천만, Top2 예산 5백만, Top3 0.
    #   내 미체결(in-flight) 주문이 이미 예약한 현금은 usable(주문가능현금 cash)에서 빠져 있으므로,
    #   예산 배분 기준 base_cash 에 되더한다(안 그러면 상위가 주문 중일 때 하위 예산이 과소평가).
    #   미수 방지는 별도 하드 한도(cash − local_spent, 실시간 재조회)로 보장한다.
    #   (2026-11-09) 신용 종목은 같은 방식으로 '신용주문가능금액'을 예약(credit_*). 현금 풀과 분리.
    #   ※ 반드시 정수(int) — 실수가 섞이면 budget//price 가 300.0 이 되어 주문수량 "300.0" 으로 나간다(2026-11-09 검토 지적).
    def _inflight(credit):
        return int(sum(int((v or {}).get("open_order_qty", 0) or 0) * int((v or {}).get("open_order_price", 0) or 0)
                       for v in codes_state.values()
                       if (v or {}).get("open_order_no") and bool((v or {}).get("open_order_credit")) == credit))
    base_cash   = cash + _inflight(False)
    committed   = 0      # 상위 AI 유효 현금종목이 예약한 현금(각자 잔여목표금액)
    local_spent = 0      # 이번 틱에 새로 발주한 현금 매수액(미수 방지 로컬 차감)
    credit_inflight  = _inflight(True)
    credit_base      = None   # 신용 예산 기준 = 신용주문가능(틱당 첫 신용 종목에서 1회 조회) + 내 신용 미체결
    credit_room0     = 0
    credit_committed = 0      # 상위 신용 종목이 예약한 신용 매수액
    local_credit     = 0      # 이번 틱에 새로 발주한 신용 매수액

    # ── (2026-11-11) 정규장: 비중 비례 배분(사용자 확정) ────────────────────────────────
    #   매 틱 plan 의 매수 대상(오늘 AI BUY + AI 평가 대기)을 두 풀로 나눠 '종목 비중(alloc)' 비례로 배분한다.
    #     신용 풀: 신용주문가능금액 + 내 신용 미체결 → 신용 실사용 종목
    #     현금 풀: 주문가능현금 + 내 현금 미체결 → 신용불가·현금대체 종목
    #     현금 풀이 남으면 신용여력을 다 못 받은 신용 종목에 당초 비중(1배, cash_target_qty) 한도까지 재배분(현금 매수)
    #   AI 평가 대기 종목은 몫만 차지하고 주문하지 않는다 → 평가가 BUY 로 나오면 바로 그 몫으로 매수,
    #   관망·회피면 plan 에서 빠져 다음 틱부터 나머지 종목에 재배분. 이미 산 물량은 팔지 않는다(추가매수만 멈춤).
    #   점수 미달·청산예정(평활)·당일매도 재매수 불가 종목은 배분에서 제외. NXT 애프터는 종전 규칙(지금 살 수 있는 종목 순).
    alloc_credit, alloc_cash, alloc_extra = {}, {}, {}
    credit_err = ""
    if not nxt:
        tmap6 = {_fmt(c): t for c, t in targets.items()}
        # 하루 기준 배분(2026-11-11 검토 반영): 매 틱 '남은 돈'만 나누면 살 수 있는 종목이 틱마다 새 몫을 받아
        #   평가 대기·호가 대기 종목의 몫을 결국 다 가져간다. → 배분 총액 = 지금 가용액 + 오늘 이 풀 종목들이 이미 산 금액,
        #   종목 필요액 = 오늘 시작 기준 부족분. 각 종목은 '배분액 − 오늘 산 금액'만 더 산다(현재 부족분이 상한).
        m_credit, m_cash, m_px, spent, cur_need = {}, {}, {}, {}, {}
        for c6, t in tmap6.items():
            uc, tq = _mode(c6, t)
            px = int(t.get("promising_price", 0) or 0)
            if c6 in sold_today:
                cap = _rebuy_cap((sell_fills or {}).get(c6) or {})[2]
                px = min(px, cap) if cap > 0 else 0
            h = held_map.get(c6, 0)
            sc = live_scores.get(c6)
            if (px <= 0 or tq - h < 1 or c6 in sell_warn or sc is None
                    or sc < float(t.get("min_score", SCORE_MIN) or SCORE_MIN)):
                continue
            b_sh = max(0, int((codes_state.get(c6) or {}).get("bought", 0) or 0))
            need0 = max(0, tq - (h - b_sh))                 # 오늘 시작 기준 부족 수량
            m_px[c6] = px
            spent[c6] = min(b_sh, need0) * px
            cur_need[c6] = (tq - h) * px
            w = float(t.get("alloc", 0) or 0) if uc else float(t.get("base_alloc") or t.get("alloc") or 0)
            (m_credit if uc else m_cash)[c6] = (w or 1.0, need0 * px)   # 비중 없는 구버전 plan → 동일 가중

        def _budgets(members, free, pre=None):
            """하루 기준 비중 비례 → 종목별 '지금 더 살 수 있는 금액'. pre {code: 우선 배정액}(교체매도 대금 등)."""
            pre = {c: min(int(v), members[c][1]) for c, v in (pre or {}).items() if c in members and v > 0}
            total = free + sum(spent.get(c, 0) for c in members) - sum(pre.values())
            rest = {c: (w, need - pre.get(c, 0)) for c, (w, need) in members.items()}
            al = _waterfill(rest, total)
            return {c: max(0, min(al.get(c, 0) + pre.get(c, 0) - spent.get(c, 0), cur_need[c])) for c in members}

        if m_credit:
            c0 = next(iter(m_credit))
            _r0 = float(tmap6[c0].get("credit_rate") or CREDIT_RATE_DEFAULT)
            room0, why0 = (credit_pre[_r0] if _r0 in credit_pre      # 위 신용 소진 판정 때 조회한 값 재사용
                           else kt.fetch_credit_room(c0, _r0))
            if room0 is None:
                credit_err = "조회 실패" if why0 == "조회 실패" else (why0 or "신용여력 조회 거부")
            else:
                credit_room0 = int(room0)
                credit_base = credit_room0 + credit_inflight
                alloc_credit = _budgets(m_credit, credit_base)
        # 순위교체 매도 대금은 그 매도가 자금을 마련하려던 신규종목 몫으로 우선 배정(다른 보유종목 추가매수로 새지 않게)
        earmark = {}
        if isinstance(rot_exec, dict) and rot_exec.get("date") == today_str:
            for _rc in (rot_exec.get("codes") or {}).values():
                for ev in (_rc or {}).get("events") or []:
                    fors = [_fmt(x) for x in (ev.get("for") or [])]
                    amt = int(ev.get("cash_done", 0) or 0) * int(ev.get("price", 0) or 0)
                    for x in fors:
                        if x in m_cash and (tmap6.get(x) or {}).get("new"):
                            earmark[x] = earmark.get(x, 0) + amt // len(fors)
        alloc_cash = _budgets(m_cash, base_cash, pre=earmark)
        leftover = base_cash - sum(alloc_cash.values())
        if m_credit and leftover > 0 and credit_err != "조회 실패":
            # 신용 몫이 1주 미만이라 현금으로 넘어갈 신용 종목 → 남는 현금을 당초 비중(1배) 한도까지
            m_extra = {}
            for c6 in m_credit:
                t = tmap6[c6]; px = m_px[c6]; h = held_map.get(c6, 0)
                if alloc_credit.get(c6, 0) >= px:
                    continue                                # 신용으로 살 종목 — 현금 보충 몫 불필요(안 쓰면 잠김)
                ex_need = max(0, int(t.get("cash_target_qty", 0) or 0) - h) * px
                if ex_need > 0:
                    m_extra[c6] = (float(t.get("base_alloc") or t.get("alloc") or 0) or 1.0, ex_need)
            alloc_extra = _waterfill(m_extra, leftover)
        if m_cash and base_cash < min(m_px[c] for c in m_cash) and exec_state.get("cash_short") != cash:
            report.append(f"💸 현금부족 — 현금 매수 대상 {len(m_cash)}종목 배분 불가(주문가능 {cash:,}원)")
            exec_state["cash_short"] = cash
            changed = True
        elif m_cash and base_cash >= min(m_px[c] for c in m_cash) and exec_state.get("cash_short") is not None:
            exec_state["cash_short"] = None
            changed = True
        n_wait = sum(1 for t in targets.values() if t.get("await_eval"))
        _log_once("allochdr", f"  🧮 배분 — 신용 {len(m_credit)}종목·{sum(alloc_credit.values()):,}원"
                              f"(여력 {credit_room0:,}{'·' + credit_err if credit_err else ''}) · 현금 {len(m_cash)}종목·"
                              f"{sum(alloc_cash.values()):,}원 · 신용부족분 현금 {sum(alloc_extra.values()):,}원"
                              f"{f' · AI평가대기 {n_wait}종목 몫 예약' if n_wait else ''}")

    # plan 삽입순 = 매수 우선순위. 할 일이 남은 종목 중 상한까지만 감시(rate-limit 보호).
    for code in watch[:MAX_CODES]:
        t = targets[code]
        code = _fmt(code)
        name = t.get("name", "")
        price = int(t.get("promising_price", 0) or 0)
        if int(t.get("target_qty", 0) or 0) < 1 or price <= 0:
            continue

        held = held_map.get(code, 0)
        cst = codes_state.get(code)
        if cst is None:
            cst = codes_state[code] = _new_cst(held)
            changed = True

        # ── (2026-11-09) 신용 종목: 신용 사용 가능(스위치 켜짐·최근 신용 거부/여력부족 대기 중 아님)이면 2배 목표를
        #    신용으로, 아니면 당초 비중(1배) 현금 목표로 대체(NXT 는 대체 없음). 대기 해제 후 다시 신용 시도.
        use_credit, target_qty = _mode(code, t)
        crate = float(t.get("credit_rate") or CREDIT_RATE_DEFAULT)
        close = 0
        if nxt:
            # NXT 지정가 = 오늘 KRX 종가 × (1 − 2%) 호가단위 내림 → 이 가격 이하 NXT 매도호가만 매수
            close = _krx_close(today_str, code)
            if close > 0 and nxt_thr is not None:
                price = kt.normalize_krx_price(int(close * (1 + nxt_thr)))

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
            if not nxt and _is_closing_auction():
                # 동시호가(15:20~15:30): 체결 없는 호가접수 시간 — 취소/재주문 반복 금지(사용자 확정).
                # 낸 주문 1건을 그대로 유지해 종가(15:30) 단일가 체결을 기다린다. 결과는 장마감 후 1회 알림.
                if remaining >= 1:
                    if use_credit:
                        credit_committed += remaining_amt
                    else:
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
                if rem and kt.cancel_order(cst["open_order_no"], code, rem,
                                           credit=bool(cst.get("open_order_credit")),   # 신용매수 주문은 kt10009
                                           stex=cst.get("open_order_stex") or None):    # NXT 애프터 주문은 NXT 로 취소
                    report.append(f"🚫 미체결취소 {name}({code}) 잔량 {rem}주×{int(cst.get('open_order_price') or price):,}원")
                # 취소 실패면 다음 틱 재시도(중복주문 방지 위해 그대로 둠)
            if cst.get("open_order_credit") and credit_base is None:
                # 신용여력을 아직 이번 틱에 조회하지 않음 → 이 주문은 지금 취소(여력에 복귀)됐거나 체결(잔여에 반영)됨.
                #   조회 시 되더하는 '내 신용 미체결'에서 빼 이중 계산 방지(2026-11-09 검토 반영).
                credit_inflight -= int(cst.get("open_order_qty", 0) or 0) * int(cst.get("open_order_price", 0) or 0)
            cst["open_order_no"] = ""; cst["open_order_qty"] = 0; cst["open_order_price"] = 0
            cst["open_order_credit"] = False; cst["open_order_stex"] = ""
            changed = True
            if remaining >= 1:
                if use_credit:
                    credit_committed += remaining_amt   # 신용 예산 예약 유지 → 하위 신용 종목이 이 종목 몫 못 씀
                else:
                    committed += remaining_amt          # 예산 예약 유지 → 하위가 이 종목 몫 못 씀
                _log_once(f"buy:{code}", f"  🔄 {name}({code}) 직전주문 해소 → 계속 채움(잔여 {remaining}주, 예약)")
            else:
                _log_once(f"buy:{code}", f"  🔄 {name}({code}) 주문 해소·목표달성")
            continue

        if remaining < 1:               # 목표 달성(완료) → 예약 없음, 다음 종목
            _log_once(f"buy:{code}", f"  ✔️ {name}({code}) 목표달성 보유{held}/목표{target_qty}")
            continue
        if nxt and close <= 0:
            _log_once(f"buy:{code}", f"  ⏳ {name}({code}) 오늘 KRX 종가 확인 불가 — NXT 매수 보류")
            continue
        if nxt and nxt_thr is None:
            _log_once(f"buy:{code}", f"  ⏸️ {name}({code}) {nxt_info or 'NXT 시장 평균 계산 중'} — 시장 평균이 마이너스일 때만 NXT 매수")
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

        # ── (2026-11-11) 정규장: 위 비중 비례 배분액으로 매수. 신용 몫이 1주 미만이면 신용부족분 현금 몫으로.
        if not nxt:
            if use_credit and credit_err and credit_err != "조회 실패":
                # 증권사 거부(신용계좌 아님 등) → 잠시 현금·당초 비중으로(다음 틱부터 현금 풀에서 배분)
                changed |= _credit_off(cst, code, name, credit_err, report, key="room")
                continue
            cbudget = alloc_credit.get(code, 0) if use_credit else 0
            if use_credit and cbudget < price:
                xb = alloc_extra.get(code, 0)
                c_rem = int(t.get("cash_target_qty", 0) or 0) - held
                if t.get("new") and c_rem >= 1 and xb < c_rem * price and not _is_closing_auction():
                    # 신용여력 부족 신규종목의 현금 보충분도 모자라면 순위교체 매도 후보(당초 비중 1배 기준)
                    _rot_need.append({"code": code, "name": name, "remaining": c_rem, "price": price, "budget": xb})
                if xb >= price:
                    # 신용여력 부족분 → 현금으로 당초 비중(1배)까지
                    use_credit = False
                    target_qty = int(t.get("cash_target_qty", 0) or 0)
                    remaining = target_qty - held
                    remaining_amt = remaining * price
                    budget = xb
                else:
                    _log_once(f"buy:{code}", f"  ⏸️ {name}({code}) 신용 배분 {cbudget:,}원·현금 보충 {xb:,}원 < {price:,}원/주 — 대기"
                                             f"{'(신용여력 조회 실패)' if credit_err else ''}")
                    continue
                if remaining < 1:
                    continue
            elif not use_credit:
                budget = alloc_cash.get(code, 0)
                if t.get("new") and budget < remaining_amt and not _is_closing_auction():
                    # 신규종목 배분액 부족 → 순위교체 매도 후보(기존 보유 추가매수는 기록 안 함)
                    _rot_need.append({"code": code, "name": name, "remaining": remaining,
                                      "price": price, "budget": budget})
                if budget < price:
                    _log_once(f"buy:{code}", f"  ⏸️ {name}({code}) 현금 배분 {budget:,}원<{price:,}원/주 — 대기")
                    continue
        elif use_credit:
            # NXT 애프터 신용 풀: 신용주문가능금액(틱당 1회)
            if credit_base is None:
                room0, why0 = kt.fetch_credit_room(code, crate)
                if room0 is None:
                    if why0 != "조회 실패":      # 증권사 거부(신용계좌 아님 등) → 잠시 현금·당초 비중(NXT 는 대기)
                        changed |= _credit_off(cst, code, name, why0, report, key="room", nxt=nxt)
                    else:
                        credit_committed += remaining_amt   # 통신 실패 → 이 종목 몫은 예약 유지(하위 신용 종목이 못 가져감)
                    continue                     # 다음 신용 종목에서 다시 조회
                credit_room0 = int(room0)
                credit_base = credit_room0 + credit_inflight
            if nxt:
                # NXT 애프터(2026-11-09 실가동 보완): 지금 기준가(시장평균 −3%p) 이하로 살 수 있는 종목만 plan 순서대로 신용여력을 쓴다.
                #   대기 종목(NXT 미거래·아직 기준가 미달)이 정규장처럼 여력을 미리 예약하면 실제 하락 종목이 예산 0 으로 못 산다.
                if credit_room0 < price:                 # 계좌 신용여력 자체가 부족 → 신용 대기
                    changed |= _credit_off(cst, code, name, f"신용주문가능 {credit_room0:,}원", report, key="room", nxt=True)
                    continue
                cbudget = max(0, credit_room0 - local_credit)
                if cbudget < price:                      # 이번 틱 상위 종목 주문으로 소진 → 다음 틱
                    _log_once(f"buy:{code}", f"  ⏸️ {name}({code}) 이번 틱 신용여력 소진 — 다음 틱")
                    continue
        else:
            continue                         # NXT 애프터는 신용 종목만(현금 매수 없음)

        # ── 동시호가(15:20~15:30): sweep 불가(체결 없음) — 잔여목표를 지정가 1회 주문으로 내고
        #    종가 체결(15:30)을 기다린다. 취소/재주문 없음. 체결 여부는 장마감 후 1회 알림.
        if not nxt and _is_closing_auction():
            if use_credit:
                room, why = kt.fetch_credit_room(code, crate)
                if room is None and why == "조회 실패":
                    continue                     # 통신 실패 → 다음 틱 재시도(현금 대체 안 함)
                if (room or 0) < price:          # 이 종목 보증금율 구간의 실시간 신용여력 부족 → 잠시 현금·당초 비중
                    changed |= _credit_off(cst, code, name, f"신용주문가능 {room or 0:,}원 · {why}", report, key="room")
                    continue
                qty = int(min(remaining, room // price, cbudget // price, (credit_room0 - local_credit) // price))
                if qty < 1:
                    _log_once(f"buy:{code}", f"  💸 {name}({code}) 동시호가 — 신용 예산 부족, 주문 없음")
                    continue
                if _new_small(t, held, qty, target_qty):
                    _log_once(f"buy:{code}", f"  🪙 {name}({code}) 동시호가 신규 자투리 {qty}주(보유 {held}주) — 매수 안 함")
                    continue
                res = kt.place_credit_buy_order(code, qty, price)
            else:
                live_cash = kt.fetch_order_cash()
                if live_cash is None:
                    continue
                cash_room = min(cash - local_spent, live_cash)
                qty = int(min(remaining, budget // price, cash_room // price))
                if qty < 1:
                    _log_once(f"buy:{code}", f"  💸 {name}({code}) 동시호가 — 예산/현금 부족, 주문 없음")
                    continue
                if _new_small(t, held, qty, target_qty):
                    _log_once(f"buy:{code}", f"  🪙 {name}({code}) 동시호가 신규 자투리 {qty}주(보유 {held}주) — 매수 안 함")
                    continue
                res = kt.place_buy_order(code, qty, price)
            ctag = "신용 " if use_credit else ""
            if res and res.get("return_code") == 0:
                ono = res.get("ord_no", "?")
                if use_credit:
                    local_credit += qty * price
                else:
                    local_spent += qty * price
                cst["last_fail"] = None
                cst["open_order_no"] = ono
                cst["open_order_qty"] = qty
                cst["open_order_price"] = price
                cst["open_order_credit"] = use_credit
                cst["open_order_stex"] = "KRX"
                cst["close_buy_hold"] = True
                changed = True
                report.append(f"🕒 동시호가 {ctag}지정가매수 {name}({code}) {qty}주×{price:,}원 = {qty*price:,}원 "
                              f"— 종가 체결 대기(장마감 후 결과 알림, 주문 {ono})")
                _log_once(f"buy:{code}", f"  🕒 동시호가 {ctag}지정가매수 {name}({code}) {qty}주×{price:,}원 (주문 {ono})")
            elif use_credit and res is not None:
                changed |= _credit_off(cst, code, name, f"신용주문 거부: {res.get('return_msg', '')}", report)
            else:
                err = (res or {}).get("return_msg", "응답 없음")
                if cst.get("last_fail") != err:
                    report.append(f"❌ 동시호가 {ctag}매수실패 {name}({code}): {err}")
                    cst["last_fail"] = err
                    changed = True
                _log_once(f"buy:{code}", f"  ❌ 동시호가 {ctag}매수실패 {name}({code}): {err}")
            continue

        # ── 매도호가 sweep: 지정가 이하 잔량만큼. 없으면(가격상승 등) 예약 유지한 채 다음 종목.
        #    정규장 = KRX+NXT 통합호가(SOR 주문), NXT 애프터 = NXT 단독 호가(NXT 주문).
        avail, best_ask = kt.ask_qty_at_or_below(code, price, venue="NX" if nxt else "AL")
        if nxt and best_ask <= 0:
            # NXT 매도호가 자체가 없음(NXT 미거래 종목 등) → 10분간 NXT 감시 슬롯에서 제외(다른 종목 감시 기회)
            _nxt_nobook[code] = time.time()
        if avail < 1:
            if nxt:
                _log_once(f"buy:{code}", f"  ⏳ {name}({code}) 잔여{remaining} · 종가 {close:,} → 지정가 {price:,}"
                          f"(시장 {nxt_thr * 100 + NXT_REL_GAP * 100:+.2f}% −3%p) · NXT 매도최우선 {best_ask:,} — 하락 대기")
            else:
                _log_once(f"buy:{code}", f"  ⏳ {name}({code}) 잔여{remaining} · 점수{live_sc:.0f} · 지정가{price:,} "
                          f"매도최우선 {best_ask:,} — 호가대기({'신용' if use_credit else f'예산 {budget:,}원 예약 유지'})")
            continue

        if use_credit:
            # ── 신용: 주문 직전 신용주문가능금액(kt00012) 실시간 확인 = 신용 하드 한도.
            room, why = kt.fetch_credit_room(code, crate)
            if room is None and why == "조회 실패":
                continue                     # 통신 실패 → 이 종목만 이번 틱 건너뜀(현금 대체 안 함)
            if (room or 0) < price:          # 이 종목 보증금율 구간의 실시간 신용여력 부족
                # NXT 는 신용 대기, 정규장은 잠시 현금·당초 비중으로(다음 틱부터 현금 풀 배분 — 종목별 구간 여력이 달라 영구 대기 방지)
                changed |= _credit_off(cst, code, name, f"신용주문가능 {room or 0:,}원 · {why}", report, key="room", nxt=nxt)
                continue
            # 신용 수량 = 잔여·호가·신용 예산(상위 몫 제외)·실시간 신용여력·이번 틱 신용 발주 차감 중 최소
            qty = int(min(remaining, avail, cbudget // price, room // price, (credit_room0 - local_credit) // price))
            if qty < 1:
                _log_once(f"buy:{code}", f"  💸 {name}({code}) 신용 매수불가(신용 예산 {cbudget:,}·여력 {room:,} < {price:,}/주)")
                continue
            _nm = _new_small(t, held, qty, target_qty)
            if _nm:
                _log_once(f"buy:{code}", f"  🪙 {name}({code}) 신규 자투리 {qty}주(보유 {held}주) — 보유 {_nm}주 미만이라 대기")
                continue
            ostex = "NXT" if nxt else "SOR"
            res = kt.place_credit_buy_order(code, qty, price, stex=ostex)
        else:
            # ── 주문 직전 현금 실시간 재조회(미수 방지 하드 한도).
            #    cash−local_spent(내 이번 틱 발주 반영) 과 실시간값 중 작은 쪽. 예산과도 min.
            live_cash = kt.fetch_order_cash()
            if live_cash is None:
                continue                     # 현금 조회 실패 → 이 종목만 이번 틱 건너뜀(하위는 진행)
            cash_room = min(cash - local_spent, live_cash)
            qty = int(min(remaining, avail, budget // price, cash_room // price))
            if qty < 1:
                _log_once(f"buy:{code}", f"  💸 {name}({code}) 매수불가(예산 {budget:,}·현금여유 {cash_room:,} < {price:,}/주)")
                continue
            _nm = _new_small(t, held, qty, target_qty)
            if _nm:
                _log_once(f"buy:{code}", f"  🪙 {name}({code}) 신규 자투리 {qty}주(보유 {held}주) — 보유 {_nm}주 미만이라 대기")
                continue
            ostex = "SOR"
            res = kt.place_buy_order(code, qty, price, stex=ostex)   # 정규장 sweep 은 SOR(KRX/NXT 최선 라우팅)

        ctag = ("NXT 애프터 신용 " if nxt else "신용 ") if use_credit else ""
        if res and res.get("return_code") == 0:
            ono = res.get("ord_no", "?")
            amt = qty * price
            if use_credit:
                local_credit += amt
            else:
                local_spent += amt
            cst["last_fail"] = None
            cst["open_order_no"] = ono
            cst["open_order_qty"] = qty
            cst["open_order_price"] = price
            cst["open_order_credit"] = use_credit
            cst["open_order_stex"] = ostex
            changed = True
            want = min(remaining, avail)
            if use_credit:
                tag = (" ·신용한도" if qty < want and qty == room // price
                       else " ·예산한도" if qty < want else " ·부분(호가한도)" if qty < remaining else "")
            else:
                tag = (" ·예산한도" if qty < want and (budget // price) <= (cash_room // price)
                       else " ·현금한도" if qty < want
                       else " ·부분(호가한도)" if qty < remaining else "")
            ref = (f"오늘 종가 {close:,} · NXT 최우선 {best_ask:,}({(best_ask - close) / close * 100:+.2f}%) · "
                   f"시장 평균 {(nxt_thr + NXT_REL_GAP) * 100:+.2f}% · "
                   if nxt and close > 0 and nxt_thr is not None else "")
            report.append(f"🟢 {ctag}sweep매수 {name}({code}) {qty}주×{price:,}원 = {amt:,}원 "
                          f"({ref}잔여 {remaining}·호가 {avail}·최우선 {best_ask:,}{tag}, 주문 {ono})")
            _log_once(f"buy:{code}", f"  🟢 {ctag}sweep매수 {name}({code}) {qty}주×{price:,}원 = {amt:,}원 "
                      f"({ref}잔여{remaining}·호가{avail}{tag}, 주문 {ono})")
            # 동시 집행: 이 종목 주문 후에도 하위 종목이 '자기 예산' 안에서 매수하도록 계속 진행.
        elif use_credit and res is not None:
            # 신용주문 거부(한도·보증금·종목 사유 등) → 잠시 현금·당초 비중으로 대체 후 신용 재시도(NXT 는 대기)
            changed |= _credit_off(cst, code, name, f"신용주문 거부: {res.get('return_msg', '')}", report, nxt=nxt)
            continue
        else:
            err = (res or {}).get("return_msg", "응답 없음")
            _log_once(f"buy:{code}", f"  ❌ {ctag}매수실패 {name}({code}): {err}")
            if cst.get("last_fail") != err:              # 실패 알림 도배 방지(같은 에러 1회만)
                report.append(f"❌ {ctag}매수실패 {name}({code}): {err}")
                cst["last_fail"] = err
                changed = True
            # 실패는 '초점 유지'의 예외 — 특정 종목 오류로 전체 매수가 얼지 않도록 다음 종목으로 진행.
            continue

    if changed:
        _save_json(_exec_path(today_str), exec_state)
    if report:
        _send_owner(("🌙 NXT 애프터 신용매수\n" if nxt else "🛒 실시간 sweep 집행\n") + "\n".join(report))


# ── 순위교체 매도 (2026-10-07) ────────────────────────────────────────────
#   신규종목(plan target.new) 매수 예산이 모자라면, 보유비중(평가금액) 1위부터 순서대로
#   '수수료·세금 차감 후 순익 3%(ROT_MIN_PROFIT) 이상'(매수최우선 > 기준가 = 평단×(1+0.015%)×1.03÷(1−0.215%)) 종목만
#   부족분만큼 매도. 그 외 건너뜀. (2026-10-27: 최소 순익 0% → 3%)
#   (2026-10-14) 현재 정규장 재평가 점수 60점(ROT_KEEP_SCORE)+ 보유종목은 교체매도 제외(점수 미산출 종목은 대상 유지).
#   (2026-10-16) 마지막 매수일(자동·수기 전부, kt00007 체결내역)로부터 거래일 D+6(ROT_MIN_HOLD_TD, 10-27 D+5→D+6) 이상 경과한 종목만 대상.
#               해당 종목이 없으면 교체매도 안 함. 매수일 조회 실패 시에도 보류(fail-closed).
#   백테스트(addbuy_analysis/rotate_test.py '최종안') 규칙을 장중 sweep 으로 옮긴 것.
#   (2026-10-29) 담보대출·신용 물량은 팔지 않는다(ROT_CASH_ONLY) — 매도대금이 대출상환에 먼저 쓰여 매수현금이 거의 안 나옴.
#               현금 물량만 매도(평단·순익도 현금 물량 기준), 현금 물량이 없는 종목은 제외. 비중 순위는 종목 전체 평가금액 기준.
#   · 매도 수량 = 부족금액 ÷ 매수최우선 (현금 트랜치만 발주). 지정가 = max(매수최우선×(1−1%), 손익분기 초과 최소호가)
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


_buy_day_cache = {}   # {YYYYMMDD: set(code)} — 지난 거래일 매수체결 종목(당일엔 불변 → 날짜별 1회 조회)


def _recent_buy_age(today_str):
    """최근 ROT_MIN_HOLD_TD 거래일(오늘 포함)에 매수 체결이 있었던 종목의 경과 거래일 {code: k}
       (k=0 오늘, 1 전일 … 가장 최근 매수 기준). 여기에 없는 보유종목 = 마지막 매수 후 D+{ROT_MIN_HOLD_TD} 이상 경과.
       하루라도 조회 실패하면 None → 교체매도 보류(fail-closed). 오늘분은 매번 새로 조회(장중 수기 매수 반영)."""
    days = kt.recent_trading_days(ROT_MIN_HOLD_TD, today_str)
    for d in list(_buy_day_cache):
        if d not in days:
            del _buy_day_cache[d]
    age = {}
    for k, d in enumerate(days):
        codes = _buy_day_cache.get(d) if d != today_str else None
        if codes is None:
            codes = kt.fetch_buy_codes_on(d)
            if codes is None:
                return None
            if d != today_str:
                _buy_day_cache[d] = codes
        for c in codes:
            age.setdefault(_fmt(c), k)
    return age


def _rot_breakeven(avg):
    """교체매도 최저 매도가 = 평단 × (1+매수수수료) × (1+최소순이익 ROT_MIN_PROFIT) ÷ (1−매도수수료−거래세).
       이 가격 이하로 팔면 수수료·세금 차감 후 순수익률이 ROT_MIN_PROFIT(3%) 미만
       (2026-10-14: '평단 초과'만 보다 손실 매도 발생 → 비용 반영, 2026-10-27: 최소 순익 0% → 3%)."""
    return avg * (1 + BUY_FEE_EST) * (1 + ROT_MIN_PROFIT) / (1 - ROT_SELL_COST)


def _is_cash_tranche(p):
    """현금 잔고 트랜치 여부 — 대출일자가 있거나 신용구분이 현금(00)이 아니면 담보대출·신용 물량."""
    return not str(p.get("loan_dt") or "").strip() and str(p.get("crd_type") or "00").strip() == "00"


def _cash_held(poss):
    """트랜치 목록 중 현금 물량 합계(교체매도 체결 판정 기준)."""
    return sum(p["qty"] for p in poss if _is_cash_tranche(p))


def _price_above(x):
    """x 보다 큰 가장 낮은 KRX 호가(호가단위 정렬)."""
    p = kt.normalize_krx_price(int(x))
    while p <= x:
        p += kt.krx_tick_size(p)
    return p


def _rotate_place(today_str, need, tranche_map, codes, report):
    """부족금액을 계산해 보유비중 순위대로 교체매도 주문. 주문을 냈으면 True."""
    want_amt, need_amt = 0.0, 0.0
    for n in need:
        avail, _ = kt.ask_qty_at_or_below(n["code"], n["price"])
        # (2026-11-09) 현금 소요 = 매수대금 × rate (신용 신규종목은 보증금율만큼만 현금 필요, 현금 종목 rate=1)
        w = min(n["remaining"], avail) * n["price"] * n.get("rate", 1.0) * (1 + BUY_FEE_EST)
        want_amt += w
        # (2026-11-11) 비중 비례 배분: 종목별 '지금 살 수 있는 금액 − 그 종목 배분액' 부족분의 합(종전: 첫 부족 종목 예산만 차감)
        need_amt += max(0.0, w - float(n.get("budget", 0) or 0))
    min_px = min(n["price"] * n.get("rate", 1.0) for n in need)
    if need_amt < min_px:
        _log_once("rot", f"  🔁 교체매도 불필요(신규종목 즉시매수 가능분 {want_amt:,.0f}원 ≤ 배분액)")
        return False

    plan = _load_json(_plan_path(today_str), {}) or {}
    new_codes = {_fmt(c) for c, t in (plan.get("targets") or {}).items() if (t or {}).get("new")}
    sp = _load_json(_sell_plan_path(today_str), {}) or {}
    selling = {_fmt(c) for c in (sp.get("targets") or {})} if sp.get("date") == today_str else set()
    ex = _load_json(_exec_path(today_str), {}) or {}
    bought = ({_fmt(c) for c, v in (ex.get("codes") or {}).items() if int((v or {}).get("bought", 0) or 0) > 0}
              if ex.get("date") == today_str else set())
    live = _load_live_scores(today_str)      # (2026-10-14) 현재 정규장 점수 ROT_KEEP_SCORE(60)+ 보유종목은 팔지 않음
    age = _recent_buy_age(today_str)         # (2026-10-16) 마지막 매수 후 D+6(ROT_MIN_HOLD_TD) 미만 보유종목은 팔지 않음
    if age is None:
        _log_once("rot", "  🔁 교체매도 보류 — 최근 매수일 조회(kt00007) 실패, 보유기간 확인 불가")
        return False

    srcs = []
    for code, poss in tranche_map.items():
        code = _fmt(code)
        if code in new_codes or code in selling or code in bought or not _rot_eligible(code):
            continue
        k = age.get(code)
        if k is not None:
            _log_once(f"rot:{code}", f"  🔁 {_hold_names.get(code, '')}({code}) 마지막 매수 D+{k}"
                                     f"<D+{ROT_MIN_HOLD_TD} — 교체매도 제외")
            continue
        sc = live.get(code)
        if sc is not None and sc >= ROT_KEEP_SCORE:
            _log_once(f"rot:{code}", f"  🔁 {_hold_names.get(code, '')}({code}) 정규장 점수 {sc:.0f}≥{ROT_KEEP_SCORE:.0f} — 교체매도 제외")
            continue
        qty = sum(p["qty"] for p in poss)               # 종목 전체 보유(비중 순위·체결 판정 기준)
        # (2026-10-29) 매도는 현금 물량만 — 담보대출·신용 물량은 대금이 대출상환에 쓰여 매수현금이 거의 안 나옴
        sell_poss = [p for p in poss if _is_cash_tranche(p)] if ROT_CASH_ONLY else poss
        cash_q = sum(p["qty"] for p in sell_poss)
        sellable = sum(p.get("sell_possible_qty", 0) for p in sell_poss)
        if qty < 1:
            continue
        if cash_q < 1:
            _log_once(f"rot:{code}", f"  🔁 {_hold_names.get(code, '')}({code}) 담보·신용 물량만 보유(현금 0주) — 교체매도 제외")
            continue
        if sellable < 1:
            continue
        avg = sum(p["qty"] * p["avg_buy_price"] for p in sell_poss) / cash_q   # 파는 물량(현금분) 평단
        cur = max((p.get("cur_price", 0) or 0) for p in poss) or avg
        srcs.append((qty * cur, code, sell_poss, qty, sellable, avg, qty - cash_q))
    srcs.sort(key=lambda x: -x[0])

    for_names = ", ".join(f"{n['name']}({n['code']})" for n in need[:3]) + (" 외" if len(need) > 3 else "")
    placed = False
    for rank, (_, code, poss, qty, sellable, avg, loan_q) in enumerate(srcs, 1):
        if need_amt < min_px:
            break
        name = _hold_names.get(code, "")
        levels = kt.fetch_bid_book(code + "_AL") or kt.fetch_bid_book(code)
        if not levels:
            continue
        best_bid = levels[0][0]
        be = _rot_breakeven(avg)
        if best_bid <= be:
            _log_once(f"rot:{code}", f"  🔁 {name}({code}) 비중{rank}위 비용차감 순익 {ROT_MIN_PROFIT*100:.0f}% 미만(매수최우선 {best_bid:,}"
                                     f"≤기준가 {be:,.0f}·평단 {avg:,.0f}) — 교체매도 제외")
            continue
        upl = _upper_limit(code)
        if upl and best_bid >= upl:
            _log_once(f"rot:{code}", f"  🔁 {name}({code}) 상한가 — 교체매도 제외")
            continue
        # 지정가 하한 = 기준가(비용차감 순익 3%) 초과 최소호가 → 어느 가격에 체결돼도 순익 3% 이상
        floor = _price_above(max(best_bid * (1 - ROT_MAX_DROP), be))
        if floor > best_bid:
            continue
        avail = sum(q for px, q in levels if px >= floor)
        q_total = min(int(math.ceil(need_amt / (best_bid * (1 - ROT_SELL_COST)))), sellable, avail)
        if q_total < 1:
            continue
        rem, orders = q_total, []
        for pos in sorted(poss, key=lambda h: (not bool(h["loan_dt"]), h["loan_dt"])):   # 현금 트랜치만(ROT_CASH_ONLY 끄면 종전대로 담보·신용 먼저)
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
            c = codes[code] = {"name": name, "held_seen": qty, "sold": 0, "open_orders": [], "events": [],
                               "cash_seen": _cash_held(tranche_map.get(code, []))}
        c["open_orders"] = orders
        c["events"].append({"time": datetime.now().strftime("%H:%M:%S"), "qty": q_ord, "price": floor,
                            "cash_qty": sum(o["qty"] for o in orders if not o.get("loan_dt")),   # 현금 트랜치분(안전망 기준)
                            "best_bid": best_bid, "avg": round(avg, 1), "rank": rank,
                            "for": [n["code"] for n in need]})
        need_amt -= q_ord * best_bid * (1 - ROT_SELL_COST)
        placed = True
        net_min = (floor * (1 - ROT_SELL_COST)) / (avg * (1 + BUY_FEE_EST)) - 1   # 하한가 체결 시 비용 차감 순수익률
        loan_txt = f" · 현금분만(담보·신용 {loan_q}주 제외)" if loan_q > 0 else ""
        report.append(f"🔁 교체매도 {name}({code}) {q_ord}주×{floor:,}원↑ (보유비중 {rank}위 · 평단 {avg:,.0f} → "
                      f"매수최우선 {best_bid:,} · 비용차감 순익 ≥{net_min * 100:+.2f}%{loan_txt}) → 신규 {for_names} 매수자금")
        _log_once(f"rot:{code}", f"  🔁 교체매도 {name}({code}) {q_ord}주×{floor:,}원 (비중{rank}위, 순익≥{net_min*100:+.2f}%)")
    if need_amt >= min_px and not placed:
        # 부족금액은 호가에 따라 매 틱 바뀌어 문구에 넣으면 같은 상태가 4초마다 반복 출력됨 → 상태만 표시
        _log_once("rot", f"  🔁 교체매도 대상 없음(매수 후 D+{ROT_MIN_HOLD_TD} 경과·정규장 {ROT_KEEP_SCORE:.0f}점 미만·현금 물량 중 "
                         f"비용차감 순익 {ROT_MIN_PROFIT*100:.0f}% 이상인 보유종목 없음) — 교체매도 안 함")
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
        poss = tranche_map.get(code, [])
        held = sum(p["qty"] for p in poss)
        held_cash = _cash_held(poss)
        d_tot = max(0, c["held_seen"] - held)
        if ROT_CASH_ONLY:
            # 교체매도는 현금 물량만 팖 → 체결 = 현금 물량 감소(총보유도 같이 준 만큼). 담보·신용 물량 감소(수기·반대매매·
            # 자동매도)나 현금→담보 전환은 교체매도 체결로 집계하지 않음(허위 체결보고·안전망 오발동 방지)
            d = min(d_tot, max(0, int(c.get("cash_seen", held_cash)) - held_cash))
            if d_tot > d:
                _log_once(f"rotx:{code}", f"  🔁 {c.get('name', '')}({code}) 담보·신용 물량 {d_tot - d}주 감소 — 교체매도 체결 아님(집계 제외)")
        else:
            d = d_tot
        if d > 0:
            c["sold"] += d
            ev = (c.get("events") or [{}])[-1]
            # 안전망 기준 금액은 '현금 트랜치' 체결분만 — 담보·신용분은 대출상환 후 일부만 현금이 되므로 제외.
            # 이벤트별 집계 누계(cash_done)로 같은 주문분이 여러 번 더해지지 않게 함
            cash_left = int(ev.get("cash_qty", ev.get("qty", d)) or 0) - int(ev.get("cash_done", 0) or 0)
            cash_d = min(d, max(0, cash_left))
            if ev:
                ev["cash_done"] = int(ev.get("cash_done", 0) or 0) + cash_d
            rs["sold_amt"] = float(rs.get("sold_amt", 0) or 0) + cash_d * float(ev.get("price", 0) or 0)  # 지정가 기준(보수적)
            rs["last_action"] = now
            report.append(f"✅ 교체매도 체결 {c.get('name', '')}({code}) -{d}주 → 잔여보유 {held}주")
        if held != c["held_seen"] or held_cash != c.get("cash_seen"):
            c["held_seen"], c["cash_seen"] = held, held_cash
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
        _tkey = _close_key(pos)
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
                       and _close_key(p) not in done_keys]
            if not pending:
                _log_once(f"sell:{code}", f"  ⏳ {name}({code}) 종가 시장가 주문 대기(전 트랜치 발주완료, 체결 15:30)")
                continue
            for pos in pending:
                key = _close_key(pos)
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
                    _log_once(f"sell:{code}:{_close_key(pos)}",
                              f"  🕒 동시호가 지정가매도 {name}({code}) {qty}주×{sell_price:,}원 (주문 {ono})")
                else:
                    err = (res or {}).get("return_msg", "응답 없음")
                    report.append(f"❌ 동시호가 매도실패 {name}({code}) {pos['order_type']}: {err}")
                    _log_once(f"sell:{code}:{_close_key(pos)}", f"  ❌ 동시호가 매도실패 {name}({code}): {err}")
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
            _tkey = _close_key(pos)
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
def _maybe_post_close_report(today_str, now=None):
    """15:30 종가 단일가 체결 확인. 동시호가에 유지한 매수/매도 주문(+종가 시장가 매도)의
       체결 여부를 보유잔고 변화로 판정해 장마감 후 1회만 텔레그램 알림(15:30:30~17:00 사이 실행,
       수능일 등 KRX 지연일은 16:30:30~18:00 — kt.krx_t)."""
    now = now or datetime.now()
    if now.weekday() >= 5 or not (kt.krx_t(dtime(15, 30, 30), now) <= now.time() <= kt.krx_t(dtime(17, 0), now)):
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
        _send_owner(f"🔔 장마감({kt.krx_close_time(now):%H:%M}) 종가 체결 결과\n" + "\n".join(lines))
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
    print(f"   · 매수/매도는 KRX 정규장(오늘 {kt.krx_hours_text()}) + 정규장 점수 {SCORE_MIN:.0f}+ 에만 집행")
    if kt.KRX_LATE_DAYS:
        print(f"   · KRX 지연 개장일(정규장 1시간 순연): {', '.join(sorted(kt.KRX_LATE_DAYS))}")
    print("   · 장전/장후 NXT 점수는 매매에 관여하지 않음(참고용)")
    print(f"   · NXT 애프터 신용매수 15:35~19:55: NXT 시장 평균(종가 대비)이 마이너스일 때 plan 신용 종목 중 NXT 매도호가 ≤ 종가×(1+평균−{NXT_REL_GAP*100:.0f}%p)만, "
          f"신용 2배 목표까지 — 스위치 nxt_buy·credit_buy (지금 {trade_switches.status_text().replace(chr(10), ' ')})")
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
                if kt.is_market_open():
                    st = "정규장(집행 ON)"
                elif kt.is_nxt_after_buy_window():
                    st = ("NXT 애프터 신용매수 ON(시장평균 −3%p↓)" if trade_switches.get("nxt_buy") and trade_switches.get("credit_buy")
                          else "NXT 애프터 신용매수 꺼짐(스위치)")
                else:
                    st = "정규장 외(매매 안 함·참고만)"
                _log(f"💓 execution_monitor 대기/감시 중 · {st}")
                last_beat = now
            _maybe_post_close_report(today)   # 장마감 후 동시호가 주문 체결 결과 1회 알림(NXT 애프터 틱보다 먼저 — 체결 판정 기준 보존)
            _tick(today)          # 매수 sweep (KRX 정규장에만 동작)
            _tick(today, "NXT")   # (2026-11-09) NXT 애프터 신용매수 (15:35~19:55, 스위치 nxt_buy)
            _rotate_tick(today)   # 순위교체 매도 (신규종목 매수자금 부족 시, 정규장에만)
            _sell_tick(today)     # 매도 sweep (KRX 정규장에만 동작)
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
