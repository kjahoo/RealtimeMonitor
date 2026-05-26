"""
Update_Promising_Stocks_exp01.py — Exp-01 섀도 트래커
=======================================================
Update_Promising_Stocks.py(프로덕션)와 동시에 실행해
Exp-01(fold_01) 모델 기준 매도 시그널을 비교·검증한다.

주요 차이점:
  - 모델    : fold_01 (V3_MODEL_DIR_EXP01)
  - 매수기준 : score >= 0.55
  - 매도기준 : score <  0.50  (단일 임계값, 전량매도)
  - 데이터 파일 쓰기 없음 (프로덕션과 충돌 방지)
  - 텔레그램 : [Exp-01] 태그 포함
  - 상태 파일: last_scores_exp01.json (프로덕션과 분리)
  - 추적 소스: {date}_Exp01_V3.csv  (main_stock_exp01.py 출력)
              + {date}_Search_History.csv (사용자 검색 기록)
"""

import os
import sys
import json
import time
import pickle
import warnings
import requests
import pandas as pd
import numpy as np
import tensorflow as tf
from datetime import datetime
from tensorflow.keras.models import load_model

os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
warnings.filterwarnings("ignore")

from config import secrets
from kis_api import auth, inquiry, indicators

# ── 경로 ──────────────────────────────────────────────────────────────────────
MODEL_DIR      = secrets.V3_MODEL_DIR_EXP01
DATA_DIR_STOCK = r"C:\Projects\RealtimeMonitor\Data\Stock"
DATA_DIR_ETF   = r"C:\Projects\RealtimeMonitor\Data\ETF"
LOG_DIR        = r"C:\Projects\RealtimeMonitor\logs"
LAST_SCORES_FILE = os.path.join(LOG_DIR, "last_scores_exp01.json")

# ── 전략 파라미터 ───────────────────────────────────────────────────────────
BUY_THRESH   = 0.55   # score >= 0.55 → 매수 신호
SELL_THRESH  = 0.50   # score <  0.50 → 매도 시그널
TARGET_SCORE = 0.55   # Exp01 결과 파일에서 추적할 최소 점수
CYCLE_DELAY  = 30

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


# ── 텔레그램 ────────────────────────────────────────────────────────────────
def send_telegram(msg, chat_ids=None):
    if not secrets.TELEGRAM_BOT_TOKEN:
        return
    ids = chat_ids if chat_ids else secrets.TELEGRAM_NOTIFY_IDS
    try:
        url = f"https://api.telegram.org/bot{secrets.TELEGRAM_BOT_TOKEN}/sendMessage"
        for cid in ids:
            requests.post(url, data={"chat_id": cid, "text": msg}, timeout=3)
    except Exception as e:
        print(f"   ⚠️ 텔레그램 전송 실패: {e}")


# ── last_scores 영속화 ────────────────────────────────────────────────────────
def load_last_scores():
    today_str = datetime.now().strftime("%Y%m%d")
    try:
        if os.path.exists(LAST_SCORES_FILE):
            with open(LAST_SCORES_FILE, encoding='utf-8') as f:
                data = json.load(f)
            if data.get("date") == today_str:
                return data.get("scores", {})
    except Exception:
        pass
    return {}


def save_last_scores(last_scores):
    today_str = datetime.now().strftime("%Y%m%d")
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LAST_SCORES_FILE, 'w', encoding='utf-8') as f:
            json.dump({"date": today_str, "scores": last_scores}, f)
    except Exception as e:
        print(f"   ⚠️ last_scores_exp01 저장 실패: {e}")


# ── 모델 로드 ────────────────────────────────────────────────────────────────
def load_exp01_models():
    models = {}
    print(f"📂 [Exp-01 Tracker] 모델 로딩 중... ({MODEL_DIR})")
    for m_name, settings in MODEL_SETTINGS.items():
        m_path = os.path.join(MODEL_DIR, f"{m_name}_lstm_v3.h5")
        s_path = os.path.join(MODEL_DIR, f"{m_name}_lstm_v3.scaler")
        if os.path.exists(m_path) and os.path.exists(s_path):
            try:
                model = load_model(m_path)
                with open(s_path, 'rb') as f:
                    scaler = pickle.load(f)
                models[m_name] = {
                    "model":     model,
                    "scaler":    scaler,
                    "lookback":  settings['lb'],
                    "threshold": settings['thr'],
                    "weight":    settings['weight'],
                    "type":      "surge" if "target" in m_name else "drop",
                }
                print(f"   ✅ {m_name} (LB:{settings['lb']}, thr:{settings['thr']:.4f})")
            except Exception as e:
                print(f"   ⚠️ {m_name} 로드 실패: {e}")
    return models


# ── 코드 포맷 ────────────────────────────────────────────────────────────────
def format_code(x):
    s = str(x).strip()
    try:
        if s.replace('.', '', 1).isdigit() and '.' in s:
            return str(int(float(s))).zfill(6)
        if s.isdigit():
            return s.zfill(6)
    except Exception:
        pass
    return s


def check_is_etf(code):
    if os.path.exists(os.path.join(DATA_DIR_ETF,   f"A{code}.csv")): return True
    if os.path.exists(os.path.join(DATA_DIR_STOCK, f"A{code}.csv")): return False
    return False


# ── 타겟 리스트 로드 ─────────────────────────────────────────────────────────
def get_targets(today_str):
    """Exp01_V3.csv + Search_History.csv 에서 추적 대상 수집."""
    targets      = {}   # {code: is_etf}
    history_set  = set()
    history_chat = {}   # {code: set(chat_id)}

    # Exp-01 스캐너 결과 (main_stock_exp01.py 출력)
    exp01_path = os.path.join(LOG_DIR, f"{today_str}_Exp01_V3.csv")
    if os.path.exists(exp01_path):
        try:
            df = pd.read_csv(exp01_path, encoding='utf-8-sig', dtype=str)
            df = df.dropna(subset=['code'])
            if 'score_total' in df.columns:
                df['score_total'] = pd.to_numeric(df['score_total'], errors='coerce').fillna(0)
                codes = df[df['score_total'] >= TARGET_SCORE]['code'].apply(format_code).tolist()
                for c in codes:
                    targets[c] = False
        except Exception as e:
            print(f"   ⚠️ Exp01_V3 읽기 오류: {e}")

    # Search History (사용자가 직접 조회한 종목 — 점수 무관 추적)
    hist_path = os.path.join(LOG_DIR, f"{today_str}_Search_History.csv")
    if os.path.exists(hist_path):
        try:
            df = pd.read_csv(hist_path, encoding='utf-8-sig', dtype=str)
            if 'code' in df.columns:
                df = df.dropna(subset=['code'])
                for _, row in df.iterrows():
                    c = format_code(row['code'])
                    history_set.add(c)
                    if c not in targets:
                        targets[c] = check_is_etf(c)
                    cid = str(row.get('chat_id', '')).strip()
                    if cid:
                        history_chat.setdefault(c, set()).add(cid)
        except Exception as e:
            print(f"   ⚠️ Search_History 읽기 오류: {e}")

    return targets, history_set, history_chat


# ── 메인 루프 ────────────────────────────────────────────────────────────────
def run_updater():
    if not auth.get_access_token():
        print("❌ 토큰 발급 실패")
        return

    models = load_exp01_models()
    if not models:
        print("❌ 모델 로드 실패")
        return

    max_lb       = max(m['lookback'] for m in models.values())
    market_mode  = os.environ.get("MARKET_MODE", "KRX")

    print(f"\n🧪 [Exp-01 Tracker] 시작 (모드: {market_mode})")
    print(f"   전략: buy≥{BUY_THRESH}  sell<{SELL_THRESH}  (알림만 / 자동주문 없음)")
    print(f"   추적 소스: {{date}}_Exp01_V3.csv  +  Search_History.csv\n")

    last_scores    = load_last_scores()
    nxt_skip_cache = set()

    while True:
        try:
            today_str   = datetime.now().strftime("%Y%m%d")
            market_mode = os.environ.get("MARKET_MODE", "KRX")

            if market_mode == "KRX":
                nxt_skip_cache.clear()

            k_val  = inquiry.fetch_index_change("0001")
            kq_val = inquiry.fetch_index_change("1001")

            targets, history_codes, history_chat = get_targets(today_str)

            if not targets:
                print(f"   ⏳ [{datetime.now().strftime('%H:%M:%S')}] [Exp-01] 추적 종목 없음. 대기 중...")
                time.sleep(CYCLE_DELAY)
                continue

            print(f"🧪 [Exp-01 Cycle {datetime.now().strftime('%H:%M:%S')}] "
                  f"대상: {len(targets)}개 | 검색기록: {len([c for c in targets if c in history_codes])}개 | "
                  f"KOSPI {k_val*100:+.2f}% | KOSDAQ {kq_val*100:+.2f}%")

            for code, is_etf in targets.items():
                try:
                    if code in nxt_skip_cache:
                        continue

                    # (1) 실시간 시세
                    try:
                        rt = inquiry.fetch_realtime_price(code)
                        if not rt or inquiry.safe_int(rt.get("stck_prpr", 0)) == 0:
                            if market_mode == "NXT":
                                nxt_skip_cache.add(code)
                            continue
                    except Exception:
                        continue

                    curr = inquiry.safe_int(rt.get("stck_prpr"))
                    oprc = inquiry.safe_int(rt.get("stck_oprc"))
                    if oprc == 0:
                        oprc = curr
                    vol = inquiry.safe_int(rt.get("acml_vol"))

                    if vol == 0 and market_mode == "KRX":
                        continue

                    # (2) 프로그램 매매
                    target_date = pd.Timestamp.today().normalize()
                    prog = inquiry.fetch_program_today(code, target_date.strftime('%Y%m%d'))
                    p_net, p_ratio = 0, 0.0
                    if prog:
                        p_net = inquiry.safe_int(prog.get("whol_smtn_ntby_qty"))
                        p_tot = inquiry.safe_int(prog.get("acml_vol"))
                        if p_tot > 0:
                            p_ratio = round(
                                (inquiry.safe_int(prog.get("whol_smtn_shnu_vol")) +
                                 inquiry.safe_int(prog.get("whol_smtn_seln_vol"))) / p_tot, 4)

                    # (3) 데이터 파일 읽기 (쓰기 없음 — 프로덕션 트래커가 이미 최신 상태 유지)
                    base_dir  = DATA_DIR_ETF if is_etf else DATA_DIR_STOCK
                    file_path = os.path.join(base_dir, f"A{code}.csv")
                    if not os.path.exists(file_path):
                        alt_dir  = DATA_DIR_STOCK if is_etf else DATA_DIR_ETF
                        alt_path = os.path.join(alt_dir, f"A{code}.csv")
                        if os.path.exists(alt_path):
                            file_path = alt_path
                            is_etf    = not is_etf
                        else:
                            continue

                    df = pd.read_csv(file_path, encoding='utf-8-sig')
                    df['date'] = pd.to_datetime(df['date'])
                    stock_name = df['name'].iloc[0] if 'name' in df.columns else code

                    # (4) 오늘 행 병합 (메모리에서만 — 파일 저장 없음)
                    df = df[df['date'] != target_date]
                    today_row = {
                        "date": target_date, "code": code, "name": stock_name,
                        "open": oprc,
                        "high": inquiry.safe_int(rt.get("stck_hgpr")),
                        "low":  inquiry.safe_int(rt.get("stck_lwpr")),
                        "close": curr, "volume": vol,
                        "change_pct":     (curr / oprc - 1) if oprc > 0 else 0,
                        "kospi_change":   k_val,
                        "kosdaq_change":  kq_val,
                        "prog_net_qty":   p_net,
                        "prog_ratio_vol": p_ratio,
                    }
                    df = pd.concat(
                        [df, pd.DataFrame([today_row])]
                    ).sort_values('date').reset_index(drop=True)

                    df = indicators.calculate_indicators_v3_save(df)
                    if 'prog_net_ratio' not in df.columns:
                        df['prog_net_ratio'] = df.apply(
                            lambda x: x['prog_net_qty'] / x['volume'] if x['volume'] > 0 else 0, axis=1)
                    for col in V3_FEATURES:
                        if col not in df.columns:
                            df[col] = 0.0
                    df = df.fillna(0)

                    # (5) Exp-01 예측
                    if len(df) < max_lb:
                        continue

                    s_sum, d_sum   = 0.0, 0.0
                    s_hits, d_hits = 0, 0

                    for m_name, info in models.items():
                        window     = df.iloc[-info['lookback']:][V3_FEATURES].values
                        win_scaled = info['scaler'].transform(window).reshape(
                            1, info['lookback'], len(V3_FEATURES))
                        tensor_in  = tf.convert_to_tensor(win_scaled, dtype=tf.float32)
                        prob       = float(info['model'](tensor_in, training=False)[0, 0])
                        if prob > info['threshold']:
                            if info['type'] == "surge":
                                s_sum += prob * info['weight']; s_hits += 1
                            else:
                                d_sum += prob * info['weight']; d_hits += 1

                    total_score = round(s_sum - d_sum, 4)

                    # (6) 출력 및 알림
                    # 콘솔 출력은 본인(TELEGRAM_CHAT_ID)이 등록한 종목만
                    is_my_code = (code in history_codes and
                                  secrets.TELEGRAM_CHAT_ID in history_chat.get(code, set()))

                    if is_my_code:
                        print(f"   🧪 [{code}] {stock_name:<8} | "
                              f"Exp-01 점수: {total_score:.4f} ({total_score*100:.1f}점) | "
                              f"현재가: {curr:,}원")

                    if code in history_codes:
                        prev_score = last_scores.get(code)
                        if prev_score is None:
                            last_scores[code] = total_score
                        else:
                            notify_ids = list(history_chat.get(code) or [secrets.TELEGRAM_CHAT_ID])

                            # 매도 시그널: prev >= SELL_THRESH → total < SELL_THRESH
                            if prev_score >= SELL_THRESH and total_score < SELL_THRESH:
                                msg = (f"🧪 [Exp-01 매도 시그널] {stock_name} ({code})\n"
                                       f"점수: {prev_score*100:.1f} → {total_score*100:.1f}점 (0.50선 이탈)\n"
                                       f"전량 매도 시그널 (알림만 / 자동주문 없음)\n"
                                       f"현재가: {curr:,}원")
                                if is_my_code:
                                    print(f"   🔔 {msg.replace(chr(10), '  ')}")
                                send_telegram(msg, notify_ids)

                            # 매수 신호 복귀: prev < BUY_THRESH → total >= BUY_THRESH
                            elif prev_score < BUY_THRESH and total_score >= BUY_THRESH:
                                msg = (f"🧪 [Exp-01 매수 신호] {stock_name} ({code})\n"
                                       f"점수: {prev_score*100:.1f} → {total_score*100:.1f}점 (0.55선 돌파)\n"
                                       f"추천 비중: 10% (알림만 / 자동주문 없음)\n"
                                       f"현재가: {curr:,}원")
                                if is_my_code:
                                    print(f"   🔔 {msg.replace(chr(10), '  ')}")
                                send_telegram(msg, notify_ids)

                            last_scores[code] = total_score

                except Exception as e:
                    print(f"   ❌ [Exp-01] [{code}] 오류: {e}")
                    continue

            save_last_scores(last_scores)
            time.sleep(CYCLE_DELAY)

        except KeyboardInterrupt:
            print("\n🛑 [Exp-01 Tracker] 사용자 종료")
            save_last_scores(last_scores)
            break
        except Exception as e:
            print(f"   ❌ [Exp-01 Tracker] 사이클 오류: {e}")
            time.sleep(CYCLE_DELAY)


if __name__ == "__main__":
    run_updater()