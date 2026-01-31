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
DATA_DIR = r"C:\Projects\RealtimeMonitor\Data\Stock"
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


# [수정] main_stock.py 및 main_etf.py의 process_stock 함수

# 저장할 30개 컬럼 순서 (Reset 도구와 동일하게 맞춤)
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


def process_stock(file_path, code, target_date, k_val, kq_val, models, max_lb):
    try:
        # 1. 파일 읽기
        df = pd.read_csv(file_path, encoding='utf-8-sig')
        df['date'] = pd.to_datetime(df['date'])
        stock_name = df['name'].iloc[0] if 'name' in df.columns else ""

        # 2. 실시간 데이터 수집
        rt = inquiry.fetch_realtime_price(code)
        if not rt: return None
        curr = inquiry.safe_int(rt.get("stck_prpr"))
        oprc = inquiry.safe_int(rt.get("stck_oprc"))
        if oprc == 0: oprc = curr
        vol = inquiry.safe_int(rt.get("acml_vol"))
        if vol == 0: return None  # 거래량 0이면 패스

        # 3. 프로그램 매매 정보
        prog = inquiry.fetch_program_today(code, target_date.strftime('%Y%m%d'))
        p_net, p_ratio = 0, 0.0
        if prog:
            p_net = inquiry.safe_int(prog.get("whol_smtn_ntby_qty"))
            p_tot = inquiry.safe_int(prog.get("acml_vol"))
            if p_tot > 0:
                p_ratio = round((inquiry.safe_int(prog.get("whol_smtn_shnu_vol")) + inquiry.safe_int(
                    prog.get("whol_smtn_seln_vol"))) / p_tot, 4)

        # 4. 오늘 데이터 행 생성 (30개 컬럼 요소 포함)
        today_row = {
            "date": target_date, "code": code, "name": stock_name,
            "open": oprc, "high": inquiry.safe_int(rt.get("stck_hgpr")), "low": inquiry.safe_int(rt.get("stck_lwpr")),
            "close": curr, "volume": vol,
            "change_pct": (curr / oprc - 1) if oprc > 0 else 0,
            "kospi_change": k_val, "kosdaq_change": kq_val,
            "prog_net_qty": p_net, "prog_ratio_vol": p_ratio
        }

        # 5. 합치기
        df = pd.concat([df[df['date'] != target_date], pd.DataFrame([today_row])]).sort_values('date').reset_index(
            drop=True)

        # 6. 기술적 지표 계산 (V3 기본)
        df = indicators.calculate_indicators_v3_save(df)

        # 7. [추가] V3 함수에 없는 나머지 컬럼 수동 계산 (Reset 파일과 동기화)
        if 'ma60' not in df.columns: df['ma60'] = df['close'].rolling(window=60).mean()
        if 'disparity_60' not in df.columns: df['disparity_60'] = (df['close'] / df['ma60'] - 1).fillna(0)
        if 'bb_pos' not in df.columns: df['bb_pos'] = 0.0  # 일단 0으로 처리
        if 'prog_net_ratio' not in df.columns:
            df['prog_net_ratio'] = df.apply(lambda x: x['prog_net_qty'] / x['volume'] if x['volume'] > 0 else 0, axis=1)

        # Target (실시간 봇에서는 미래를 알 수 없으므로 0으로 둠)
        for t in ['target1', 'target5', 'target20']:
            if t not in df.columns: df[t] = 0.0

        # 결측치 채우기
        df = df.fillna(0)

        # 8. [핵심] 컬럼 순서 강제 적용 (30개)
        for col in FINAL_COLUMNS:
            if col not in df.columns: df[col] = 0
        df = df[FINAL_COLUMNS]  # 순서 재배열

        # 9. [수정] 안전한 저장 (Atomic Write)
        # 파일을 직접 덮어쓰지 않고 .tmp 파일에 쓴 뒤 교체합니다.
        # 이렇게 하면 쓰기 도중 프로그램이 종료되어도 원본 파일이 손상되지 않습니다.
        temp_file_path = file_path + ".tmp"

        try:
            # 1) 임시 파일에 먼저 저장
            df.to_csv(temp_file_path, index=False, encoding='utf-8-sig')

            # 2) 임시 파일이 정상적으로 생성되었는지 확인 후 원본과 교체
            if os.path.exists(temp_file_path):
                os.replace(temp_file_path, file_path)
        except Exception as save_err:
            print(f"   ❌ 파일 저장 중 오류 발생 ({code}): {save_err}")
            # 저장 실패 시 임시 파일 삭제 시도 (청소)
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            return None  # 저장 실패 시 None 반환하여 진행 막음

        # 10. 예측 진행
        if len(df) < max_lb: return None

        s_sum, d_sum = 0.0, 0.0
        s_hits, d_hits = 0, 0
        results = {}

        for model_name, info in models.items():
            lb = info['lookback']
            # 학습 때 썼던 Feature만 뽑아서 예측에 사용
            window = df.iloc[-lb:][V3_FEATURES].values
            win_scaled = info['scaler'].transform(window).reshape(1, lb, len(V3_FEATURES))
            tensor_input = tf.convert_to_tensor(win_scaled, dtype=tf.float32)
            prob = float(info['model'](tensor_input, training=False)[0, 0])

            results[model_name] = round(prob, 4)
            if prob > info['threshold']:
                if info['type'] == "surge":
                    s_sum += prob * info['weight'];
                    s_hits += 1
                else:
                    d_sum += prob * info['weight'];
                    d_hits += 1

        return {
            "code": code, "name": stock_name,
            "close_price": curr, "market_cap": inquiry.fetch_market_cap(code),
            "score_total": round(s_sum - d_sum, 4),
            "net_hits": s_hits - d_hits, "surge_hits": s_hits, "drop_hits": d_hits,
            **results
        }
    except Exception as e:
        # print(f"Error {code}: {e}") # 디버깅용
        return None


# ====== 결과 저장 및 백업 ======
def save_and_backup_results(results, date_str, is_stock=True):
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


# [추가] 원본 데이터 안전 백업 함수
def backup_source_data(source_dir, date_str, subdir_name):
    """프로그램 시작 전 원본 데이터를 Backup 폴더로 복사합니다."""
    backup_root = os.path.join(secrets.LOCAL_DATA_PATH, "Backup", date_str, subdir_name)

    if not os.path.exists(backup_root):
        os.makedirs(backup_root)

    print(f"📂 [안전장치] 원본 데이터 백업 시작... -> {backup_root}")

    cnt = 0
    try:
        for f in os.listdir(source_dir):
            if f.endswith(".csv"):
                src = os.path.join(source_dir, f)
                dst = os.path.join(backup_root, f)
                # 백업 폴더에 파일이 없을 때만 복사 (중복 복사 방지)
                if not os.path.exists(dst):
                    shutil.copy2(src, dst)
                    cnt += 1
        print(f"   ✅ {cnt}개 파일 백업 완료.")
    except Exception as e:
        print(f"   ⚠️ 백업 중 오류 발생: {e}")


# ====== 메인 루프 ======
if __name__ == "__main__":
    print("\n🚀 [Realtime Monitor V3] 시스템 시작 (개선된 구조 C:)")

    # 폴더 생성 확인
    if not os.path.exists(secrets.LOCAL_DATA_PATH):
        os.makedirs(secrets.LOCAL_DATA_PATH)
        print(f"📂 로컬 데이터 폴더 생성: {secrets.LOCAL_DATA_PATH}")

    # ==========================================
    # ✅ [추가] 시작 전 원본 데이터 백업 실행
    # ==========================================
    today_str = datetime.now().strftime("%Y%m%d")

    # 현재 파일이 Stock인지 ETF인지 경로를 보고 판단하여 백업 폴더명 지정
    data_type_name = "Stock" if "Stock" in DATA_DIR else "ETF"
    backup_source_data(DATA_DIR, today_str, data_type_name)
    # ==========================================

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

    today_results = load_existing_results(today_str, is_stock=True)
    sent_codes = {code for code, info in today_results.items() if info.get('score_total', 0) >= 0.5}

    STOCK_KIND_CACHE = {}

    try:
        while True:
            # [이전 위치] 여기서 지수를 한 번만 가져오던 것을 아래 for 루프 안으로 이동합니다.

            for idx, filename in enumerate(files, 1):
                # ✅ [수정] 1번 종목(시작) 및 50개 종목마다 지수 데이터 갱신
                if (idx - 1) % 50 == 0:
                    k_val = inquiry.fetch_index_change("0001")  # 코스피
                    kq_val = inquiry.fetch_index_change("1001")  # 코스닥
                    print(f"\n📊 [지수 갱신] {idx}번 종목 블록 - KOSPI: {k_val * 100:.2f}%, KOSDAQ: {kq_val * 100:.2f}%")

                code = filename[1:7]
                file_path = os.path.join(DATA_DIR, filename)

                # 분석 수행
                res = process_stock(file_path, code, target_date, k_val, kq_val, models, max_lb)

                if res:
                    # 1. 시가총액 필터 (1000억 이상) - process_stock 내부 혹은 외부에서 체크
                    # res에 이미 fetch_market_cap이 포함되어 있으므로 중복 호출 방지를 위해 res 사용
                    if res['market_cap'] < 1000:
                        continue

                    # 2. 분석 시간 기록 및 결과 업데이트
                    res['time'] = datetime.now().strftime("%H:%M:%S")
                    today_results[code] = res

                    # 3. 알림 조건 체크 (점수 0.5 이상 및 미전송 종목)
                    if res['score_total'] >= 0.5 and code not in sent_codes:
                        msg = f"🚀 [포착] {res['name']} ({code})\n점수: {res['score_total']:.2f}\n현재가: {res['close_price']:,}"
                        print(f"   🔔 {msg.replace(chr(10), ' ')}")
                        send_telegram(msg)
                        sent_codes.add(code)

                # 4. 진행상황 출력 및 중간 저장 (50종목마다)
                if idx % 50 == 0:
                    print(f"   ⏳ {idx}/{len(files)} 완료 및 데이터 백업 중...")
                    save_and_backup_results(list(today_results.values()), today_str)
                    gc.collect()

            # 모든 종목(한 사이클) 완료 후 최종 저장
            save_and_backup_results(list(today_results.values()), today_str)
            print(f"✅ {datetime.now().strftime('%H:%M:%S')} 전 종목 분석 완료. 1분 대기.")
            time.sleep(60)

    except KeyboardInterrupt:
        print("\n🛑 사용자 종료")