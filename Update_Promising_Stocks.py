import os
import sys
import time
import json
import subprocess
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
CYCLE_DELAY    = 5    # 사이클 간 숨고르기(초). 사이클 자체가 수십 초라 idle 최소화(30→5).
                      # 0으로 두지 않는 이유: 키움 REST 를 execution_monitor(4초 폴링, 실주문)와
                      # 공유 — 핫루프 시 rate-limit 경합으로 주문 집행이 느려질 수 있음.

# 실제 매매(주문 접수/취소/정정) 알림은 소유자(나)만 수신 — 친구는 시그널만
OWNER_IDS = [secrets.TELEGRAM_CHAT_ID]

# 매도 시그널 임계값
DROP_THRESHOLDS = [0.40, 0.35, 0.30, 0.25, 0.20, 0]

# ✅ [추가] NXT 시간대 API 타임아웃 제한
#    NXT 데이터가 없는 종목은 call_api가 재시도하며 오래 걸림
#    → 종목당 최대 대기 시간을 설정해 멈춤 방지
API_TIMEOUT_SEC = 4   # 단건 API 호출 타임아웃 (초)
MAX_FAIL_SKIP   = 3   # 연속 실패 N회면 해당 종목 이번 사이클 건너뜀

# promising 자동정리: 60+ 자동등록(claude_eval_pipeline.SCORE_THRESHOLD=0.60) 종목이
# 미보유 & 현재점수 < 이 값이면 promising 에서 제거해 목록 비대화·지연 방지.
PROMISING_KEEP_MIN   = 0.60
HOLDINGS_REFRESH_SEC = 300   # 보유종목 조회 캐시 주기(초)


# ====================================================
# 💼 보유종목 코드 set 캐시 (promising 정리용, 5분 주기)
# ====================================================
_holdings_cache = {"ts": 0.0, "set": None}

def get_holdings_set():
    """키움 보유 코드 set. 조회 실패 시 직전 캐시(없으면 None) 반환 → 정리 생략용."""
    now = time.time()
    if _holdings_cache["set"] is not None and (now - _holdings_cache["ts"]) < HOLDINGS_REFRESH_SEC:
        return _holdings_cache["set"]
    hs = trading.fetch_all_holdings()
    if hs is None:                       # 조회 실패 → 직전 캐시 유지(오삭제 방지)
        return _holdings_cache["set"]
    s = set(format_code(h["code"]) for h in hs if h.get("code"))
    _holdings_cache["ts"]  = now
    _holdings_cache["set"] = s
    return s

PLAN_KICK_MIN_SEC = 90   # 비중구간 상승 → auto_buy 즉시 킥 최소 간격(초, 디바운스)


def _alloc_bucket(score):
    """매수 비중구간(1안) id. <0.60=0(비대상) · 60~69=6 · 70~79=7 · 80~89=8 · 90~99=9 · 100+=10.
       구간이 '상승'하는 순간( 신규 60+ 진입 포함 ) auto_buy plan 즉시 갱신 킥의 감지 기준."""
    if score < 0.60:
        return 0
    return min(int(score * 100) // 10, 10)


# ====================================================
# 🏷️ V3 주식 마스터 등재 여부 (자동매도 게이트용, 1시간 캐시)
# ====================================================
_v3_master_cache = {"ts": 0.0, "set": None}

def is_v3_master_code(code):
    """V3 주식 마스터(logs/stock_master.json) 등재 여부. 자동매도(B전략) 게이트 —
    미등재(ETF·ETN·우선주 등 수기 매수 영역)는 자동매도에서 제외한다.
    로드 실패 시 True(fail-open: 마스터 손상으로 전 종목 자동매도가 멈추는 것 방지)."""
    now = time.time()
    if _v3_master_cache["set"] is None or (now - _v3_master_cache["ts"]) > 3600:
        try:
            with open(os.path.join(LOG_DIR, "stock_master.json"), encoding="utf-8") as f:
                _v3_master_cache["set"] = set(json.load(f).get("stocks", {}).keys())
            _v3_master_cache["ts"] = now
        except Exception:
            return True
    return code in _v3_master_cache["set"]


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

def get_cached_avg_price(code, force=False):
    """보유 평단가를 하루 1회만 잔고조회로 캐시 (없으면 None).
    force=True 면 캐시를 무시하고 즉시 재조회해 갱신한다(-12% 손절 주문 직전
    장중 추가매수/물타기 반영용)."""
    today = datetime.now().strftime("%Y%m%d")
    if not force:
        c = _avg_price_cache.get(code)
        if c and c[0] == today:
            return c[1]
    avg = None
    try:
        hs = trading.fetch_stock_holdings(code)
        if hs:
            tq = sum(h["qty"] for h in hs)
            # 평단은 브로커 제공 매입가(pur_pric=avg_buy_price)를 수량가중 평균한다.
            # pur_amt/qty 재계산은 신용·담보 매수의 금융비용 포함, 부분매도 시
            # 매입금액이 잔여수량과 어긋나면 평단이 부풀려져 -12% 손절이 오발동함.
            # (매도주문 코드 fetch_stock_holdings 사용부도 avg_buy_price 기준 → 일치)
            ta = sum(h["avg_buy_price"] * h["qty"] for h in hs)
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


def bought_today(code, today_str):
    """오늘자 autobuy_exec.json 에 이 종목 체결(bought>0)이 있으면 True(=오늘이 마지막 매수일).
    execution_monitor 가 매 체결마다 갱신한다. 실패/미존재 시 False(=보류 안 함)."""
    try:
        p = os.path.join(LOG_DIR, f"{today_str}_autobuy_exec.json")
        if not os.path.exists(p):
            return False
        with open(p, encoding='utf-8') as f:
            data = json.load(f)
        if data.get("date") != today_str:
            return False
        c = (data.get("codes") or {}).get(format_code(code))
        return bool(c and int(c.get("bought", 0) or 0) > 0)
    except Exception:
        return False


def load_name_map(today_str):
    """오늘자 Search_History.csv 에서 {code: name} 매핑을 읽는다(없으면 빈 dict)."""
    m = {}
    p = os.path.join(LOG_DIR, f"{today_str}_Search_History.csv")
    try:
        if os.path.exists(p):
            df = pd.read_csv(p, encoding='utf-8-sig', dtype=str)
            if 'code' in df.columns and 'name' in df.columns:
                for _, r in df.iterrows():
                    m[format_code(r['code'])] = str(r.get('name', '') or '').strip()
    except Exception:
        pass
    return m


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
def update_search_history_scores(updates, today_str, holdings_set=None):
    """
    updates: {code: {'total_score': ..., 'close_price': ..., 'net_hits': ...,
                     'surge_hits': ..., 'drop_hits': ...}}
    Search_History.csv의 해당 code 행 점수·현재가를 일괄 덮어씁니다.
    holdings_set 이 주어지면(=키움 보유 코드 set): 자동등록(60+)인데 미보유 & 점수<임계
    종목을 promising 에서 제거한다(목록 비대화 방지). None 이면(조회 실패 등) 정리 생략.
    ※ signal 컬럼은 출처 마커(60+자동등록/보유종목/클로드평가/수동)로 쓰이므로 덮어쓰지 않는다
      (Target/Drop 카운트는 surge_hits/drop_hits 컬럼에 그대로 저장됨).
    """
    hist_path = os.path.join(LOG_DIR, f"{today_str}_Search_History.csv")
    if not os.path.exists(hist_path):
        return
    if not updates and holdings_set is None:
        return
    try:
        df = pd.read_csv(hist_path, encoding='utf-8-sig', dtype=str, on_bad_lines='skip')
        if 'code' not in df.columns:
            return
        df['code'] = df['code'].apply(format_code)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for code, vals in (updates or {}).items():
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
            df.loc[mask, 'timestamp']     = now_str

        # ── promising 정리: 60+ 자동등록 종목이 미보유 & 점수<임계면 제거 ──────────
        if holdings_set is not None and 'signal' in df.columns and 'total_score' in df.columns:
            sc = pd.to_numeric(df['total_score'], errors='coerce').fillna(1.0)  # 파싱실패=보존
            is_auto  = df['signal'].astype(str).str.strip() == "60+자동등록"
            not_held = ~df['code'].isin(holdings_set)
            low      = sc < PROMISING_KEEP_MIN
            drop_mask = is_auto & not_held & low
            n_drop = int(drop_mask.sum())
            if n_drop:
                dropped = df.loc[drop_mask, ['code', 'name']].values.tolist()
                df = df[~drop_mask]
                print(f"   🧹 promising 정리: 60+자동등록·미보유·{PROMISING_KEEP_MIN*100:.0f}점미만 "
                      f"{n_drop}개 제거 → {dropped[:10]}")

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

    _sb = sell_strategy_b
    print(f"\n🚀 [Promising Updater] 시작 (마켓 모드: {market_mode})")
    print(f"   - 자동발굴 : 점수 {TARGET_SCORE*100:.0f}점 이상만 추적")
    print(f"   - 검색기록 : 점수 무관 무조건 추적")
    print(f"   - 매도규칙 : ①-{abs(_sb.STOP_PCT)*100:.0f}% 손절(raw≥{_sb.STOP_SCORE_KEEP*100:.0f} 면제) "
          f"②raw<0 {_sb.RAW_NEG_HOLD_SEC//60}분연속(15:00~ 즉시매도, 15:20~ 동시호가 시장가) "
          f"③평활<{_sb.SELL_THRESH*100:.0f}점 2일연속(오늘 raw≥{_sb.SELL_THRESH*100:.0f} 회복시 보류)")
    print(f"   - 하락경고 : 평활/raw < {_sb.SELL_THRESH*100:.0f}점 시 텔레그램 알림(자동매매는 본인 등록종목만)")
    print(f"   - 주기      : {CYCLE_DELAY}초\n")

    last_scores      = load_last_scores()   # 재시작 후에도 이전 점수 복원
    nxt_skip_cache   = set()
    session_notified = set()  # 이번 실행에서 첫 관측 완료한 코드 (재실행 알림용)
    sell_alert_sent  = {}     # {code: keep_amount} — 소유자 자동매매 실행알림 중복 방지
    user_sell_alert  = {}     # {(chat_id, code): keep_amount} — 등록자별 매도시그널 중복 방지
    corr_notified    = {}     # {code: order_price} — 정정요망 알림 중복 방지
    prev_market_mode = None   # 모드 전환 감지용
    drop_warn_sent   = {}     # {(chat_id, code): (dir, day, streak)} — 평활<0.2 하락/회복 경고 중복 방지
    raw_warn_sent    = {}     # {(chat_id, code): (dir, day)} — raw<0.2 조기경보(하락/회복) 중복 방지
    rawneg_warn_sent = {}     # {(chat_id, code): (day, 5분버킷)} — raw<0 30분 카운트다운 진입/5분단위/회복 중복 방지
    nxt_carry_reported = None  # 평활<0.2 이월 리포트 발송한 날짜(YYYYMMDD) — NXT 아침 1회
    alloc_bucket_prev = {}    # {code: 비중구간} — 직전 사이클 값(구간 '상승' edge 감지용)
    plan_kick_down_wait = {}  # {code: 하락한 구간} — 하락 1사이클 유예(왕복 노이즈 억제, 2026-08-19)
    plan_kick_pending = set() # 상승 감지됐으나 아직 킥 안 나간 종목 라벨(디바운스에 걸리면 이월)
    plan_kick_last    = 0.0   # 마지막 auto_buy 킥 시각(epoch) — PLAN_KICK_MIN_SEC 디바운스

    while True:
        try:
            now = datetime.now()

            # 휴장일(주말/공휴일) 가드: promising·데이터 갱신 안 함(휴일 날짜 stale 기록→오류 방지)
            if not trading.is_trading_day(now):
                print(f"   📴 [{now:%H:%M:%S}] 휴장(주말/공휴일) — promising 갱신 스킵(10분 후 재확인)")
                time.sleep(600)
                continue

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

            # ── (NXT 아침 1회) 어제 평활<SELL_THRESH 이월 종목 → 오늘 raw 임계 리포트 ──
            #    어제 평활이 SELL_THRESH(현재 0.10) 미만이던 종목은 오늘도 미만이면 즉시 2일 연속 → 매도.
            if (market_mode == "NXT" and now.time() < dtime(9, 0)
                    and nxt_carry_reported != today_str):
                nxt_carry_reported = today_str
                _nm_map = load_name_map(today_str)
                _lines  = []
                _cthr = sell_strategy_b.SELL_THRESH * 100   # 표시용 청산임계(점) — SELL_THRESH 연동
                for _c in sorted(history_codes):
                    _co = sell_strategy_b.carryover_below(_c)
                    if not _co:
                        continue
                    _name = _nm_map.get(_c) or _c
                    _lines.append(
                        f"· {_name}({_c}) 어제평활 {_co['last_smoothed']*100:.1f}점 → "
                        f"오늘 {_co['today_thresh']*100:.1f}점 미만이면 평활{_cthr:.0f}↓(2일연속 매도)")
                if _lines:
                    _msg = (f"📊 [평활 {_cthr:.0f}↓ 이월] 어제 평활 {_cthr:.0f}점 미만 종목\n"
                            "오늘 아래 점수 미만이면 평활 2일 연속 → 매도\n"
                            + "\n".join(_lines))
                    send_telegram(_msg, [secrets.TELEGRAM_CHAT_ID])
                    print(f"   📊 평활 이월 리포트 발송: {len(_lines)}종목")

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
                            # 이력 CSV 없으면 백필 시도 (수동 추가/신규 종목도 추적 대상에 포함)
                            #   검색기록 종목만 백필 (자동발굴 점수≥0.2 는 원래 CSV 존재)
                            new_path = None
                            if code in history_codes:
                                try:
                                    import build_stock_master as _bsm
                                    new_path = _bsm.ensure_stock_csv(code)  # 이름은 마스터에서 자동조회
                                except Exception:
                                    new_path = None
                            if new_path and os.path.exists(new_path):
                                file_path = new_path
                                is_etf    = False
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
                        # 자동매도 게이트: ETF·V3 마스터 미등재(=수기 매수 영역) 종목은 B전략 제외.
                        #   장마감 동기화가 보유종목을 전부 추적목록에 넣으므로, 수기 매수한 ETF 가
                        #   주식용 V3 점수로 자동매도되는 오작동 방지(2026-08-07 530107 점검).
                        #   점수·가격 추적과 일반 알림은 유지 — 매도 결정(decide)·plan 등록만 차단.
                        if is_etf or not is_v3_master_code(code):
                            _avg_b = None
                            _full_sell, _smoothed_b, _sell_reason = False, None, ""
                        else:
                            _avg_b   = get_cached_avg_price(code) if is_my_code else None
                            _full_sell, _smoothed_b, _sell_reason = sell_strategy_b.decide(
                                code, total_score, curr, _avg_b, _today_b)
                        # -12% 손절 확정 직전: 캐시 무효화 후 최신 평단으로 재확인.
                        #   장중 sweep 추가매수(물타기)로 평단이 내려가 -12% 미달이면 손절 취소.
                        #   재조회 실패(None)면 기존(캐시 평단) 판정을 그대로 유지(폴백).
                        if _full_sell and _sell_reason == "stop12" and is_my_code:
                            _avg_fresh = get_cached_avg_price(code, force=True)
                            if _avg_fresh:
                                _avg_b = _avg_fresh
                                if not sell_strategy_b.is_stop_loss(curr, _avg_fresh):
                                    _full_sell, _sell_reason = False, ""
                                    print(f"   ↩️ [{code}] 손절 재확인: 최신 평단 {_avg_fresh:,.0f}원 · "
                                          f"현재가 {curr:,}원 → -12% 미달, 손절 취소")
                        # [C] 매수 당일 점수청산 보류: 마지막 매수일(=오늘 체결)엔 점수청산 안 함.
                        #     (-12% 손절 stop12 은 그대로 유효 — 여기서 제외)
                        if (_full_sell and _sell_reason == "score" and is_my_code
                                and bought_today(code, _today_b)):
                            _full_sell, _sell_reason = False, ""
                            print(f"   ⏸️ [{code}] 오늘 매수 체결 종목 → 점수청산 당일 보류")
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
                                _reason_txt = {"stop12": " (-12% 손절)",
                                               "score":  " (점수청산)"}.get(_sell_reason, "")
                                signal_label = f"[매도 시그널-전량매도]{_reason_txt}"
                            else:
                                signal_label = f"[매도 시그널-{keep_amount // 10_000:,}만원 보유]"
                            prev_str  = f"{prev_score * 100:.1f}→" if prev_score is not None else ""
                            # -12% 손절이면 평단·손익률을 함께 표기해 -12% 확인 가능하게
                            _extra = ""
                            if _sell_reason == "stop12" and _avg_b and _avg_b > 0:
                                _loss_pct = (curr - _avg_b) / _avg_b * 100
                                _extra = f"\n평단: {_avg_b:,.0f}원 ({_loss_pct:+.1f}%)"
                            alert_msg = (f"🚨 {signal_label} {stock_name} ({code})\n"
                                         f"점수: {prev_str}{total_score * 100:.1f}점\n"
                                         f"현재가: {curr:,}원{_extra}")
                            for _cid in registrants:
                                if user_sell_alert.get((_cid, code)) != keep_amount:
                                    user_sell_alert[(_cid, code)] = keep_amount
                                    if str(_cid) == secrets.TELEGRAM_CHAT_ID:
                                        print(f"   🔔 {alert_msg.replace(chr(10), '  ')}")
                                    send_telegram(alert_msg, [_cid])

                        # ── 평활 하락/회복 경고: 평활 SELL_THRESH(현재 0.10) 아래로 내려가면 하락경고,
                        #    다시 그 이상으로 올라오면 회복알림. 장중 오르내림마다 반복(등록자별).
                        #    중복방지 키에 방향(D=하락 / U=회복)을 넣어 방향이 바뀔 때만 발송.
                        _st = sell_strategy_b.status_after_decide(code)
                        _thrp = sell_strategy_b.SELL_THRESH * 100   # 표시용 청산임계(점) — SELL_THRESH 연동
                        if _st and _st["below"]:
                            _sm_pts = _st["smoothed"] * 100
                            _streak = _st["streak"]
                            if _streak < sell_strategy_b.CONFIRM_DAYS:
                                _need = _st["next_raw_thresh"] * 100
                                _tail = (f"→ 첫날. 내일 점수 {_need:.1f}점 미만이면 "
                                         f"평활 {_thrp:.0f}점 미만 2일 연속(매도) 도달")
                            elif _sell_reason == "score":
                                _tail = f"→ 평활 {_thrp:.0f}점 미만 {_streak}일 연속, 매도주문 실행"
                            elif total_score >= sell_strategy_b.SELL_THRESH:
                                _tail = (f"→ 평활 {_thrp:.0f}점 미만 {_streak}일 연속이나 오늘 점수 "
                                         f"{total_score*100:.1f}점(≥{_thrp:.0f}) 회복 → 매도 보류(A)")
                            else:
                                _tail = f"→ 평활 {_thrp:.0f}점 미만 {_streak}일 연속이나 매수 당일 → 매도 보류(C)"
                            warn_msg = (f"⚠️ [평활 하락] {stock_name} ({code})\n"
                                        f"오늘 평활: {_sm_pts:.1f}점 (평활 {_thrp:.0f}점 미만 {_streak}일째)\n"
                                        f"{_tail}\n현재가: {curr:,}원")
                            _key = ("D", _today_b, _streak)
                            for _cid in registrants:
                                if drop_warn_sent.get((_cid, code)) != _key:
                                    drop_warn_sent[(_cid, code)] = _key
                                    if str(_cid) == secrets.TELEGRAM_CHAT_ID:
                                        print(f"   ⚠️ {warn_msg.replace(chr(10), '  ')}")
                                    send_telegram(warn_msg, [_cid])
                        elif _st:
                            # 평활 회복(>=0.20): 직전에 '하락경고(D)'를 받은 등록자에게만 1회 알림.
                            #   (한 번도 하락 안 했거나 이미 회복 알림을 보낸 경우엔 발송 안 함)
                            _sm_pts = _st["smoothed"] * 100
                            recover_msg = (f"✅ [평활 회복] {stock_name} ({code})\n"
                                           f"오늘 평활: {_sm_pts:.1f}점 ({_thrp:.0f}점 이상 회복)\n"
                                           f"현재가: {curr:,}원")
                            _key = ("U", _today_b)
                            for _cid in registrants:
                                _prev = drop_warn_sent.get((_cid, code))
                                if _prev and _prev[0] == "D":
                                    drop_warn_sent[(_cid, code)] = _key
                                    if str(_cid) == secrets.TELEGRAM_CHAT_ID:
                                        print(f"   ✅ {recover_msg.replace(chr(10), '  ')}")
                                    send_telegram(recover_msg, [_cid])

                        # ── 점수(raw) 조기경보: 표시 점수(total_score) < 0.20 이면 경고 →
                        #    다시 20 이상이면 회복 알림(상하 토글, 등록자별). 평활 경고와 별개.
                        #    단 평활도 이미 <0.20(위 평활경고 발동)이면 raw 조기경보는 생략(중복 방지).
                        _raw_low = total_score < sell_strategy_b.SELL_THRESH
                        _sm_low  = bool(_st and _st["below"])
                        if _raw_low and not _sm_low:
                            _smtxt = (f"\n※ 평활 {_st['smoothed']*100:.1f}점 — 실제 매도(평활 2일연속<{_thrp:.0f})와는 별개"
                                      if _st else "")
                            raw_msg = (f"⚠️ [점수 하락] {stock_name} ({code})\n"
                                       f"오늘 점수: {total_score*100:.1f}점 ({_thrp:.0f}점 미만){_smtxt}\n"
                                       f"현재가: {curr:,}원")
                            _rk = ("D", _today_b)
                            for _cid in registrants:
                                if raw_warn_sent.get((_cid, code)) != _rk:
                                    raw_warn_sent[(_cid, code)] = _rk
                                    if str(_cid) == secrets.TELEGRAM_CHAT_ID:
                                        print(f"   ⚠️ {raw_msg.replace(chr(10), '  ')}")
                                    send_telegram(raw_msg, [_cid])
                        else:
                            # raw 20 이상 회복(둘 다 정상)이면 직전 raw경고(D) 받은 사람에게 회복알림.
                            # 평활<0.20 으로 '승격'된 경우엔 회복 아님 → 조용히 raw 상태만 정리.
                            _raw_recovered = (not _raw_low) and (not _sm_low)
                            recover_raw = (f"✅ [점수 회복] {stock_name} ({code})\n"
                                           f"오늘 점수: {total_score*100:.1f}점 ({_thrp:.0f}점 이상)\n"
                                           f"현재가: {curr:,}원")
                            for _cid in registrants:
                                _pv = raw_warn_sent.get((_cid, code))
                                if _pv and _pv[0] == "D":
                                    if _raw_recovered:
                                        raw_warn_sent[(_cid, code)] = ("U", _today_b)
                                        if str(_cid) == secrets.TELEGRAM_CHAT_ID:
                                            print(f"   ✅ {recover_raw.replace(chr(10), '  ')}")
                                        send_telegram(recover_raw, [_cid])
                                    else:
                                        raw_warn_sent.pop((_cid, code), None)

                        # ── raw<0 30분 카운트다운 진행 알림: 진입 1회 + 5분 단위 + 회복 1회.
                        #    raw_neg_status(active/elapsed/remaining). 30분 도달 시엔 execution_monitor 가
                        #    매도(체결)를 보고하므로, 카운트다운 알림은 매도 직전(25분)까지만 보낸다.
                        _rn = sell_strategy_b.raw_neg_status(code)
                        _rn_hold_m = sell_strategy_b.RAW_NEG_HOLD_SEC // 60
                        if _rn and _rn["active"]:
                            # 보유 게이트: '전량매도' 카운트다운은 실제 보유 종목만 의미 있음.
                            #   decide() 는 평활 이력 유지를 위해 미보유 promising 에도 돌므로,
                            #   미보유(매도완료 포함)면 알림 억제 + 발송상태 정리(회복알림도 안 나가게).
                            #   (2026-08-05 대덕전자: 아침 전량매도 후 raw<0 재진입 알림 발송 사례)
                            #   get_holdings_set()=None(최초 조회실패)이면 fail-open(발송) — 보유 매도경고 누락 방지.
                            _hset = get_holdings_set()   # 5분 캐시
                            if _hset is not None and code not in _hset:
                                for _cid in list(registrants):
                                    rawneg_warn_sent.pop((_cid, code), None)
                            else:
                                _el_m = int(_rn["elapsed"] // 60)
                                _rm_m = max(0, int((_rn["remaining"] + 59) // 60))   # 남은 분(올림)
                                _ms   = int(_rn["elapsed"] // 300)                   # 5분 버킷(0=진입)
                                if _ms <= (_rn_hold_m // 5) - 1:                     # 25분까지만(30분은 매도가 대신)
                                    _rnk = (_today_b, _ms)
                                    for _cid in registrants:
                                        _prev = rawneg_warn_sent.get((_cid, code))
                                        if _prev != _rnk:
                                            _is_entry = (_ms == 0 and (_prev is None or _prev[0] != _today_b))
                                            rawneg_warn_sent[(_cid, code)] = _rnk
                                            if _is_entry:
                                                rn_msg = (f"⚠️ [raw<0 진입] {stock_name} ({code})\n"
                                                          f"raw {total_score*100:.1f}점 (0 미만) — {_rn_hold_m}분 연속 시 전량매도\n"
                                                          f"현재가: {curr:,}원")
                                            else:
                                                rn_msg = (f"⏳ [raw<0 {_el_m}분째] {stock_name} ({code})\n"
                                                          f"약 {_rm_m}분 후 전량매도 (raw {total_score*100:.1f}점)\n"
                                                          f"현재가: {curr:,}원")
                                            if str(_cid) == secrets.TELEGRAM_CHAT_ID:
                                                print(f"   ⚠️ {rn_msg.replace(chr(10), '  ')}")
                                            send_telegram(rn_msg, [_cid])
                        elif _rn is not None:
                            # raw 0 이상 회복 → 직전에 raw<0 진입 알림 받은 사람에게 1회 해소 알림.
                            for _cid in list(registrants):
                                if (_cid, code) in rawneg_warn_sent:
                                    rawneg_warn_sent.pop((_cid, code), None)
                                    rn_rec = (f"✅ [raw<0 해소] {stock_name} ({code})\n"
                                              f"raw {total_score*100:.1f}점 (0 이상) 회복 — 매도 타이머 리셋\n"
                                              f"현재가: {curr:,}원")
                                    if str(_cid) == secrets.TELEGRAM_CHAT_ID:
                                        print(f"   ✅ {rn_rec.replace(chr(10), '  ')}")
                                    send_telegram(rn_rec, [_cid])

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

                        # ── 비중구간 '변동' 감지 → auto_buy plan 즉시 킥 예약 (edge-trigger, 양방향).
                        #    상승(신규 60+ 진입·증액): 10분 평가주기를 기다리지 않고 즉시 매수 반영.
                        #    하락(감액·60 미만 이탈): 집행 중이던 옛 큰 목표수량을 즉시 축소/제거 —
                        #      monitor 의 매수직전 재확인은 60 미만만 차단하므로 60 위 구간하락
                        #      (예: 85→75)은 plan 재생성이 있어야 매수 목표가 줄어든다.
                        #    ETF·마스터 미등재는 plan 대상이 아니므로 제외(헛킥 방지).
                        if (not is_etf) and is_v3_master_code(code):
                            _bk = _alloc_bucket(total_score)
                            _pb = alloc_bucket_prev.get(code, 0)
                            if _bk == _pb:
                                # 하락 유예 중 직전구간 회귀(왕복 노이즈) → 킥 없이 취소 (2026-08-19)
                                plan_kick_down_wait.pop(code, None)
                            elif _bk > _pb:
                                # 구간 상승: 즉시 킥 (유예 중이었어도 상승이면 새 상태로 확정)
                                plan_kick_down_wait.pop(code, None)
                                plan_kick_pending.add(f"{stock_name}({code}) {_pb or '·'}→{_bk or '·'}구간↑")
                                alloc_bucket_prev[code] = _bk
                            else:
                                # 구간 하락: 1사이클 유예 — 다음 사이클에도 하락 유지 시에만 킥
                                # (한 구간 내려갔다 바로 직전구간 회귀하는 왕복은 킥·알림 억제)
                                if code in plan_kick_down_wait:
                                    plan_kick_down_wait.pop(code, None)
                                    plan_kick_pending.add(f"{stock_name}({code}) {_pb or '·'}→{_bk or '·'}구간↓")
                                    alloc_bucket_prev[code] = _bk
                                else:
                                    plan_kick_down_wait[code] = _bk

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
            # 보유종목 set(5분 캐시) — promising 자동정리(60+미달·미보유 제거)에 사용
            holdings_set = get_holdings_set()
            update_search_history_scores(history_updates, today_str, holdings_set)
            save_last_scores(last_scores)
            # ── 비중구간 변동(상승·하락) → auto_buy 즉시 킥 (KRX 정규장에만, PLAN_KICK_MIN_SEC 디바운스).
            #    백그라운드 1회 실행: plan 재생성만 하고 종료(집행은 execution_monitor 담당).
            #    plan 저장은 원자적(tmp+replace)이라 10분 파이프라인과 겹쳐도 안전(최신쓰기 승리).
            if plan_kick_pending and market_mode == "KRX":
                _nowk = time.time()
                if _nowk - plan_kick_last >= PLAN_KICK_MIN_SEC:
                    plan_kick_last = _nowk
                    _kick_label = ", ".join(sorted(plan_kick_pending))
                    try:
                        _klog = open(os.path.join(LOG_DIR, f"{today_str}_autobuy_kick.log"),
                                     "a", encoding="utf-8")
                        _klog.write(f"\n[{datetime.now():%H:%M:%S}] 구간변동 킥: {_kick_label}\n")
                        subprocess.Popen(
                            [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_buy.py")],
                            stdout=_klog, stderr=subprocess.STDOUT,
                            creationflags=0x08000000)   # CREATE_NO_WINDOW
                        print(f"   ⚡ 비중구간 변동 → auto_buy 즉시 킥: {_kick_label}")
                        plan_kick_pending.clear()
                    except Exception as e:
                        print(f"   ⚠️ auto_buy 킥 실패(다음 사이클 재시도): {e}")

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
