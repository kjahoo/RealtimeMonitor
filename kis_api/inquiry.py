import requests
import time
import sys
import os

# 상위 경로 설정 (config, kis_api 접근용)
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import secrets
from kis_api import auth

# ====================================================
# 🏪 시장 모드 (scheduler.py가 환경변수로 주입)
# "KRX" → 정규장 (09:00~15:30)
# "NXT" → 프리/애프터마켓 (08:00~09:00, 15:30~20:00)
# ====================================================
def get_market_div_code():
    """
    현재 MARKET_MODE 환경변수를 읽어 시장 구분 코드를 반환합니다.
    scheduler.py가 봇 실행 시 자동으로 주입합니다.
    수동 실행 시에는 기본값 "J" (KRX) 를 사용합니다.
    """
    mode = os.environ.get("MARKET_MODE", "KRX").upper()
    return "NX" if mode == "NXT" else "J"


def safe_int(val):
    try:
        return int(str(val).replace(",", "").split(".")[0])
    except:
        return 0


def safe_float(val):
    try:
        return float(str(val).replace(",", ""))
    except:
        return 0.0


# ====================================================
# 공통 API 호출 함수 (토큰 자동 갱신 기능 포함)
# ====================================================
def call_api(url, params, headers=None, retry=True):
    if headers is None:
        headers = {}

    token = auth.get_access_token()
    if not token:
        return None

    headers["authorization"] = f"Bearer {token}"
    headers["appkey"] = secrets.APP_KEY
    headers["appsecret"] = secrets.APP_SECRET

    try:
        res = requests.get(url, headers=headers, params=params, timeout=5)

        if res.status_code == 401 or (res.status_code == 200 and "EGW00123" in res.text):
            print("⚠️ [API] 토큰 만료 감지. 재발급 후 재시도합니다.")
            auth.get_access_token()
            time.sleep(1)
            if retry:
                return call_api(url, params, headers, retry=False)

        if res.status_code == 200:
            data = res.json()
            if data.get("rt_cd") == "0":
                return data

        if retry and res.status_code in (429, 500, 502, 503, 504):
            time.sleep(1.0)
            return call_api(url, params, headers, retry=False)

        return None
    except Exception as e:
        print(f"❌ API 호출 중 오류: {e}")
        if retry:
            time.sleep(1)
            return call_api(url, params, headers, retry=False)
        return None


# ====================================================
# 1. 주식 현재가 시세 조회 (KRX / NXT 자동 전환)
# ====================================================
def fetch_realtime_price(code):
    """
    현재 MARKET_MODE에 따라 KRX 또는 NXT 시세를 조회합니다.
    - KRX 모드: fid_cond_mrkt_div_code = "J"  (기존 방식)
    - NXT 모드: fid_cond_mrkt_div_code = "NX" (NXT 시장)
    반환 필드는 동일하므로 기존 코드 수정 불필요.
    """
    mkt_div = get_market_div_code()

    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {"tr_id": "FHKST01010100", "custtype": "P"}
    params = {
        "fid_cond_mrkt_div_code": mkt_div,
        "fid_input_iscd": code
    }
    data = call_api(url, params, headers)
    return data.get("output", {}) if data else {}


# ====================================================
# 2. 프로그램 매매 동향 조회
# ====================================================
def fetch_program_today(code, date_str):
    """
    프로그램 매매는 KRX 기준 데이터를 사용합니다.
    NXT 시간대에도 동일하게 호출 가능 (전일 or 당일 누적).
    """
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/program-trade-by-stock-daily"
    headers = {"tr_id": "FHPPG04650201", "custtype": "P"}
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",   # 프로그램 매매는 항상 KRX 기준
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": date_str
    }
    data = call_api(url, params, headers)
    if data and data.get("output"):
        for item in data["output"]:
            if item["stck_bsop_date"] == date_str:
                return item
    return None


# ====================================================
# 3. 업종(KOSPI, KOSDAQ) 지수 조회
# ====================================================
def fetch_index_change(iscd):
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-index-price"
    headers = {"tr_id": "FHPUP02100000", "custtype": "P"}
    params = {
        "fid_cond_mrkt_div_code": "U",
        "fid_input_iscd": iscd
    }
    data = call_api(url, params, headers)
    if data and "output" in data:
        return safe_float(data["output"].get("bstp_nmix_prdy_ctrt", 0)) / 100.0
    return 0.0


# ====================================================
# 4. 시가총액 조회
# ====================================================
def fetch_market_cap(code):
    res = fetch_realtime_price(code)
    if res:
        return safe_int(res.get("hts_avls", "0"))
    return 0


# ====================================================
# 5. 주식/ETF 구분 조회
# ====================================================
def fetch_stock_kind(code):
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/search-stock-info"
    headers = {"tr_id": "CTPF1002R", "custtype": "P"}
    params = {
        "PRDT_TYPE_CD": "300",
        "PDNO": code
    }

    res = call_api(url, params, headers)

    if res and "output" in res:
        output = res["output"]
        kind_code = output.get("stck_kind_cd")

        if kind_code == "109":
            return "ETF"
        elif kind_code == "110":
            return "ETN"

        if output.get("etf_type_cd"):
            return "ETF"

        name = output.get("prdt_name", "").upper().strip()
        etf_brands = [
            "KODEX", "TIGER", "KBSTAR", "RISE", "ACE", "SOL",
            "ARIRANG", "HANARO", "KOSEF", "TIMEFOLIO", "KOACT",
            "UNICORN", "HERO", "FOCUS", "MASTER", "WOORI"
        ]
        for brand in etf_brands:
            if brand in name:
                return "ETF"

        if "ETN" in name:
            return "ETN"

    return "STOCK"


# ====================================================
# 6. 종목명 조회
# ====================================================
def fetch_stock_name(code):
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/search-stock-info"
    headers = {"tr_id": "CTPF1002R", "custtype": "P"}
    params = {
        "PRDT_TYPE_CD": "300",
        "PDNO": code
    }

    res = call_api(url, params, headers)
    if res and "output" in res:
        return res["output"].get("prdt_abrv_name") or res["output"].get("prdt_name", "")
    return ""


# ====================================================
# 🛠️ [디버그용] 현재 시장 모드 확인
# ====================================================
if __name__ == "__main__":
    mode = os.environ.get("MARKET_MODE", "KRX")
    div  = get_market_div_code()
    print(f"현재 MARKET_MODE: {mode} → API 시장코드: {div}")