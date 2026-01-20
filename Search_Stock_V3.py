import os
import sys
import time
import csv
import pandas as pd
import numpy as np
import tensorflow as tf
from datetime import datetime

# 1. CPU 모드 강제 및 경고 끄기
os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
import warnings

warnings.filterwarnings("ignore")

# 2. 모듈 불러오기 (구조 변경 반영)
from config import secrets
from kis_api import auth, inquiry, indicators
from tensorflow.keras.models import load_model
import pickle

# =================================================================================
# 🛠️ [설정] V3 모델 설정 (main_stock.py와 동일하게 맞춤)
# =================================================================================

# secrets.py에 저장된 모델 경로 사용
MODEL_DIR = secrets.V3_MODEL_DIR
DATA_DIR_STOCK = r"C:\Projects\RealtimeMonitor\Data\Stock"  # 기본 주식 폴더
DATA_DIR_ETF = r"C:\Projects\RealtimeMonitor\Data\ETF"  # 기본 ETF 폴더

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

def ensure_data_ready(code, name, is_etf):
    # 1. 파일 찾기 (Stock 폴더 -> ETF 폴더 순서)
    base_path = DATA_DIR_ETF if is_etf else DATA_DIR_STOCK
    file_path = os.path.join(base_path, f"A{code}.csv")

    # 2. 파일이 없으면 다른 폴더도 찾아봄 (혹시 모르니까)
    if not os.path.exists(file_path):
        other_path = os.path.join(DATA_DIR_STOCK if is_etf else DATA_DIR_ETF, f"A{code}.csv")
        if os.path.exists(other_path):
            file_path = other_path

    # 3. 파일 읽기
    if os.path.exists(file_path):
        try:
            df = pd.read_csv(file_path, encoding='utf-8-sig')
            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
            if name != "Unknown":
                df['name'] = name
            return df, file_path
        except Exception:
            pass

    # 4. 파일도 없으면 API로 과거 데이터 다운로드 (비상용)
    print(f"   ↳ 📂 {code} 데이터 파일이 없습니다. (과거 데이터 다운로드 시도)")
    # 이 부분은 inquiry.py에 fetch_chart_history_force 함수가 구현되어 있어야 사용 가능
    # 현재는 빈 데이터프레임 리턴
    return pd.DataFrame(), file_path


def custom_sort_key(key):
    prio = 0 if 'target' in key else 1
    try:
        num = int(''.join(filter(str.isdigit, key)))
    except:
        num = 999
    return (prio, num)


# ====== 메인 실행 함수 ======
def run_manual_analysis():
    # 1. 토큰 발급
    if not auth.get_access_token():
        print("❌ 토큰 발급 실패")
        return
    print(f"🔐 토큰 준비 완료")

    # 2. 모델 로드
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
        code = input("\n👉 종목코드(6자리) 입력 (종료: q): ").strip()
        if code.lower() == 'q': break
        if not (code.isdigit() and len(code) == 6):
            print("⚠️ 올바른 6자리 코드를 입력하세요.")
            continue

        print(f"⏳ [{code}] 데이터 수집 및 분석 중...")

        # 1. API 데이터 (시장지수)
        k_val = inquiry.fetch_index_change("0001")

        # 2. 종목 정보 조회 (ETF 여부 확인)
        kind = inquiry.fetch_stock_kind(code)
        is_etf = (kind in ["ETF", "ETN"])

        # 3. 실시간 시세 조회
        rt = inquiry.fetch_realtime_price(code)
        if not rt:
            print("❌ 실시간 시세 조회 실패 (종목코드 확인 필요)")
            continue

        curr = inquiry.safe_int(rt.get("stck_prpr"))
        oprc = inquiry.safe_int(rt.get("stck_oprc"))
        if oprc == 0: oprc = curr

        # 종목명이 비어있으면 전용 API로 다시 조회
        stock_name = rt.get("hts_kor_isnm", "").strip()
        if not stock_name:
            stock_name = inquiry.fetch_stock_name(code)

        # 4. 프로그램 매매 조회
        target_date = pd.Timestamp.today().normalize()
        prog = inquiry.fetch_program_today(code, target_date.strftime('%Y%m%d'))
        p_net, p_ratio = 0, 0.0
        if prog:
            p_net = inquiry.safe_int(prog.get("whol_smtn_ntby_qty"))
            p_tot = inquiry.safe_int(prog.get("acml_vol"))
            if p_tot > 0:
                p_ratio = round((inquiry.safe_int(prog.get("whol_smtn_shnu_vol")) + inquiry.safe_int(
                    prog.get("whol_smtn_seln_vol"))) / p_tot, 4)

        # 5. 데이터 파일 읽기
        df, file_path = ensure_data_ready(code, stock_name, is_etf)

        # 🔴 [추가] API에서 이름을 못 가져왔다면 파일에서라도 가져와라!
        if not stock_name and not df.empty:
            stock_name = df['name'].iloc[0]

        if df.empty:
            print("❌ 과거 데이터 파일(csv)이 없습니다.")
            continue

        # 오늘 날짜 중복 제거 후 합치기
        df = df[df['date'] != target_date]
        vol = inquiry.safe_int(rt.get("acml_vol"))

        today_row = {
            "date": target_date, "code": code, "name": stock_name,
            "open": oprc, "high": inquiry.safe_int(rt.get("stck_hgpr")),
            "low": inquiry.safe_int(rt.get("stck_lwpr")), "close": curr,
            "volume": vol,
            "change_pct": (curr / oprc - 1) if oprc > 0 else 0,
            "kospi_change": k_val, "kosdaq_change": 0.0,  # 코스닥은 생략 가능
            "prog_net_qty": p_net, "prog_ratio_vol": p_ratio
        }

        df = pd.concat([df, pd.DataFrame([today_row])]).sort_values('date').reset_index(drop=True)

        # 6. 지표 계산 (indicators.py 사용)
        df = indicators.calculate_indicators_v3_save(df)

        if 'prog_net_ratio' not in df.columns:
            df['prog_net_ratio'] = df.apply(lambda x: x['prog_net_qty'] / x['volume'] if x['volume'] > 0 else 0, axis=1)

        # 결측치 채우기
        for col in V3_FEATURES:
            if col not in df.columns: df[col] = 0.0
        df = df.fillna(0)

        # 7. 파일 업데이트 (선택 사항)
        # print(f"   💾 데이터 파일 업데이트: {file_path}")
        df.to_csv(file_path, index=False, encoding='utf-8-sig')

        # 8. 예측 실행
        res = {}
        s_score_sum, d_score_sum = 0.0, 0.0
        s_hits, d_hits = 0, 0
        error_occured = False

        # 데이터 길이 체크
        max_lb = max([m['lookback'] for m in models.values()])
        if len(df) < max_lb:
            print(f"⚠️ 데이터 부족 (필요: {max_lb}, 보유: {len(df)}) - 예측 불가")
            continue

        # 예측 루프
        for m_name, info in models.items():
            try:
                lb = info['lookback']
                thr = info['threshold']
                weight = info['weight']

                window = df.iloc[-lb:][V3_FEATURES].values
                win_scaled = info['scaler'].transform(window).reshape(1, lb, len(V3_FEATURES))
                tensor_input = tf.convert_to_tensor(win_scaled, dtype=tf.float32)

                prob = float(info['model'](tensor_input, training=False)[0, 0])
                res[m_name] = round(prob, 4)

                if prob > thr:
                    contrib = prob * weight
                    if info['type'] == "surge":
                        s_score_sum += contrib
                        s_hits += 1
                    else:
                        d_score_sum += contrib
                        d_hits += 1
            except Exception as e:
                # print(f"⚠️ {m_name} 예측 에러: {e}")
                res[m_name] = 0.0
                error_occured = True

        total_score = round(s_score_sum - d_score_sum, 4)
        net_hits = s_hits - d_hits
        cap = inquiry.fetch_market_cap(code)

        # 거래대금 (최근 20일 평균)
        avg_val = (df['close'] * df['volume']).tail(20).mean() if len(df) >= 20 else 0

        # 9. 결과 출력
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


if __name__ == "__main__":
    try:
        run_manual_analysis()
    except KeyboardInterrupt:
        print("\n🛑 프로그램 종료")