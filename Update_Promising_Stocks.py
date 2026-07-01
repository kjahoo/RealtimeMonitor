import os
import sys
import time
import json
import requests
import pandas as pd
import numpy as np
import tensorflow as tf
from datetime import datetime, timedelta
from datetime import time as dtime
import csv
import warnings

os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
warnings.filterwarnings("ignore")

from config import secrets
from kis_api import auth, inquiry, indicators, kiwoom_trading as trading
from kis_api import kiwoom_inquiry  # 시세/지수/프로그램매매를 키움 REST로 수신 (한투 부하 분리·속도 개선)
from kis_api import sell_strategy_b  # B 매도전략(3일 평활+2일 확인+-12% 손절 → 전량청산)
from tensorflow.keras.models import load_model
import pickle

# ====== [환경 설정] ======
MODEL_DIR        = secrets.V3_MODEL_DIR
DATA_DIR_STOCK   = r"C:\Projects\RealtimeMonitor\Data\Stock"
DATA_DIR_ETF     = r"C:\Projects\RealtimeMonitor\Data\ETF"
LOG_DIR          = r"C:\Projects\RealtimeMonitor\logs"
LAST_SCORES_FILE = os.path.join(LOG_DIR, "last_scores.json")

TARGET_SCORE   = 0.2
CYCLE_DELAY    = 30

# 실제 매매(주문 접수/취소/정정) 알림은 소유자(나)만 수신 — 친구는 시그널만
OWNER_IDS = [secrets.TELEGRAM_CHAT_ID]

# 매도 시그널 임계값
DROP_THRESHOLDS = [0.40, 0.35, 0.30, 0.25, 0.20, 0]

# ✅ [추가] NXT 시간대 API 타임아웃 제한
#    NXT 데이터가 없는 종목은 call_api가 재시도하며 오래 걸림
#    → 종목당 최대 대기 시간을 설정해 멈춤 방지
API_TIMEOUT_SEC = 4   # 단건 API 호출 타임아웃 (초)
MAX_FAIL_SKIP   = 3   # 연속 실패 N회면 해당 종목 이번 사이클 건너뜀

MODEL_SETTINGS = {
    "target1":  {"lb": 65, "thr": 0.5256, "weight": 0.1775},
    "target5":  {"lb": 55, "thr": 0.6484, "weight": 0.3639},
    "target20": {"lb": 95, "thr": 0.9197, "weight": 0.4586},
    "drop1":    {"lb": 80, "thr": 0.4018, "weight": 0.2544},
    "drop5":    {"lb": 85, "thr": 0.5041, "weight": 0.3376},
    "drop20":   {"lb": 85, "thr": 0.5723, "weight": 0.4079}
}

V3_FEATURES = [
    'change_pct', 'volume_ratio', 'vol_power',
    'prog_net_ratio', 'prog_ratio_vol',
    'disparity_5', 'disparity_20',
    'rsi', 'bb_p', 'bb_w', 'adx',
    'kospi_change', 'kosdaq_change'
]


# ====================================================
# 🔔 텔레그램
# ====================================================
def send_telegram(msg, chat_ids=None):
    if not secrets.TELEGRAM_BOT_TOKEN: return
    ids = chat_ids if chat_ids else secrets.TELEGRAM_NOTIFY_IDS
    try:
        url = f"https://api.telegram.org/bot{secrets.TELEGRAM_BOT_TOKEN}/sendMessage"
        for chat_id in ids:
            requests.post(url, data={"chat_id": chat_id, "text": msg}, timeout=3)
    except Exception as e:
        print(f"   ⚠️ 텔레그램 전송 실패: {e}")


# ====================================================
# 💾 last_scores 영속화 (재시작 후에도 이전 점수 유지)
# ====================================================
def load_last_scores():
    today_str = datetime.now().strftime("%Y%m%d")
    try:
        if os.path.exists(LAST_SCORES_FILE):
            with open(LAST_SCORES_FILE, encoding='utf-8') as f:
                data = json.load(f)
            # 날짜가 다르면 (전날 데이터) 초기화
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
        print(f"   ⚠️ last_scores 저장 실패: {e}")


def save_sell_plan(targets, today_str):
    """매도 sweep 계획을 logs/{날짜}_autosell_plan.json 에 원자적 기록.
       execution_monitor 가 4초마다 읽어 '매수호가 sweep' 으로 전량청산 집행한다.
       targets: {code: {name, sell_price, reason, score}}  (매 사이클 전체 재작성)"""
    path = os.path.join(LOG_DIR, f"{today_str}_autosell_plan.json")
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({"date": today_str, "targets": targets}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        print(f"   ⚠️ autosell_plan 저장 실패: {e}")


# ====================================================
# 💰 평단가 하루 1회 캐시 (B 전략 -12% 손절용)
# ====================================================
_avg_price_cache = {}   # code -> (YYYYMMDD, avg_price or None)

def get_cached_avg_price(code):
    """보유 평단가를 하루 1회만 잔고조회로 캐시 (없으면 None)."""
    today = datetime.now().strftime("%Y%m%d")
    c = _avg_price_cache.get(code)
    if c and c[0] == today:
        return c[1]
    avg = None
    try:
        hs = trading.fetch_stock_holdings(code)
        if hs:
            tq = sum(h["qty"] for h in hs)
            ta = sum(h["purchase_amount"] for h in hs)
            avg = (ta / tq) if tq > 0 else None
    except Exception:
        avg = None
    _avg_price_cache[code] = (today, avg)
    return avg


# ====================================================
# 📂 모델 로드
# ====================================================
def load_v3_models():
    models = {}
    print(f"📂 [Tracker] 모델 로딩 중...")
    for m_name, settings in MODEL_SETTINGS.items():
        try:
            m_path = os.path.join(MODEL_DIR, f"{m_name}_lstm_v3.h5")
            s_path = os.path.join(MODEL_DIR, f"{m_name}_lstm_v3.scaler")
            if os.path.exists(m_path) and os.path.exists(s_path):
                model = load_model(m_path)
                with open(s_path, 'rb') as f:
                    scaler = pickle.load(f)
                models[m_name] = {
                    "model": model, "scaler": scaler,
                    "lookback": settings['lb'], "threshold": settings['thr'],
                    "weight": settings['weight'],
                    "type": "surge" if "target" in m_name else "drop"
                }
        except Exception as e:
            print(f"   ⚠️ {m_name} 로드 실패: {e}")
    return models


# ====================================================
# 🛠️ 헬퍼
# ====================================================
def check_is_etf(code):
    if os.path.exists(os.path.join(DATA_DIR_ETF,   f"A{code}.csv")): return True
    if os.path.exists(os.path.join(DATA_DIR_STOCK, f"A{code}.csv")): return False
    return False


def format_code(x):
    s = str(x).strip()
    try:
        if s.replace('.', '', 1).isdigit() and '.' in s:
            return str(int(float(s))).zfill(6)
        if s.isdigit():
            return s.zfill(6)
        return s
    except:
        return s


# ====================================================
# ✅ [핵심 수정] 안전한 실시간 시세 조회
#    - NXT 데이터 없는 종목에서 무한 대기 방지
#    - 빈 응답이면 즉시 None 반환 (재시도 없음)
# ====================================================
def fetch_realtime_safe(code):
    """
    NXT 시간대에 데이터가 없는 종목은 빈 dict {}를 반환.
    fetch_realtime_price의 재시도 로직을 우회해
    멈춤 현상을 방지합니다.
    """
    try:
        rt = kiwoom_inquiry.fetch_realtime_price(code)
        # 현재가가 0이거나 없으면 NXT 미지원 종목으로 판단
        if not rt or inquiry.safe_int(rt.get("stck_prpr", 0)) == 0:
            return None
        return rt
    except Exception:
        return None


# ====================================================
# 📋 타겟 리스트 로드
# ====================================================
def get_all_targets_and_history(today_str):
    targets      = {}
    history_set  = set()
    history_chat = {}   # {code: set(chat_id)} — 검색한 사용자 매핑

    files = {
        'Stock':   os.path.join(LOG_DIR, f"{today_str}_Stock_V3.csv"),
        'History': os.path.join(LOG_DIR, f"{today_str}_Search_History.csv")
    }

    # Stock Log (점수 필터 적용)
    if os.path.exists(files['Stock']):
        try:
            df = pd.read_csv(files['Stock'], encoding='utf-8-sig', dtype=str)
            df = df.dropna(subset=['code'])
            if 'score_total' in df.columns:
                df['score_total'] = pd.to_numeric(df['score_total'], errors='coerce').fillna(0)
                codes = df[df['score_total'] >= TARGET_SCORE]['code'].apply(format_code).tolist()
                for c in codes:
                    targets[c] = False
        except Exception as e:
            print(f"   ⚠️ Stock Log 읽기 오류: {e}")

    # Search History (점수 무관 전부 추적, chat_id별 매핑)
    if os.path.exists(files['History']):
        try:
            df = pd.read_csv(files['History'], encoding='utf-8-sig', dtype=str)
            if 'code' in df.columns:
                df = df.dropna(subset=['code'])
                for _, row in df.iterrows():
                    c = format_code(row['code'])
                    history_set.add(c)
                    if c not in targets:
                        targets[c] = check_is_etf(c)
                    # chat_id 컬럼이 있으면 검색자 기록
                    cid = str(row.get('chat_id', '')).strip()
                    if cid:
                        history_chat.setdefault(c, set()).add(cid)
        except Exception as e:
            print(f"   ⚠️ History Log 읽기 오류: {e}")

    return targets, history_set, history_chat


# ====================================================
# 💾 로그 저장
# ====================================================
def update_split_logs(stock_results, etf_results, today_str):
    def _save_to_file(file_path, data_list):
        if not data_list: return
        fieldnames = [
            'code', 'name', 'close_price', 'market_cap', 'score_total',
            'net_hits', 'surge_hits', 'drop_hits', 'time',
            'target1', 'target5', 'target20', 'drop1', 'drop5', 'drop20'
        ]
        if os.path.exists(file_path):
            try:
                df = pd.read_csv(file_path, encoding='utf-8-sig', dtype=str)
                df['code'] = df['code'].apply(format_code)
            except:
                df = pd.DataFrame(columns=fieldnames)
        else:
            df = pd.DataFrame(columns=fieldnames)

        for row in data_list:
            code = row['code']
            idx  = df.index[df['code'] == code].tolist()
            if idx:
                for col, val in row.items():
                    if col in df.columns:
                        df.at[idx[0], col] = val
            else:
                df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)

        # 원자적 저장 (temp + os.replace) — main_stock이 읽는 중 깨진 파일 방지
        _tmp = file_path + ".tmp"
        df.to_csv(_tmp, index=False, encoding='utf-8-sig')
        os.replace(_tmp, file_path)

    stock_path = os.path.join(LOG_DIR, f"{today_str}_Stock_V3.csv")
    etf_path   = os.path.join(LOG_DIR, f"{today_str}_ETF_V3.csv")
    if stock_results: _save_to_file(stock_path, stock_results)
    if etf_results:   _save_to_file(etf_path,   etf_results)


# ====================================================
# 🔄 Search_History 점수 업데이트
# ====================================================
def update_search_history_scores(updates, today_str):
    """
    updates: {code: {'total_score': ..., 'close_price': ..., 'net_hits': ...,
                     'surge_hits': ..., 'drop_hits': ...}}
    Search_History.csv의 해당 code 행 점수·현재가를 일괄 덮어씁니다.
    """
    if not updates:
        return
    hist_path = os.path.join(LOG_DIR, f"{today_str}_Search_History.csv")
    if not os.path.exists(hist_path):
        return
    try:
        df = pd.read_csv(hist_path, encoding='utf-8-sig', dtype=str, on_bad_lines='skip')
        if 'code' not in df.columns:
            return
        df['code'] = df['code'].apply(format_code)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for code, vals in updates.items():
            mask = df['code'] == code
            if not mask.any():
                continue
            df.loc[mask, 'total_score']   = str(vals.get('total_score', ''))
            df.loc[mask, 'current_price'] = str(vals.get('close_price', ''))
            if 'market_cap' in vals:
                df.loc[mask, 'market_cap'] = str(vals.get('market_cap', ''))
            if 'change_pct' in vals:
                df.loc[mask, 'change_pct'] = str(vals.get('change_pct', ''))
            df.loc[mask, 'net_hits']      = str(vals.get('net_hits', ''))
            df.loc[mask, 'surge_hits']    = str(vals.get('surge_hits', ''))
            df.loc[mask, 'drop_hits']     = str(vals.get('drop_hits', ''))
            df.loc[mask, 'signal']        = f"Target {vals.get('surge_hits',0)} / Drop {vals.get('drop_hits',0)}"
            df.loc[mask, 'timestamp']     = now_str
        _tmp = hist_path + ".tmp"
        df.to_csv(_tmp, index=False, encoding='utf-8-sig')
        os.replace(_tmp, hist_path)
    except Exception as e:
        print(f"   ⚠️ Search_History 점수 업데이트 실패: {e}")


# ====================================================
# 🚀 메인 루프
# ====================================================
def run_updater():
    if not auth.get_access_token():
        print("❌ 토큰 발급 실패")
        return

    models = load_v3_models()
    if not models:
        print("❌ 모델 로드 실패")
        return

    max_lb       = max([m['lookback'] for m in models.values()])
    market_mode  = os.environ.get("MARKET_MODE", "KRX")

    print(f"\n🚀 [Promising Updater] 시작 (마켓 모드: {market_mode})")
    print(f"   - 자동발굴 : 점수 {TARGET_SCORE}점 이상만 추적")
    print(f"   - 검색기록 : 점수 무관 무조건 추적")
    print(f"   - 매도시그널: {DROP_THRESHOLDS} 이하 하락 시 알림")
    print(f"   - 주기      : {CYCLE_DELAY}초\n")

    last_scores      = load_last_scores()   # 재시작 후에도 이전 점수 복원
    nxt_skip_cache   = set()
    session_notified = set()  # 이번 실행에서 첫 관측 완료한 코드 (재실행 알림용)
    sell_alert_sent  = {}     # {code: keep_amount} — 소유자 자동매매 실행알림 중복 방지
    user_sell_alert  = {}     # {(chat_id, code): keep_amount} — 등록자별 매도시그널 중복 방지
    corr_notified    = {}     # {code: order_price} — 정정요망 알림 중복 방지
    prev_market_mode = None   # 모드 전환 감지용

    while True:
        try:
            now = datetime.now()

            # 장 운영 시간(평일 08:00~20:00) 외에는 대기
            if now.weekday() >= 5 or not (dtime(8, 0) <= now.time() < dtime(20, 0)):
                next_open = now.replace(hour=8, minute=0, second=0, microsecond=0)
                if now.time() >= dtime(20, 0) or now.weekday() >= 5:
                    next_open += timedelta(days=1)
                    while next_open.weekday() >= 5:
                        next_open += timedelta(days=1)
                wait_sec = max(0, int((next_open - now).total_seconds()))
                h, m = divmod(wait_sec // 60, 60)
                print(f"   😴 [{now.strftime('%H:%M:%S')}] 장 시간 외 — 다음 개장({next_open.strftime('%m/%d %H:%M')})까지 대기 ({h}시간 {m}분)")
                time.sleep(min(wait_sec, 600))  # 최대 10분 단위로 나눠서 대기
                continue

            today_str   = now.strftime("%Y%m%d")
            market_mode = os.environ.get("MARKET_MODE", "KRX")

            # 모드가 바뀌면 스킵 캐시 초기화
            # 모드 전환 시에만 캐시 초기화 (매 사이클이 아님)
            if market_mode != prev_market_mode:
                prev_market_mode = market_mode
                nxt_skip_cache.clear()
                sell_alert_sent.clear()
                corr_notified.clear()
                if market_mode == "KRX":
                    # NXT 주문은 정규장 개시 시 만료 → sell_level·open_orders 초기화
                    for _code in list(last_scores.keys()):
                        e = last_scores[_code]
                        if isinstance(e, dict):
                            e["sell_level"] = None
                            e.pop("open_orders", None)

            # 1. 시장 지수
            k_val  = kiwoom_inquiry.fetch_index_change("0001")
            kq_val = kiwoom_inquiry.fetch_index_change("1001")

            # 2. 타겟 리스트
            targets, history_codes, history_chat = get_all_targets_and_history(today_str)

            if not targets:
                print(f"   ⏳ [{datetime.now().strftime('%H:%M:%S')}] 유망 종목 없음. 대기 중...")
                time.sleep(CYCLE_DELAY)
                continue

            nxt_skip_count = len([c for c in targets if c in nxt_skip_cache])
            print(f"🔥 [Cycle {datetime.now().strftime('%H:%M:%S')}] "
                  f"대상: {len(targets)}개 | 검색기록: {len([c for c in targets if c in history_codes])}개 | "
                  f"KOSPI {k_val*100:+.2f}% | KOSDAQ {kq_val*100:+.2f}% | "
                  f"모드: {market_mode}"
                  + (f" | NXT스킵: {nxt_skip_count}개" if nxt_skip_count else ""))

            results_stock   = []
            results_etf     = []
            history_updates = {}   # {code: score 정보} — 사이클 끝에 Search_History 갱신용
            sell_plan_targets = {} # {code: 매도 sweep 목표} — 사이클 끝에 autosell_plan 기록

            for code, is_etf in targets.items():
                try:
                    # ✅ [핵심] NXT 스킵 캐시에 있으면 건너뜀
                    if code in nxt_skip_cache:
                        continue

                    # (1) 실시간 시세 — 안전 버전 사용
                    rt = fetch_realtime_safe(code)

                    if rt is None:
                        # NXT 모드에서 데이터 없는 종목 → 캐시에 등록 후 스킵
                        if market_mode == "NXT":
                            nxt_skip_cache.add(code)
                            if code in history_codes:
                                print(f"   ⏭️  [{code}] NXT 시세 없음 → 이번 사이클 스킵")
                        continue

                    curr = inquiry.safe_int(rt.get("stck_prpr"))
                    oprc = inquiry.safe_int(rt.get("stck_oprc"))
                    if oprc == 0: oprc = curr
                    vol  = inquiry.safe_int(rt.get("acml_vol"))

                    # ✅ [수정] NXT 시간대는 거래량 0이어도 진행
                    #           (프리마켓은 아직 거래 전일 수 있음)
                    if vol == 0 and market_mode == "KRX":
                        continue

                    # (2) 프로그램 매매
                    target_date = pd.Timestamp.today().normalize()
                    prog  = kiwoom_inquiry.fetch_program_today(code, target_date.strftime('%Y%m%d'))
                    p_net, p_ratio = 0, 0.0
                    if prog:
                        p_net  = inquiry.safe_int(prog.get("whol_smtn_ntby_qty"))
                        p_tot  = inquiry.safe_int(prog.get("acml_vol"))
                        if p_tot > 0:
                            p_ratio = round(
                                (inquiry.safe_int(prog.get("whol_smtn_shnu_vol")) +
                                 inquiry.safe_int(prog.get("whol_smtn_seln_vol"))) / p_tot, 4)

                    # (3) 파일 읽기 (경로 자동 보정)
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

                    df = pd.read_csv(file_path, encoding='utf-8-sig', dtype={'code': str, 'name': str}, on_bad_lines='skip')
                    df['date']   = pd.to_datetime(df['date'], errors='coerce').dt.normalize()
                    df = df.dropna(subset=['date'])
                    stock_name   = df['name'].iloc[0] if 'name' in df.columns else code

                    # (4) 오늘 데이터 병합
                    df = df[df['date'] != target_date]
                    today_row = {
                        "date": target_date, "code": code, "name": stock_name,
                        "open": oprc,
                        "high": inquiry.safe_int(rt.get("stck_hgpr")),
                        "low":  inquiry.safe_int(rt.get("stck_lwpr")),
                        "close": curr, "volume": vol,
                        "change_pct":    (curr / oprc - 1) if oprc > 0 else 0,
                        "kospi_change":  k_val,
                        "kosdaq_change": kq_val,
                        "prog_net_qty":   p_net,
                        "prog_ratio_vol": p_ratio
                    }
                    df = pd.concat([df, pd.DataFrame([today_row])]).sort_values('date').reset_index(drop=True)

                    df = indicators.calculate_indicators_v3_save(df)
                    if 'prog_net_ratio' not in df.columns:
                        df['prog_net_ratio'] = df.apply(
                            lambda x: x['prog_net_qty'] / x['volume'] if x['volume'] > 0 else 0, axis=1)
                    for col in V3_FEATURES:
                        if col not in df.columns: df[col] = 0.0
                    _non_date = [c for c in df.columns if c != 'date']
                    df[_non_date] = df[_non_date].fillna(0)

                    # (5) 파일 저장 — date: YYYY-MM-DD, code: 6자리 문자열
                    df['date'] = df['date'].dt.strftime('%Y-%m-%d')
                    df['code'] = df['code'].astype(str).apply(lambda x: x.split('.')[0].zfill(6))
                    # 원자적 저장 (temp + os.replace) — 동시 쓰기로 인한 줄바꿈 유실/파일 손상 방지
                    _tmp_path = file_path + ".tmp"
                    df.to_csv(_tmp_path, index=False, encoding='utf-8-sig')
                    os.replace(_tmp_path, file_path)

                    # (6) 예측
                    if len(df) < max_lb: continue

                    s_sum, d_sum   = 0.0, 0.0
                    s_hits, d_hits = 0, 0
                    res_probs      = {}

                    for m_name, info in models.items():
                        window     = df.iloc[-info['lookback']:][V3_FEATURES].values
                        win_scaled = info['scaler'].transform(window).reshape(1, info['lookback'], len(V3_FEATURES))
                        tensor_in  = tf.convert_to_tensor(win_scaled, dtype=tf.float32)
                        prob       = float(info['model'](tensor_in, training=False)[0, 0])
                        res_probs[m_name] = round(prob, 4)
                        if prob > info['threshold']:
                            if info['type'] == "surge":
                                s_sum += prob * info['weight']; s_hits += 1
                            else:
                                d_sum += prob * info['weight']; d_hits += 1

                    total_score = round(s_sum - d_sum, 4)

                    # ✅ [수정] 시가총액은 이미 받은 rt에서 바로 추출 (API 이중 호출 방지)
                    cap = inquiry.safe_int(rt.get("hts_avls", "0"))

                    # (7) 검색기록 종목 출력 및 매도 시그널
                    # 콘솔 출력은 본인(TELEGRAM_CHAT_ID)이 등록한 종목만
                    is_my_code = (code in history_codes and
                                  secrets.TELEGRAM_CHAT_ID in history_chat.get(code, set()))

                    if is_my_code:
                        print(f"   🔍 [{code}] {stock_name:<8} | "
                              f"점수: {total_score:.4f} | 현재가: {curr:,}원 | 모드: {market_mode}")

                    if code in history_codes:
                        # ── B 매도전략 결정 (3일 평활 + 2일 확인 + -12% 손절 → 전량) ──
                        _today_b = datetime.now().strftime("%Y%m%d")
                        _avg_b   = get_cached_avg_price(code) if is_my_code else None
                        _full_sell, _smoothed_b, _sell_reason = sell_strategy_b.decide(
                            code, total_score, curr, _avg_b, _today_b)
                        keep_amount_b = 0 if _full_sell else None

                        # ── 첫 관측 알림 (당일 첫 스코어 or 재실행) ──────────
                        if is_my_code and code not in session_notified:
                            session_notified.add(code)
                            # 매도 구간이면 아래 sell 로직이 알림을 보내므로 정상 구간만 여기서 알림
                            if keep_amount_b is None:
                                notify_ids_first = [secrets.TELEGRAM_CHAT_ID]
                                first_msg = (f"📌 {stock_name} ({code}) 모니터링\n"
                                             f"점수: {total_score*100:.1f}점  현재가: {curr:,}원")
                                send_telegram(first_msg, notify_ids_first)

                        # 이전 상태 로드 — 구형(float) 호환
                        entry = last_scores.get(code)
                        if isinstance(entry, (int, float)):
                            entry = {"score": float(entry), "sell_level": None}
                        elif not isinstance(entry, dict):
                            entry = {"score": None, "sell_level": None}

                        prev_score     = entry.get("score")
                        prev_sell_lvl  = entry.get("sell_level")
                        prev_open_ords = entry.get("open_orders", [])  # 저장된 주문 정보

                        # 매도 결정: B 전략 (전량 0 또는 보유 None)
                        keep_amount = keep_amount_b

                        # ── 매도 시그널: 각 등록자(chat_id)별로 '본인 등록 종목' 기준 알림 ──
                        #    트리거/중복방지를 (chat_id, code) 단위로 관리 → 뒤늦게 등록한 사용자도 수신
                        registrants = list(history_chat.get(code) or [])
                        if keep_amount is None:
                            # 매도 구간 이탈 → 등록자별 시그널 상태 초기화 (다음 하락 시 재알림)
                            for _cid in registrants:
                                user_sell_alert.pop((_cid, code), None)
                        else:
                            if keep_amount == 0:
                                signal_label = "[매도 시그널-전량매도]"
                            else:
                                signal_label = f"[매도 시그널-{keep_amount // 10_000:,}만원 보유]"
                            prev_str  = f"{prev_score * 100:.1f}→" if prev_score is not None else ""
                            alert_msg = (f"🚨 {signal_label} {stock_name} ({code})\n"
                                         f"점수: {prev_str}{total_score * 100:.1f}점\n"
                                         f"현재가: {curr:,}원")
                            for _cid in registrants:
                                if user_sell_alert.get((_cid, code)) != keep_amount:
                                    user_sell_alert[(_cid, code)] = keep_amount
                                    if str(_cid) == secrets.TELEGRAM_CHAT_ID:
                                        print(f"   🔔 {alert_msg.replace(chr(10), '  ')}")
                                    send_telegram(alert_msg, [_cid])

                        if not is_my_code:
                            # 자동매매는 '내 ID로 등록된 종목'만 실행 (소유자 계좌 조회/주문)
                            # 친구 등록·자동발굴 종목은 잔고조회·주문 없이 점수만 갱신
                            last_scores[code] = {"score": total_score,
                                                 "sell_level": prev_sell_lvl,
                                                 "open_orders": prev_open_ords}
                        else:
                            # 내 보유종목: B전략 결정을 매도 plan(autosell_plan)에 반영.
                            #   실제 집행은 execution_monitor 가 '매수호가 sweep' 으로 수행
                            #   (한 번에 전량발주 X → 기준가 이상 매수호가 잔량만큼 분할 청산).
                            #   기준가(sell_price)는 매 사이클 promising 현재가(curr)로 갱신.
                            if keep_amount == 0:
                                sell_plan_targets[code] = {
                                    "name":       stock_name,
                                    "sell_price": curr,
                                    "reason":     _sell_reason,
                                    "score":      total_score,
                                }
                            last_scores[code] = {"score": total_score,
                                                 "sell_level": keep_amount,
                                                 "open_orders": []}

                    # (8) history 종목이면 업데이트 수집
                    if code in history_codes:
                        history_updates[code] = {
                            'total_score': total_score,
                            'close_price': curr,
                            'market_cap':  cap,
                            'change_pct':  round((curr / oprc - 1) * 100, 2) if oprc > 0 else 0,
                            'net_hits':    s_hits - d_hits,
                            'surge_hits':  s_hits,
                            'drop_hits':   d_hits,
                        }

                    # (9) 결과 저장
                    result_row = {
                        'code': code, 'name': stock_name,
                        'close_price': curr, 'market_cap': cap,
                        'score_total': total_score,
                        'net_hits':    s_hits - d_hits,
                        'surge_hits':  s_hits, 'drop_hits': d_hits,
                        'time':        datetime.now().strftime("%H:%M:%S"),
                        'target1':  res_probs.get('target1',  0),
                        'target5':  res_probs.get('target5',  0),
                        'target20': res_probs.get('target20', 0),
                        'drop1':    res_probs.get('drop1',    0),
                        'drop5':    res_probs.get('drop5',    0),
                        'drop20':   res_probs.get('drop20',   0)
                    }

                    if is_etf: results_etf.append(result_row)
                    else:      results_stock.append(result_row)

                except Exception as e:
                    print(f"   ❌ [{code}] 오류: {e}")
                    continue

            # 3. 로그 저장
            update_split_logs(results_stock, results_etf, today_str)
            update_search_history_scores(history_updates, today_str)
            save_last_scores(last_scores)
            save_sell_plan(sell_plan_targets, today_str)   # 매도 sweep 계획 → execution_monitor
            sell_strategy_b.persist()   # B 전략 교차일 상태 영속
            time.sleep(CYCLE_DELAY)

        except KeyboardInterrupt:
            print("\n🛑 중단됨")
            break
        except Exception as e:
            print(f"   ⚠️ 런타임 에러: {e}")
            time.sleep(30)


if __name__ == "__main__":
    run_updater()
