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
from kis_api import auth, kiwoom_trading as kt
import buy_gate

LOG_DIR = secrets.LOCAL_DATA_PATH
OWNER_ID = str(secrets.TELEGRAM_CHAT_ID)

ALLOC_MAX = 25.0          # 비중 상한 % (1안: 100점 25%)
SCORE_MIN = 60.0          # Total score 최소(점) (1안: 진입 60점)
KEEP_RESIDUAL = 0.03      # (2026-10-27) 기보유 같은 구간: 확정 목표 대비 부족분이 이 비율(최소 1주) 이하면 추가매수 안 함


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
    """보유 평가비중(%) → 가장 가까운 비중 구간(5·10·15·20·25). 확정 구간 기록이 없는 기존 보유분 초기화용."""
    return max(5.0, min(ALLOC_MAX, int(weight_pct / 5.0 + 0.5) * 5.0))


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
def _build_plan(today_str):
    """당일 BUY 평가풀에서 종목별 '목표수량(절대치)'과 'sweep 기준가(promising 지정가)'를
       계산해 logs/{날짜}_autobuy_plan.json 에 저장한다. (주문은 내지 않는다.)
       실제 집행은 execution_monitor.py 가 이 plan 을 읽어 호가 sweep 으로 수행한다."""
    # 장전/장후엔 promising(Search_History) 점수가 NXT 시세 기준이라, 순간 50+ 스파이크가
    # 그대로 plan 에 실려 개장 후 실매수로 이어진다(태웅 사례). → KRX 정규장(09:00~15:30)에만
    # plan 을 빌드한다. 정규장 밖에선 기존 plan 을 건드리지 않는다(당일 신규 plan 은 개장 후 생성).
    if not kt.is_market_open():
        print("ℹ️ KRX 정규장 아님 — plan 빌드 생략(장전/장후 NXT 점수로 매수 방지)")
        return
    rpath = _p(f"{today_str}_claude_results_all.json")
    results_all = _load_json(rpath, [])
    # 당일 신규 평가(results.json)도 병합 — 같은 코드는 최신값 우선.
    fresh = _load_json(_results_path(today_str), [])
    # [2순위] results_all(누적)이 아직 없어도 당일 results.json(평가세션 확정분)만으로 plan 생성.
    #   promote 가 results_all 을 늦게 쓰거나 타임아웃에 잘려도 매수 감시가 밀리지 않게 한다.
    if not results_all and not fresh:
        print("ℹ️ 평가결과 전무(results_all·results 모두 없음) — plan 생략")
        return

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
    rescored = buy_gate.rescored_rows(v3_all, since)
    # 보조매수 규칙(buy_gate): 정규장 재평가 기준 60점+ 가 하나도 없으면 40점+ BUY 종목을 5% 목표로 편입
    #   (장전 60점+ 가 재평가 대기 중이거나 첫 정규장 스캔이 덜 끝났으면 판정 보류 = 60점 모드)
    low = buy_gate.low_mode_ready(v3_all, rescored)
    cands = [r for r in results
             if str(r.get("recommendation", "")).upper() == "BUY"]
    cands.sort(key=lambda r: float(r.get("claude_score", 0) or 0), reverse=True)

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
        bought_today = {_fmt(c): int((v or {}).get("bought", 0) or 0)
                        for c, v in (ex.get("codes") or {}).items()}

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
        for c, p in pend.items():
            if held.get(c, 0) > int((p or {}).get("held_at", 0) or 0):
                tiers[c] = {"alloc": float(p["alloc"]), "target_qty": int(p["target_qty"]),
                            "date": today_str, "src": "fill"}
        pend = {}                       # 이번 빌드가 새로 발행(미체결 목표는 버림)
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

    targets = {}
    notes = {}      # {code: (사유종류, 알림줄)} — BUY 인데 plan 에 못 넣은 종목
    low_txt =" · 정규장 60점+ 0개 → 40점+ 보조매수(종목당 5%)" if low else ""
    lines = [f"🧮 매수 plan 갱신 (기준 총자산 {base:,}원){low_txt}"]
    for r in cands:
        code = _fmt(r.get("code", ""))
        name = r.get("name", "")
        cscore = float(r.get("claude_score", 0) or 0)
        if not code:
            continue

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

        target_amt = int(base * alloc / 100)
        target_qty = target_amt // price if price > 0 else 0
        if target_qty < 1:
            ln = f"⏭️ {name}({code}) 비중목표({target_amt:,}원) 1주 미만 — plan 제외"
            lines.append(ln); notes[code] = ("small", ln)
            continue

        # 기존 보유종목: 확정 구간보다 높아질 때만 새 목표, 아니면 확정 목표수량 고정(구간 하향 시 비례 축소)
        tier, tier_from, tier_note = None, None, ""
        if existing:
            est = tiers.get(code)
            hq = held.get(code, 0)
            if not est and hq > 0:          # (Stock_V3 가격이 없어 위 ③에서 못 한 경우) 재평가 가격으로 초기화
                est = tiers[code] = {"alloc": _nearest_tier(hq * price / base * 100), "target_qty": hq,
                                     "date": today_str, "src": "bootstrap"}
            if est:
                tier_from = float(est["alloc"])
                short = target_qty - hq
                if alloc > tier_from and hq > 0 and short <= max(1, int(target_qty * KEEP_RESIDUAL)):
                    # (2026-10-29) 이미 상향 구간 목표를 채웠거나 잔여 단주(≤3%)뿐 → 지금 보유수량으로 그 구간 즉시 확정.
                    #    체결이 안 나 pend 로는 확정되지 않아 매 빌드 'up' 재계산 → 주가 하락·총자산 변동 때 단주 추가매수가 나던 문제.
                    target_qty = hq
                    if not tier_frozen:
                        tiers[code] = {"alloc": float(alloc), "target_qty": hq, "date": today_str, "src": "up_met"}
                    tier, tier_note = "up_met", f"·비중상향 {tier_from:.0f}→{alloc:.0f}% 기충족(보유 {hq}주 확정)"
                elif alloc > tier_from:
                    tier, tier_note = "up", f"·비중상향 {tier_from:.0f}→{alloc:.0f}%"
                else:
                    frozen = int(int(est["target_qty"]) * alloc / tier_from) if tier_from > 0 else 0
                    target_qty = min(target_qty, frozen)
                    short = target_qty - hq
                    if 0 < short <= max(1, int(target_qty * KEEP_RESIDUAL)):
                        target_qty = hq     # 확정 목표 대비 잔여 단주(≤3%) → 채운 것으로 간주(단주 추가매수 안 함)
                    tier, tier_note = "keep", f"·비중유지({tier_from:.0f}% 확정 {int(est['target_qty'])}주)"

        targets[code] = {
            "name": name,
            "promising_price": price,        # sweep 기준 지정가
            "target_qty": target_qty,        # 절대 목표수량(보유 포함)
            "alloc": round(alloc, 1),
            "score100": round(sc, 1),
            "claude_score": round(cscore, 1),
            "price_src": psrc,
            "min_score": min_sc,             # 집행 직전 재확인 임계(점) — 보조매수는 40
            "low": low_t,
            "new": not existing,             # 신규종목(장 시작 시 미보유) — 매수 우선 + 순위교체 매도 대상
            "tier": tier,                    # 기존 보유: up=비중구간 상향(새 목표) · keep=확정 목표수량 고정
            "tier_from": tier_from,          # 기존 보유 확정 구간(%)
        }
        lines.append(f"🎯 {name}({code}) 목표 {target_qty}주×{price:,}원 "
                     f"(비중 {alloc:.0f}%{'·40점보조' if low_t else ''}{'·신규' if not existing else '·보유'}"
                     f"{tier_note}·AI{cscore:.0f}·{psrc})")
        # ④ 신규·구간상향 목표는 '대기 목표(pend)'로 발행 — 이후 보유가 늘면 다음 빌드 ①에서 이 목표로 확정
        #    (2026-10-29) tier=None 인 기보유(= 오늘 전량 매도해 기억이 지워진 종목)도 발행 — 당일 재매수 체결이
        #    확정되지 않고 부분체결 수량으로 bootstrap 되어 나머지를 영영 안 채우던 문제
        if tier in (None, "up") and not tier_frozen:
            pend[code] = {"alloc": round(alloc, 1), "target_qty": target_qty,
                          "held_at": held.get(code, 0), "date": today_str}

    if tier_frozen:
        print("   ⚠️ 잔고 0종목인데 계좌 합계가 남아 있거나 조회 실패(일시 오류 의심) — 보유 구간 기억(tier_state) 갱신 생략")
    else:
        _save_json(_tier_path(), {"codes": tiers, "pend": pend,
                                  "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

    # 매수 순서: 신규종목(AI점수순) → 기존 보유종목(AI점수순). monitor 는 이 순서로 현금을 예약한다.
    targets = {**{c: t for c, t in targets.items() if t["new"]},
               **{c: t for c, t in targets.items() if not t["new"]}}

    plan = {
        "date": today_str,
        "base": int(base),
        "low_mode": low,
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
        return {c: t.get("alloc") for c, t in tmap.items()}

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
    out = [f"🧮 매수 plan 갱신 (기준 총자산 {base:,}원){low_txt}"]
    need_total = 0
    shortage = False
    for code, t in targets.items():
        hq = held.get(code, 0)
        diff = int(t["target_qty"]) - hq
        if diff > 0:
            shortage = True
            amt = diff * int(t["promising_price"])
            need_total += amt
            up = (f"·비중상향 {t['tier_from']:.0f}→{t['alloc']:.0f}%" if t.get("tier") == "up"
                  else "·확정수량까지 채움" if t.get("tier") == "keep" else "")
            out.append(f"🎯 {t['name']}({code}) 목표 {t['target_qty']}주 / 보유 {hq}주 "
                       f"→ 부족 {diff}주 ≈ {amt:,}원 "
                       f"(기준가 {t['promising_price']:,}원·비중 {t['alloc']:.0f}%·{'신규' if t['new'] else '보유'}"
                       f"{up}·AI{t['claude_score']:.0f})")
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
    out += [ln for ln in lines[1:] if ln.startswith(("⏭️", "⏳", "⏸️"))]   # 제외·재평가대기·40점보유 사유 그대로 표기
    _send_owner("\n".join(out))


def run(today_str=None):
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
        _build_plan(today_str)     # 목표수량·기준가 계산 → plan 파일 (집행은 monitor)
    finally:
        _plan_unlock(lock)


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else None
    run(d)
