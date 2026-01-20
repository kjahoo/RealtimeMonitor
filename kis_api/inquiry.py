import requests
import time
import sys
import os

# 상위 경로 설정 (config, kis_api 접근용)
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import secrets
from kis_api import auth


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


# 공통 API 호출 함수 (토큰 자동 갱신 기능 포함)
def call_api(url, params, headers=None, retry=True):
    if headers is None:
        headers = {}

    # 토큰 가져오기 (auth.py 사용)
    token = auth.get_access_token()
    if not token:
        return None

    headers["authorization"] = f"Bearer {token}"
    headers["appkey"] = secrets.APP_KEY
    headers["appsecret"] = secrets.APP_SECRET

    try:
        res = requests.get(url, headers=headers, params=params, timeout=5)

        # 토큰 만료 시 재시도 로직
        if res.status_code == 401 or (res.status_code == 200 and "EGW00123" in res.text):
            print("⚠️ [API] 토큰 만료 감지. 재발급 후 재시도합니다.")
            auth.get_access_token()  # 내부적으로 파일 삭제 후 재발급 유도 로직 필요 (또는 강제 갱신)
            # 여기서는 간단히 재귀 호출 전 쿨타임
            time.sleep(1)
            if retry:
                return call_api(url, params, headers, retry=False)

        if res.status_code == 200:
            data = res.json()
            if data.get("rt_cd") == "0":
                return data

        # 서버 과부하 등으로 인한 실패 시 잠시 대기 후 1회 재시도
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


# 1. 주식 현재가 시세 조회
def fetch_realtime_price(code):
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {"tr_id": "FHKST01010100", "custtype": "P"}
    params = {
        "fid_cond_mrkt_div_code": "J",
        "fid_input_iscd": code
    }
    data = call_api(url, params, headers)
    return data.get("output", {}) if data else {}


# 2. 프로그램 매매 동향 조회
def fetch_program_today(code, date_str):
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/program-trade-by-stock-daily"
    headers = {"tr_id": "FHPPG04650201", "custtype": "P"}
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": date_str
    }
    data = call_api(url, params, headers)
    if data and data.get("output"):
        for item in data["output"]:
            if item["stck_bsop_date"] == date_str:
                return item
    return None


# 3. 업종(KOSPI, KOSDAQ) 지수 조회
def fetch_index_change(iscd):
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-index-price"
    headers = {"tr_id": "FHPUP02100000", "custtype": "P"}
    params = {
        "fid_cond_mrkt_div_code": "U",
        "fid_input_iscd": iscd
    }
    data = call_api(url, params, headers)
    if data and "output" in data:
        # bstp_nmix_prdy_ctrt: 전일 대비율
        return safe_float(data["output"].get("bstp_nmix_prdy_ctrt", 0)) / 100.0
    return 0.0


# 4. 시가총액 조회 (캐싱 적용 권장)
def fetch_market_cap(code):
    res = fetch_realtime_price(code)
    if res:
        return safe_int(res.get("hts_avls", "0"))  # 시가총액(억)
    return 0


# 5. 주식/ETF 구분 조회 (정밀 판별 버전)
def fetch_stock_kind(code):
    """
    종목 코드를 입력받아 ETF/ETN 여부를 반환합니다.
    (3중 체크로 정확도 향상)
    """
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

        # 1. [기본] 종류 코드로 확인
        if kind_code == "109":
            return "ETF"
        elif kind_code == "110":
            return "ETN"

        # 2. [추가] ETF 전용 필드 값 확인 (코드가 주식으로 잡혀도 내용물이 ETF인 경우)
        # 'etf_type_cd'(ETF유형코드)에 값이 있으면 ETF입니다.
        if output.get("etf_type_cd"):
            return "ETF"

        # 3. [보완] 이름에 명확한 브랜드가 포함된 경우 (RISE 등 최신 브랜드 반영)
        name = output.get("prdt_name", "").upper().strip()
        etf_brands = [
            "KODEX", "TIGER", "KBSTAR", "RISE", "ACE", "SOL",
            "ARIRANG", "HANARO", "KOSEF", "TIMEFOLIO", "KOACT",
            "UNICORN", "HERO", "FOCUS", "MASTER", "WOORI"
        ]

        # 이름이 브랜드로 시작하거나 포함되면 ETF로 간주
        for brand in etf_brands:
            if brand in name:
                return "ETF"

        # ETN 브랜드 확인
        if "ETN" in name:
            return "ETN"

    return "STOCK"  # 위 3가지를 다 통과하면 진짜 일반 주식


# 6. 종목명 조회 (이름이 없을 때 사용)
def fetch_stock_name(code):
    """
    종목 코드로 한글 종목명을 조회합니다.
    """
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/search-stock-info"
    headers = {"tr_id": "CTPF1002R", "custtype": "P"}
    params = {
        "PRDT_TYPE_CD": "300",
        "PDNO": code
    }

    res = call_api(url, params, headers)
    if res and "output" in res:
        # prdt_abrv_name: 주식단축명, prdt_name: 주식명
        return res["output"].get("prdt_abrv_name") or res["output"].get("prdt_name", "")
    return ""