import os
import sys
import time
import csv
import pandas as pd
import numpy as np
import tensorflow as tf
from datetime import datetime

# 1. CPU 모드 및 경고 끄기
os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
import warnings

warnings.filterwarnings("ignore")

# 2. 모듈 불러오기
from config import secrets
from kis_api import auth, inquiry, indicators
from tensorflow.keras.models import load_model
import pickle

# =================================================================================
# 🛠️ [설정]
# =================================================================================
MODEL_DIR = secrets.V3_MODEL_DIR
DATA_DIR_STOCK = r"C:\Projects\RealtimeMonitor\Data\Stock"
DATA_DIR_ETF = r"C:\Projects\RealtimeMonitor\Data\ETF"

# 로그 폴더 경로
LOG_DIR = r"C:\Projects\RealtimeMonitor\logs"

# 모델 가중치 설정 (V3)
MODEL_SETTINGS = {
    "target1": {"lb": 21, "thr": 0.4974, "weight": 0.1384},
    "target5": {"lb": 50, "thr": 0.6327, "weight": 0.3099},
    "target20": {"lb": 60, "thr": 0.9046, "weight": 0.5517},
    "drop1": {"lb": 10, "thr": 0.4349, "weight": 0.2411},
    "drop5": {"lb": 94, "thr": 0.4314, "weight": 0.3714},
    "drop20": {"lb": 98, "thr": 0.4686, "weight": 0.3875}
}

V3_FEATURES = [
    'change_pct', 'volume_ratio', 'vol_power',
    'prog_net_ratio', 'prog_ratio_vol',
    'disparity_5', 'disparity_20',
    'rsi', 'bb_p', 'bb_w', 'adx',
    'kospi_change', 'kosdaq_change'
]


# =================================================================================
# 📋 로그 저장 함수들
# =================================================================================

def save_search_log(data_dict):
    """(1) 검색 기록을 날짜별 파일에 저장 (YYYYMMDD_Search_History.csv)
    동일 code + chat_id가 이미 있으면 행을 추가하지 않고 업데이트한다."""
    if not os.path.exists(LOG_DIR): os.makedirs(LOG_DIR)

    today_str = datetime.now().strftime("%Y%m%d")
    filename  = f"{today_str}_Search_History.csv"
    file_path = os.path.join(LOG_DIR, filename)
    file_exists = os.path.exists(file_path)

    fieldnames = ['timestamp', 'code', 'name', 'current_price', 'market_cap',
                  'change_pct', 'total_score', 'net_hits', 'surge_hits', 'drop_hits', 'signal', 'chat_id']

    try:
        code = str(data_dict.get('code', '')).strip().zfill(6)
        cid  = str(data_dict.get('chat_id', ''))

        if file_exists:
            try:
                df = pd.read_csv(file_path, encoding='utf-8-sig', dtype=str, on_bad_lines='skip')
            except TypeError:
                df = pd.read_csv(file_path, encoding='utf-8-sig', dtype=str, error_bad_lines=False)

            if 'code' in df.columns and 'chat_id' in df.columns:
                df['code'] = df['code'].astype(str).str.strip().str.zfill(6)
                dup = (df['code'] == code) & (df['chat_id'].fillna('') == cid)
                if dup.any():
                    for col, val in data_dict.items():
                        if col in df.columns:
                            df.loc[dup, col] = val
                    df.to_csv(file_path, index=False, encoding='utf-8-sig')
                    print(f"   💾 [History] 기존 기록 업데이트 ({filename})")
                    return

        with open(file_path, mode='a', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            if not file_exists:
                writer.writeheader()
            writer.writerow(data_dict)

        print(f"   💾 [History] 검색 기록 저장 완료 ({filename})")
    except Exception as e:
        print(f"   ❌ 검색 기록 저장 실패: {e}")


def update_daily_bot_log(data_dict, is_etf, prob_res):
    """
    (2) 실시간 봇의 데일리 로그 파일에 결과를 추가 (YYYYMMDD_Stock_V3.csv)
    [중요] main_etf.py / main_stock.py와 컬럼명 및 순서를 100% 일치시킴
    """
    if not os.path.exists(LOG_DIR): os.makedirs(LOG_DIR)

    today_str = datetime.now().strftime("%Y%m%d")

    type_str = "ETF" if is_etf else "Stock"
    filename = f"{today_str}_{type_str}_V3.csv"
    file_path = os.path.join(LOG_DIR, filename)
    file_exists = os.path.exists(file_path)

    # 🟢 [수정됨] 메인 봇과 동일한 컬럼 순서 및 이름 (소문자)
    fieldnames = [
        'code', 'name', 'close_price', 'market_cap', 'score_total',
        'net_hits', 'surge_hits', 'drop_hits', 'time',
        'target1', 'target5', 'target20',
        'drop1', 'drop5', 'drop20'
    ]

    # 시가총액 정보가 data_dict에 없으면 API나 계산된 값에서 가져와야 함
    # run_manual_analysis에서 cap 변수를 log_data에 추가해야 함 (아래 참조)
    market_cap = data_dict.get('market_cap', 0)

    # 저장할 데이터 매핑 (키 이름을 fieldnames에 맞춤)
    row_data = {
        'code': data_dict['code'],
        'name': data_dict['name'],
        'close_price': data_dict['current_price'],
        'market_cap': market_cap,
        'score_total': data_dict['total_score'],
        'net_hits': data_dict['net_hits'],
        'surge_hits': data_dict['surge_hits'],
        'drop_hits': data_dict['drop_hits'],
        'time': datetime.now().strftime("%H:%M:%S"),  # Time -> time
        # 확률값
        'target1': prob_res.get('target1', 0),
        'target5': prob_res.get('target5', 0),
        'target20': prob_res.get('target20', 0),
        'drop1': prob_res.get('drop1', 0),
        'drop5': prob_res.get('drop5', 0),
        'drop20': prob_res.get('drop20', 0)
    }

    try:
        with open(file_path, mode='a', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            # 파일이 처음 생성될 때만 헤더 작성 (이미 있으면 데이터만 추가)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row_data)
        print(f"   💾 [Main Log] {filename} 에 병합 완료")
    except Exception as e:
        print(f"   ❌ 메인 로그 병합 실패: {e}")


# =================================================================================
# 🛠️ 헬퍼 함수
# =================================================================================

def ensure_data_ready(code, name, is_etf):
    base_path = DATA_DIR_ETF if is_etf else DATA_DIR_STOCK
    file_path = os.path.join(base_path, f"A{code}.csv")

    if not os.path.exists(file_path):
        other_path = os.path.join(DATA_DIR_STOCK if is_etf else DATA_DIR_ETF, f"A{code}.csv")
        if os.path.exists(other_path): file_path = other_path

    if not os.path.exists(file_path):
        return pd.DataFrame(), file_path, False

    try:
        df = pd.read_csv(file_path, encoding='utf-8-sig')
        if 'date' in df.columns: df['date'] = pd.to_datetime(df['date'])
        if name and name != "Unknown": df['name'] = name
        return df, file_path, True
    except Exception:
        return pd.DataFrame(), file_path, False


def custom_sort_key(key):
    prio = 0 if 'target' in key else 1
    try:
        num = int(''.join(filter(str.isdigit, key)))
    except:
        num = 999
    return (prio, num)


# ====== 메인 실행 함수 ======
def run_manual_analysis():
    if not auth.get_access_token():
        print("❌ 토큰 발급 실패")
        return
    print(f"🔐 토큰 준비 완료")

    models = {}
    print(f"\n📂 [V3] 모델 로드 중... ({MODEL_DIR})")

    if not os.path.exists(MODEL_DIR):
        print(f"❌ 모델 폴더가 없습니다.")
        sys.exit()

    for m_name, settings in MODEL_SETTINGS.items():
        model_path = os.path.join(MODEL_DIR, f"{m_name}_lstm_v3.h5")
        scaler_path = os.path.join(MODEL_DIR, f"{m_name}_lstm_v3.scaler")

        if os.path.exists(model_path) and os.path.exists(scaler_path):
            try:
                model = load_model(model_path)
                with open(scaler_path, 'rb') as f:
                    scaler = pickle.load(f)

                models[m_name] = {
                    "model": model, "scaler": scaler,
                    "lookback": settings['lb'], "threshold": settings['thr'],
                    "weight": settings['weight'], "type": "surge" if "target" in m_name else "drop"
                }
            except Exception as e:
                print(f"   ⚠️ {m_name} 로드 실패: {e}")
        else:
            print(f"   ℹ️ {m_name}: 파일 없음")

    print(f"🚀 {len(models)}개 모델 준비 완료.")
    print("\n" + "=" * 60)
    print(f"🔎 [Search Stock] 종목 정밀 진단기 (V3)")
    print("=" * 60)

    while True:
        code = input("\n👉 종목코드(6자리) 입력 (종료: q): ").strip().upper()
        if code == 'Q': break

        if len(code) != 6 or not code.isalnum():
            print("⚠️ 올바른 6자리 코드를 입력하세요. (문자 포함 가능)")
            continue

        print(f"⏳ [{code}] 데이터 수집 및 분석 중...")

        # 🟢 [수정 1] KOSPI 뿐만 아니라 KOSDAQ 지수도 가져오기
        k_val = inquiry.fetch_index_change("0001")  # 코스피
        kq_val = inquiry.fetch_index_change("1001")  # 코스닥 (추가됨)

        kind = inquiry.fetch_stock_kind(code)
        is_etf = (kind in ["ETF", "ETN"])

        rt = inquiry.fetch_realtime_price(code)
        if not rt:
            print("❌ 존재하지 않는 종목코드입니다. (API 조회 불가)")
            continue

        curr = inquiry.safe_int(rt.get("stck_prpr"))
        oprc = inquiry.safe_int(rt.get("stck_oprc"))
        if oprc == 0: oprc = curr

        stock_name = rt.get("hts_kor_isnm", "").strip()
        if not stock_name: stock_name = inquiry.fetch_stock_name(code)

        target_date = pd.Timestamp.today().normalize()
        prog = inquiry.fetch_program_today(code, target_date.strftime('%Y%m%d'))
        p_net, p_ratio = 0, 0.0
        if prog:
            p_net = inquiry.safe_int(prog.get("whol_smtn_ntby_qty"))
            p_tot = inquiry.safe_int(prog.get("acml_vol"))
            if p_tot > 0:
                p_ratio = round((inquiry.safe_int(prog.get("whol_smtn_shnu_vol")) + inquiry.safe_int(
                    prog.get("whol_smtn_seln_vol"))) / p_tot, 4)

        df, file_path, exists = ensure_data_ready(code, stock_name, is_etf)

        if not stock_name and not df.empty: stock_name = df['name'].iloc[0]

        if not exists:
            print(f"❌ 데이터 폴더에 파일(A{code}.csv)이 없습니다.")
            print("   (Reset_A_Stock_Data.py를 실행하여 데이터를 먼저 생성하세요)")
            continue

        if df.empty:
            print("❌ 데이터 파일이 비어있습니다.")
            continue

        df = df[df['date'] != target_date]
        vol = inquiry.safe_int(rt.get("acml_vol"))

        # 🟢 [수정 2] kosdaq_change에 0.0 대신 kq_val 넣기
        today_row = {
            "date": target_date, "code": code, "name": stock_name,
            "open": oprc, "high": inquiry.safe_int(rt.get("stck_hgpr")),
            "low": inquiry.safe_int(rt.get("stck_lwpr")), "close": curr,
            "volume": vol,
            "change_pct": (curr / oprc - 1) if oprc > 0 else 0,
            "kospi_change": k_val,
            "kosdaq_change": kq_val,  # 👈 여기가 핵심 수정 사항입니다!
            "prog_net_qty": p_net, "prog_ratio_vol": p_ratio
        }

        df = pd.concat([df, pd.DataFrame([today_row])]).sort_values('date').reset_index(drop=True)

        df = indicators.calculate_indicators_v3_save(df)

        if 'prog_net_ratio' not in df.columns:
            df['prog_net_ratio'] = df.apply(lambda x: x['prog_net_qty'] / x['volume'] if x['volume'] > 0 else 0, axis=1)

        for col in V3_FEATURES:
            if col not in df.columns: df[col] = 0.0
        df = df.fillna(0)

        df.to_csv(file_path, index=False, encoding='utf-8-sig')

        # 예측 실행
        res = {}
        s_score_sum, d_score_sum = 0.0, 0.0
        s_hits, d_hits = 0, 0

        max_lb = max([m['lookback'] for m in models.values()])
        if len(df) < max_lb:
            print(f"⚠️ 데이터 부족 (필요: {max_lb}, 보유: {len(df)}) - 예측 불가")
            continue

        for m_name, info in models.items():
            try:
                window = df.iloc[-info['lookback']:][V3_FEATURES].values
                win_scaled = info['scaler'].transform(window).reshape(1, info['lookback'], len(V3_FEATURES))
                tensor_input = tf.convert_to_tensor(win_scaled, dtype=tf.float32)

                prob = float(info['model'](tensor_input, training=False)[0, 0])
                res[m_name] = round(prob, 4)

                if prob > info['threshold']:
                    contrib = prob * info['weight']
                    if info['type'] == "surge":
                        s_score_sum += contrib;
                        s_hits += 1
                    else:
                        d_score_sum += contrib;
                        d_hits += 1
            except Exception:
                res[m_name] = 0.0

        total_score = round(s_score_sum - d_score_sum, 4)
        net_hits = s_hits - d_hits
        cap = inquiry.fetch_market_cap(code)
        avg_val = (df['close'] * df['volume']).tail(20).mean() if len(df) >= 20 else 0

        # 결과 출력
        print("\n" + "-" * 50)
        print(f"📊 [분석 결과] {stock_name} ({code}) - {kind}")
        print(f"   💰 현재가: {curr:,}원 (등락률: {today_row['change_pct'] * 100:+.2f}%)")
        print(f"   🏢 시가총액: {cap:,}억 / 평균거래대금: {avg_val / 100000000:.1f}억")
        print(f"   🤖 프로그램 순매수: {p_net:,}주")
        print("-" * 50)
        print(f"   🎯 종합 점수: {total_score:.4f}")
        print(f"   🚩 Signal: Target {s_hits} / Drop {d_hits} (Net: {net_hits})")
        print("-" * 50)

        print("   [상세 확률]")
        sorted_keys = sorted([k for k in res.keys()], key=custom_sort_key)
        for k in sorted_keys:
            print(f"    • {k:<10}: {res[k]:.4f}")
        print("-" * 50)

        # 🟢 [데이터 준비] market_cap 추가됨
        log_data = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'code': code, 'name': stock_name,
            'current_price': curr,
            'market_cap': cap,
            'change_pct': round(today_row['change_pct'] * 100, 2),
            'total_score': total_score,
            'net_hits': net_hits, 'surge_hits': s_hits, 'drop_hits': d_hits,
            'signal': f"Target {s_hits} / Drop {d_hits}",
            'chat_id': secrets.TELEGRAM_CHAT_ID,
        }

        # 🟢 [저장]
        save_search_log(log_data)
        update_daily_bot_log(log_data, is_etf, res)


if __name__ == "__main__":
    try:
        run_manual_analysis()
    except KeyboardInterrupt:
        print("\n🛑 프로그램 종료")