import os
import sys
import time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from tqdm import tqdm
from kis_api import auth, inquiry, indicators, common
from config import secrets

# ====== 설정 ======
START_DATE_FIXED = "20200101"  # 시작일
DATA_DIRS = [
    r"C:\Projects\RealtimeMonitor\Data\Stock",
    r"C:\Projects\RealtimeMonitor\Data\ETF"
]

# 30개 컬럼 (순서 엄수)
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
# 🛠️ 데이터 수집 헬퍼 함수
# =========================================================

def fetch_market_index_history(market_code, start_date, end_date):
    """
    [전략 1] 지수 데이터 수집 (수정됨)
    - 기존 3개월 -> 60일 단위로 변경 (데이터 잘림 방지)
    """
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
        # [수정] 60일 단위로 끊어서 요청
        next_step = curr + timedelta(days=60)
        if next_step > end_dt: next_step = end_dt

        params = {
            "FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": market_code,
            "FID_INPUT_DATE_1": curr.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": next_step.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D"
        }
        res = common.call_api(url, params, headers)
        time.sleep(0.05)  # 안정성 확보

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


def fetch_program_history_chunked(code, start_date, end_date):
    """
    [전략 2] 프로그램 매매 수집
    - 40일 단위 청킹
    """
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/program-trade-by-stock-daily"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {auth.get_access_token()}",
        "appkey": secrets.APP_KEY, "appsecret": secrets.APP_SECRET,
        "tr_id": "FHPPG04650201", "custtype": "P"
    }

    start_dt = datetime.strptime(start_date, "%Y%m%d")
    end_dt = datetime.strptime(end_date, "%Y%m%d")

    all_rows = []
    curr = start_dt

    while curr <= end_dt:
        # 40일 단위 설정
        curr_end = curr + timedelta(days=40)
        if curr_end > end_dt: curr_end = end_dt

        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                  "FID_INPUT_DATE_1": curr_end.strftime("%Y%m%d")}
        res = common.call_api(url, params, headers)
        time.sleep(0.03)

        if res and "output" in res:
            for item in res['output']:
                if not item['stck_bsop_date']: continue
                d_str = item['stck_bsop_date']
                if not (curr.strftime("%Y%m%d") <= d_str <= curr_end.strftime("%Y%m%d")): continue
                all_rows.append({
                    "date": d_str,
                    "prog_net_qty": int(item['whol_smtn_ntby_qty']),
                    "prog_buy": int(item['whol_smtn_shnu_vol']),
                    "prog_sell": int(item['whol_smtn_seln_vol'])
                })
        curr = curr_end + timedelta(days=1)

    if not all_rows: return pd.DataFrame()
    df = pd.DataFrame(all_rows)
    df['date'] = pd.to_datetime(df['date'])
    return df.drop_duplicates('date')


def fetch_chart_data_chunked(code, start_date_str, end_date_str):
    """
    [전략 3] 주가 데이터 수집
    - 3개월 단위 청킹 (속도 최적화)
    """
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {auth.get_access_token()}",
        "appkey": secrets.APP_KEY, "appsecret": secrets.APP_SECRET,
        "tr_id": "FHKST03010100", "custtype": "P"
    }

    start_dt = datetime.strptime(start_date_str, "%Y%m%d")
    end_dt = datetime.strptime(end_date_str, "%Y%m%d")

    all_rows = []
    curr = start_dt

    while curr <= end_dt:
        # 3개월 단위 설정
        next_step = curr + relativedelta(months=3)
        if next_step > end_dt: next_step = end_dt

        params = {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": curr.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": next_step.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1"
        }
        res = common.call_api(url, params, headers)
        time.sleep(0.02)

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
# 🚀 메인 로직
# =========================================================
def run_reset_all():
    if not auth.get_access_token():
        print("❌ 토큰 발급 실패")
        return

    print("\n" + "=" * 70)
    print("🚀 [Reset All Data] Customized Speed Optimization")
    print(f"   - 기간: {START_DATE_FIXED} ~ 오늘")
    print("   1️⃣ 지수: 60일 단위 (누락 방지)")
    print("   2️⃣ 프로그램: 40일 단위 (누락 방지)")
    print("   3️⃣ 주가: 3개월 단위 (속도 향상)")
    print("=" * 70)

    confirm = input("정말로 진행하시겠습니까? (YES 입력): ").strip()
    if confirm != "YES":
        print("취소되었습니다.")
        return

    end_date_str = datetime.now().strftime("%Y%m%d")

    # 1. 지수 데이터 수집 (1회)
    print(f"\n1️⃣ 시장 지수(KOSPI/KOSDAQ) 로딩 중... (60일 단위)")
    df_kospi = fetch_market_index_history("0001", START_DATE_FIXED, end_date_str)
    df_kosdaq = fetch_market_index_history("1001", START_DATE_FIXED, end_date_str)
    print("   ✅ 지수 준비 완료.")

    # 2. 대상 파일 확인
    all_files = []
    for d_dir in DATA_DIRS:
        if os.path.exists(d_dir):
            files = [os.path.join(d_dir, f) for f in os.listdir(d_dir) if f.startswith("A") and f.endswith(".csv")]
            all_files.extend(files)

    print(f"\n📂 총 {len(all_files)}개 종목 초기화 시작\n")

    # 3. 메인 루프
    success_cnt, fail_cnt = 0, 0

    try:
        pbar = tqdm(all_files, unit="file")
        for file_path in pbar:
            code = os.path.basename(file_path)[1:7]

            try:
                # 종목명 가져오기
                try:
                    df_old = pd.read_csv(file_path, encoding='utf-8-sig', nrows=1)
                    name = df_old['name'].iloc[0] if 'name' in df_old.columns else ""
                except:
                    name = code

                pbar.set_description(f"{name}({code})")

                # (1) 주가 수집 (3개월 단위)
                df = fetch_chart_data_chunked(code, START_DATE_FIXED, end_date_str)
                if df.empty:
                    fail_cnt += 1
                    continue

                # (2) 프로그램 매매 수집 (40일 단위)
                df_prog = fetch_program_history_chunked(code, START_DATE_FIXED, end_date_str)

                # 병합
                if not df_prog.empty:
                    df = pd.merge(df, df_prog, on='date', how='left')
                    df[['prog_net_qty', 'prog_buy', 'prog_sell']] = df[
                        ['prog_net_qty', 'prog_buy', 'prog_sell']].fillna(0)
                else:
                    df['prog_net_qty'] = 0;
                    df['prog_buy'] = 0;
                    df['prog_sell'] = 0

                # (3) 지수 병합
                df = pd.merge(df, df_kospi, on='date', how='left')
                df = pd.merge(df, df_kosdaq, on='date', how='left')
                df[['kospi_change', 'kosdaq_change']] = df[['kospi_change', 'kosdaq_change']].fillna(0)

                # (4) 계산 및 지표
                df['code'] = code
                df['name'] = name
                df['change_pct'] = df['close'].pct_change().fillna(0)

                df['prog_ratio_vol'] = np.where(df['volume'] > 0, (df['prog_buy'] + df['prog_sell']) / df['volume'],
                                                0.0)
                df['prog_net_ratio'] = np.where(df['volume'] > 0, df['prog_net_qty'] / df['volume'], 0.0)

                df['target1'] = df['close'].shift(-1) / df['close'] - 1
                df['target5'] = df['close'].shift(-5) / df['close'] - 1
                df['target20'] = df['close'].shift(-20) / df['close'] - 1

                df = indicators.calculate_indicators_v3_save(df)

                if 'ma60' not in df.columns: df['ma60'] = df['close'].rolling(window=60).mean()
                if 'disparity_60' not in df.columns: df['disparity_60'] = (df['close'] / df['ma60'] - 1).fillna(0)
                if 'bb_pos' not in df.columns: df['bb_pos'] = 0.0

                # (5) 저장
                for col in FINAL_COLUMNS:
                    if col not in df.columns: df[col] = 0.0

                df[FINAL_COLUMNS].fillna(0).to_csv(file_path, index=False, encoding='utf-8-sig')
                success_cnt += 1

            except Exception:
                fail_cnt += 1

    except KeyboardInterrupt:
        print("\n🛑 사용자 중단.")

    print(f"\n✅ 완료: 성공 {success_cnt} / 실패 {fail_cnt}")


if __name__ == "__main__":
    run_reset_all()