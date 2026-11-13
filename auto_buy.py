# -*- coding: utf-8 -*-
"""
auto_buy.py — AI '매수'(BUY) & 정규장 재평가 60점+(SCORE_MIN) 종목 매수 '계획(plan)' 산출 (실거래 집행은 분리)
─────────────────────────────────────────────────────────────────────
2026-06 개편: 이 스크립트는 더 이상 직접 주문을 내지 않는다.
  매 30분 사이클(=Cowork 평가 세션)마다 1회 실행되어
  '종목별 목표수량 + sweep 기준가(promising 지정가)'만 계산해
  logs/{날짜}_autobuy_plan.json 에 기록한다.

  실제 매수 집행은 별도 상시 데몬 execution_monitor.py 가 담당한다:
    - 09:00~15:30 정규장에 4초 간격으로 plan 을 읽어
    - 각 종목 매도호가(ka10004)를 실시간 감시
    - 매도호가가 promising 지정가 이하로 내려오면 그 가격까지 쌓인
      호가 잔량만큼(=즉시 체결될 수량) sweep 매수
    - target_qty − 현재보유 = 잔여를 모두 채울 때까지 매 틱 반복
    - 체결현황 텔레그램 보고도 monitor 가 담당

목표수량 산출 규칙 (사용자 확정):
  - 대상: 당일 누적 평가풀(logs/{날짜}_claude_results_all.json) 중 recommendation == BUY
          (+ 당일 신규 results.json 병합, 같은 코드는 최신값 우선)
  - 가격·점수 소스(2026-09-29): '정규장 재평가' 행만 — 오늘 Stock_V3 중 KRX 봇 기동 경계
      (buy_gate.krx_since, logs/{날짜}_krx_session.json) 이후 계산된 행의 score_total·close_price.
      장전 NXT 에서 60점+·AI BUY 여도 재평가 전이면 plan 보류(⏳), 재평가 60점+ 유지 시에만 편입.
  - 재평가 score×100 < 60(SCORE_MIN) → plan 제외
  - (2026-09-14) 보조매수: 정규장 재평가 60점+ 가 하나도 없으면 40점+ BUY 종목을 종목당 5% 목표로 편입
    (buy_gate.low_mode_ready — 장전 60점+ 재평가 대기·첫 정규장 스캔 미완료 시엔 판정 보류).
    target 에 min_score(집행 직전 재확인 임계)·low 표시 기록
  - 비중(구간제, 진입 60점, 재평가 점수 기준): 60~69=5% · 70~79=10% · 80~89=15% · 90~99=20% · 100=25%
  - 기준금액: 당일 총자산(추정예탁자산) 스냅샷 (당일 고정)
  - 종목당 목표수량(절대치) = (기준금액 × alloc%) ÷ 기준가(promising 지정가)
  - 목표 1주 미만(초고가주)인 종목은 plan 제외
  - 30분 세션마다 plan 전체를 새 promising 가격으로 재계산(목표수량·기준가 갱신)
  - (2026-10-07) 매수 순서 = 신규종목(장 시작 시 미보유) AI점수순 → 기존 보유종목 AI점수순.
    target 에 new 표시 → execution_monitor 가 신규종목 자금 부족 시 '순위교체 매도'(보유비중 순위대로
    수익 중 종목을 부족분만큼 매도) 수행. 40점 보조매수는 미보유 종목만 5% 신규매수 —
    이미 보유한 종목은 60점 넘기 전까지 추가매수 없음.
    롤백: logs/auto_buy.py.bak_20261007_rotate
  - (2026-10-21) 기존 보유종목은 비중 '구간'이 올라갈 때만 추가매수. 종목별 확정 구간·목표수량을
    logs/autobuy_tier_state.json 에 기억해, 같은 구간이면 확정 목표수량 고정(구간 하향 시 비례 축소)
    → 주가 하락·총자산 변동으로 목표수량이 늘어나도 단주 추가매수 없음. 덜 채워진 분량은 계속 채움.
    확정(2026-10-27 개정) = 빌드가 신규·구간상향 목표를 낼 때 '대기 목표(pend)+그때 보유수량'을 적어두고,
    다음 빌드(익일 첫 빌드 포함)에서 보유가 늘었으면 그 목표의 구간·목표수량으로 확정 — 체결 후 plan 제외·
    15:30 종가 체결도 확정되고, 장 막판 재계산값·체결 없는 상향은 확정되지 않음. 기록 없는 보유종목은
    매 빌드 즉시 현재 평가비중의 가장 가까운 구간·현재 보유수량으로 초기화. 같은 구간 잔여가 확정 목표의
    3%(최소 1주) 이하면 채운 것으로 간주. 전량 매도 시 기억 삭제.
    (2026-10-29 보완) 구간이 올랐는데 이미 그 목표를 채웠으면(잔여 3% 이하 포함) 지금 보유수량으로 즉시 확정
    (체결이 없어 확정 안 된 채 매 빌드 재계산되며 단주 추가매수 나던 문제). 잔고가 0종목으로 왔는데 계좌 합계가
    남아 있거나 조회 실패면(일시 오류 의심) 그 빌드는 기억 갱신 안 함(합계 0 = 진짜 전량 청산은 정상 처리).
    오늘 전량 매도 후 재매수하는 기보유 종목도 pend 발행(부분체결 고착 방지).
    롤백: logs/auto_buy.py.bak_20261021_tierkeep (+ logs/autobuy_tier_state.json 삭제)
          10-27 개정만 되돌리기: logs/auto_buy.py.bak_20261027_tierpend + logs/autobuy_tier_state.json.bak_20261027
          10-29 보완만 되돌리기: logs/auto_buy.py.bak_20261029_tierfix + logs/autobuy_tier_state.json.bak_20261029
  - (2026-11-09) 신용매수 우선(지수 바닥권 반등 대응, 사용자 지시): 신용가능 종목(kt20016, 키움 회사 기준·회사한도 미초과)은
    신용(kt10006)으로 매수하고 비중 2배(60~69=10 · 70~79=20 · 80~89=30 · 90~99=40 · 100=50%, 40점 보조 10%).
    신용불가 종목은 현금·당초 비중(5~25%, 보조 5%). 매수 순서 = 신용가능(신규→보유) → 신용불가(신규→보유), 각 AI점수순
    (신용주문가능 0 — 미수 등으로 신용을 실제로 못 쓰면 종전 순서 신규→보유).
    target 에 credit·credit_rate(보증금율)·base_alloc(1배)·cash_target_qty(1배 목표) 기록 — monitor 는 신용주문 거부·
    신용여력 부족 시 잠시 현금·cash_target_qty 로 대체. 신용계좌 아님(kt00012 거부)이면 전 종목 현금·당초 비중.
    켜기/끄기: 운영 스위치 credit_buy(kis_api/trade_switches.py · logs/trading_switches.json · 텔레그램 /신용 켜기|끄기) —
    매 빌드마다 읽음(재시작 불필요). 꺼지면 신용 조회 없이 전 종목 현금·당초 비중(11-09 이전과 동일).
    롤백: logs/auto_buy.py.bak_20261109_credit (+ autobuy_tier_state.json.bak_20261109_credit)

상태파일 (logs/):
  {날짜}_autobuy_base.json    당일 기준금액(총자산) 스냅샷
  {날짜}_autobuy_plan.json    종목별 목표수량 + sweep 기준가 (monitor 가 읽음)
  {날짜}_autobuy_exec.json    monitor 의 체결추적·미체결주문 상태 (monitor 가 씀)
"""

import os
import sys
import json
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import secrets
from kis_api import auth, kiwoom_trading as kt, trade_switches
import buy_gate

LOG_DIR = secrets.LOCAL_DATA_PATH
OWNER_ID = str(secrets.TELEGRAM_CHAT_ID)

ALLOC_MAX = 25.0          # 비중 상한 % (1안: 100점 25%)
SCORE_MIN = 60.0          # Total score 최소(점) (1안: 진입 60점)
KEEP_RESIDUAL = 0.03      # (2026-10-27) 기보유 같은 구간: 확정 목표 대비 부족분이 이 비율(최소 1주) 이하면 추가매수 안 함
ADD_GAP_PP = 1.0          # (2026-11-13) 기보유 추가매수 기준: 목표 비중 − 지금 보유비중 ≥ 이 값(%p)일 때만. None = 종전 확정 구간 기억 규칙
# (2026-11-09) 신용매수 우선 켜기/끄기 = 운영 스위치 credit_buy (_credit_on(), 텔레그램 /신용 켜기|끄기)
CREDIT_ALLOC_MULT  = 2.0  # 신용가능 종목 비중 배수(5~25% → 10~50%)
CREDIT_TTL_SEC     = 1800 # 신용계좌 여부·종목별 신용가능 조회 캐시(초) — logs/{날짜}_credit_info.json
CREDIT_PROBE_CODE  = "005930"   # 신용계좌 여부 확인용 상시 신용가능 종목(삼성전자 A군)
TIER_CAP = ALLOC_MAX * CREDIT_ALLOC_MULT   # 보유 구간 기억 상한(%) — 신용 2배 구간(최대 50)까지 기억(스위치 꺼도 동일: 낮은 구간은 keep 처리)


def _credit_on():
    """신용매수 우선 스위치(logs/trading_switches.json 'credit_buy')."""
    return trade_switches.get("credit_buy")


def _fmt(x):
    s = str(x).strip().split(".")[0]
    return s.zfill(6) if s.isdigit() else s


def _p(name):  # logs 경로
    return os.path.join(LOG_DIR, name)


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
        tmp = f"{path}.{os.getpid()}.tmp"     # 프로세스별 임시파일(동시 실행 시 서로 덮어쓰기 방지)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        print(f"   ⚠️ 저장 실패({path}): {e}")


def _plan_lock(today_str, wait_sec=60):
    """auto_buy 동시 실행 직렬화(프로세스 간 파일락). 개장킥·10분 사이클·CLI직후·UPS 구간킥이
    겹쳐도 한 번에 하나만 plan 을 만든다 → 직전 plan 비교가 정확해져 같은 알림 중복 방지.
    잠금 획득 시 파일객체, 대기 초과 시 None. (Windows msvcrt 없으면 잠금 없이 진행)"""
    try:
        import msvcrt, time
    except ImportError:
        return True
    os.makedirs(LOG_DIR, exist_ok=True)
    f = open(_p(f"{today_str}_autobuy.lock"), "a+")
    deadline = time.time() + wait_sec
    while True:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return f
        except OSError:
            if time.time() >= deadline:
                f.close()
                return None
            time.sleep(0.5)


def _plan_unlock(f):
    if f is True or f is None:
        return
    try:
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    finally:
        f.close()


def _results_path(d):       return _p(f"{d}_claude_results.json")
def _base_path(d):          return _p(f"{d}_autobuy_base.json")
def _plan_path(d):          return _p(f"{d}_autobuy_plan.json")
def _tier_path():           return _p("autobuy_tier_state.json")   # 날짜 무관 — 보유 중 유지


def _nearest_tier(weight_pct):
    """보유 평가비중(%) → 가장 가까운 비중 구간(5% 단위, 상한 TIER_CAP — 신용 2배면 50). 확정 구간 기록이 없는 기존 보유분 초기화용."""
    return max(5.0, min(TIER_CAP, int(weight_pct / 5.0 + 0.5) * 5.0))


def _filled_tier(alloc, target_qty, held_qty, date, src):
    """(2026-11-13) 목표(alloc%, target_qty주) 중 실제 보유 held_qty 만큼 채웠을 때의 확정 구간.
       다 채웠으면(잔여 ≤3%) 목표 그대로. 덜 채웠으면 '채운 비율 × 목표 비중'에 가장 가까운 구간(목표 이하)으로 확정
       — 그 구간이 목표 구간과 같으면 목표수량 유지(나머지 계속 채움), 낮으면 실제 보유수량을 확정수량으로.
       종전엔 1주라도 체결되면 목표 구간(예: NXT -3% 부분체결 40%)으로 확정돼, 실제 보유가 10% 수준이어도
       다음 날 점수 구간이 40% 이하인 동안 구간 상향으로 인식되지 않던 문제(재훈님 지적)."""
    alloc, tq, h = float(alloc), int(target_qty), int(held_qty)
    if tq <= 0 or h >= tq - max(1, int(tq * KEEP_RESIDUAL)):
        return {"alloc": alloc, "target_qty": tq, "date": date, "src": src}
    w = alloc * h / tq                       # 실제 채운 비중(목표 당시 가격 기준)
    a = min(alloc, _nearest_tier(w))
    # 반올림으로 목표 구간이 됐더라도 실제 채운 비중이 목표보다 2.5%p 이상 모자라면(5% 하한 구간 등) 보유수량으로 확정
    keep_tq = a >= alloc and w >= alloc - 2.5
    return {"alloc": a, "target_qty": tq if keep_tq else h, "date": date,
            "src": src if keep_tq else f"{src}_part"}


# ── (2026-11-09) 신용 가능 여부: 계좌(kt00012) + 종목(kt20016), CREDIT_TTL_SEC 캐시 ─────────────
def _credit_cache_path(d):  return _p(f"{d}_credit_info.json")


def _master_credit(code):
    """stock_master.json(매일 갱신) 의 '신용가능' 표시 — kt20016 조회 실패·캐시 없음일 때만 쓰는 폴백."""
    m = _load_json(_p("stock_master.json"), {})
    st = ((m.get("stocks") or {}).get(code) or {}).get("state", "") if isinstance(m, dict) else ""
    return "신용가능" in str(st)


def _credit_account(today_str, cache):
    """신용계좌 여부 (ok:bool, room:int|None, msg). kt00012 성공(return_code 0)=신용계좌,
       '신용계좌' 거부 메시지=아님. 통신 실패·그 외 거부는 직전 캐시값(없으면 아님)."""
    now = datetime.now().timestamp()
    acct = cache.get("acct") or {}
    if acct and now - float(acct.get("ts", 0) or 0) < CREDIT_TTL_SEC:
        return bool(acct.get("ok")), acct.get("room"), acct.get("msg", "")
    room, info = kt.fetch_credit_room(CREDIT_PROBE_CODE)
    if room is not None:
        acct = {"ok": True, "room": int(room), "msg": info, "ts": now}
    elif "신용계좌" in info:
        acct = {"ok": False, "room": None, "msg": info, "ts": now}
    else:                                   # 통신 실패 등 → 판정 보류(직전값 유지, 짧게 재시도)
        print(f"   ⚠️ 신용계좌 확인 실패({info}) — 직전 판정 유지")
        return bool(acct.get("ok")), acct.get("room"), acct.get("msg", info)
    cache["acct"] = acct
    return acct["ok"], acct["room"], acct["msg"]


def _credit_info(code, cache):
    """종목 신용가능 {"able", "rate", "over", "txt", "src"} — kt20016(캐시 TTL), 실패 시 직전 캐시 → stock_master 폴백."""
    now = datetime.now().timestamp()
    codes = cache.setdefault("codes", {})
    c = codes.get(code)
    if c and now - float(c.get("ts", 0) or 0) < CREDIT_TTL_SEC:
        return c
    info = kt.fetch_credit_info(code)
    if info is not None:
        c = dict(info, ts=now, src="kt20016")
        codes[code] = c
        return c
    if c:
        return c
    return {"able": _master_credit(code), "rate": None, "over": False, "txt": "", "src": "master", "ts": 0}


def _tier_target(code, alloc, price, base, existing, hq, tiers, today_str, filling=False):
    """비중 alloc(%) 의 목표수량 + 기존 보유종목 추가매수 판정.
       (2026-11-13 재훈님 지시) 기존 보유 = 지금 보유 평가비중(보유수량×기준가÷기준 총자산)이 목표 비중보다
       ADD_GAP_PP(1%p) 이상 모자랄 때만 목표 비중 수량까지 추가매수, 아니면 추가매수 없음(확정 구간 기억 미사용).
       (filling 인자는 미사용 — '체결 진행 중이면 마저 채움' 예외는 주가 하락 시 1%p 미만 단주 추가매수를 유발해 제거, 11-13 검토)
       반환 dict: amt·raw_qty(비중 그대로의 수량, <1 이면 plan 제외)·target_qty·tier(up=추가매수/keep=없음)·
       tier_from(지금 보유비중 %)·note·rec(항상 None — 호환용)."""
    amt = int(base * alloc / 100)
    raw_qty = amt // price if price > 0 else 0
    out = {"amt": amt, "raw_qty": raw_qty, "target_qty": raw_qty, "tier": None, "tier_from": None,
           "note": "", "rec": None}
    if raw_qty < 1 or not existing or hq <= 0:
        return out          # 신규·오늘 전량 매도한 기보유 = 목표 비중 수량 그대로
    if ADD_GAP_PP is not None:
        w = hq * price / base * 100 if base > 0 else 0.0
        gap = alloc - w
        if gap >= ADD_GAP_PP - 1e-9:
            tier, target_qty = "up", raw_qty
            note = f"·보유비중 {w:.1f}%→{alloc:.0f}% 추가매수(차이 {gap:.1f}%p)"
        else:
            tier, target_qty = "keep", min(raw_qty, hq)
            note = f"·보유비중 {w:.1f}%(목표 {alloc:.0f}%와 차이 {gap:.1f}%p<{ADD_GAP_PP:g}%p)"
        out.update(target_qty=target_qty, tier=tier, tier_from=round(w, 1), note=note)
        return out
    # ── 이하 2026-10-21~11-13 확정 구간 기억 규칙(ADD_GAP_PP=None 이면 사용) ──
    target_qty = raw_qty
    est = tiers.get(code)
    if not est and hq > 0:          # (Stock_V3 가격이 없어 위 ③에서 못 한 경우) 재평가 가격으로 초기화
        est = {"alloc": _nearest_tier(hq * price / base * 100), "target_qty": hq,
               "date": today_str, "src": "bootstrap"}
        out["rec"] = ("boot", est)
    if est:
        tier_from = float(est["alloc"])
        short = target_qty - hq
        if alloc > tier_from and hq > 0 and short <= max(1, int(target_qty * KEEP_RESIDUAL)):
            # (2026-10-29) 이미 상향 구간 목표를 채웠거나 잔여 단주(≤3%)뿐 → 지금 보유수량으로 그 구간 즉시 확정.
            #    체결이 안 나 pend 로는 확정되지 않아 매 빌드 'up' 재계산 → 주가 하락·총자산 변동 때 단주 추가매수가 나던 문제.
            target_qty = hq
            out["rec"] = ("up_met", {"alloc": float(alloc), "target_qty": hq, "date": today_str, "src": "up_met"})
            tier, note = "up_met", f"·비중상향 {tier_from:.0f}→{alloc:.0f}% 기충족(보유 {hq}주 확정)"
        elif alloc > tier_from:
            tier, note = "up", f"·비중상향 {tier_from:.0f}→{alloc:.0f}%"
        else:
            frozen = int(int(est["target_qty"]) * alloc / tier_from) if tier_from > 0 else 0
            target_qty = min(target_qty, frozen)
            short = target_qty - hq
            if 0 < short <= max(1, int(target_qty * KEEP_RESIDUAL)):
                target_qty = hq     # 확정 목표 대비 잔여 단주(≤3%) → 채운 것으로 간주(단주 추가매수 안 함)
            tier, note = "keep", f"·비중유지({tier_from:.0f}% 확정 {int(est['target_qty'])}주)"
        out.update(target_qty=target_qty, tier=tier, tier_from=tier_from, note=note)
    return out


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


def _alloc_pct(total_score100):
    """구간제 매수 비중(%). 1안(현재): 진입 60점, 10점 구간마다 5%p, 상한 25.
       60~69=5 · 70~79=10 · 80~89=15 · 90~99=20 · 100=25. 60 미만은 별도 컷에서 제외.
       예) 67→5%, 70→10%, 89→15%. (main_stock 알림과 동일)"""
    bucket = int(float(total_score100) // 10)      # 60~69→6, 70~79→7 … 100→10
    return max(0.0, min(ALLOC_MAX, (bucket - 5) * 5.0))


# ── 당일 기준금액(총자산) 스냅샷 ──────────────────────────────────────
def _get_base(today_str):
    saved = _load_json(_base_path(today_str), None)
    if isinstance(saved, dict) and saved.get("date") == today_str and saved.get("base"):
        return int(saved["base"])
    ta = kt.fetch_total_assets()
    if ta and ta > 0:
        _save_json(_base_path(today_str), {"date": today_str, "base": int(ta),
                                           "captured": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        print(f"   💰 기준금액(총자산) 스냅샷: {ta:,}원")
        return int(ta)
    return None


# ── 매수 계획(plan) 산출: 종목별 목표수량 + sweep 기준가 → plan 파일 ──────
def _build_plan(today_str, after=False):
    """당일 BUY 평가풀에서 종목별 '목표수량(절대치)'과 'sweep 기준가(promising 지정가)'를
       계산해 logs/{날짜}_autobuy_plan.json 에 저장한다. (주문은 내지 않는다.)
       실제 집행은 execution_monitor.py 가 이 plan 을 읽어 호가 sweep 으로 수행한다.
       after=True(2026-11-09): NXT 애프터 신용매수 창(15:35~19:55)에서 1회 재산출 — 점수·가격은 정규장
       재평가 행(Stock_V3, 장후 미갱신)만 쓰므로 장후 NXT 점수가 섞이지 않는다. execution_monitor 가 호출."""
    # 장전/장후엔 promising(Search_History) 점수가 NXT 시세 기준이라, 순간 50+ 스파이크가
    # 그대로 plan 에 실려 개장 후 실매수로 이어진다(태웅 사례). → KRX 정규장(09:00~15:30, 수능일 10:00~16:30)에만
    # plan 을 빌드한다. 정규장 밖에선 기존 plan 을 건드리지 않는다(당일 신규 plan 은 개장 후 생성).
    if not kt.is_market_open() and not (after and kt.is_nxt_after_buy_window()):
        print("ℹ️ KRX 정규장 아님 — plan 빌드 생략(장전/장후 NXT 점수로 매수 방지)")
        return
    rpath = _p(f"{today_str}_claude_results_all.json")
    results_all = _load_json(rpath, [])
    # 당일 신규 평가(results.json)도 병합 — 같은 코드는 최신값 우선.
    fresh = _load_json(_results_path(today_str), [])
    # [2순위] results_all(누적)이 아직 없어도 당일 results.json(평가세션 확정분)만으로 plan 생성.
    #   promote 가 results_all 을 늦게 쓰거나 타임아웃에 잘려도 매수 감시가 밀리지 않게 한다.
    #   (2026-11-11) 평가 결과가 아직 없어도 V3 정규장 60점+ 'AI 평가 대기' 종목으로 plan 을 만든다(배분 예약).

    base = _get_base(today_str)
    if not base:
        print("   ⚠️ 총자산 조회 실패 — plan 생략")
        return

    # 병합 — 같은 코드는 최신값(fresh) 우선.
    merged = {}
    for r in (results_all + fresh):     # fresh가 뒤 → 같은 코드면 최신값으로 갱신
        c = _fmt(r.get("code", ""))
        if c:
            merged[c] = r
    results = list(merged.values())

    # ── 점수·가격 소스 = '정규장 재평가' 행만 (buy_gate 규칙, 2026-09-29)
    #    장전 NXT 에서 60점+ 가 나와 AI 평가 BUY 를 받았어도, 09시 이후 KRX 봇이 다시 계산한
    #    점수가 나오기 전까지는 plan 에 넣지 않는다(보류). 재평가 60점+ 유지 시에만 편입하고
    #    비중·지정가도 재평가 시점 값으로 산정한다.
    since = buy_gate.krx_since(LOG_DIR, today_str)
    v3_all = buy_gate.load_v3_rows(LOG_DIR, today_str)
    rescored = buy_gate.rescored_rows(v3_all, since, today_str)   # 수능일 등 KRX 지연일은 ~16:30:59
    # 보조매수 규칙(buy_gate): 정규장 재평가 기준 60점+ 가 하나도 없으면 40점+ BUY 종목을 5% 목표로 편입
    #   (장전 60점+ 가 재평가 대기 중이거나 첫 정규장 스캔이 덜 끝났으면 판정 보류 = 60점 모드)
    low = buy_gate.low_mode_ready(v3_all, rescored)
    # ── (2026-11-11) 매수 대상 = 오늘 AI 평가 BUY + 'AI 평가 대기'(정규장 재평가 기준점+ 인데 오늘 미평가).
    #    오늘 평가가 관망·회피면 제외. 오늘 미평가인데 가장 최근(직전) 평가가 관망·회피면 재평가 BUY 전까지 제외.
    #    평가 대기 종목은 plan 에 await_eval 로 실려 monitor 가 비중대로 매수가능금액을 '예약'만 한다(주문 없음)
    #    → AI 평가가 늦게 끝난 V3 상위 종목이 먼저 평가된 종목들에 자금을 다 뺏기지 않게.
    today_eval = {_fmt(r.get("code", "")): r for r in results if _fmt(r.get("code", ""))}
    prior_eval = buy_gate.latest_evals(LOG_DIR, today_str)          # 오늘 이전 '직전 평가'
    thr100 = (buy_gate.LOW_MIN if low else buy_gate.PRIMARY_MIN) * 100
    # 평가 대기 기준점은 평가 파이프라인(build_pending: 오늘 V3 전 행 기준 low_mode)과 맞춘다 — 실제로 평가 큐에
    #   들어갈 종목만 예약(파이프라인은 60점 모드인데 여기만 40점 모드면 영영 평가 안 될 종목이 몫을 잡음)
    pipe_low = buy_gate.low_mode([r["score100"] / 100.0 for r in v3_all.values()])
    await_thr100 = max(thr100, (buy_gate.LOW_MIN if pipe_low else buy_gate.PRIMARY_MIN) * 100)
    cand_codes = ({c for c, r in today_eval.items() if str(r.get("recommendation", "")).upper() == "BUY"}
                  | {c for c, rs in rescored.items() if float(rs.get("score100", 0) or 0) >= await_thr100})
    if not cand_codes:
        print("ℹ️ 매수 대상 없음(오늘 AI BUY·정규장 기준점+ 평가대기 모두 없음) — plan 생략")
        return
    # V3 정규장 점수 높은 순(재평가 전 종목은 뒤)
    cands = sorted(cand_codes, key=lambda c: -float((rescored.get(c) or {}).get("score100", -1) or -1))

    # ── 신규/기존 보유 구분 (2026-10-07): 기존 = 장 시작 시 보유(현재보유 − 오늘 우리 sweep 체결분 > 0).
    #    오늘 막 사기 시작한 신규종목은 끝까지 '신규'로 남아 우선순위를 유지한다.
    hs = kt.fetch_all_holdings()
    if hs is None:
        print("   ⚠️ 잔고 조회 실패 — 신규/보유 구분 불가, plan 갱신 생략(직전 plan 유지)")
        return
    held = {_fmt(h.get("code", "")): int(h.get("qty", 0) or 0) for h in hs}
    ex = _load_json(_p(f"{today_str}_autobuy_exec.json"), {})
    bought_today = {}
    if isinstance(ex, dict) and ex.get("date") == today_str:
        for c, v in (ex.get("codes") or {}).items():
            v = v or {}
            b = int(v.get("bought", 0) or 0)
            # (2026-11-09) 장후 재산출: 15:30 종가 단일가 체결분은 정규장 틱이 없어 bought 에 아직 없음 → 보유 증가분을 더해
            #   오늘 처음 산 종목이 '기존 보유'로 잡히지 않게(신규 유지·40점 보조 유지). exec 상태는 바꾸지 않음(종가 결과 알림용).
            if after and v.get("close_buy_hold"):
                b += max(0, held.get(_fmt(c), 0) - int(v.get("held_seen", 0) or 0))
            bought_today[_fmt(c)] = b

    # 오늘 매도 체결분(손절/청산·순위교체)을 되더해 '장 시작 시 보유'를 복원(2026-10-14):
    #   오늘 판 종목이 잔고 0 이라고 '신규'로 잡히면 재매수 자금 마련용 순위교체 매도가 돌아 churn 발생.
    sold_today = {}
    for fn in (f"{today_str}_autosell_exec.json", f"{today_str}_rotate_exec.json"):
        st = _load_json(_p(fn), {})
        if isinstance(st, dict) and st.get("date") == today_str:
            for c, v in (st.get("codes") or {}).items():
                sold_today[_fmt(c)] = sold_today.get(_fmt(c), 0) + int((v or {}).get("sold", 0) or 0)
    fills = kt.fetch_today_sell_fills()         # 실제 매도체결(수기 매도 포함) — 조회 실패 시 상태파일 값만 사용
    for c, f in (fills or {}).items():
        sold_today[_fmt(c)] = max(sold_today.get(_fmt(c), 0), int((f or {}).get("qty", 0) or 0))

    def _existing(c):
        return held.get(c, 0) - bought_today.get(c, 0) + sold_today.get(c, 0) > 0

    # ── 기존 보유종목 확정 비중구간 (2026-10-21): {code: {alloc, target_qty, date, src}}
    #    기존 보유종목은 비중 구간(5·10·15·20·25%)이 '올라갈 때만' 새 목표로 추가매수한다.
    #    같은 구간이면 확정 당시 목표수량으로 고정 → 주가 하락·총자산 변동으로 목표수량이 늘어도 단주 추가매수 없음
    #    (2026-10-21 파두 15% 동일구간 11주·프로텍 1주 추가매수 사례). 덜 채워진 분량은 계속 채운다.
    tier_state = _load_json(_tier_path(), {})
    tier_state = tier_state if isinstance(tier_state, dict) else {}
    tiers = dict(tier_state.get("codes") or {})
    pend = dict(tier_state.get("pend") or {})   # 직전 빌드가 낸 신규·구간상향 목표 {code: {alloc, target_qty, held_at, date}}
    # (2026-10-29) 잔고가 통째로 비어 왔는데 기억이 있으면 계좌 합계(총매입·총평가)로 교차확인:
    #    둘 다 0 = 진짜 빈 계좌(전량 청산) → 정상 처리(기억 삭제). 합계가 남아 있거나 조회 실패 = 일시 오류 의심
    #    → 이번 빌드는 tier 기억을 읽기만 하고 확정·삭제·발행·저장 안 함(plan 은 그대로 빌드).
    #    갱신하면 pend 유실 + 전 종목이 '신규'로 pend(held_at 0) 발행 → 다음 빌드에서 재계산값으로 일괄 덮어써짐.
    tier_frozen = False
    if not held and (tiers or pend):
        tot = kt.fetch_stock_totals()
        tier_frozen = not (tot and tot["pur"] == 0 and tot["evlt"] == 0)
        if not tier_frozen:
            print("   ℹ️ 잔고 0종목·계좌 합계 0 확인(전량 청산) — 보유 구간 기억 정리")

    if not tier_frozen:
        # ① 확정(2026-10-27): 직전 빌드가 낸 신규·구간상향 목표(pend) 이후 보유가 늘었으면 그 목표로 체결된 것
        #    → 그 목표의 구간·목표수량으로 확정. 체결 후 plan 에서 빠졌거나 구간이 내려갔어도,
        #    15:30 종가 체결이라 당일 재빌드가 없어도 다음 빌드(익일 첫 빌드)에서 확정된다.
        #    (빌드 시점 재계산값·당일 누적 bought 로 기록하면 장 막판 가격 드리프트나 체결 없는 상향이 확정되던 문제 수정)
        #    (2026-11-13) 목표 구간이 아니라 '실제 보유수량이 채운 비율'의 구간으로 확정(_filled_tier) — 부분체결이면 낮은 구간
        for c, p in pend.items():
            if held.get(c, 0) > int((p or {}).get("held_at", 0) or 0):
                tiers[c] = _filled_tier(p["alloc"], p["target_qty"], held.get(c, 0), today_str, "fill")
        pend = {}                       # 이번 빌드가 새로 발행(미체결 목표는 버림)
        # (2026-11-13) 1회 정리: 종전 규칙으로 목표 구간 확정된 기억을 실제 보유 기준으로 재확정
        if held and not tier_state.get("fill_actual_v2"):
            for c, e in list(tiers.items()):
                if str(e.get("src", "")).startswith("fill") and held.get(c, 0) > 0:
                    tiers[c] = _filled_tier(e["alloc"], e["target_qty"], held[c], e.get("date", today_str), "fill")
            tier_state["fill_actual_v2"] = today_str
        # ② 전량 매도 → 기억 삭제(다시 사면 신규로 새로 산정). 빈 잔고 응답은 위 tier_frozen 이 걸러냄.
        for c in list(tiers):
            if held.get(c, 0) <= 0:
                del tiers[c]
    # ③ 기록 없는 보유종목은 plan 편입 여부와 무관하게 지금 즉시 초기화(수기 매수 등).
    #    나중에 plan 에 들어올 때 초기화하면 그새 주가·총자산이 변해 구간을 잘못 읽는다.
    #    = 지금 평가비중에 가장 가까운 구간 + 지금 보유수량을 확정으로 간주.
    for c, q in held.items():
        px = int((v3_all.get(c) or {}).get("price", 0) or 0)
        if q > 0 and c not in tiers and px > 0:
            tiers[c] = {"alloc": _nearest_tier(q * px / base * 100), "target_qty": q,
                        "date": today_str, "src": "bootstrap"}

    # ── (2026-11-09) 신용매수 우선: 신용계좌 여부(kt00012) → 종목별 신용가능(kt20016) → 신용가능은 비중 2배·신용매수
    credit_cache = _load_json(_credit_cache_path(today_str), {})
    credit_cache = credit_cache if isinstance(credit_cache, dict) else {}
    credit_on = _credit_on()                 # 운영 스위치(매 빌드 읽음) — 꺼지면 신용 조회 없이 전 종목 현금·당초 비중
    acct_ok, acct_room, acct_msg = (_credit_account(today_str, credit_cache) if credit_on
                                    else (False, None, "스위치 꺼짐"))
    if not credit_on:
        credit_txt = ""
    elif acct_ok:
        credit_txt = (f" · 신용가능 종목 신용매수 우선(비중 {CREDIT_ALLOC_MULT:g}배"
                      f"{f', 신용주문가능 {acct_room:,}원' if acct_room is not None else ''})")
    else:
        credit_txt = f" · 신용계좌 아님({acct_msg[:30]}) → 전 종목 현금·당초 비중"

    targets = {}
    notes = {}      # {code: (사유종류, 알림줄)} — BUY 인데 plan 에 못 넣은 종목
    low_txt =" · 정규장 60점+ 0개 → 40점+ 보조매수(종목당 5%)" if low else ""
    lines = [f"🧮 매수 plan 갱신 (기준 총자산 {base:,}원){low_txt}{credit_txt}"]
    for code in cands:
        r = today_eval.get(code)
        rs0 = rescored.get(code) or v3_all.get(code) or {}
        name = (r or {}).get("name", "") or rs0.get("name", "")
        cscore = float((r or {}).get("claude_score", 0) or 0)
        pe = prior_eval.get(code)
        if r is not None:
            rec = str(r.get("recommendation", "")).upper()
            if rec != "BUY":
                ln = f"🚫 {name}({code}) 오늘 AI {r.get('grade') or rec}({cscore:.0f}) — 매수대상 제외·재배분"
                lines.append(ln); notes[code] = ("ai_hold", ln)
                continue
            await_eval = False
        elif pe and pe.get("rec") in ("HOLD", "AVOID"):
            ln = (f"⏸️ {name}({code}) 직전 AI {pe['rec']}({pe['date'][4:6]}-{pe['date'][6:]}·{pe.get('score', 0)}점) "
                  f"— 재평가 BUY 전까지 매수대상 제외")
            lines.append(ln); notes[code] = ("prior_hold", ln)
            continue
        else:
            await_eval = True                # 오늘 AI 평가 대기 — 비중대로 매수가능금액만 예약

        rs = rescored.get(code)
        if not rs or rs.get("price", 0) <= 0:
            pre = v3_all.get(code)
            pre_txt = (f" · 장전 {pre['score100']:.0f}점({pre['time']})"
                       if pre and pre.get("time") and pre["time"] < since else "")
            ln = f"⏳ {name}({code}) 정규장 재평가 대기{pre_txt} — plan 보류"
            lines.append(ln); notes[code] = ("wait", ln)
            continue
        price = int(rs["price"]); sc = float(rs["score100"]); psrc = f"정규장재평가 {rs['time']}"
        if rs.get("name"):
            name = rs["name"]

        if sc >= SCORE_MIN:
            alloc, min_sc, low_t = _alloc_pct(sc), SCORE_MIN, False
        elif low and sc >= buy_gate.LOW_MIN * 100:
            alloc, min_sc, low_t = buy_gate.LOW_ALLOC_PCT, buy_gate.LOW_MIN * 100, True
        else:
            cut = buy_gate.LOW_MIN * 100 if low else SCORE_MIN
            ln = f"⏭️ {name}({code}) 정규장 점수 {sc:.0f}<{cut:.0f} — plan 제외({psrc})"
            lines.append(ln); notes[code] = ("below", ln)
            continue

        existing = _existing(code)
        if low_t and existing:
            # 40점 보조매수는 미보유 종목만 5% — 이미 보유한 종목은 60점 넘기 전까지 추가매수 없음
            ln = f"⏸️ {name}({code}) 40점 보조 · 이미 보유 — 60점 전까지 추가매수 없음({sc:.0f}점)"
            lines.append(ln); notes[code] = ("lowheld", ln)
            continue

        # (2026-11-09) 신용가능 종목 = 신용매수 + 비중 2배. 신용불가(또는 신용계좌 아님)는 현금·당초 비중.
        cinfo = _credit_info(code, credit_cache) if acct_ok else None
        use_credit = bool(cinfo and cinfo.get("able"))
        base_alloc = alloc
        if use_credit:
            alloc = base_alloc * CREDIT_ALLOC_MULT
        hq = held.get(code, 0)

        # 기존 보유종목: 확정 구간보다 높아질 때만 새 목표, 아니면 확정 목표수량 고정(구간 하향 시 비례 축소)
        filling = bought_today.get(code, 0) > 0      # 오늘 이미 추가매수 체결 시작 → 1%p 미만이어도 마저 채움
        tt = _tier_target(code, alloc, price, base, existing, hq, tiers, today_str, filling)
        target_amt, target_qty = tt["amt"], tt["target_qty"]
        if tt["raw_qty"] < 1:
            ln = f"⏭️ {name}({code}) 비중목표({target_amt:,}원) 1주 미만 — plan 제외"
            lines.append(ln); notes[code] = ("small", ln)
            continue
        if tt["rec"]:
            kind, rec = tt["rec"]
            # 평가 대기 종목은 매수하지 않으므로 구간 상향 기충족(up_met) 확정은 하지 않음(부트스트랩만)
            if kind == "boot" or (not tier_frozen and not await_eval):
                tiers[code] = rec
        tier, tier_from, tier_note = tt["tier"], tt["tier_from"], tt["note"]
        # 신용주문이 막힐 때(거부·신용여력 부족) monitor 가 대신 쓸 '당초 비중(1배)' 현금 목표 — tier 기억은 바꾸지 않음
        cash_tq = target_qty
        if use_credit:
            ct = _tier_target(code, base_alloc, price, base, existing, hq, tiers, today_str, filling)
            cash_tq = ct["target_qty"] if ct["raw_qty"] >= 1 else 0

        targets[code] = {
            "name": name,
            "promising_price": price,        # sweep 기준 지정가
            "target_qty": target_qty,        # 절대 목표수량(보유 포함) — 신용 종목은 2배 비중 기준
            "alloc": round(alloc, 1),        # 적용 비중(신용 종목은 2배)
            "base_alloc": round(base_alloc, 1),   # 당초 비중(1배)
            "credit": use_credit,            # (2026-11-09) True = 신용(kt10006) 매수 대상
            "credit_rate": (cinfo or {}).get("rate") if use_credit else None,   # 신용보증금율(0.45)
            "cash_target_qty": cash_tq,      # 신용 불가 시 대체 목표(당초 비중·현금)
            "score100": round(sc, 1),
            "claude_score": round(cscore, 1),
            "price_src": psrc,
            "min_score": min_sc,             # 집행 직전 재확인 임계(점) — 보조매수는 40
            "low": low_t,
            "new": not existing,             # 신규종목(장 시작 시 미보유) — 매수 우선 + 순위교체 매도 대상
            "tier": tier,                    # 기존 보유: up=비중구간 상향(새 목표) · keep=확정 목표수량 고정
            "tier_from": tier_from,          # 기존 보유 확정 구간(%)
            "await_eval": await_eval,        # (2026-11-11) True = 오늘 AI 평가 대기 — monitor 가 배분만 예약, 주문 없음
            "prior_eval": (pe or {}).get("rec"),   # 직전(오늘 이전) AI 평가
        }
        if await_eval:
            lines.append(f"⏳ {name}({code}) AI 평가 대기 — 비중 {alloc:.0f}%{'·신용' if use_credit else ''} 몫 예약"
                         f"(V3 {sc:.0f}{'·직전 ' + pe['rec'] if pe else '·첫 평가'})")
        else:
            lines.append(f"🎯 {name}({code}) 목표 {target_qty}주×{price:,}원 "
                         f"(비중 {alloc:.0f}%{'·신용' if use_credit else ''}{'·40점보조' if low_t else ''}"
                         f"{'·신규' if not existing else '·보유'}{tier_note}·AI{cscore:.0f}·{psrc})")
        # ④ 신규·구간상향 목표는 '대기 목표(pend)'로 발행 — 이후 보유가 늘면 다음 빌드 ①에서 이 목표로 확정
        #    (2026-10-29) tier=None 인 기보유(= 오늘 전량 매도해 기억이 지워진 종목)도 발행 — 당일 재매수 체결이
        #    확정되지 않고 부분체결 수량으로 bootstrap 되어 나머지를 영영 안 채우던 문제
        #    (2026-11-11) 평가 대기 종목은 매수하지 않으므로 발행 안 함
        if tier in (None, "up") and not tier_frozen and not await_eval:
            pend[code] = {"alloc": round(alloc, 1), "target_qty": target_qty,
                          "held_at": held.get(code, 0), "date": today_str}

    if tier_frozen:
        print("   ⚠️ 잔고 0종목인데 계좌 합계가 남아 있거나 조회 실패(일시 오류 의심) — 보유 구간 기억(tier_state) 갱신 생략")
    else:
        _save_json(_tier_path(), {"codes": tiers, "pend": pend,
                                  "fill_actual_v2": tier_state.get("fill_actual_v2"),
                                  "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    if credit_on:
        _save_json(_credit_cache_path(today_str), credit_cache)

    # 매수 순서(2026-11-09): 신용을 실제로 쓸 수 있으면(신용주문가능 > 0) 신용가능(신규→보유) → 신용불가(신규→보유),
    #   쓸 수 없으면(스위치 꺼짐·신용계좌 아님·미수 등으로 신용주문가능 0 → 신용 종목도 현금·당초 비중으로 대체되므로)
    #   종전대로 신규 → 보유. 각 그룹 안은 AI점수순(안정 정렬). monitor 는 이 순서로 현금(신용은 보증금 몫)을 예약한다.
    credit_live = bool(acct_ok and (acct_room or 0) > 0)
    # (2026-11-11) 금액은 monitor 가 비중 비례로 배분하므로 순서는 감시 슬롯·표시용 — 신용 묶음 안에서 V3 점수 높은 순
    targets = dict(sorted(targets.items(), key=lambda kv: ((0 if (credit_live and kv[1].get("credit")) else 1),
                                                           -float(kv[1].get("score100", 0) or 0))))

    plan = {
        "date": today_str,
        "base": int(base),
        "low_mode": low,
        "credit_acct": acct_ok,              # (2026-11-09) 신용계좌 여부(kt00012) — False 면 전 종목 현금·당초 비중
        "credit_room": acct_room,            # 신용주문가능금액(삼성전자 기준, 참고)
        "credit_mult": CREDIT_ALLOC_MULT if credit_on else 1.0,
        "credit_live": credit_live,          # 신용 우선 정렬 적용 여부(신용주문가능 > 0)
        "built_after": bool(after and not kt.is_market_open()),   # NXT 애프터 신용매수용 장후 재산출(정규장 점수·가격 그대로)
        "krx_since": since,                  # 정규장 재평가 경계(이후 계산된 점수만 사용)
        "rescored_cnt": len(rescored),       # 재평가 완료 종목 수 / 오늘 Stock_V3 전체
        "v3_cnt": len(v3_all),
        "session_primary_cnt": buy_gate.primary_count([x["score100"] / 100.0 for x in rescored.values()]),
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # 집행 우선순위 = 신규종목 AI점수순 → 기존 보유 AI점수순 (dict 삽입순 보존)
        "targets": targets,
        # BUY 인데 plan 에 못 들어간 종목의 사유 종류(wait=재평가대기·below=점수미달·small=1주미만·lowheld=40점 보유종목)
        "held": {c: k for c, (k, _) in notes.items()},
    }
    # ── 직전 plan 과 비교: 종목구성·비중 변동 시에만 알림 (2026-08-19, 2026-09-29 개정)
    #    가격 변동에 따른 목표수량 미세변동은 plan 파일만 갱신하고 알림 생략(개장킥 60초 반복 대비).
    #    BUY 종목의 보류/제외 사유 종류가 바뀌면(예: 재평가대기→점수미달) 그 줄만 1회 알림.
    prev_plan = _load_json(_plan_path(today_str), {})
    prev_plan = prev_plan if isinstance(prev_plan, dict) else {}
    prev_targets = prev_plan.get("targets", {}) or {}
    prev_held = prev_plan.get("held", {}) or {}

    def _sig(tmap):
        return {c: (t.get("alloc"), bool(t.get("await_eval"))) for c, t in tmap.items()}

    changed = _sig(prev_targets) != _sig(targets)
    note_lines = [ln for c, (k, ln) in notes.items() if prev_held.get(c) != k]

    _save_json(_plan_path(today_str), plan)
    print(f"   📝 plan 저장: {len(targets)}종목 → {_plan_path(today_str)}")

    if not changed:
        if note_lines:
            _send_owner("\n".join([f"🧮 매수 plan 보류·제외 변동{low_txt}"] + note_lines))
        else:
            print("   🔕 종목구성·비중·보류사유 변동 없음 — 알림 생략")
        return

    # ── 잔고 대비 부족수량 계산 (2026-08-19): 전 종목 목표 이상 보유 시 알림 생략,
    #    부족 종목은 목표/보유/부족수량 + 매수 필요 금액 표시. (잔고는 plan 빌드 시 조회한 값 재사용)
    out = [f"🧮 매수 plan 갱신 (기준 총자산 {base:,}원){low_txt}{credit_txt}"]
    need_total = 0
    shortage = False
    for code, t in targets.items():
        hq = held.get(code, 0)
        diff = int(t["target_qty"]) - hq
        if t.get("await_eval"):
            out.append(f"⏳ {t['name']}({code}) AI 평가 대기 — 비중 {t['alloc']:.0f}%{'·신용' if t.get('credit') else ''} 몫 예약"
                       f"(부족 {max(0, diff)}주·V3 {t['score100']:.0f})")
            continue
        if diff > 0:
            shortage = True
            amt = diff * int(t["promising_price"])
            need_total += amt
            if ADD_GAP_PP is not None:
                up = f"·보유비중 {t['tier_from']:.1f}%→{t['alloc']:.0f}%" if t.get("tier") == "up" else ""
            else:
                up = (f"·비중상향 {t['tier_from']:.0f}→{t['alloc']:.0f}%" if t.get("tier") == "up"
                      else "·확정수량까지 채움" if t.get("tier") == "keep" else "")
            out.append(f"🎯 {t['name']}({code}) 목표 {t['target_qty']}주 / 보유 {hq}주 "
                       f"→ 부족 {diff}주 ≈ {amt:,}원 "
                       f"(기준가 {t['promising_price']:,}원·비중 {t['alloc']:.0f}%"
                       f"{'·신용' if t.get('credit') else ''}·{'신규' if t['new'] else '보유'}"
                       f"{up}·AI{t['claude_score']:.0f})")
        elif t.get("tier") == "keep" and ADD_GAP_PP is not None:
            out.append(f"✅ {t['name']}({code}) 비중 {t['alloc']:.0f}% (보유 {hq}주·보유비중 {t['tier_from']:.1f}%) "
                       f"— 차이 {ADD_GAP_PP:g}%p 미만, 추가매수 없음")
        elif t.get("tier") == "keep":
            why = ("같은 구간은 추가매수 없음" if t["alloc"] >= t["tier_from"]
                   else f"확정 {t['tier_from']:.0f}%보다 낮은 구간 — 추가매수 없음")
            out.append(f"✅ {t['name']}({code}) 비중 {t['alloc']:.0f}% (보유 {hq}주) — {why}")
        elif t.get("tier") == "up_met":
            out.append(f"✅ {t['name']}({code}) 비중상향 {t['tier_from']:.0f}→{t['alloc']:.0f}% "
                       f"이미 충족(보유 {hq}주) — 추가매수 없음")
        else:
            out.append(f"✅ {t['name']}({code}) 목표 {t['target_qty']}주 충족(보유 {hq}주)")
    if not shortage:
        if note_lines:
            _send_owner("\n".join([f"🧮 매수 plan 보류·제외 변동{low_txt}"] + note_lines))
        else:
            print("   🔕 전 종목 목표수량 이상 보유 — 알림 생략")
        return
    out.append(f"💸 총 매수 필요 금액 ≈ {need_total:,}원")
    out += [ln for ln in lines[1:] if ln.startswith(("⏭️", "⏸️", "🚫")) or ln.startswith("⏳") and "재평가 대기" in ln]   # 제외·재평가대기·40점보유·AI관망 사유
    _send_owner("\n".join(out))


def run(today_str=None, after=False):
    today_str = today_str or datetime.now().strftime("%Y%m%d")
    if not getattr(secrets, "AUTO_BUY_ENABLED", False):
        print("ℹ️ AUTO_BUY_ENABLED=False — 자동매수 비활성")
        return
    if not kt.is_trading_day():
        print("📴 휴장일(주말/공휴일) — auto_buy 스킵(base/plan 미기록)")
        return
    auth.get_access_token()        # 토큰 준비 (한투/공용)
    lock = _plan_lock(today_str)
    if lock is None:
        print("⏳ 다른 auto_buy 가 60초 넘게 plan 작성 중 — 이번 실행 생략")
        return
    try:
        _get_base(today_str)       # 기준금액 스냅샷(장 시작 전이라도 확보)
        _build_plan(today_str, after=after)   # 목표수량·기준가 계산 → plan 파일 (집행은 monitor)
    finally:
        _plan_unlock(lock)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--after"]
    run(args[0] if args else None, after="--after" in sys.argv[1:])
