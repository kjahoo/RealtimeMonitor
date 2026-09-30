# kis_api/kiwoom_inquiry.py
# 키움 REST 기반 시세/지수/프로그램매매 조회 어댑터.
# Update_Promising_Stocks.py 가 쓰던 한투(KIS) inquiry 함수와 동일한 키 형태로 반환해
# 호출부 변경을 최소화한다. (한투 rate-limit 부담을 키움으로 분리 + 수신 속도 개선 목적)
#   현재가/OHLCV/시총 : ka10001 (/api/dostk/stkinfo)
#   지수 등락률        : ka20001 (/api/dostk/sect)
#   종목 프로그램매매  : ka90013 (/api/dostk/mrkcond)
import os
import time
import sys
import requests

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kis_api import kiwoom_auth

KIWOOM_URL_BASE = "https://api.kiwoom.com"


# ── 숫자 파싱 (키움 값은 "+130000" / "-2800" 처럼 부호 접두 포함) ───────────
def _num(v):
    """부호 포함 숫자 문자열 → int (부호 유지).
    키움은 일부 필드(ka90013 prm_netprps_qty 등)에서 음수를 "--3513348" 처럼 이중 부호로 준다
    → 앞쪽 부호를 모두 걷어내고 '-' 가 있으면 음수 (2026-09-14: 기존엔 int 변환 실패로 0 처리됐음)."""
    try:
        s = str(v).replace(",", "").strip()
        body = s.lstrip("+-")
        n = int(body.split(".")[0])
        return -n if "-" in s[:len(s) - len(body)] else n
    except (ValueError, TypeError):
        return 0


def _abs(v):
    """절대값 int — 가격/거래량/시총처럼 부호가 등락방향일 뿐인 값용"""
    return abs(_num(v))


def safe_int(val):
    """호출부 호환용 (kis_api.inquiry.safe_int 과 동일 동작)"""
    try:
        return int(str(val).replace(",", "").split(".")[0])
    except (ValueError, TypeError):
        return 0


def safe_float(val):
    try:
        return float(str(val).replace(",", "").replace("+", ""))
    except (ValueError, TypeError):
        return 0.0


def _market_suffix():
    """MARKET_MODE=NXT 시 키움 종목코드에 '_NX' 접미 (scheduler.py가 주입)"""
    mode = os.environ.get("MARKET_MODE", "KRX").upper()
    return "_NX" if mode == "NXT" else ""


def _post(api_id, url_path, body):
    for attempt in range(2):
        token = kiwoom_auth.get_access_token()
        if not token:
            return None
        headers = {
            "api-id":        api_id,
            "authorization": "Bearer " + token,
            "content-type":  "application/json;charset=UTF-8",
        }
        try:
            res = requests.post(KIWOOM_URL_BASE + url_path, headers=headers, json=body, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if data.get("return_code") == 0:
                    return data
                if data.get("return_code") == 3 and attempt == 0:
                    # 8005(Token 무효) — 동시 재발급 경합으로 폐기된 토큰이 파일에
                    # 남은 경우(2026-08-31). 토큰 복구 후 1회 재시도.
                    print(f"   🔄 [{api_id}] 인증실패(8005) → 토큰 복구 후 재시도")
                    kiwoom_auth.refresh_after_auth_fail()
                    continue
            return None
        except Exception:
            return None
    return None


# ====================================================
# 1. 주식 현재가 시세 (ka10001) — KIS inquire-price 호환 반환
#    반환 키: stck_prpr, stck_oprc, stck_hgpr, stck_lwpr, stck_sdpr, acml_vol, hts_avls
# ====================================================
def fetch_realtime_price(code):
    stk_cd = code + _market_suffix()
    data = _post("ka10001", "/api/dostk/stkinfo", {"stk_cd": stk_cd})
    if not data:
        return {}
    return {
        "stck_prpr": str(_abs(data.get("cur_prc"))),    # 현재가
        "stck_oprc": str(_abs(data.get("open_pric"))),  # 시가
        "stck_hgpr": str(_abs(data.get("high_pric"))),  # 고가
        "stck_lwpr": str(_abs(data.get("low_pric"))),   # 저가
        "stck_sdpr": str(_abs(data.get("base_pric"))),  # 기준가(전일종가) — change_pct 산출용
        "acml_vol":  str(_abs(data.get("trde_qty"))),   # 누적거래량
        "hts_avls":  str(_abs(data.get("mac"))),        # 시가총액(억원)
    }


def fetch_sor_price(code):
    """통합(SOR: KRX+NXT) 현재가 int — 두 시장 중 마지막 체결가. 실패 시 0.
    장후 알림 표시·손절 판정용. 점수 계산에는 쓰지 않는다: 통합 거래량은 KRX+NXT 합계,
    시가는 NXT 08시 시가라 KRX 일봉으로 학습한 V3 피처(거래량비율·등락률)가 왜곡된다(2026-09-14 확인)."""
    data = _post("ka10001", "/api/dostk/stkinfo", {"stk_cd": code + "_AL"})
    return _abs(data.get("cur_prc")) if data else 0


# ====================================================
# 2. 업종(KOSPI/KOSDAQ) 등락률 (ka20001) — KIS와 동일하게 비율(소수) 반환
#    iscd: "0001"=KOSPI, "1001"=KOSDAQ (KIS 코드 유지)
# ====================================================
_INDEX_MAP = {
    "0001": ("0", "001"),   # 코스피 종합
    "1001": ("1", "101"),   # 코스닥 종합
}


def fetch_index_change(iscd):
    mp = _INDEX_MAP.get(iscd)
    if not mp:
        return 0.0
    mrkt_tp, inds_cd = mp
    data = _post("ka20001", "/api/dostk/sect", {"mrkt_tp": mrkt_tp, "inds_cd": inds_cd})
    if not data:
        return 0.0
    return safe_float(data.get("flu_rt", 0)) / 100.0


# ====================================================
# 3. 종목별 당일 프로그램 매매 (ka90013) — KIS program-trade 호환 반환
#    반환 키: whol_smtn_ntby_qty(순매수수량,부호유지),
#             acml_vol(거래량), whol_smtn_shnu_vol(매수), whol_smtn_seln_vol(매도)
#    당일 행이 없으면 None (KIS 동작과 동일 → 호출부에서 0 처리)
# ====================================================
def fetch_program_today(code, date_str):
    data = _post("ka90013", "/api/dostk/mrkcond",
                 {"amt_qty_tp": "2", "stk_cd": code, "date": date_str})
    if not data:
        return None
    rows = data.get("stk_daly_prm_trde_trnsn") or []
    row = next((r for r in rows if r.get("dt") == date_str), None)
    if row is None:
        return None
    return {
        "whol_smtn_ntby_qty": str(_num(row.get("prm_netprps_qty"))),  # 순매수수량(부호 유지)
        "acml_vol":           str(_abs(row.get("trde_qty"))),         # 거래량
        "whol_smtn_shnu_vol": str(_abs(row.get("prm_buy_qty"))),      # 프로그램 매수수량
        "whol_smtn_seln_vol": str(_abs(row.get("prm_sell_qty"))),     # 프로그램 매도수량
    }


# ====================================================
# 3-1. 정규장(09:00~15:30) 전용 일간 집계 — Update_Data_All 용 (2026-09-14 애프터마켓 신설 대응)
#    한투 일봉(FHKST03010100)·일별 프로그램매매(FHPPG04650201)는 당일 값에 애프터마켓 체결이 섞이므로
#    키움 1분봉(ka10080)과 시간별 프로그램매매 누적(ka90008)에서 정규장 구간만 잘라낸다.
#    ※ 15:35~ 장후 체결(시간외 종가·애프터마켓) 제외. 15:30 종가 단일가 체결은 포함 (2026-09-14 분봉·시간별 행으로 경계 확인).
# ====================================================
REGULAR_START_HHMM = "0900"
REGULAR_END_HHMMSS = "153459"     # 15:30 종가 단일가 체결(15:30:0x~) 포함 · 15:35~ 장후 체결 제외


def _post_page(api_id, url_path, body, next_key=""):
    """페이지 조회 + 429(초당 5회 유량) 대기 재시도. 반환 (data, next_key or '') / 실패 (None, '')."""
    for attempt in range(4):
        token = kiwoom_auth.get_access_token()
        if not token:
            return None, ""
        headers = {"api-id": api_id, "authorization": "Bearer " + token,
                   "content-type": "application/json;charset=UTF-8"}
        if next_key:
            headers.update({"cont-yn": "Y", "next-key": next_key})
        try:
            res = requests.post(KIWOOM_URL_BASE + url_path, headers=headers, json=body, timeout=10)
        except Exception:
            time.sleep(0.5)
            continue
        if res.status_code == 429:
            time.sleep(0.4 * (attempt + 1))
            continue
        if res.status_code != 200:
            return None, ""
        data = res.json()
        if data.get("return_code") == 3 and attempt == 0:
            kiwoom_auth.refresh_after_auth_fail()
            continue
        if data.get("return_code") != 0:
            return None, ""
        nk = res.headers.get("next-key", "") if res.headers.get("cont-yn") == "Y" else ""
        return data, nk
    return None, ""


def fetch_regular_session_ohlcv(code, date_str, max_pages=3):
    """date_str(YYYYMMDD) 정규장 1분봉 집계 {open, high, low, close, volume}. 봉이 없거나 실패 시 None.
    ka10080 은 최신봉부터 900개/페이지 — 해당일 09:00 봉에 닿을 때까지 페이지를 넘긴다."""
    bars, nk = [], ""
    for _ in range(max_pages):
        data, nk = _post_page("ka10080", "/api/dostk/chart",
                              {"stk_cd": code, "tic_scope": "1", "upd_stkpc_tp": "1"}, nk)
        if data is None:
            return None
        page = data.get("stk_min_pole_chart_qry") or []
        bars += page
        oldest = min((str(b.get("cntr_tm", "")) for b in page), default="")
        if not nk or not page or oldest < date_str + REGULAR_START_HHMM:
            break
    reg = sorted((b for b in bars
                  if str(b.get("cntr_tm", "")).startswith(date_str)
                  and REGULAR_START_HHMM <= str(b["cntr_tm"])[8:14] <= REGULAR_END_HHMMSS),
                 key=lambda b: str(b["cntr_tm"]))
    if not reg:
        return None
    return {
        "open":   _abs(reg[0].get("open_pric")),
        "high":   max(_abs(b.get("high_pric")) for b in reg),
        "low":    min(_abs(b.get("low_pric")) for b in reg),
        "close":  _abs(reg[-1].get("cur_prc")),
        "volume": sum(_abs(b.get("trde_qty")) for b in reg),
    }


def fetch_regular_session_program(code, date_str, max_pages=15):
    """date_str 정규장 마감 시점 프로그램매매 누적 {prog_net_qty, prog_buy, prog_sell}. 실패 시 None.
    ka90008 은 시각 역순 누적값 — REGULAR_END_HHMMSS(15:34:59) 이하 첫 행이 정규장 최종치. 해당 행이 없고
    목록이 끝났으면 정규장 프로그램 체결 없음(0)."""
    nk = ""
    for _ in range(max_pages):
        data, nk = _post_page("ka90008", "/api/dostk/mrkcond",
                              {"amt_qty_tp": "2", "stk_cd": code, "date": date_str}, nk)
        if data is None:
            return None
        for r in data.get("stk_tm_prm_trde_trnsn") or []:
            if str(r.get("tm", "")) <= REGULAR_END_HHMMSS:
                return {"prog_net_qty": _num(r.get("prm_netprps_qty")),
                        "prog_buy":     _abs(r.get("prm_buy_qty")),
                        "prog_sell":    _abs(r.get("prm_sell_qty"))}
        if not nk:
            return {"prog_net_qty": 0, "prog_buy": 0, "prog_sell": 0}
    return None


# ====================================================
# 4. 종목정보 리스트 (ka10099) — 시장별 전체 상장종목 열거
#    mrkt_tp: "0"=코스피, "10"=코스닥 (요청값과 무관하게 응답의 marketName이 실제 유형)
#    반환: [{code, name, listCount, auditInfo, regDay, lastPrice, state,
#            marketCode, marketName, upName, upSizeName, companyClassName,
#            orderWarning, nxtEnable, kind}, ...]  (cont-yn 페이지네이션 자동 처리)
# ====================================================
def fetch_stock_list(mrkt_tp):
    token = kiwoom_auth.get_access_token()
    if not token:
        return []
    out, cont_yn, next_key = [], "N", ""
    while True:
        headers = {
            "api-id":        "ka10099",
            "authorization": "Bearer " + token,
            "content-type":  "application/json;charset=UTF-8",
            "cont-yn":       cont_yn,
            "next-key":      next_key,
        }
        try:
            res = requests.post(KIWOOM_URL_BASE + "/api/dostk/stkinfo",
                                headers=headers, json={"mrkt_tp": str(mrkt_tp)}, timeout=10)
        except Exception:
            break
        if res.status_code != 200:
            break
        data = res.json()
        if data.get("return_code") != 0:
            break
        out.extend(data.get("list", []) or [])
        # 다음 페이지 존재 시 응답 헤더로 이어받기
        if res.headers.get("cont-yn") == "Y" and res.headers.get("next-key"):
            cont_yn, next_key = "Y", res.headers.get("next-key")
            continue
        break
    return out
