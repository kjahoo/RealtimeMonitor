import os
import sys
import gc
import time
import pickle
import warnings
import pandas as pd
import numpy as np
import tensorflow as tf
import shutil  # 파일 백업용
from datetime import datetime
from tensorflow.keras.models import load_model

# ====== [환경설정] ======
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
warnings.filterwarnings("ignore")

# GPU 설정 (기존 코드 유지)
gpus = tf.config.experimental.list_physical_devices('GPU')
if gpus:
    try:
        for gpu in gpus: tf.config.experimental.set_memory_growth(gpu, True)
        print(f"✅ GPU 가속 활성화됨: {len(gpus)}개")
    except RuntimeError as e:
        print(e)

# 모듈 불러오기
from config import secrets
from kis_api import auth, inquiry, indicators

# ====== 설정 변수 (사용자 수정 영역) ======
# [변경 전]
# DATA_DIR = r"G:\내 드라이브\ST Project\Data"

# [변경 후] 로컬 경로로 수정
DATA_DIR = r"C:\Projects\RealtimeMonitor\Data\ETF"
MODEL_DIR = secrets.V3_MODEL_DIR  # secrets.py에 설정된 모델 경로

# V3 모델 설정 (기존 값 유지)
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


# 텔레그램 알림
def send_telegram(msg):
    if not secrets.TELEGRAM_BOT_TOKEN: return
    try:
        import requests
        url = f"https://api.telegram.org/bot{secrets.TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": secrets.TELEGRAM_CHAT_ID, "text": msg}, timeout=3)
    except Exception as e:
        print(f"⚠️ 텔레그램 전송 실패: {e}")


# ====== 모델 로드 함수 ======
def load_v3_models():
    models = {}
    print(f"\n📂 모델 로드 중... ({MODEL_DIR})")

    if not os.path.exists(MODEL_DIR):
        print(f"❌ 모델 경로가 없습니다. secrets.py를 확인하세요.")
        return {}, 0

    max_lb = 0
    for name, settings in MODEL_SETTINGS.items():
        model_path = os.path.join(MODEL_DIR, f"{name}_lstm_v3.h5")
        scaler_path = os.path.join(MODEL_DIR, f"{name}_lstm_v3.scaler")

        if os.path.exists(model_path) and os.path.exists(scaler_path):
            try:
                model = load_model(model_path)
                # 추론 속도 향상을 위한 컴파일
                model.make_predict_function()

                with open(scaler_path, 'rb') as f:
                    scaler = pickle.load(f)

                models[name] = {
                    "model": model, "scaler": scaler,
                    "lookback": settings['lb'], "threshold": settings['thr'],
                    "weight": settings['weight'],
                    "type": "surge" if "target" in name else "drop"
                }
                if settings['lb'] > max_lb: max_lb = settings['lb']
                print(f"   ✅ {name} 로드 완료 (LB:{settings['lb']})")
            except Exception as e:
                print(f"   ⚠️ {name} 로드 실패: {e}")
        else:
            print(f"   ℹ️ {name} 파일 없음")

    return models, max_lb


# ====== 데이터 업데이트 및 예측 ======
def process_stock(file_path, code, target_date, k_val, kq_val, models, max_lb):
    try:
        df = pd.read_csv(file_path, encoding='utf-8-sig')
        df['date'] = pd.to_datetime(df['date'])

        # 1. 여기서 'name' 변수에 종목명(예: 삼성전자)을 저장함
        stock_name = df['name'].iloc[0]

        rt = inquiry.fetch_realtime_price(code)
        if not rt: return None
        curr = inquiry.safe_int(rt.get("stck_prpr"))
        oprc = inquiry.safe_int(rt.get("stck_oprc"))
        if oprc == 0: oprc = curr
        vol = inquiry.safe_int(rt.get("acml_vol"))
        if vol == 0: return None

        prog = inquiry.fetch_program_today(code, target_date.strftime('%Y%m%d'))
        p_net, p_ratio = 0, 0.0
        if prog:
            p_net = inquiry.safe_int(prog.get("whol_smtn_ntby_qty"))
            p_tot = inquiry.safe_int(prog.get("acml_vol"))
            if p_tot > 0: p_ratio = round((inquiry.safe_int(prog.get("whol_smtn_shnu_vol")) + inquiry.safe_int(
                prog.get("whol_smtn_seln_vol"))) / p_tot, 4)

        today_row = {"date": target_date, "code": code, "name": stock_name, "open": oprc,
                     "high": inquiry.safe_int(rt.get("stck_hgpr")), "low": inquiry.safe_int(rt.get("stck_lwpr")),
                     "close": curr, "volume": vol, "change_pct": (curr / oprc - 1) if oprc > 0 else 0,
                     "kospi_change": k_val, "kosdaq_change": kq_val, "prog_net_qty": p_net, "prog_ratio_vol": p_ratio}

        df = pd.concat([df[df['date'] != target_date], pd.DataFrame([today_row])]).sort_values('date').reset_index(
            drop=True)
        df = indicators.calculate_indicators_v3_save(df)

        if 'prog_net_ratio' not in df.columns: df['prog_net_ratio'] = df.apply(
            lambda x: x['prog_net_qty'] / x['volume'] if x['volume'] > 0 else 0, axis=1)
        for col in V3_FEATURES:
            if col not in df.columns: df[col] = 0.0
        df = df.fillna(0)

        # 원본 데이터 업데이트
        df.to_csv(file_path, index=False, encoding='utf-8-sig')

        # [주식 필터] (main_stock.py인 경우 주석 해제, main_etf.py인 경우 주석 처리된 상태 유지하거나 삭제)
        # if len(df) >= 20 and (df['close'] * df['volume']).iloc[-20:].mean() < 1000000000: return None

        if len(df) < max_lb: return None

        s_sum, d_sum = 0.0, 0.0
        s_hits, d_hits = 0, 0
        results = {}

        # 🔴 [수정된 부분] 변수명을 name -> model_name으로 변경하여 충돌 방지
        for model_name, info in models.items():
            lb = info['lookback']
            window = df.iloc[-lb:][V3_FEATURES].values
            win_scaled = info['scaler'].transform(window).reshape(1, lb, len(V3_FEATURES))
            tensor_input = tf.convert_to_tensor(win_scaled, dtype=tf.float32)
            prob = float(info['model'](tensor_input, training=False)[0, 0])

            results[model_name] = round(prob, 4)

            if prob > info['threshold']:
                if info['type'] == "surge":
                    s_sum += prob * info['weight']
                    s_hits += 1
                else:
                    d_sum += prob * info['weight']
                    d_hits += 1

        return {
            "code": code,
            "name": stock_name,  # ✅ 이제 정상적으로 종목명이 들어갑니다
            "close_price": curr,
            "score_total": round(s_sum - d_sum, 4),
            "net_hits": s_hits - d_hits,
            "surge_hits": s_hits,
            "drop_hits": d_hits,
            **results
        }
    except:
        return None


def save_and_backup_results(results, date_str, is_stock=False):
    if not results: return

    df = pd.DataFrame(results)

    # 1. 정렬: Total Score(score_total) 높은 순
    if 'score_total' in df.columns:
        df = df.sort_values(by='score_total', ascending=False)

    # 2. 컬럼 순서 지정 (보내주신 파일 기준)
    columns_order = [
        'code', 'name', 'close_price', 'market_cap', 'score_total',
        'net_hits', 'surge_hits', 'drop_hits', 'time',
        'target1', 'target5', 'target20', 'drop1', 'drop5', 'drop20'
    ]

    # 없는 컬럼은 0이나 공백으로 채움 (안전장치)
    for col in columns_order:
        if col not in df.columns:
            df[col] = 0

    # 최종 데이터프레임 완성
    df_final = df[columns_order]

    # 파일명 결정
    type_str = "Stock" if is_stock else "ETF"
    file_name = f"{date_str}_{type_str}_V3.csv"

    local_path = os.path.join(secrets.LOCAL_DATA_PATH, file_name)
    drive_path = os.path.join(secrets.G_DRIVE_PATH, file_name)

    try:
        # csv 저장
        df_final.to_csv(local_path, index=False, encoding='utf-8-sig')
        shutil.copy2(local_path, drive_path)
    except Exception as e:
        print(f"⚠️ 저장 실패: {e}")


# [추가] 기존 결과 파일 불러오기 (재시작 시 데이터 보존용)
def load_existing_results(date_str, is_stock=True):
    type_str = "Stock" if is_stock else "ETF"
    file_name = f"{date_str}_{type_str}_V3.csv"
    local_path = os.path.join(secrets.LOCAL_DATA_PATH, file_name)

    results = {}
    if os.path.exists(local_path):
        try:
            print(f"📂 [복구] 기존 결과 파일 읽기: {local_path}")
            df = pd.read_csv(local_path, encoding='utf-8-sig')

            # 코드 컬럼 문자열 변환 및 자릿수 맞춤
            if 'code' in df.columns:
                df['code'] = df['code'].astype(str).str.zfill(6)

            # DataFrame을 딕셔너리로 변환 {종목코드: 정보}
            results = {row['code']: row for row in df.to_dict('records')}
            print(f"   ✅ {len(results)}개 종목 기록 복구 완료!")
        except Exception as e:
            print(f"   ⚠️ 복구 실패 (파일 새로 생성됨): {e}")

    return results

# ====== 메인 루프 ======
if __name__ == "__main__":
    print("\n🚀 [Realtime Monitor V3] 시스템 시작 (개선된 구조 C:)")

    # 폴더 생성 확인
    if not os.path.exists(secrets.LOCAL_DATA_PATH):
        os.makedirs(secrets.LOCAL_DATA_PATH)
        print(f"📂 로컬 데이터 폴더 생성: {secrets.LOCAL_DATA_PATH}")

    # 1. 토큰 발급
    token = auth.get_access_token()
    if not token:
        print("❌ 토큰 발급 실패. 프로그램을 종료합니다.")
        sys.exit(1)

    # 2. 모델 로드
    models, max_lb = load_v3_models()
    if not models:
        print("❌ 로드된 모델이 없습니다.")
        sys.exit(1)

    # 3. 대상 파일 목록 (G드라이브에서 읽어서 로컬로 복사 후 시작 권장)
    # 여기서는 원본(G:)을 읽고 저장만 C:와 G:에 하는 방식으로 함
    files = sorted([f for f in os.listdir(DATA_DIR) if f.startswith("A") and f.endswith(".csv")])
    print(f"📂 분석 대상: {len(files)}개 종목")

    today_str = datetime.now().strftime("%Y%m%d")
    target_date = pd.to_datetime(today_str).normalize()

    today_results = load_existing_results(today_str, is_stock=False)
    sent_codes = {code for code, info in today_results.items() if info.get('score_total', 0) >= 0.5}

    STOCK_KIND_CACHE = {}

    try:
        while True:
            # 시장 지수 확인
            k_val = inquiry.fetch_index_change("0001")
            kq_val = inquiry.fetch_index_change("1001")
            print(
                f"\n📊 [Cycle] {datetime.now().strftime('%H:%M:%S')} KOSPI: {k_val * 100:.2f}%, KOSDAQ: {kq_val * 100:.2f}%")

            for idx, filename in enumerate(files, 1):
                code = filename[1:7]
                file_path = os.path.join(DATA_DIR, filename)

                # 분석 수행
                res = process_stock(file_path, code, target_date, k_val, kq_val, models, max_lb)

                if res:

                    # 결과 딕셔너리에 추가 정보 담기
                    if 'market_cap' not in res:
                        res['market_cap'] = inquiry.fetch_market_cap(code)
                    res['time'] = datetime.now().strftime("%H:%M:%S")

                    # 딕셔너리에 저장
                    today_results[code] = res

                    # ✅ [수정완료] 키 이름을 score_total, close_price로 변경
                    if res['score_total'] >= 0.5 and code not in sent_codes:
                        type_str = "ETF" if "main_etf" in sys.argv[0] else "주식"  # (선택사항)

                        msg = f"🚀 [포착] {res['name']} ({code})\n점수: {res['score_total']:.2f}\n현재가: {res['close_price']:,}"
                        print(f"   🔔 {msg.replace(chr(10), ' ')}")
                        send_telegram(msg)
                        sent_codes.add(code)

                # 진행상황 및 중간 저장 (50종목마다)
                if idx % 50 == 0:
                    print(f"   ⏳ {idx}/{len(files)} 완료...")
                    save_and_backup_results(list(today_results.values()), today_str)
                    gc.collect()

            # 사이클 종료 후 저장
            save_and_backup_results(list(today_results.values()), today_str)
            print("✅ 사이클 완료. 1분 대기.")
            time.sleep(60)

    except KeyboardInterrupt:
        print("\n🛑 사용자 종료")