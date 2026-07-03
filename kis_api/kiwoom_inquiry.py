# kis_api/kiwoom_inquiry.py
# 키움 REST 기반 시세/지수/프로그램매매 조회 어댑터.
# Update_Promising_Stocks.py 가 쓰던 한투(KIS) inquiry 함수와 동일한 키 형태로 반환해
# 호출부 변경을 최소화한다. (한투 rate-limit 부담을 키움으로 분리 + 수신 속도 개선 목적)
#   현재가/OHLCV/시총 : ka10001 (/api/dostk/stkinfo)
#   지수 등락률        : ka20001 (/api/dostk/sect)
#   종목 프로그램매매  : ka90013 (/api/dostk/mrkcond)
import os
import sys
import requests

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kis_api import kiwoom_auth

KIWOOM_URL_BASE = "https://api.kiwoom.com"


# ── 숫자 파싱 (키움 값은 "+130000" / "-2800" 처럼 부호 접두 포함) ───────────
def _num(v):
    """부호 포함 숫자 문자열 → int (부호 유지)"""
    try:
        return int(str(v).replace(",", "").replace("+", "").split(".")[0])
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
        return None
    except Exception:
        return None


# ====================================================
# 1. 주식 현재가 시세 (ka10001) — KIS inquire-price 호환 반환
#    반환 키: stck_prpr, stck_oprc, stck_hgpr, stck_lwpr, acml_vol, hts_avls
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
        "acml_vol":  str(_abs(data.get("trde_qty"))),   # 누적거래량
        "hts_avls":  str(_abs(data.get("mac"))),        # 시가총액(억원)
    }


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
