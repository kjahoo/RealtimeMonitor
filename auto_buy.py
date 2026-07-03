# -*- coding: utf-8 -*-
"""
auto_buy.py — AI '매수'(BUY) & 실시간 50점+(SCORE_MIN) 종목 매수 '계획(plan)' 산출 (실거래 집행은 분리)
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
  - 가격·점수 소스: ① promising(logs/{날짜}_Search_History.csv) 실시간 current_price·total_score
                    ② Stock_V3 최신 close_price·score_total (폴백)
                    ③ 둘 다 없으면 평가시점 점수로는 매수 안 함 → plan 제외
  - 실시간 total_score×100 < 50(SCORE_MIN) → plan 제외
  - 비중(구간제, 진입 50점): 10점 구간마다 5%p — 50~59=5% · 60~69=10% · 70~79=15% · 80~89=20% · 90~99=25% · 100=30%
  - 기준금액: 당일 총자산(추정예탁자산) 스냅샷 (당일 고정)
  - 종목당 목표수량(절대치) = (기준금액 × alloc%) ÷ 기준가(promising 지정가)
  - 목표 1주 미만(초고가주)인 종목은 plan 제외
  - 30분 세션마다 plan 전체를 새 promising 가격으로 재계산(목표수량·기준가 갱신)

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

LOG_DIR = secrets.LOCAL_DATA_PATH
OWNER_ID = str(secrets.TELEGRAM_CHAT_ID)

ALLOC_MAX = 30.0          # 비중 상한 % (2안 확대: 100점 30%)
SCORE_MIN = 50.0          # Total score 최소(점) (2안 확대: 진입 50점)


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
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        print(f"   ⚠️ 저장 실패({path}): {e}")


def _results_path(d):       return _p(f"{d}_claude_results.json")
def _base_path(d):          return _p(f"{d}_autobuy_base.json")
def _plan_path(d):          return _p(f"{d}_autobuy_plan.json")
def _search_history_path(d): return _p(f"{d}_Search_History.csv")


def _load_promising_latest(today_str):
    """오늘자 Search_History.csv 에서 코드별 '최신' 행을 모아 promising 실시간값 반환.
       {code(6자리): {"price": int, "score100": float, "name": str, "ts": str}}
       - current_price : 실시간 지정가 산출에 사용
       - total_score×100 : 실시간 비중(alloc) 및 50점 컷(SCORE_MIN)에 사용
    """
    import csv
    path = _search_history_path(today_str)
    latest = {}
    if not os.path.exists(path):
        return latest
    try:
        with open(path, encoding="utf-8-sig") as f:   # BOM 제거 → timestamp 헤더 정상 인식
            for row in csv.DictReader(f):
                code = _fmt(row.get("code", ""))
                ts = (row.get("timestamp") or "").strip()
                if not code:
                    continue
                cur = latest.get(code)
                if cur is None or ts >= cur["ts"]:
                    try:
                        price = int(float(row.get("current_price", 0) or 0))
                    except Exception:
                        price = 0
                    try:
                        score100 = float(row.get("total_score", 0) or 0) * 100.0
                    except Exception:
                        score100 = 0.0
                    latest[code] = {"price": price, "score100": score100,
                                    "name": (row.get("name") or "").strip(), "ts": ts}
    except Exception as e:
        print(f"   ⚠️ Search_History 로드 실패: {e}")
    return latest


def _load_v3_latest(today_str):
    """오늘자 Stock_V3.csv 에서 코드별 '최신' score_total·close_price 반환.
       {code(6자리): {"price": int, "score100": float, "name": str}}
       Stock_V3 는 전 종목을 매 스캔 사이클마다 갱신하므로 장중 점수 변화의
       권위 소스다. 매수 직전 50점 컷(SCORE_MIN)·비중 산출은 이 값을 최우선으로 쓴다."""
    import csv
    path = _p(f"{today_str}_Stock_V3.csv")
    latest = {}
    if not os.path.exists(path):
        return latest
    try:
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code = _fmt(row.get("code", ""))
                if not code:
                    continue
                try:
                    price = int(float(row.get("close_price", 0) or 0))
                except Exception:
                    price = 0
                try:
                    score100 = float(row.get("score_total", 0) or 0) * 100.0
                except Exception:
                    score100 = 0.0
                latest[code] = {"price": price, "score100": score100,
                                "name": (row.get("name") or "").strip()}
    except Exception as e:
        print(f"   ⚠️ Stock_V3 로드 실패: {e}")
    return latest


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
    """구간제 매수 비중(%). 2안(확대): 진입 50점, 10점 구간마다 5%p, 상한 30.
       50~59=5 · 60~69=10 · 70~79=15 · 80~89=20 · 90~99=25 · 100=30. 50 미만은 별도 컷에서 제외.
       예) 55→5%, 67→10%, 89→20%. (main_stock 알림과 동일)"""
    bucket = int(float(total_score100) // 10)      # 50~59→5, 60~69→6 … 100→10
    return max(0.0, min(ALLOC_MAX, (bucket - 4) * 5.0))


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
    if not os.path.exists(rpath):
        print("ℹ️ 평가풀(results_all) 없음 — plan 생략")
        return

    base = _get_base(today_str)
    if not base:
        print("   ⚠️ 총자산 조회 실패 — plan 생략")
        return

    results = _load_json(rpath, [])
    # 당일 신규 평가(results.json)도 병합 — 같은 코드는 최신값 우선.
    fresh = _load_json(_results_path(today_str), [])
    if fresh:
        merged = {}
        for r in (results + fresh):     # fresh가 뒤 → 같은 코드면 최신값으로 갱신
            c = _fmt(r.get("code", ""))
            if c:
                merged[c] = r
        results = list(merged.values())

    v3_latest = _load_v3_latest(today_str)          # 폴백: 전 종목 최신 score_total·close_price
    promising = _load_promising_latest(today_str)   # 우선: Search_History 실시간값
    cands = [r for r in results
             if str(r.get("recommendation", "")).upper() == "BUY"]
    cands.sort(key=lambda r: float(r.get("claude_score", 0) or 0), reverse=True)

    targets = {}
    lines = [f"🧮 매수 plan 갱신 (기준 총자산 {base:,}원)"]
    for r in cands:
        code = _fmt(r.get("code", ""))
        name = r.get("name", "")
        cscore = float(r.get("claude_score", 0) or 0)

        # 가격·점수 소스 우선순위: ① promising ② Stock_V3 ③ 없으면 plan 제외(평가점수로 매수 금지)
        live = promising.get(code)
        v3l = v3_latest.get(code)
        if live and live.get("price", 0) > 0:
            price = int(live["price"]); sc = float(live["score100"]); psrc = "promising"
            if live.get("name"):
                name = live["name"]
        elif v3l and v3l.get("price", 0) > 0:
            price = int(v3l["price"]); sc = float(v3l["score100"]); psrc = "V3최신"
            if v3l.get("name"):
                name = v3l["name"]
        else:
            lines.append(f"⏭️ {name}({code}) 최신가/점수 없음 — plan 제외")
            continue
        if not code or price <= 0:
            continue

        if sc < SCORE_MIN:
            lines.append(f"⏭️ {name}({code}) 최신점수 {sc:.0f}<{SCORE_MIN:.0f} — plan 제외({psrc})")
            continue

        alloc = _alloc_pct(sc)
        target_amt = int(base * alloc / 100)
        target_qty = target_amt // price if price > 0 else 0
        if target_qty < 1:
            lines.append(f"⏭️ {name}({code}) 비중목표({target_amt:,}원) 1주 미만 — plan 제외")
            continue

        targets[code] = {
            "name": name,
            "promising_price": price,        # sweep 기준 지정가
            "target_qty": target_qty,        # 절대 목표수량(보유 포함)
            "alloc": round(alloc, 1),
            "score100": round(sc, 1),
            "claude_score": round(cscore, 1),
            "price_src": psrc,
        }
        lines.append(f"🎯 {name}({code}) 목표 {target_qty}주×{price:,}원 "
                     f"(비중 {alloc:.0f}%·AI{cscore:.0f}·{psrc})")

    plan = {
        "date": today_str,
        "base": int(base),
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # AI점수 상위 우선 선착순 집행을 위해 순서를 보존(dict는 삽입순)
        "targets": targets,
    }
    _save_json(_plan_path(today_str), plan)
    print(f"   📝 plan 저장: {len(targets)}종목 → {_plan_path(today_str)}")
    if len(lines) > 1:
        _send_owner("\n".join(lines))


def run(today_str=None):
    today_str = today_str or datetime.now().strftime("%Y%m%d")
    if not getattr(secrets, "AUTO_BUY_ENABLED", False):
        print("ℹ️ AUTO_BUY_ENABLED=False — 자동매수 비활성")
        return
    auth.get_access_token()        # 토큰 준비 (한투/공용)
    _get_base(today_str)           # 기준금액 스냅샷(장 시작 전이라도 확보)
    _build_plan(today_str)         # 목표수량·기준가 계산 → plan 파일 (집행은 monitor)


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else None
    run(d)
