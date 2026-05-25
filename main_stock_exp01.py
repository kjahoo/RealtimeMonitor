"""
main_stock_exp01.py — Exp-01 (fold_01) 섀도 실행 스크립트
============================================================
프로덕션(main_stock.py)과 동시에 별도로 실행해 Exp-01 모델의
실전 신호를 검증한다. 자동 주문은 없고 텔레그램 알림만 발송한다.

전략 파라미터 (strategy_sweep_bear.py 결과 기준):
  buy_thresh  = 0.55  (score >= 0.55 → 매수 신호)
  sell_thresh = 0.50  (score <  0.50 → 매도 신호)
  alloc       = 10%
  MA 필터 / 손절 없음  (Sharpe 1.19, Bear 평균 +3.4%)

출력 파일: logs/{YYYYMMDD}_Exp01_V3.csv
텔레그램:  "[Exp-01 매수]" / "[Exp-01 매도]" 태그 포함
"""

import os
import sys
import gc
import time
import pickle
import warnings
import pandas as pd
import numpy as np
import tensorflow as tf
import shutil
from datetime import datetime
from tensorflow.keras.models import load_model

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
warnings.filterwarnings("ignore")

gpus = tf.config.experimental.list_physical_devices('GPU')
if gpus:
    try:
        for gpu in gpus:
            tf.config.experimental.set_memory_growth(gpu, True)
        print(f"✅ GPU 가속 활성화됨: {len(gpus)}개")
    except RuntimeError as e:
        print(e)

from config import secrets
from kis_api import auth, inquiry, indicators

# ── 경로 ──────────────────────────────────────────────────────────────────────
DATA_DIR  = r"C:\Projects\RealtimeMonitor\Data\Stock"
MODEL_DIR = secrets.V3_MODEL_DIR_EXP01   # fold_01 모델 경로

# ── Exp-01 모델 설정 (fold_01 log F1 최적값) ──────────────────────────────────
MODEL_SETTINGS = {
    "target1":  {"lb": 30, "thr": 0.5108, "weight": 0.2967},
    "target5":  {"lb": 70, "thr": 0.6555, "weight": 0.6403},
    "target20": {"lb": 85, "thr": 0.3291, "weight": 0.5752},
    "drop1":    {"lb": 20, "thr": 0.4512, "weight": 0.5098},
    "drop5":    {"lb": 60, "thr": 0.3431, "weight": 0.7612},
    "drop20":   {"lb": 45, "thr": 0.5445, "weight": 0.8813},
}

V3_FEATURES = [
    'change_pct', 'volume_ratio', 'vol_power',
    'prog_net_ratio', 'prog_ratio_vol',
    'disparity_5', 'disparity_20',
    'rsi', 'bb_p', 'bb_w', 'adx',
    'kospi_change', 'kosdaq_change'
]

# ── 전략 파라미터 ───────────────────────────────────────────────────────────
BUY_THRESH  = 0.55   # score >= 0.55 → 매수 신호
SELL_THRESH = 0.50   # score <  0.50 → 매도 신호

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


# ── 텔레그램 ────────────────────────────────────────────────────────────────
def send_telegram(msg):
    if not secrets.TELEGRAM_BOT_TOKEN:
        return
    try:
        import requests
        url = f"https://api.telegram.org/bot{secrets.TELEGRAM_BOT_TOKEN}/sendMessage"
        for chat_id in secrets.TELEGRAM_NOTIFY_IDS:
            requests.post(url, data={"chat_id": chat_id, "text": msg}, timeout=3)
    except Exception as e:
        print(f"⚠️ 텔레그램 전송 실패: {e}")


# ── 모델 로드 ────────────────────────────────────────────────────────────────
def load_exp01_models():
    models = {}
    print(f"\n📂 [Exp-01] 모델 로드 중... ({MODEL_DIR})")

    if not os.path.exists(MODEL_DIR):
        print(f"❌ 모델 경로 없음. secrets.py의 V3_MODEL_DIR_EXP01 확인 요망.")
        return {}, 0

    max_lb = 0
    for name, settings in MODEL_SETTINGS.items():
        model_path  = os.path.join(MODEL_DIR, f"{name}_lstm_v3.h5")
        scaler_path = os.path.join(MODEL_DIR, f"{name}_lstm_v3.scaler")

        if os.path.exists(model_path) and os.path.exists(scaler_path):
            try:
                model = load_model(model_path)
                model.make_predict_function()
                with open(scaler_path, 'rb') as f:
                    scaler = pickle.load(f)
                models[name] = {
                    "model":     model,
                    "scaler":    scaler,
                    "lookback":  settings['lb'],
                    "threshold": settings['thr'],
                    "weight":    settings['weight'],
                    "type":      "surge" if "target" in name else "drop",
                }
                if settings['lb'] > max_lb:
                    max_lb = settings['lb']
                print(f"   ✅ {name} (LB:{settings['lb']}, thr:{settings['thr']:.4f})")
            except Exception as e:
                print(f"   ⚠️ {name} 로드 실패: {e}")
        else:
            print(f"   ℹ️ {name} 파일 없음")

    return models, max_lb


# ── 종목 분석 ────────────────────────────────────────────────────────────────
def process_stock(file_path, code, target_date, k_val, kq_val, models, max_lb):
    try:
        df = pd.read_csv(file_path, encoding='utf-8-sig')
        df['date'] = pd.to_datetime(df['date'])
        stock_name = df['name'].iloc[0] if 'name' in df.columns else ""

        rt = inquiry.fetch_realtime_price(code)
        if not rt:
            return None
        curr = inquiry.safe_int(rt.get("stck_prpr"))
        oprc = inquiry.safe_int(rt.get("stck_oprc"))
        if oprc == 0:
            oprc = curr
        vol = inquiry.safe_int(rt.get("acml_vol"))
        if vol == 0:
            return None

        prog = inquiry.fetch_program_today(code, target_date.strftime('%Y%m%d'))
        p_net, p_ratio = 0, 0.0
        if prog:
            p_net = inquiry.safe_int(prog.get("whol_smtn_ntby_qty"))
            p_tot = inquiry.safe_int(prog.get("acml_vol"))
            if p_tot > 0:
                p_ratio = round(
                    (inquiry.safe_int(prog.get("whol_smtn_shnu_vol")) +
                     inquiry.safe_int(prog.get("whol_smtn_seln_vol"))) / p_tot, 4)

        today_row = {
            "date": target_date, "code": code, "name": stock_name,
            "open": oprc,
            "high": inquiry.safe_int(rt.get("stck_hgpr")),
            "low":  inquiry.safe_int(rt.get("stck_lwpr")),
            "close": curr, "volume": vol,
            "change_pct":    (curr / oprc - 1) if oprc > 0 else 0,
            "kospi_change":  k_val,
            "kosdaq_change": kq_val,
            "prog_net_qty":  p_net,
            "prog_ratio_vol": p_ratio,
        }

        df = pd.concat(
            [df[df['date'] != target_date], pd.DataFrame([today_row])]
        ).sort_values('date').reset_index(drop=True)

        df = indicators.calculate_indicators_v3_save(df)

        if 'ma60' not in df.columns:
            df['ma60'] = df['close'].rolling(window=60).mean()
        if 'disparity_60' not in df.columns:
            df['disparity_60'] = (df['close'] / df['ma60'] - 1).fillna(0)
        if 'bb_pos' not in df.columns:
            df['bb_pos'] = 0.0
        if 'prog_net_ratio' not in df.columns:
            df['prog_net_ratio'] = df.apply(
                lambda x: x['prog_net_qty'] / x['volume'] if x['volume'] > 0 else 0, axis=1)

        for t in ['target1', 'target5', 'target20']:
            if t not in df.columns:
                df[t] = 0.0

        df = df.fillna(0)
        for col in FINAL_COLUMNS:
            if col not in df.columns:
                df[col] = 0
        df = df[FINAL_COLUMNS]

        # Atomic write — 원본 데이터는 건드리지 않음
        # (Exp-01은 프로덕션과 같은 Data 디렉터리를 읽지만 별도 결과 파일에만 저장)

        if len(df) < max_lb:
            return None

        s_sum, d_sum = 0.0, 0.0
        s_hits, d_hits = 0, 0
        results = {}

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
            "code": code, "name": stock_name,
            "close_price": curr,
            "market_cap":  inquiry.fetch_market_cap(code),
            "score_total": round(s_sum - d_sum, 4),
            "net_hits":    s_hits - d_hits,
            "surge_hits":  s_hits,
            "drop_hits":   d_hits,
            **results
        }
    except Exception:
        return None


# ── 결과 저장 ────────────────────────────────────────────────────────────────
def save_results(results, date_str):
    if not results:
        return
    df = pd.DataFrame(results)
    if 'score_total' in df.columns:
        df = df.sort_values('score_total', ascending=False)

    columns_order = [
        'code', 'name', 'close_price', 'market_cap', 'score_total',
        'net_hits', 'surge_hits', 'drop_hits', 'time',
        'target1', 'target5', 'target20', 'drop1', 'drop5', 'drop20'
    ]
    for col in columns_order:
        if col not in df.columns:
            df[col] = 0
    df = df[columns_order]

    file_name = f"{date_str}_Exp01_V3.csv"
    local_path = os.path.join(secrets.LOCAL_DATA_PATH, file_name)
    try:
        df.to_csv(local_path, index=False, encoding='utf-8-sig')
        drive_path = os.path.join(secrets.G_DRIVE_PATH, file_name)
        shutil.copy2(local_path, drive_path)
    except Exception as e:
        print(f"⚠️ 저장 실패: {e}")


def load_existing_results(date_str):
    file_name = f"{date_str}_Exp01_V3.csv"
    local_path = os.path.join(secrets.LOCAL_DATA_PATH, file_name)
    results = {}
    if os.path.exists(local_path):
        try:
            df = pd.read_csv(local_path, encoding='utf-8-sig')
            if 'code' in df.columns:
                df['code'] = df['code'].astype(str).str.zfill(6)
            results = {row['code']: row for row in df.to_dict('records')}
            print(f"   ✅ [Exp-01] {len(results)}개 종목 기록 복구 완료")
        except Exception as e:
            print(f"   ⚠️ [Exp-01] 복구 실패: {e}")
    return results


# ── 메인 루프 ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n🧪 [Exp-01 섀도 모니터] 시작")
    print(f"   모델 : {MODEL_DIR}")
    print(f"   전략 : buy≥{BUY_THRESH}  sell<{SELL_THRESH}  alloc=10%  (알림만, 자동주문 없음)")

    if not os.path.exists(secrets.LOCAL_DATA_PATH):
        os.makedirs(secrets.LOCAL_DATA_PATH)

    token = auth.get_access_token()
    if not token:
        print("❌ 토큰 발급 실패.")
        sys.exit(1)

    models, max_lb = load_exp01_models()
    if not models:
        print("❌ 로드된 모델 없음.")
        sys.exit(1)

    files = sorted([f for f in os.listdir(DATA_DIR) if f.startswith("A") and f.endswith(".csv")])
    print(f"📂 분석 대상: {len(files)}개 종목  (max_lb={max_lb})")

    today_str   = datetime.now().strftime("%Y%m%d")
    target_date = pd.to_datetime(today_str).normalize()

    today_results = load_existing_results(today_str)

    # 세션 내 알림 추적 — 같은 신호를 중복 발송하지 않음
    sent_buy  = set()   # 이미 매수 알림 보낸 코드
    sent_sell = set()   # 이미 매도 알림 보낸 코드

    try:
        while True:
            for idx, filename in enumerate(files, 1):
                if (idx - 1) % 50 == 0:
                    k_val  = inquiry.fetch_index_change("0001")
                    kq_val = inquiry.fetch_index_change("1001")
                    print(f"\n📊 [Exp-01 지수] {idx}번 블록 — KOSPI:{k_val*100:.2f}%  KOSDAQ:{kq_val*100:.2f}%")

                code      = filename[1:7]
                file_path = os.path.join(DATA_DIR, filename)

                res = process_stock(file_path, code, target_date, k_val, kq_val, models, max_lb)
                if not res:
                    continue

                if res['market_cap'] < 1000:
                    continue

                res['time'] = datetime.now().strftime("%H:%M:%S")
                today_results[code] = res
                score = res['score_total']
                name  = res['name']
                price = res['close_price']

                # 매수 신호
                if score >= BUY_THRESH and code not in sent_buy:
                    msg = (f"🧪 [Exp-01 매수] {name} ({code})\n"
                           f"점수: {score*100:.1f}점  현재가: {price:,}원\n"
                           f"추천 비중: 10%  (알림만 / 자동주문 없음)")
                    print(f"   🔔 {msg.replace(chr(10), ' ')}")
                    send_telegram(msg)
                    sent_buy.add(code)
                    sent_sell.discard(code)

                # 매도 신호 (매수 알림을 보냈던 종목에 한해)
                elif score < SELL_THRESH and code in sent_buy and code not in sent_sell:
                    msg = (f"🧪 [Exp-01 매도] {name} ({code})\n"
                           f"점수: {score*100:.1f}점  현재가: {price:,}원\n"
                           f"전량 매도 시그널  (알림만 / 자동주문 없음)")
                    print(f"   🔔 {msg.replace(chr(10), ' ')}")
                    send_telegram(msg)
                    sent_sell.add(code)

                if idx % 50 == 0:
                    print(f"   ⏳ [Exp-01] {idx}/{len(files)} 완료")
                    save_results(list(today_results.values()), today_str)
                    gc.collect()

            save_results(list(today_results.values()), today_str)
            print(f"✅ [Exp-01] {datetime.now().strftime('%H:%M:%S')} 전 종목 완료. 1분 대기.")
            time.sleep(60)

    except KeyboardInterrupt:
        print("\n🛑 [Exp-01] 사용자 종료")
        save_results(list(today_results.values()), today_str)