import os
import sys
import time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from kis_api import auth, inquiry, indicators, common
from config import secrets

# ====== 설정 ======
DATA_DIRS = [
    r"C:\Projects\RealtimeMonitor\Data\Stock",
    r"C:\Projects\RealtimeMonitor\Data\ETF"
]

# 저장할 최종 컬럼 순서 (30개)
FINAL_COLUMNS = [
    'date', 'code', 'name', 'open', 'high', 'low', 'close', 'volume',
    'change_pct', 'kospi_change', 'kosdaq_change',
    'prog_net_qty', 'prog_ratio_vol',
    'ma5', 'ma20', 'ma60',
    'disparity_5', 'disparity_20', 'disparity_60',
    'volume_ratio', 'vol_power',
    'bb_w', 'bb_p', 'rsi', 'adx',
    'target1', 'target5', 'target20',
    'prog_net_ratio', 'bb_pos'
]


# =========================================================
# 🛠️ 데이터 수집 헬퍼 함수 (청킹 적용)
# =========================================================

def fetch_market_index_history(market_code, start_date, end_date):
    """ 지수 데이터 수집 (2개월 단위 분할) """
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {auth.get_access_token()}",
        "appkey": secrets.APP_KEY,
        "appsecret": secrets.APP_SECRET,
        "tr_id": "FHKUP03500100",
        "custtype": "P"
    }

    start_dt = datetime.strptime(start_date, "%Y%m%d")
    end_dt = datetime.strptime(end_date, "%Y%m%d")

    # 💡 [핵심 수정] 첫 날짜의 등락률(pct_change)을 구하기 위해, 수집 시작일을 15일 전으로 당깁니다.
    fetch_start_dt = start_dt - timedelta(days=15)

    all_rows = []
    curr = fetch_start_dt

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
                    "date": pd.to_datetime(item['stck_bsop_date']),
                    "close": float(item['bstp_nmix_prpr'])
                })
        curr = next_step + timedelta(days=1)

    if not all_rows: return pd.DataFrame()
    df = pd.DataFrame(all_rows)
    df = df.sort_values('date').drop_duplicates('date').reset_index(drop=True)

    col_name = 'kospi_change' if market_code == '0001' else 'kosdaq_change'

    # 여유 있게 가져온 과거 데이터가 있으므로, 시작 날짜의 등락률이 정상적으로 계산됩니다.
    df[col_name] = df['close'].pct_change().fillna(0)

    # 💡 [핵심 수정] 계산이 끝난 후, 사용자가 원래 요청한 'start_dt' 이후의 데이터만 필터링합니다.
    df = df[df['date'] >= start_dt].reset_index(drop=True)

    return df[['date', col_name]]


def fetch_program_history(code, start_date, end_date):
    """ 프로그램 매매 데이터 수집 (1개월 단위 분할) """
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/program-trade-by-stock-daily"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {auth.get_access_token()}",
        "appkey": secrets.APP_KEY,
        "appsecret": secrets.APP_SECRET,
        "tr_id": "FHPPG04650201",
        "custtype": "P"
    }

    start_dt = datetime.strptime(start_date, "%Y%m%d")
    end_dt = datetime.strptime(end_date, "%Y%m%d")

    all_rows = []
    curr = start_dt

    while curr <= end_dt:
        curr_end = curr + relativedelta(months=1)
        if curr_end > end_dt: curr_end = end_dt

        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                  "FID_INPUT_DATE_1": curr_end.strftime("%Y%m%d")}
        res = common.call_api(url, params, headers)
        time.sleep(0.05)

        if res and "output" in res:
            for item in res['output']:
                if not item['stck_bsop_date']: continue
                d_str = item['stck_bsop_date']
                if not (curr.strftime("%Y%m%d") <= d_str <= curr_end.strftime("%Y%m%d")): continue

                all_rows.append({
                    "date": pd.to_datetime(d_str),
                    "prog_net_qty": int(item['whol_smtn_ntby_qty']),
                    "prog_buy": int(item['whol_smtn_shnu_vol']),
                    "prog_sell": int(item['whol_smtn_seln_vol'])
                })
        curr = curr_end + timedelta(days=1)

    if not all_rows: return pd.DataFrame()
    df = pd.DataFrame(all_rows)
    return df.drop_duplicates('date')


def fetch_chart_data_chunked(code, start_date, end_date):
    """ 주가 데이터 수집 (1개월 단위 분할 요청) """
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {auth.get_access_token()}",
        "appkey": secrets.APP_KEY, "appsecret": secrets.APP_SECRET,
        "tr_id": "FHKST03010100", "custtype": "P"
    }

    start_dt = datetime.strptime(start_date, "%Y%m%d")
    end_dt = datetime.strptime(end_date, "%Y%m%d")

    all_rows = []
    curr = start_dt

    while curr <= end_dt:
        next_step = curr + relativedelta(months=1)
        if next_step > end_dt: next_step = end_dt

        req_start = curr.strftime("%Y%m%d")
        req_end = next_step.strftime("%Y%m%d")

        params = {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": req_start, "FID_INPUT_DATE_2": req_end,
            "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1"
        }

        res = common.call_api(url, params, headers)
        time.sleep(0.05)

        if res and "output2" in res:
            for item in res['output2']:
                if not item['stck_bsop_date']: continue
                all_rows.append({
                    "date": pd.to_datetime(item['stck_bsop_date']),
                    "open": int(item['stck_oprc']),
                    "high": int(item['stck_hgpr']),
                    "low": int(item['stck_lwpr']),
                    "close": int(item['stck_clpr']),
                    "volume": int(item['acml_vol'])
                })

        curr = next_step + timedelta(days=1)

    if all_rows:
        return pd.DataFrame(all_rows).sort_values('date').drop_duplicates('date').reset_index(drop=True)
    return pd.DataFrame()


# =========================================================
# 🚀 메인 업데이트 로직
# =========================================================
def update_all_files():
    if not auth.get_access_token():
        print("❌ 토큰 발급 실패")
        return

    print("\n" + "=" * 50)
    print("🛠️ [데이터 일괄 업데이트] 데이터 무결성 보존 모드 적용")
    print("=" * 50)

    user_date = input("👉 시작 날짜를 입력하세요 (예: 20260101): ").strip()
    if len(user_date) != 8 or not user_date.isdigit():
        print("⚠️ 날짜 형식이 올바르지 않습니다.")
        return

    start_date_str = user_date
    end_date_str = datetime.now().strftime("%Y%m%d")

    print(f"\n1️⃣ 시장 지수(KOSPI/KOSDAQ) 데이터를 수집합니다... ({start_date_str} ~ {end_date_str})")
    df_kospi = fetch_market_index_history("0001", start_date_str, end_date_str)
    df_kosdaq = fetch_market_index_history("1001", start_date_str, end_date_str)

    # 지수 병합을 위한 인덱스 세팅
    if not df_kospi.empty: df_kospi.set_index('date', inplace=True)
    if not df_kosdaq.empty: df_kosdaq.set_index('date', inplace=True)

    print("   ✅ 지수 데이터 준비 완료.")

    confirm = input("\n데이터 업데이트를 진행하시겠습니까? (y/n): ").strip().lower()
    if confirm != 'y': return

    all_files = []
    for d_dir in DATA_DIRS:
        if os.path.exists(d_dir):
            files = [os.path.join(d_dir, f) for f in os.listdir(d_dir) if f.startswith("A") and f.endswith(".csv")]
            all_files.extend(files)

    total_files = len(all_files)
    print(f"\n📂 총 대상 파일: {total_files}개")

    success_cnt, fail_cnt = 0, 0
    failed_items = []

    for idx, file_path in enumerate(all_files, 1):
        code = os.path.basename(file_path)[1:7]
        try:
            # 1. 기존 파일 읽기
            df_old = pd.read_csv(file_path, encoding='utf-8-sig')
            df_old['date'] = pd.to_datetime(df_old['date'])
            name = df_old['name'].iloc[0] if 'name' in df_old.columns else ""

            # 2. 신규 데이터 수집 (주가 및 프로그램)
            df_price = fetch_chart_data_chunked(code, start_date_str, end_date_str)

            if not df_price.empty:
                df_prog = fetch_program_history(code, start_date_str, end_date_str)

                if not df_prog.empty:
                    df_new = pd.merge(df_price, df_prog, on='date', how='left')
                    df_new['prog_net_qty'] = df_new['prog_net_qty'].fillna(0)
                    df_new['prog_buy'] = df_new['prog_buy'].fillna(0)
                    df_new['prog_sell'] = df_new['prog_sell'].fillna(0)
                else:
                    df_new = df_price
                    df_new['prog_net_qty'] = 0
                    df_new['prog_buy'] = 0
                    df_new['prog_sell'] = 0

                # 파생 계산 (신규 데이터 한정)
                df_new['prog_ratio_vol'] = np.where(df_new['volume'] > 0,
                                                    (df_new['prog_buy'] + df_new['prog_sell']) / df_new['volume'], 0.0)
                df_new['prog_net_ratio'] = np.where(df_new['volume'] > 0,
                                                    df_new['prog_net_qty'] / df_new['volume'], 0.0)
                df_new['code'] = code
                df_new['name'] = name

                # 3. 데이터 결합 (combine_first 활용)
                df_old.set_index('date', inplace=True)
                df_new.set_index('date', inplace=True)

                # 신규 데이터를 기준으로 기존 데이터를 덮어씀 (결측치, 신규컬럼 보호)
                df_combined = df_new.combine_first(df_old).reset_index()
                df_combined = df_combined.sort_values('date').reset_index(drop=True)
            else:
                df_combined = df_old

            # 4. 시장 지수 병합 (업데이트)
            if 'kospi_change' not in df_combined.columns: df_combined['kospi_change'] = np.nan
            if 'kosdaq_change' not in df_combined.columns: df_combined['kosdaq_change'] = np.nan

            df_combined.set_index('date', inplace=True)

            if not df_kospi.empty:
                df_combined['kospi_change'] = df_kospi['kospi_change'].combine_first(df_combined['kospi_change'])
            if not df_kosdaq.empty:
                df_combined['kosdaq_change'] = df_kosdaq['kosdaq_change'].combine_first(df_combined['kosdaq_change'])

            df_combined.reset_index(inplace=True)
            df_combined[['kospi_change', 'kosdaq_change']] = df_combined[['kospi_change', 'kosdaq_change']].fillna(0)

            # 5. 지표 및 Target 재계산
            # 종목별 변동률은 기존 데이터를 포함한 전체 결합 프레임(df_combined)에서 계산하므로 첫날도 정상 계산됨
            df_combined['change_pct'] = df_combined['close'].pct_change().fillna(0)

            # Target
            df_combined['target1'] = df_combined['close'].shift(-1) / df_combined['close'] - 1
            df_combined['target5'] = df_combined['close'].shift(-5) / df_combined['close'] - 1
            df_combined['target20'] = df_combined['close'].shift(-20) / df_combined['close'] - 1

            # 보조지표 (전체 데이터 기준으로 재계산하여 안정성 확보)
            df_combined = indicators.calculate_indicators_v3_save(df_combined)

            # 추가 지표
            if 'ma60' not in df_combined.columns: df_combined['ma60'] = df_combined['close'].rolling(window=60).mean()
            if 'disparity_60' not in df_combined.columns: df_combined['disparity_60'] = (
                        df_combined['close'] / df_combined['ma60'] - 1).fillna(0)
            if 'bb_pos' not in df_combined.columns: df_combined['bb_pos'] = 0.0

            # 6. 최종 저장
            for col in FINAL_COLUMNS:
                if col not in df_combined.columns:
                    df_combined[col] = 0

            df_final = df_combined[FINAL_COLUMNS].fillna(0)
            # 원자적 저장 (temp + os.replace) — 동시 쓰기로 인한 줄바꿈 유실/파일 손상 방지
            _tmp_path = file_path + ".tmp"
            df_final.to_csv(_tmp_path, index=False, encoding='utf-8-sig')
            os.replace(_tmp_path, file_path)
            success_cnt += 1

        except Exception as e:
            fail_cnt += 1
            failed_items.append({'code': code, 'error': str(e)})

        progress = (idx / total_files) * 100
        sys.stdout.write(f"\r🚀 진행률: [{idx}/{total_files}] {progress:.1f}% (성공: {success_cnt}, 실패: {fail_cnt})")
        sys.stdout.flush()

        time.sleep(0.05)

    print("\n\n✅ 모든 작업이 완료되었습니다.")
    print(f"   - 성공: {success_cnt}개, 실패: {fail_cnt}개")

    if failed_items:
        print("\n⚠️ [업데이트 실패 종목 목록]")
        print("=" * 40)
        for item in failed_items:
            print(f"❌ 종목코드: {item['code']} | 사유: {item['error']}")
        print("=" * 40)


if __name__ == "__main__":
    update_all_files()