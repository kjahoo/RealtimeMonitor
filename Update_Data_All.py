import os
import sys
import time
import pandas as pd
from datetime import datetime
from kis_api import auth, inquiry, indicators, common
from config import secrets

# ====== 설정 ======
DATA_DIRS = [
    r"C:\Projects\RealtimeMonitor\Data\Stock",
    r"C:\Projects\RealtimeMonitor\Data\ETF"
]


def fetch_chart_data(code, start_date, end_date):
    """
    특정 기간의 일봉 데이터를 API로 조회합니다.
    """
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {auth.get_access_token()}",
        "appkey": secrets.APP_KEY,
        "appsecret": secrets.APP_SECRET,
        "tr_id": "FHKST03010100",
        "custtype": "P"
    }

    # KIS API 날짜 포맷 (YYYYMMDD)
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": start_date,  # 시작일
        "FID_INPUT_DATE_2": end_date,  # 종료일
        "FID_PERIOD_DIV_CODE": "D",  # 일봉
        "FID_ORG_ADJ_PRC": "1"  # 수정주가 반영
    }

    res = common.call_api(url, params, headers)

    data_list = []
    if res and "output2" in res:
        for item in res['output2']:
            # 데이터가 비어있는 경우 건너뜀
            if not item['stck_bsop_date']: continue

            data_list.append({
                "date": pd.to_datetime(item['stck_bsop_date']),
                "open": int(item['stck_oprc']),
                "high": int(item['stck_hgpr']),
                "low": int(item['stck_lwpr']),
                "close": int(item['stck_clpr']),
                "volume": int(item['acml_vol']),
                # 거래대금 등은 필요하면 추가
            })

    # API는 최신순으로 들어오므로 날짜 오름차순 정렬
    if data_list:
        return pd.DataFrame(data_list).sort_values('date')
    return pd.DataFrame()


def update_all_files():
    # 1. 토큰 발급
    if not auth.get_access_token():
        print("❌ 토큰 발급 실패")
        return

    print("\n" + "=" * 50)
    print("🛠️ [데이터 일괄 업데이트 도구] Missed Data Recovery")
    print("=" * 50)

    # 2. 사용자 입력
    user_date = input("👉 시작 날짜를 입력하세요 (예: 20240101): ").strip()
    if len(user_date) != 8 or not user_date.isdigit():
        print("⚠️ 날짜 형식이 올바르지 않습니다. (YYYYMMDD)")
        return

    start_date_str = user_date
    end_date_str = datetime.now().strftime("%Y%m%d")

    print(f"\n📅 기간 설정: {start_date_str} ~ {end_date_str}")
    print("⚠️ 주의: 파일이 많을 경우 시간이 꽤 걸립니다.")
    print("⚠️ API 호출 제한을 피하기 위해 천천히 진행됩니다.\n")

    confirm = input("진행하시겠습니까? (y/n): ").strip().lower()
    if confirm != 'y':
        print("취소되었습니다.")
        return

    # 3. 전체 파일 목록 수집
    all_files = []
    for d_dir in DATA_DIRS:
        if os.path.exists(d_dir):
            files = [os.path.join(d_dir, f) for f in os.listdir(d_dir) if f.startswith("A") and f.endswith(".csv")]
            all_files.extend(files)

    total_files = len(all_files)
    print(f"\n📂 총 대상 파일: {total_files}개")

    success_cnt = 0
    fail_cnt = 0

    # 4. 순회 및 업데이트
    for idx, file_path in enumerate(all_files, 1):
        code = os.path.basename(file_path)[1:7]  # A005930.csv -> 005930

        try:
            # (1) 기존 파일 읽기
            df_old = pd.read_csv(file_path, encoding='utf-8-sig')
            df_old['date'] = pd.to_datetime(df_old['date'])

            # 종목명 백업
            name = df_old['name'].iloc[0] if 'name' in df_old.columns else ""

            # (2) API로 신규 데이터 요청 (기간 조회)
            df_new = fetch_chart_data(code, start_date_str, end_date_str)

            if df_new.empty:
                # 거래정지 등으로 데이터가 없을 수 있음
                # print(f"   ⚠️ 신규 데이터 없음: {name}({code})")
                pass
            else:
                # (3) 데이터 병합 (Merge)
                # 기존 데이터에서 해당 기간 이후의 데이터는 삭제하고(덮어쓰기 위해) 붙이기
                # 혹은 그냥 concat 후 drop_duplicates가 더 안전함

                # 병합을 위해 컬럼명 맞추기
                # df_new에는 name, change_pct 등의 컬럼이 없을 수 있으므로 채워줌
                df_new['code'] = code
                df_new['name'] = name

                # 기존 데이터 + 신규 데이터 합치기
                df_combined = pd.concat([df_old, df_new])

                # 날짜 기준 중복 제거 (최신 데이터 우선)
                # keep='last'를 쓰면 뒤에 붙인 새 데이터(df_new)가 남음 -> 업데이트 효과
                df_combined = df_combined.drop_duplicates(subset=['date'], keep='last')

                # 날짜순 정렬
                df_combined = df_combined.sort_values('date').reset_index(drop=True)

                # 등락률(change_pct) 재계산 (데이터가 중간에 끼어들었을 수 있으므로)
                df_combined['change_pct'] = df_combined['close'].pct_change().fillna(0)

                # (4) 보조지표 전면 재계산 (가장 중요!)
                # V3 모델용 지표들을 다시 계산해서 값들을 올바르게 맞춤
                df_final = indicators.calculate_indicators_v3_save(df_combined)

                # 결측치 처리 (지표 계산 초반부 등)
                df_final = df_final.fillna(0)

                # (5) 저장
                df_final.to_csv(file_path, index=False, encoding='utf-8-sig')
                success_cnt += 1

        except Exception as e:
            print(f"❌ 실패: {code} - {e}")
            fail_cnt += 1

        # 진행률 표시 (한 줄에 갱신)
        progress = (idx / total_files) * 100
        sys.stdout.write(f"\r🚀 진행률: [{idx}/{total_files}] {progress:.1f}% (성공: {success_cnt})")
        sys.stdout.flush()

        # API 호출 제한 준수 (너무 빠르면 차단됨)
        time.sleep(0.05)

    print("\n\n✅ 모든 작업이 완료되었습니다.")
    print(f"   - 성공: {success_cnt}개")
    print(f"   - 실패/건너뜀: {fail_cnt}개")


if __name__ == "__main__":
    update_all_files()