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
START_DATE_FIXED = "20200101"  # 고정 시작일
DATA_DIR_STOCK = r"C:\Projects\RealtimeMonitor\Data\Stock"
DATA_DIR_ETF = r"C:\Projects\RealtimeMonitor\Data\ETF"

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
# 🛠️ 데이터 수집 함수들
# =========================================================

def fetch_market_index_history(market_code, start_date, end_date):
    """
    시장 지수(KOSPI:0001, KOSDAQ:1001) 데이터를 가져옵니다.
    """
    # print(f"   📊 지수 데이터 수집 중 ({market_code})...")
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {auth.get_access_token()}",
        "appkey": secrets.APP_KEY,
        "appsecret": secrets.APP_SECRET,
        "tr_id": "FHKUP03500100",  # 업종/지수 기간별 시세
        "custtype": "P"
    }

    start_dt = datetime.strptime(start_date, "%Y%m%d")
    end_dt = datetime.strptime(end_date, "%Y%m%d")

    all_rows = []
    curr = start_dt

    while curr <= end_dt:
        # 지수 API는 기간을 좀 길게 잡아도 됨 (2개월 단위)
        next_step = curr + relativedelta(months=2)
        if next_step > end_dt: next_step = end_dt

        params = {
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": market_code,
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
                    "close": float(item['bstp_nmix_prpr'])  # 지수는 실수형일 수 있음
                })

        curr = next_step + timedelta(days=1)

    if not all_rows: return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').drop_duplicates('date').reset_index(drop=True)

    # 등락률 계산
    col_name = 'kospi_change' if market_code == '0001' else 'kosdaq_change'
    df[col_name] = df['close'].pct_change().fillna(0)

    return df[['date', col_name]]


def fetch_program_history_chunked(code, start_date, end_date):
    """
    프로그램 매매 추이 데이터를 가져옵니다. (1개월 단위 반복)
    """
    # print(f"   🤖 프로그램 매매 데이터 수집 중...")
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/program-trade-by-stock-daily"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {auth.get_access_token()}",
        "appkey": secrets.APP_KEY,
        "appsecret": secrets.APP_SECRET,
        "tr_id": "FHPPG04650201",  # 종목별 프로그램 매매 추이
        "custtype": "P"
    }

    start_dt = datetime.strptime(start_date, "%Y%m%d")
    end_dt = datetime.strptime(end_date, "%Y%m%d")

    all_rows = []
    curr = start_dt

    while curr <= end_dt:
        # 이 API는 '종료일' 기준 과거 데이터를 주는 방식이므로 1개월씩 끊어서 요청
        curr_end = curr + relativedelta(months=1)
        if curr_end > end_dt: curr_end = end_dt

        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": curr_end.strftime("%Y%m%d")  # 기준일
        }

        res = common.call_api(url, params, headers)
        time.sleep(0.1)

        if res and "output" in res:
            for item in res['output']:
                if not item['stck_bsop_date']: continue
                # 날짜 필터링 (요청 범위 내만)
                d_str = item['stck_bsop_date']
                if not (curr.strftime("%Y%m%d") <= d_str <= curr_end.strftime("%Y%m%d")):
                    continue

                all_rows.append({
                    "date": d_str,
                    "prog_net_qty": int(item['whol_smtn_ntby_qty']),  # 순매수량
                    "prog_buy": int(item['whol_smtn_shnu_vol']),
                    "prog_sell": int(item['whol_smtn_seln_vol'])
                })

        curr = curr_end + timedelta(days=1)

    if not all_rows: return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').drop_duplicates('date').reset_index(drop=True)
    return df


def fetch_chart_data_chunked(code, start_date_str, end_date_str):
    """ 주식 시세 데이터 (기존 로직 유지) """
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {auth.get_access_token()}",
        "appkey": secrets.APP_KEY,
        "appsecret": secrets.APP_SECRET,
        "tr_id": "FHKST03010100",
        "custtype": "P"
    }

    start_dt = datetime.strptime(start_date_str, "%Y%m%d")
    end_dt = datetime.strptime(end_date_str, "%Y%m%d")

    all_rows = []
    curr = start_dt
    while curr <= end_dt:
        next_step = curr + timedelta(days=30)
        if next_step > end_dt: next_step = end_dt

        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": curr.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": next_step.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "1"
        }
        res = common.call_api(url, params, headers)
        time.sleep(0.1)

        if res and "output2" in res:
            for item in res['output2']:
                if not item['stck_bsop_date']: continue
                all_rows.append({
                    "date": item['stck_bsop_date'],
                    "open": int(item['stck_oprc']),
                    "high": int(item['stck_hgpr']),
                    "low": int(item['stck_lwpr']),
                    "close": int(item['stck_clpr']),
                    "volume": int(item['acml_vol'])
                })
        curr = next_step + timedelta(days=1)
        sys.stdout.write(f"\r   ⏳ 시세 수집: {len(all_rows)} row(s)")
        sys.stdout.flush()

    print()
    if not all_rows: return pd.DataFrame()
    df = pd.DataFrame(all_rows)
    df['date'] = pd.to_datetime(df['date'])
    return df.sort_values('date').drop_duplicates('date').reset_index(drop=True)


# =========================================================
# 🚀 메인 실행 로직
# =========================================================
def run_reset_tool():
    if not auth.get_access_token():
        print("❌ 토큰 발급 실패")
        return

    print("\n" + "=" * 60)
    print("🧹 [Reset A Stock Data] Full Data Refresh")
    print("   - 포함: 시세 + 프로그램매매 + KOSPI/KOSDAQ 지수 + Target 계산")
    print("=" * 60)

    # 1. 지수 데이터 미리 받아두기 (반복 호출 방지)
    end_date = datetime.now().strftime("%Y%m%d")
    print("📊 시장 지수 데이터 수집 중... (KOSPI/KOSDAQ)")
    df_kospi = fetch_market_index_history("0001", START_DATE_FIXED, end_date)
    df_kosdaq = fetch_market_index_history("1001", START_DATE_FIXED, end_date)
    print("   ✅ 지수 데이터 준비 완료.")

    while True:
        code = input("\n👉 종목코드(6자리) 입력 (종료: q): ").strip()
        if code.lower() == 'q': break
        if not code.isdigit() or len(code) != 6: continue

        try:
            name = inquiry.fetch_stock_name(code)
            kind = inquiry.fetch_stock_kind(code)
            is_etf = (kind in ["ETF", "ETN"])
            save_dir = DATA_DIR_ETF if is_etf else DATA_DIR_STOCK

            print(f"   Target: {name} ({code}) - {kind}")

            # 2. 주식 시세 수집
            df = fetch_chart_data_chunked(code, START_DATE_FIXED, end_date)
            if df.empty:
                print("❌ 시세 데이터 없음.")
                continue

            # 3. 프로그램 매매 수집 및 병합
            df_prog = fetch_program_history_chunked(code, START_DATE_FIXED, end_date)
            if not df_prog.empty:
                df = pd.merge(df, df_prog, on='date', how='left')
                df['prog_net_qty'] = df['prog_net_qty'].fillna(0)
                df['prog_buy'] = df['prog_buy'].fillna(0)
                df['prog_sell'] = df['prog_sell'].fillna(0)
                # prog_ratio_vol 계산 (프로그램관여비중)
                # (매수+매도) / 전체거래량
                df['prog_ratio_vol'] = np.where(df['volume'] > 0,
                                                (df['prog_buy'] + df['prog_sell']) / df['volume'],
                                                0.0)
                # prog_net_ratio 계산 (순매수비중)
                df['prog_net_ratio'] = np.where(df['volume'] > 0,
                                                df['prog_net_qty'] / df['volume'],
                                                0.0)
            else:
                df['prog_net_qty'] = 0
                df['prog_ratio_vol'] = 0.0
                df['prog_net_ratio'] = 0.0

            # 4. 지수 데이터 병합
            df = pd.merge(df, df_kospi, on='date', how='left')
            df = pd.merge(df, df_kosdaq, on='date', how='left')
            df['kospi_change'] = df['kospi_change'].fillna(0)
            df['kosdaq_change'] = df['kosdaq_change'].fillna(0)

            # 5. 기본 지표 계산
            df['code'] = code
            df['name'] = name
            df['change_pct'] = df['close'].pct_change().fillna(0)

            # 6. Target 계산 (미래 데이터 참조)
            # -1일(내일), -5일(일주일뒤), -20일(한달뒤) 수익률
            df['target1'] = df['close'].shift(-1) / df['close'] - 1
            df['target5'] = df['close'].shift(-5) / df['close'] - 1
            df['target20'] = df['close'].shift(-20) / df['close'] - 1

            # 마지막 데이터들은 미래가 없으므로 0으로 채움
            df[['target1', 'target5', 'target20']] = df[['target1', 'target5', 'target20']].fillna(0)

            # 7. 기술적 지표 계산 (Indicators V3)
            # MA60 등은 indicators에 없으면 수동 계산
            df = indicators.calculate_indicators_v3_save(df)

            if 'ma60' not in df.columns:
                df['ma60'] = df['close'].rolling(window=60).mean()
            if 'disparity_60' not in df.columns:
                df['disparity_60'] = (df['close'] / df['ma60'] - 1).fillna(0)
            if 'bb_pos' not in df.columns:
                df['bb_pos'] = 0.0

            # 8. 최종 저장 (컬럼 순서 맞춤)
            for col in FINAL_COLUMNS:
                if col not in df.columns: df[col] = 0

            df_final = df[FINAL_COLUMNS]

            if not os.path.exists(save_dir): os.makedirs(save_dir)
            file_path = os.path.join(save_dir, f"A{code}.csv")
            df_final.to_csv(file_path, index=False, encoding='utf-8-sig')

            print(f"   💾 저장 완료: {file_path}")
            print(f"   ✅ 데이터: {len(df_final)}일 / 프로그램, 지수, 타겟 모두 포함됨.")

        except Exception as e:
            print(f"❌ 오류 발생: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    run_reset_tool()