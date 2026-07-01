import os
import sys
import time
import pandas as pd
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from kis_api import auth, inquiry, common
from config import secrets

# ====== 설정 ======
DATA_DIRS = [
    r"C:\Projects\RealtimeMonitor\Data\Stock",
    r"C:\Projects\RealtimeMonitor\Data\ETF"
]
START_DATE = "20200101"  # 복구 시작일


def fetch_market_index_history(market_code, start_date, end_date):
    """ 지수 데이터 수집 (2개월 단위 분할) """
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {auth.get_access_token()}",
        "appkey": secrets.APP_KEY, "appsecret": secrets.APP_SECRET,
        "tr_id": "FHKUP03500100", "custtype": "P"
    }

    start_dt = datetime.strptime(start_date, "%Y%m%d")
    end_dt = datetime.strptime(end_date, "%Y%m%d")

    all_rows = []
    curr = start_dt

    while curr <= end_dt:
        next_step = curr + relativedelta(months=2)
        if next_step > end_dt: next_step = end_dt

        params = {
            "FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": market_code,
            "FID_INPUT_DATE_1": curr.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": next_step.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D"
        }
        res = common.call_api(url, params, headers)
        time.sleep(0.1)

        if res and "output2" in res:
            for item in res['output2']:
                if not item['stck_bsop_date']: continue
                all_rows.append({
                    "date": item['stck_bsop_date'],
                    "close": float(item['bstp_nmix_prpr'])
                })
        curr = next_step + timedelta(days=1)

    if not all_rows: return pd.DataFrame()
    df = pd.DataFrame(all_rows)
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').drop_duplicates('date').reset_index(drop=True)

    col_name = 'kospi_change' if market_code == '0001' else 'kosdaq_change'
    df[col_name] = df['close'].pct_change().fillna(0)
    return df[['date', col_name]]


def run_fix_tool():
    if not auth.get_access_token():
        print("❌ 토큰 발급 실패")
        return

    print("\n" + "=" * 60)
    print("🚑 [Fix Market Index] 지수 데이터 긴급 수혈 도구")
    print("   - 모든 CSV 파일의 kospi/kosdaq_change 컬럼을 복구합니다.")
    print("=" * 60)

    end_date = datetime.now().strftime("%Y%m%d")

    # 1. 지수 데이터 수집 (한 번만 실행)
    print(f"📊 지수 데이터 수집 중 ({START_DATE} ~ {end_date})...")
    df_kospi = fetch_market_index_history("0001", START_DATE, end_date)
    df_kosdaq = fetch_market_index_history("1001", START_DATE, end_date)
    print("   ✅ 지수 데이터 준비 완료.")

    # 2. 파일 목록 수집
    all_files = []
    for d_dir in DATA_DIRS:
        if os.path.exists(d_dir):
            files = [os.path.join(d_dir, f) for f in os.listdir(d_dir) if f.startswith("A") and f.endswith(".csv")]
            all_files.extend(files)

    total = len(all_files)
    print(f"\n📂 총 대상 파일: {total}개 (수정 시작)")

    cnt = 0
    for idx, file_path in enumerate(all_files, 1):
        try:
            df = pd.read_csv(file_path, encoding='utf-8-sig')
            df['date'] = pd.to_datetime(df['date'])

            # 기존 지수 컬럼 삭제 (잘못된 데이터 제거)
            if 'kospi_change' in df.columns: df.drop(columns=['kospi_change'], inplace=True)
            if 'kosdaq_change' in df.columns: df.drop(columns=['kosdaq_change'], inplace=True)

            # 지수 데이터 병합 (Merge)
            df = pd.merge(df, df_kospi, on='date', how='left')
            df = pd.merge(df, df_kosdaq, on='date', how='left')

            # NaN 채우기 (휴장일 등)
            df['kospi_change'] = df['kospi_change'].fillna(0)
            df['kosdaq_change'] = df['kosdaq_change'].fillna(0)

            # 원래 컬럼 순서 유지 로직 (선택사항, 보통은 맨 뒤에 붙어도 무관하지만 깔끔하게)
            # 원자적 저장 (temp + os.replace) — 동시 쓰기로 인한 줄바꿈 유실/파일 손상 방지
            _tmp_path = file_path + ".tmp"
            df.to_csv(_tmp_path, index=False, encoding='utf-8-sig')
            os.replace(_tmp_path, file_path)
            cnt += 1

        except Exception as e:
            print(f"❌ 오류: {os.path.basename(file_path)} - {e}")

        # 진행률 표시
        if idx % 100 == 0 or idx == total:
            progress = (idx / total) * 100
            sys.stdout.write(f"\r🚀 진행률: {progress:.1f}% ({idx}/{total})")
            sys.stdout.flush()

    print(f"\n\n✅ 완료! 총 {cnt}개 파일이 정상화되었습니다.")


if __name__ == "__main__":
    run_fix_tool()