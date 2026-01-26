import os
import sys
import time
import pandas as pd
import numpy as np
import tensorflow as tf
from datetime import datetime
import csv
import warnings

# 1. 설정: CPU 모드
os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

warnings.filterwarnings("ignore")

# 2. 모듈 불러오기
from config import secrets
from kis_api import auth, inquiry, indicators
from tensorflow.keras.models import load_model
import pickle

# ====== [환경 설정] ======
MODEL_DIR = secrets.V3_MODEL_DIR
DATA_DIR_STOCK = r"C:\Projects\RealtimeMonitor\Data\Stock"
DATA_DIR_ETF = r"C:\Projects\RealtimeMonitor\Data\ETF"
LOG_DIR = r"C:\Projects\RealtimeMonitor\logs"

# 🎯 추적 기준 점수 (자동 발굴 종목에만 적용)
TARGET_SCORE = 0.2
# ⏱️ 사이클 대기 시간
CYCLE_DELAY = 30

# V3 모델 세팅
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


# ====== 함수 정의 ======

def load_v3_models():
    """모델 로드"""
    models = {}
    print(f"📂 [Tracker] 모델 로딩 중...")
    for m_name, settings in MODEL_SETTINGS.items():
        try:
            m_path = os.path.join(MODEL_DIR, f"{m_name}_lstm_v3.h5")
            s_path = os.path.join(MODEL_DIR, f"{m_name}_lstm_v3.scaler")
            if os.path.exists(m_path) and os.path.exists(s_path):
                model = load_model(m_path)
                with open(s_path, 'rb') as f: scaler = pickle.load(f)
                models[m_name] = {
                    "model": model, "scaler": scaler,
                    "lookback": settings['lb'], "threshold": settings['thr'],
                    "weight": settings['weight'], "type": "surge" if "target" in m_name else "drop"
                }
        except:
            pass
    return models


def check_is_etf(code):
    """코드가 ETF인지 확인"""
    # 1. 파일 경로로 우선 확인
    if os.path.exists(os.path.join(DATA_DIR_ETF, f"A{code}.csv")): return True
    if os.path.exists(os.path.join(DATA_DIR_STOCK, f"A{code}.csv")): return False

    # 2. 파일이 없을 경우 코드 형식으로 추측 (문자가 섞여있거나 길이가 다르면 ETF일 가능성 체크 필요하나, 우선 False)
    return False


def format_code(x):
    """
    종목코드 안전 변환 함수
    - 5930.0 (엑셀 실수형) -> '005930'
    - 5930 (숫자) -> '005930'
    - 0050E0 (문자 포함 ETF) -> '0050E0' (그대로 유지)
    """
    s = str(x).strip()
    try:
        # 1. 엑셀 실수형 케이스 (예: 5930.0 -> 5930 -> 005930)
        if s.replace('.', '', 1).isdigit() and '.' in s:
            return str(int(float(s))).zfill(6)

        # 2. 순수 숫자로만 구성된 경우 (예: 5930 -> 005930)
        if s.isdigit():
            return s.zfill(6)

        # 3. 문자가 섞인 경우 (ETF 등) -> 그대로 반환
        return s
    except:
        return s


def get_all_targets_and_history(today_str):
    """
    Returns:
        targets: 추적할 전체 종목 (dict)
        history_set: 검색 기록 종목 (set) -> 무조건 추적 + 화면 출력용
    """
    targets = {}
    history_set = set()

    files = {
        'Stock': os.path.join(LOG_DIR, f"{today_str}_Stock_V3.csv"),
        'History': os.path.join(LOG_DIR, f"{today_str}_Search_History.csv")
    }

    # 1. Stock Log 읽기 (메인 봇이 찾은 것 -> 점수 필터 적용)
    if os.path.exists(files['Stock']):
        try:
            # 🛠️ [수정] dtype=str 옵션으로 '0050E0'가 숫자로 자동 변환되는 것을 방지
            df = pd.read_csv(files['Stock'], encoding='utf-8-sig', dtype=str)
            df = df.dropna(subset=['code'])

            # 점수 비교를 위해 score_total만 숫자로 변환
            if 'score_total' in df.columns:
                df['score_total'] = pd.to_numeric(df['score_total'], errors='coerce').fillna(0)

                codes = df[df['score_total'] >= TARGET_SCORE]['code'].apply(format_code).tolist()
                for c in codes: targets[c] = False
        except Exception as e:
            print(f"⚠️ Stock Log 읽기 오류: {e}")

    # 2. Search History 읽기 (사용자가 검색한 것 -> 점수 무관하게 전부 가져옴)
    if os.path.exists(files['History']):
        try:
            # 🛠️ [수정] dtype=str 필수!
            df = pd.read_csv(files['History'], encoding='utf-8-sig', dtype=str)

            if 'code' in df.columns:
                df = df.dropna(subset=['code'])
                codes = df['code'].apply(format_code).tolist()

                for c in codes:
                    history_set.add(c)
                    # targets에 없다면 추가 (ETF 여부 체크)
                    if c not in targets:
                        targets[c] = check_is_etf(c)
            else:
                print("⚠️ History 파일에 'code' 컬럼이 없습니다.")
        except Exception as e:
            print(f"⚠️ History Log 읽기 오류: {e}")

    return targets, history_set


def update_split_logs(stock_results, etf_results, today_str):
    """결과를 분산 저장"""

    def _save_to_file(file_path, data_list):
        if not data_list: return

        fieldnames = [
            'code', 'name', 'close_price', 'market_cap', 'score_total',
            'net_hits', 'surge_hits', 'drop_hits', 'time',
            'target1', 'target5', 'target20',
            'drop1', 'drop5', 'drop20'
        ]

        if os.path.exists(file_path):
            try:
                # 🛠️ [수정] 읽을 때도 문자열로 읽어야 기존 코드가 안 깨짐
                df = pd.read_csv(file_path, encoding='utf-8-sig', dtype=str)
                df['code'] = df['code'].apply(format_code)
            except:
                df = pd.DataFrame(columns=fieldnames)
        else:
            df = pd.DataFrame(columns=fieldnames)

        # 새 데이터 업데이트
        for row in data_list:
            code = row['code']
            # 기존 데이터프레임에 해당 코드가 있는지 확인
            idx = df.index[df['code'] == code].tolist()

            if idx:
                # 있으면 업데이트
                for col, val in row.items():
                    if col in df.columns:
                        df.at[idx[0], col] = val
            else:
                # 없으면 추가
                df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)

        df.to_csv(file_path, index=False, encoding='utf-8-sig')

    stock_path = os.path.join(LOG_DIR, f"{today_str}_Stock_V3.csv")
    if stock_results: _save_to_file(stock_path, stock_results)

    etf_path = os.path.join(LOG_DIR, f"{today_str}_ETF_V3.csv")
    if etf_results: _save_to_file(etf_path, etf_results)


# ====== 메인 루프 ======

def run_updater():
    if not auth.get_access_token():
        print("❌ 토큰 발급 실패");
        return

    models = load_v3_models()
    if not models: print("❌ 모델 로드 실패"); return

    max_lb = max([m['lookback'] for m in models.values()])
    print(f"\n🚀 [Promising Updater] 통합 추적 시작")
    print(f"   - 자동발굴: 점수 {TARGET_SCORE}점 이상만 추적")
    print(f"   - 검색기록: 점수 상관없이 🔍 무조건 추적")
    print(f"   - 주기: {CYCLE_DELAY}초\n")

    while True:
        try:
            today_str = datetime.now().strftime("%Y%m%d")

            # 1. 시장 지수 최신화
            k_val = inquiry.fetch_index_change("0001")
            kq_val = inquiry.fetch_index_change("1001")

            # 2. 타겟 리스트 (검색기록 무조건 포함)
            targets, history_codes = get_all_targets_and_history(today_str)

            if not targets:
                print(f"⏳ [{datetime.now().strftime('%H:%M:%S')}] 유망 종목 없음. 대기 중...")
                time.sleep(CYCLE_DELAY)
                continue

            # 출력: 요약 정보 (코스닥 지수 추가)
            print(
                f"🔥 [Cycle Start] 전체 대상: {len(targets)}개 (검색기록: {len([c for c in targets if c in history_codes])}개) | KOSPI {k_val * 100:+.2f}% | KOSDAQ {kq_val * 100:+.2f}%")

            results_stock = []
            results_etf = []

            for code, is_etf in targets.items():
                try:
                    # (1) 실시간 데이터 수집
                    rt = inquiry.fetch_realtime_price(code)
                    if not rt: continue

                    curr = inquiry.safe_int(rt.get("stck_prpr"))
                    oprc = inquiry.safe_int(rt.get("stck_oprc"))
                    if oprc == 0: oprc = curr
                    vol = inquiry.safe_int(rt.get("acml_vol"))

                    target_date = pd.Timestamp.today().normalize()
                    prog = inquiry.fetch_program_today(code, target_date.strftime('%Y%m%d'))
                    p_net, p_ratio = 0, 0.0
                    if prog:
                        p_net = inquiry.safe_int(prog.get("whol_smtn_ntby_qty"))
                        p_tot = inquiry.safe_int(prog.get("acml_vol"))
                        if p_tot > 0:
                            p_ratio = round((inquiry.safe_int(prog.get("whol_smtn_shnu_vol")) + inquiry.safe_int(
                                prog.get("whol_smtn_seln_vol"))) / p_tot, 4)

                    # (2) 파일 읽기 (경로 자동 보정 기능 포함)
                    base_dir = DATA_DIR_ETF if is_etf else DATA_DIR_STOCK
                    file_path = os.path.join(base_dir, f"A{code}.csv")

                    # 파일이 예상 경로에 없으면 반대 경로에서 한 번 더 찾음 (Stock <-> ETF 구분 오류 대비)
                    if not os.path.exists(file_path):
                        alt_dir = DATA_DIR_STOCK if is_etf else DATA_DIR_ETF
                        alt_path = os.path.join(alt_dir, f"A{code}.csv")
                        if os.path.exists(alt_path):
                            file_path = alt_path
                            is_etf = not is_etf  # 실제 파일 위치에 맞춰 속성 수정
                        else:
                            # 파일이 아예 없으면 건너뜀
                            continue

                    df = pd.read_csv(file_path, encoding='utf-8-sig')
                    df['date'] = pd.to_datetime(df['date'])
                    stock_name = df['name'].iloc[0] if 'name' in df.columns else code

                    # (3) 데이터 병합 및 계산
                    df = df[df['date'] != target_date]
                    today_row = {
                        "date": target_date, "code": code, "name": stock_name,
                        "open": oprc, "high": inquiry.safe_int(rt.get("stck_hgpr")),
                        "low": inquiry.safe_int(rt.get("stck_lwpr")),
                        "close": curr, "volume": vol,
                        "change_pct": (curr / oprc - 1) if oprc > 0 else 0,
                        "kospi_change": k_val, "kosdaq_change": kq_val,
                        "prog_net_qty": p_net, "prog_ratio_vol": p_ratio
                    }
                    df = pd.concat([df, pd.DataFrame([today_row])]).sort_values('date').reset_index(drop=True)

                    df = indicators.calculate_indicators_v3_save(df)
                    if 'prog_net_ratio' not in df.columns:
                        df['prog_net_ratio'] = df.apply(
                            lambda x: x['prog_net_qty'] / x['volume'] if x['volume'] > 0 else 0, axis=1)
                    for col in V3_FEATURES:
                        if col not in df.columns: df[col] = 0.0
                    df = df.fillna(0)

                    # (4) 파일 저장
                    df.to_csv(file_path, index=False, encoding='utf-8-sig')

                    # (5) 예측
                    if len(df) < max_lb: continue

                    s_sum, d_sum = 0.0, 0.0
                    s_hits, d_hits = 0, 0
                    res_probs = {}

                    for m_name, info in models.items():
                        window = df.iloc[-info['lookback']:][V3_FEATURES].values
                        win_scaled = info['scaler'].transform(window).reshape(1, info['lookback'], len(V3_FEATURES))
                        tensor_input = tf.convert_to_tensor(win_scaled, dtype=tf.float32)
                        prob = float(info['model'](tensor_input, training=False)[0, 0])
                        res_probs[m_name] = round(prob, 4)
                        if prob > info['threshold']:
                            if info['type'] == "surge":
                                s_sum += prob * info['weight'];
                                s_hits += 1
                            else:
                                d_sum += prob * info['weight'];
                                d_hits += 1

                    total_score = round(s_sum - d_sum, 4)
                    cap = inquiry.fetch_market_cap(code)

                    # 🟢 [출력] 검색 기록에 있는 종목만 콘솔 출력 (점수 낮아도 표시됨)
                    if code in history_codes:
                        print(f"   🔍 [{code}] {stock_name:<8} | 점수: {total_score:.4f} | 현재가: {curr:,}원")

                    # (6) 결과 리스트 추가
                    result_row = {
                        'code': code, 'name': stock_name,
                        'close_price': curr, 'market_cap': cap,
                        'score_total': total_score,
                        'net_hits': s_hits - d_hits,
                        'surge_hits': s_hits, 'drop_hits': d_hits,
                        'time': datetime.now().strftime("%H:%M:%S"),
                        'target1': res_probs.get('target1', 0),
                        'target5': res_probs.get('target5', 0),
                        'target20': res_probs.get('target20', 0),
                        'drop1': res_probs.get('drop1', 0),
                        'drop5': res_probs.get('drop5', 0),
                        'drop20': res_probs.get('drop20', 0)
                    }

                    if is_etf:
                        results_etf.append(result_row)
                    else:
                        results_stock.append(result_row)

                except Exception as e:
                    # 🛠️ 에러 숨기지 않고 출력 (단, 멈추지는 않음)
                    print(f"   ❌ 오류 발생 [{code}]: {e}")
                    continue

            # 3. 로그 업데이트 (메인 파일들)
            update_split_logs(results_stock, results_etf, today_str)
            time.sleep(CYCLE_DELAY)

        except KeyboardInterrupt:
            print("\n🛑 중단됨")
            break
        except Exception as e:
            print(f"⚠️ 런타임 에러: {e}")
            time.sleep(30)


if __name__ == "__main__":
    run_updater()