"""
telegram_chat.py - 텔레그램 종목 조회 봇
종목명 또는 종목코드를 입력받아 V3 분석 결과를 텔레그램으로 전송합니다.

[사용법]
  python kis_api/telegram_chat.py

[텔레그램 입력 형식]
  - 종목코드 6자리: 005930
  - 종목명 (부분 일치): 삼성전자, 카카오, KODEX200
"""

import os
import sys
import csv
import time
import pickle
import warnings
import requests
import pandas as pd
import tensorflow as tf
from datetime import datetime

os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
warnings.filterwarnings("ignore")

# 프로젝트 루트를 경로에 추가
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from config import secrets
from kis_api import auth, inquiry, indicators, kiwoom_trading
from tensorflow.keras.models import load_model

# ====================================================
# ⚙️ 설정
# ====================================================
MODEL_DIR      = secrets.V3_MODEL_DIR
DATA_DIR_STOCK = os.path.join(PROJECT_DIR, "Data", "Stock")
DATA_DIR_ETF   = os.path.join(PROJECT_DIR, "Data", "ETF")
LOG_DIR        = os.path.join(PROJECT_DIR, "logs")

MODEL_SETTINGS = {
    "target1":  {"lb": 65,  "thr": 0.5256, "weight": 0.1775},
    "target5":  {"lb": 55,  "thr": 0.6484, "weight": 0.3639},
    "target20": {"lb": 95,  "thr": 0.9197, "weight": 0.4586},
    "drop1":    {"lb": 80,  "thr": 0.4018, "weight": 0.2544},
    "drop5":    {"lb": 85,  "thr": 0.5041, "weight": 0.3376},
    "drop20":   {"lb": 85,  "thr": 0.5723, "weight": 0.4079},
}

V3_FEATURES = [
    'change_pct', 'volume_ratio', 'vol_power',
    'prog_net_ratio', 'prog_ratio_vol',
    'disparity_5', 'disparity_20',
    'rsi', 'bb_p', 'bb_w', 'adx',
    'kospi_change', 'kosdaq_change',
]

POLL_TIMEOUT = 30   # long polling 대기 시간 (초)


# ====================================================
# 📡 텔레그램 API
# ====================================================
def _tg_url(method):
    return f"https://api.telegram.org/bot{secrets.TELEGRAM_BOT_TOKEN}/{method}"


def send_message(chat_id, text):
    try:
        requests.post(
            _tg_url("sendMessage"),
            data={"chat_id": chat_id, "text": text},
            timeout=5,
        )
    except Exception as e:
        print(f"⚠️ 텔레그램 전송 실패: {e}")


def get_updates(offset):
    try:
        res = requests.get(
            _tg_url("getUpdates"),
            params={"timeout": POLL_TIMEOUT, "offset": offset},
            timeout=POLL_TIMEOUT + 5,
        )
        return res.json().get("result", [])
    except Exception:
        return []


# ====================================================
# 📋 명령어 메뉴 등록 (입력란 '/' 드롭다운 + 메뉴 버튼)
#    ⚠️ 명령어 이름은 소문자 영문/숫자/_ 만 허용 → 한글 명령(/보유종목)은 메뉴 불가.
#       영문 별칭(holdings/cleanup)을 등록하고 한글 명령도 그대로 동작하게 유지.
# ====================================================
def set_bot_commands():
    # 공통(모든 사용자에게 노출) — 매매/보유 관련 명령 제외
    common = [
        {"command": "help",  "description": "📖 사용법 안내"},
        {"command": "list",  "description": "📋 내 추적 종목 목록"},
        {"command": "top5",  "description": "🏆 점수 상위 5종목"},
        {"command": "top10", "description": "🏆 점수 상위 10종목"},
        {"command": "top20", "description": "🏆 점수 상위 20종목"},
        {"command": "del",   "description": "🗑️ 추적목록에서 종목 삭제 (예: /del 005930)"},
        {"command": "users", "description": "👥 방문자 목록"},
    ]
    # 소유자 전용 — 키움 보유종목 관련 (친구 메뉴에는 표시하지 않음)
    owner_only = [
        {"command": "holdings", "description": "📥 키움 보유종목을 추적목록에 추가 (=/보유종목)"},
        {"command": "cleanup",  "description": "🧹 키움 미보유 종목 정리 (=/종목정리)"},
    ]
    try:
        # 1) 기본 스코프(모든 사용자): 공통 명령만
        requests.post(_tg_url("setMyCommands"), json={"commands": common}, timeout=5)
        # 2) 소유자 채팅 스코프: 공통 + 매매 명령
        res = requests.post(_tg_url("setMyCommands"), json={
            "commands": common + owner_only,
            "scope": {"type": "chat", "chat_id": int(secrets.TELEGRAM_CHAT_ID)},
        }, timeout=5)
        if res.json().get("ok"):
            print("   ✅ 텔레그램 명령어 메뉴 등록 완료 (소유자/공통 분리)", flush=True)
        else:
            print(f"   ⚠️ 명령어 메뉴 등록 실패: {res.text}", flush=True)
    except Exception as e:
        print(f"   ⚠️ 명령어 메뉴 등록 오류: {e}", flush=True)


# ====================================================
# 🗂️ 종목명 캐시 (종목명 → (코드, is_etf))
# ====================================================
def build_name_cache():
    """Data/Stock, Data/ETF 전체를 스캔해 종목명 → 코드 매핑 구축"""
    print("🗂️  종목명 캐시 구축 중...", flush=True)
    cache = {}  # {정규화된 이름: (code, is_etf)}

    for is_etf, data_dir in [(False, DATA_DIR_STOCK), (True, DATA_DIR_ETF)]:
        if not os.path.exists(data_dir):
            continue
        for fname in os.listdir(data_dir):
            if not (fname.startswith("A") and fname.endswith(".csv")):
                continue
            code = fname[1:7]
            try:
                df = pd.read_csv(
                    os.path.join(data_dir, fname),
                    nrows=1,
                    encoding="utf-8-sig",
                    usecols=["name"],
                )
                if not df.empty:
                    name = str(df["name"].iloc[0]).strip()
                    if name:
                        cache[name.lower()] = (code, is_etf)
            except Exception:
                pass

    print(f"   ✅ {len(cache)}개 종목 캐시 완료", flush=True)
    return cache


def find_by_query(query: str, name_cache: dict):
    """
    코드(6자리 숫자) 또는 종목명(부분 일치)으로 (code, is_etf) 반환.
    여러 개 일치할 경우 완전 일치 우선, 그 다음 첫 번째 부분 일치.
    """
    q = query.strip()

    # 6자리 숫자면 코드로 처리
    if q.isdigit() and len(q) == 6:
        # is_etf는 나중에 API로 확인
        return q, None

    q_lower = q.lower()

    # 완전 일치
    if q_lower in name_cache:
        return name_cache[q_lower]

    # 부분 일치 (짧은 이름 우선 정렬)
    matches = [(name, info) for name, info in name_cache.items() if q_lower in name]
    if matches:
        matches.sort(key=lambda x: len(x[0]))
        return matches[0][1]

    return None, None


# ====================================================
# 🤖 V3 모델 로드
# ====================================================
def load_models():
    models = {}
    print(f"📂 모델 로드 중... ({MODEL_DIR})", flush=True)

    if not os.path.exists(MODEL_DIR):
        print("❌ 모델 폴더 없음")
        return models

    for m_name, settings in MODEL_SETTINGS.items():
        m_path = os.path.join(MODEL_DIR, f"{m_name}_lstm_v3.h5")
        s_path = os.path.join(MODEL_DIR, f"{m_name}_lstm_v3.scaler")
        if os.path.exists(m_path) and os.path.exists(s_path):
            try:
                model = load_model(m_path)
                with open(s_path, "rb") as f:
                    scaler = pickle.load(f)
                models[m_name] = {
                    "model": model, "scaler": scaler,
                    "lookback": settings["lb"], "threshold": settings["thr"],
                    "weight": settings["weight"],
                    "type": "surge" if "target" in m_name else "drop",
                }
                print(f"   ✅ {m_name} (LB:{settings['lb']})")
            except Exception as e:
                print(f"   ⚠️ {m_name} 로드 실패: {e}")

    return models


# ====================================================
# 🔬 종목 분석 (Search_Stock_V3 로직)
# ====================================================
def analyze(code, models, max_lb):
    """
    종목 코드를 받아 V3 예측을 수행하고 결과 dict 반환.
    실패 시 None 반환.
    """
    k_val  = inquiry.fetch_index_change("0001")
    kq_val = inquiry.fetch_index_change("1001")

    kind   = inquiry.fetch_stock_kind(code)
    is_etf = kind in ("ETF", "ETN")

    rt = inquiry.fetch_realtime_price(code)
    if not rt:
        return None, "존재하지 않는 종목코드입니다."

    curr = inquiry.safe_int(rt.get("stck_prpr"))
    oprc = inquiry.safe_int(rt.get("stck_oprc"))
    if oprc == 0:
        oprc = curr

    stock_name = rt.get("hts_kor_isnm", "").strip() or inquiry.fetch_stock_name(code)

    target_date = pd.Timestamp.today().normalize()
    prog = inquiry.fetch_program_today(code, target_date.strftime("%Y%m%d"))
    p_net, p_ratio = 0, 0.0
    if prog:
        p_net  = inquiry.safe_int(prog.get("whol_smtn_ntby_qty"))
        p_tot  = inquiry.safe_int(prog.get("acml_vol"))
        if p_tot > 0:
            p_ratio = round(
                (inquiry.safe_int(prog.get("whol_smtn_shnu_vol")) +
                 inquiry.safe_int(prog.get("whol_smtn_seln_vol"))) / p_tot, 4
            )

    # 데이터 파일 찾기
    base_dir  = DATA_DIR_ETF if is_etf else DATA_DIR_STOCK
    file_path = os.path.join(base_dir, f"A{code}.csv")
    if not os.path.exists(file_path):
        alt_dir  = DATA_DIR_STOCK if is_etf else DATA_DIR_ETF
        alt_path = os.path.join(alt_dir, f"A{code}.csv")
        if os.path.exists(alt_path):
            file_path = alt_path
        else:
            # 이력 CSV 없으면 즉시 백필 생성 — 수동 추가 종목은 조건 미달이어도 추적하기 위해
            try:
                import build_stock_master as _bsm
                new_path = _bsm.ensure_stock_csv(code, stock_name)
            except Exception:
                new_path = None
            if new_path and os.path.exists(new_path):
                file_path = new_path
            else:
                return None, f"데이터 파일(A{code}.csv)이 없어 생성에 실패했습니다."

    df = pd.read_csv(file_path, encoding="utf-8-sig")
    df["date"] = pd.to_datetime(df["date"])
    if stock_name:
        df["name"] = stock_name
    elif "name" in df.columns:
        stock_name = df["name"].iloc[0]

    vol = inquiry.safe_int(rt.get("acml_vol"))
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
        "prog_ratio_vol": p_ratio,
    }

    df = df[df["date"] != target_date]
    df = pd.concat([df, pd.DataFrame([today_row])]).sort_values("date").reset_index(drop=True)
    df = indicators.calculate_indicators_v3_save(df)

    if "prog_net_ratio" not in df.columns:
        df["prog_net_ratio"] = df.apply(
            lambda x: x["prog_net_qty"] / x["volume"] if x["volume"] > 0 else 0, axis=1
        )
    for col in V3_FEATURES:
        if col not in df.columns:
            df[col] = 0.0
    df = df.fillna(0)

    # 원자적 저장 (temp + os.replace) — 동시 쓰기로 인한 줄바꿈 유실/파일 손상 방지
    _tmp_path = file_path + ".tmp"
    df.to_csv(_tmp_path, index=False, encoding="utf-8-sig")
    os.replace(_tmp_path, file_path)

    if len(df) < max_lb:
        return None, f"데이터 부족 (필요: {max_lb}행, 현재: {len(df)}행)"

    # 예측
    probs  = {}
    s_sum, d_sum   = 0.0, 0.0
    s_hits, d_hits = 0, 0

    for m_name, info in models.items():
        try:
            window    = df.iloc[-info["lookback"]:][V3_FEATURES].values
            scaled    = info["scaler"].transform(window).reshape(1, info["lookback"], len(V3_FEATURES))
            tensor_in = tf.convert_to_tensor(scaled, dtype=tf.float32)
            prob      = float(info["model"](tensor_in, training=False)[0, 0])
            probs[m_name] = round(prob, 4)
            if prob > info["threshold"]:
                if info["type"] == "surge":
                    s_sum += prob * info["weight"]; s_hits += 1
                else:
                    d_sum += prob * info["weight"]; d_hits += 1
        except Exception:
            probs[m_name] = 0.0

    total_score = round(s_sum - d_sum, 4)
    cap         = inquiry.fetch_market_cap(code)
    avg_val     = (df["close"] * df["volume"]).tail(20).mean() if len(df) >= 20 else 0
    change_pct  = today_row["change_pct"]

    result = {
        "code": code, "name": stock_name, "kind": kind,
        "curr": curr, "change_pct": change_pct,
        "cap": cap, "avg_val": avg_val,
        "p_net": p_net,
        "total_score": total_score,
        "s_hits": s_hits, "d_hits": d_hits,
        "probs": probs,
    }

    return result, None


def _save_search_log(code, name, curr, cap, change_pct, total_score, s_hits, d_hits, is_etf, probs, chat_id=None):
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)

    today_str  = datetime.now().strftime("%Y%m%d")
    hist_path  = os.path.join(LOG_DIR, f"{today_str}_Search_History.csv")
    hist_exists = os.path.exists(hist_path)
    hist_fields = ["timestamp", "code", "name", "current_price", "market_cap",
                   "change_pct", "total_score", "net_hits", "surge_hits", "drop_hits", "signal", "chat_id"]

    type_str   = "ETF" if is_etf else "Stock"
    v3_path    = os.path.join(LOG_DIR, f"{today_str}_{type_str}_V3.csv")
    v3_exists  = os.path.exists(v3_path)
    v3_fields  = ["code", "name", "close_price", "market_cap", "score_total",
                  "net_hits", "surge_hits", "drop_hits", "time",
                  "target1", "target5", "target20", "drop1", "drop5", "drop20"]

    try:
        new_row = {
            "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "code": code, "name": name,
            "current_price": curr, "market_cap": cap,
            "change_pct":    round(change_pct * 100, 2),
            "total_score":   total_score,
            "net_hits":      s_hits - d_hits,
            "surge_hits":    s_hits, "drop_hits": d_hits,
            "signal":        f"Target {s_hits} / Drop {d_hits}",
            "chat_id":       str(chat_id) if chat_id else "",
        }
        if hist_exists:
            df = _read_history_csv(hist_path)
            cid = str(chat_id) if chat_id else ""
            dup = (df['code'].astype(str).str.strip().str.zfill(6) == str(code)) & \
                  (df.get('chat_id', pd.Series(dtype=str)).fillna('') == cid)
            if dup.any():
                # 기존 행 업데이트
                for col, val in new_row.items():
                    if col in df.columns:
                        df.loc[dup, col] = val
                df.to_csv(hist_path, index=False, encoding='utf-8-sig')
                return  # 파일 저장 완료, 아래 append 생략
        with open(hist_path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=hist_fields, extrasaction="ignore")
            if not hist_exists:
                w.writeheader()
            w.writerow(new_row)
    except Exception as e:
        print(f"⚠️ 검색기록 저장 실패: {e}")

    try:
        with open(v3_path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=v3_fields, extrasaction="ignore")
            if not v3_exists:
                w.writeheader()
            w.writerow({
                "code": code, "name": name,
                "close_price": curr, "market_cap": cap,
                "score_total": total_score,
                "net_hits":    s_hits - d_hits,
                "surge_hits":  s_hits, "drop_hits": d_hits,
                "time":        datetime.now().strftime("%H:%M:%S"),
                **{k: probs.get(k, 0) for k in ["target1", "target5", "target20", "drop1", "drop5", "drop20"]},
            })
    except Exception as e:
        print(f"⚠️ V3 로그 저장 실패: {e}")


def _read_history_csv(hist_path):
    """컬럼 수가 다른 구/신 행이 섞인 Search_History.csv를 안전하게 읽는다."""
    try:
        return pd.read_csv(hist_path, encoding='utf-8-sig', dtype=str, on_bad_lines='skip')
    except TypeError:
        # pandas < 1.3 fallback
        return pd.read_csv(hist_path, encoding='utf-8-sig', dtype=str, error_bad_lines=False)


def delete_from_history(code, chat_id, today_str):
    """Search_History에서 해당 code + chat_id 행을 삭제. (삭제수, 종목명) 반환."""
    hist_path = os.path.join(LOG_DIR, f"{today_str}_Search_History.csv")
    if not os.path.exists(hist_path):
        return 0, ""
    try:
        df = _read_history_csv(hist_path)
        if 'code' not in df.columns or 'chat_id' not in df.columns:
            return 0, ""
        df['code'] = df['code'].apply(lambda x: str(x).strip().zfill(6) if str(x).strip().isdigit() else str(x).strip())
        mask = (df['code'] == code) & (df['chat_id'].fillna('') == str(chat_id))
        name = df.loc[mask, 'name'].iloc[0] if mask.any() else ""
        removed = int(mask.sum())
        df[~mask].to_csv(hist_path, index=False, encoding='utf-8-sig')
        return removed, name
    except Exception as e:
        print(f"⚠️ 삭제 실패: {e}")
        return -1, ""


def get_my_watchlist(chat_id, today_str):
    """오늘 Search_History에서 해당 chat_id가 추가한 종목 목록 반환. [(code, name, total_score, timestamp), ...]"""
    hist_path = os.path.join(LOG_DIR, f"{today_str}_Search_History.csv")
    if not os.path.exists(hist_path):
        return []
    try:
        df = _read_history_csv(hist_path)
        if 'code' not in df.columns or 'chat_id' not in df.columns:
            return []
        df['code'] = df['code'].apply(lambda x: str(x).strip().zfill(6) if str(x).strip().isdigit() else str(x).strip())
        df = df[df['chat_id'].fillna('') == str(chat_id)]
        df = df.sort_values('timestamp').drop_duplicates(subset='code', keep='last')
        return [
            (row['code'], row.get('name', ''), row.get('total_score', ''), row.get('timestamp', ''))
            for _, row in df.iterrows()
        ]
    except Exception as e:
        print(f"⚠️ 목록 조회 실패: {e}")
        return []


def add_holdings_to_history(holdings, chat_id, today_str):
    """키움 보유종목을 Search_History의 해당 chat_id 행으로 추가 (중복 code 건너뜀).
       반환: (added: [(code,name)], skipped: [(code,name)])"""
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)
    hist_path   = os.path.join(LOG_DIR, f"{today_str}_Search_History.csv")
    hist_exists = os.path.exists(hist_path)
    hist_fields = ["timestamp", "code", "name", "current_price", "market_cap",
                   "change_pct", "total_score", "net_hits", "surge_hits", "drop_hits", "signal", "chat_id"]
    cid = str(chat_id)

    # 기존 내 종목 코드 집합
    existing = set()
    if hist_exists:
        try:
            df = _read_history_csv(hist_path)
            if 'code' in df.columns and 'chat_id' in df.columns:
                mine = df[df['chat_id'].fillna('') == cid]
                existing = set(mine['code'].apply(
                    lambda x: str(x).strip().zfill(6) if str(x).strip().isdigit() else str(x).strip()))
        except Exception as e:
            print(f"⚠️ 보유종목 추가-기존조회 실패: {e}")

    added, skipped, rows_to_add = [], [], []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for h in holdings:
        code = str(h['code']).strip().zfill(6)
        name = h.get('name', '')
        if code in existing:
            skipped.append((code, name))
            continue
        existing.add(code)
        rows_to_add.append({
            "timestamp": now_str, "code": code, "name": name,
            "signal": "보유종목", "chat_id": cid,
        })
        added.append((code, name))

    if rows_to_add:
        try:
            with open(hist_path, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=hist_fields, extrasaction="ignore")
                if not hist_exists:
                    w.writeheader()
                for r in rows_to_add:
                    w.writerow(r)
        except Exception as e:
            print(f"⚠️ 보유종목 추가 저장 실패: {e}")
            return [], skipped
    return added, skipped


def cleanup_history_by_holdings(chat_id, today_str, holding_codes):
    """내 Search_History 종목 중 키움 보유에 없는 종목 삭제.
       반환: removed [(code,name)] (오류 시 None)"""
    hist_path = os.path.join(LOG_DIR, f"{today_str}_Search_History.csv")
    if not os.path.exists(hist_path):
        return []
    try:
        df = _read_history_csv(hist_path)
        if 'code' not in df.columns or 'chat_id' not in df.columns:
            return []
        df['code'] = df['code'].apply(
            lambda x: str(x).strip().zfill(6) if str(x).strip().isdigit() else str(x).strip())
        cid = str(chat_id)
        holding_set = set(str(c).strip().zfill(6) for c in holding_codes)
        mask_mine   = df['chat_id'].fillna('') == cid
        mask_remove = mask_mine & (~df['code'].isin(holding_set))

        removed, seen = [], set()
        for _, r in df[mask_remove].iterrows():
            c = r['code']
            if c not in seen:
                seen.add(c)
                removed.append((c, r.get('name', '')))
        df[~mask_remove].to_csv(hist_path, index=False, encoding='utf-8-sig')
        return removed
    except Exception as e:
        print(f"⚠️ 종목정리 실패: {e}")
        return None


def get_all_watchlists(today_str):
    """오늘 Search_History 전체를 chat_id별로 묶어 반환. {chat_id: [(code, name, score, ts), ...]}"""
    hist_path = os.path.join(LOG_DIR, f"{today_str}_Search_History.csv")
    if not os.path.exists(hist_path):
        return {}
    try:
        df = _read_history_csv(hist_path)
        if 'code' not in df.columns or 'chat_id' not in df.columns:
            return {}
        df['code'] = df['code'].apply(lambda x: str(x).strip().zfill(6) if str(x).strip().isdigit() else str(x).strip())
        df = df.sort_values('timestamp').drop_duplicates(subset=['code', 'chat_id'], keep='last')
        result = {}
        for cid, group in df.groupby(df['chat_id'].fillna('')):
            if not cid:
                continue
            result[cid] = [
                (row['code'], row.get('name', ''), row.get('total_score', ''), row.get('timestamp', ''))
                for _, row in group.iterrows()
            ]
        return result
    except Exception as e:
        print(f"⚠️ 전체 목록 조회 실패: {e}")
        return {}


# ====================================================
# 📨 응답 메시지 포맷
# ====================================================
def format_result(result: dict) -> str:
    score     = result["total_score"]
    score_100 = score * 100
    probs     = result["probs"]

    # 매수 등급
    if score >= 0.8:
        grade = "⭐⭐⭐ 강력매수 (80점+)"
    elif score >= 0.7:
        grade = "⭐⭐ 매수우세 (70점+)"
    elif score >= 0.6:
        grade = "⭐ 관심 (60점+)"
    elif score >= 0.5:
        grade = "👀 주시 (50점+)"
    elif score <= 0:
        grade = "🔴 매도우세"
    else:
        grade = "⚪ 중립"

    lines = [
        f"📊 {result['name']} ({result['code']}) [{result['kind']}]",
        f"💰 현재가: {result['curr']:,}원  ({result['change_pct']*100:+.2f}%)",
        f"🏢 시총: {result['cap']:,}억  |  20일평균거래대금: {result['avg_val']/1e8:.1f}억",
        f"🤖 프로그램 순매수: {result['p_net']:,}주",
        "",
        f"🎯 종합점수: {score_100:.1f}점  →  {grade}",
        f"🚩 매수시그널: {result['s_hits']}개  /  매도시그널: {result['d_hits']}개",
        "",
        "[ 상세 확률 ]",
        f"  target1 : {probs.get('target1',0)*100:.1f}%",
        f"  target5 : {probs.get('target5',0)*100:.1f}%",
        f"  target20: {probs.get('target20',0)*100:.1f}%",
        f"  drop1   : {probs.get('drop1',0)*100:.1f}%",
        f"  drop5   : {probs.get('drop5',0)*100:.1f}%",
        f"  drop20  : {probs.get('drop20',0)*100:.1f}%",
        "",
        f"🕐 {datetime.now().strftime('%H:%M:%S')}",
    ]
    return "\n".join(lines)


# ====================================================
# 🔄 메인 루프
# ====================================================
def run():
    print("=" * 55)
    print("📱 텔레그램 종목 조회 봇 시작")
    print("=" * 55)

    if not auth.get_access_token():
        print("❌ 토큰 발급 실패")
        return

    set_bot_commands()   # 입력란 '/' 드롭다운 메뉴 등록

    models = load_models()
    if not models:
        print("❌ 모델 로드 실패")
        return

    max_lb     = max(m["lookback"] for m in models.values())
    name_cache = build_name_cache()

    print(f"\n✅ 준비 완료. 텔레그램 메시지 대기 중...\n")

    offset   = None
    visitors = {}  # {chat_id: {"name": ..., "username": ..., "last_seen": ..., "count": ...}}

    while True:
        try:
            updates = get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
                msg     = update.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                text    = msg.get("text", "").strip()

                if not text or not chat_id:
                    continue

                # 발신자 정보 출력 (chat_id 확인용)
                user      = msg.get("from", {})
                username  = user.get("username", "")
                firstname = user.get("first_name", "")
                known     = str(chat_id) in secrets.TELEGRAM_NOTIFY_IDS
                known_str = "✅ 등록됨" if known else "🚫 미등록"
                print(f"📩 [{chat_id}] {firstname}(@{username}) {known_str} | 입력: '{text}'", flush=True)

                # 방문자 기록 (등록 여부 무관하게 모두 기록)
                visitors[chat_id] = {
                    "name":      firstname,
                    "username":  username,
                    "known":     known,
                    "last_seen": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "count":     visitors.get(chat_id, {}).get("count", 0) + 1,
                }

                # 미등록 사용자 차단
                if not known:
                    send_message(chat_id, "❌ 권한이 없습니다.")
                    continue

                # /start
                if text == "/start":
                    send_message(chat_id,
                        "안녕하세요! 주식 분석 봇입니다.\n"
                        "종목코드(6자리) 또는 종목명을 입력하면 V3 모델 분석 결과를 알려드립니다.\n\n"
                        "자세한 사용법은 /help 를 입력하세요."
                    )
                    continue

                # /help
                if text == "/help":
                    help_text = (
                        "📖 사용법 안내\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "🔍 종목 분석\n"
                        "  종목코드(6자리) 또는 종목명 입력\n"
                        "  예) 005930  /  삼성전자  /  카카오\n"
                        "  → 현재가·시총·종합점수·매수/매도 시그널 표시\n"
                        "  → 분석한 종목은 자동으로 추적 목록에 추가됨\n"
                        "\n"
                        "📋 /list\n"
                        "  내가 추적 중인 종목 목록 조회\n"
                        "  3일 평활점수가 0.20 아래로 떨어지면 매도 시그널 발송\n"
                        "\n"
                        "🏆 /top5  /top10  /top20\n"
                        "  오늘 추적된 전체 종목 중 점수 상위 N개 조회\n"
                        "\n"
                        "🗑️ /del <종목>\n"
                        "  추적 목록에서 종목 삭제\n"
                        "  예) /del 005930  /  /del 삼성전자\n"
                        "  ※ 종목명은 정확히 입력해야 합니다\n"
                        "\n"
                    )
                    # 보유/매매 관련 명령은 소유자에게만 안내
                    if str(chat_id) == secrets.TELEGRAM_CHAT_ID:
                        help_text += (
                            "📥 /보유종목 (또는 /holdings)\n"
                            "  키움 계좌 보유종목을 내 추적 목록에 추가 (중복 제외)\n"
                            "\n"
                            "🧹 /종목정리 (또는 /cleanup)\n"
                            "  내 추적 목록에서 키움 미보유 종목 삭제\n"
                            "\n"
                        )
                    help_text += (
                        "💡 입력란의 '/' 버튼을 누르면 명령어 메뉴가 표시됩니다.\n"
                        "\n"
                        "🚨 매도 시그널 기준 (B전략)\n"
                        "  · 3일 평활점수 < 0.20 이 2거래일 연속 → 전량청산\n"
                        "  · 현재가가 평단 대비 -12% 이하 → 즉시 손절\n"
                        "  ※ 하루 급락만으로는 매도하지 않음 (휩쏘 방지)\n"
                        "\n"
                        "👥 /users\n"
                        "  봇 방문자 목록 조회"
                    )
                    send_message(chat_id, help_text)
                    continue

                # /보유종목 (=/holdings) — 키움 보유종목을 내 추적목록(Search_History)에 추가 (중복 건너뜀)
                if text in ("/보유종목", "/holdings"):
                    if str(chat_id) != secrets.TELEGRAM_CHAT_ID:
                        send_message(chat_id, "🔒 이 명령은 봇 소유자만 사용할 수 있습니다.")
                        continue
                    today_str_now = datetime.now().strftime("%Y%m%d")
                    holdings = kiwoom_trading.fetch_all_holdings()
                    if not holdings:
                        send_message(chat_id, "📭 키움 계좌에 보유종목이 없거나 조회에 실패했습니다.")
                        continue
                    added, skipped = add_holdings_to_history(holdings, chat_id, today_str_now)
                    lines = [f"📥 키움 보유종목 {len(holdings)}개 → 추가 {len(added)}개 / 기존 {len(skipped)}개"]
                    if added:
                        lines.append("\n[추가됨]")
                        lines += [f"• {n} ({c})" for c, n in added]
                    send_message(chat_id, "\n".join(lines))
                    print(f"   📥 [{chat_id}] 보유종목 추가 {len(added)}개 / 스킵 {len(skipped)}개", flush=True)
                    continue

                # /종목정리 (=/cleanup) — 내 추적목록 중 키움 미보유 종목 삭제
                if text in ("/종목정리", "/cleanup"):
                    if str(chat_id) != secrets.TELEGRAM_CHAT_ID:
                        send_message(chat_id, "🔒 이 명령은 봇 소유자만 사용할 수 있습니다.")
                        continue
                    today_str_now = datetime.now().strftime("%Y%m%d")
                    holdings = kiwoom_trading.fetch_all_holdings()
                    if holdings is None:
                        # 조회 실패 → 전체 삭제 방지를 위해 중단
                        send_message(chat_id, "❌ 키움 보유종목 조회에 실패했습니다. 정리를 중단합니다.")
                        continue
                    holding_codes = [h['code'] for h in holdings]
                    removed = cleanup_history_by_holdings(chat_id, today_str_now, holding_codes)
                    if removed is None:
                        send_message(chat_id, "❌ 종목정리 중 오류가 발생했습니다.")
                    elif not removed:
                        send_message(chat_id, "✅ 정리할 종목이 없습니다. (추적목록이 모두 보유종목입니다)")
                    else:
                        lines = [f"🧹 키움 미보유 {len(removed)}개 종목을 추적목록에서 삭제했습니다.\n"]
                        lines += [f"• {n} ({c})" for c, n in removed]
                        send_message(chat_id, "\n".join(lines))
                    print(f"   🧹 [{chat_id}] 종목정리 삭제 {0 if not removed else len(removed)}개", flush=True)
                    continue

                # 그 외 슬래시 커맨드
                if text.startswith("/") and text.split()[0] not in ("/users", "/del", "/list", "/top5", "/top10", "/top20", "/top"):
                    send_message(chat_id, "알 수 없는 명령어입니다. /help 를 입력하면 사용법을 확인할 수 있습니다.")
                    continue

                # /top5 /top10 /top20 /top N — 점수 상위 N종목
                _top_n = 0
                _cmd = text.split()[0].lower()
                if _cmd in ("/top5", "/top10", "/top20"):
                    _top_n = int(_cmd[4:])
                elif _cmd == "/top" and len(text.split()) > 1:
                    try:
                        _top_n = int(text.split()[1])
                    except ValueError:
                        pass

                if _top_n > 0:
                    today_str_now = datetime.now().strftime("%Y%m%d")
                    stock_path = os.path.join(LOG_DIR, f"{today_str_now}_Stock_V3.csv")
                    if not os.path.exists(stock_path):
                        send_message(chat_id, "📭 오늘 집계된 종목이 없습니다.")
                    else:
                        try:
                            df_v = pd.read_csv(stock_path, encoding="utf-8-sig", dtype=str)
                            df_v["score_total"] = pd.to_numeric(df_v["score_total"], errors="coerce").fillna(0)
                            df_v = df_v.sort_values("score_total", ascending=False).head(_top_n)
                            if df_v.empty:
                                send_message(chat_id, "📭 오늘 집계된 종목이 없습니다.")
                            else:
                                lines = [f"🏆 점수 상위 {_top_n}종목 ({today_str_now})\n"]
                                for rank, (_, row) in enumerate(df_v.iterrows(), 1):
                                    score = float(row["score_total"]) * 100
                                    price = row.get("close_price", "-")
                                    time_str = row.get("time", "")
                                    lines.append(
                                        f"{rank:2d}. {row.get('name','')} ({row['code']})\n"
                                        f"    점수: {score:.1f}점  현재가: {price}원  {time_str}"
                                    )
                                send_message(chat_id, "\n".join(lines))
                        except Exception as e:
                            send_message(chat_id, f"❌ top 조회 중 오류: {e}")
                    continue

                # /list — 추적 종목 목록
                if text == "/list":
                    today_str_now = datetime.now().strftime("%Y%m%d")
                    is_owner = str(chat_id) == secrets.TELEGRAM_NOTIFY_IDS[0]

                    def _format_stock_line(code, name, score, ts):
                        try:
                            score_str = f"{float(score)*100:.1f}점" if score else "-"
                        except (ValueError, TypeError):
                            score_str = "-"
                        time_str = ts[11:16] if len(str(ts)) >= 16 else str(ts)
                        return f"• {name} ({code})  {score_str}  [{time_str}]"

                    if is_owner:
                        all_lists = get_all_watchlists(today_str_now)
                        if not all_lists:
                            send_message(chat_id, "📋 오늘 추적 중인 종목이 없습니다.")
                        else:
                            lines = []
                            for cid, stocks in all_lists.items():
                                info = visitors.get(int(cid) if str(cid).isdigit() else cid, {})
                                user_label = info.get('name', '') or info.get('username', '') or cid
                                lines.append(f"👤 {user_label} ({len(stocks)}개)")
                                for item in stocks:
                                    lines.append(_format_stock_line(*item))
                                lines.append("")
                            send_message(chat_id, "\n".join(lines).strip())
                    else:
                        watchlist = get_my_watchlist(chat_id, today_str_now)
                        if not watchlist:
                            send_message(chat_id, "📋 오늘 추적 중인 종목이 없습니다.\n종목을 검색하면 자동으로 추적 목록에 추가됩니다.")
                        else:
                            lines = [f"📋 내 추적 종목 ({len(watchlist)}개)\n"]
                            for item in watchlist:
                                lines.append(_format_stock_line(*item))
                            send_message(chat_id, "\n".join(lines))
                    continue

                # /del — 검색 기록에서 삭제 (완전 일치만 허용)
                if text.lower().startswith("/del"):
                    parts = text.split(maxsplit=1)
                    if len(parts) < 2:
                        send_message(chat_id, "사용법: /del <종목코드 또는 종목명>\n예) /del 005930  /  /del 삼성전자")
                        continue
                    query = parts[1].strip()
                    q = query.strip()
                    if q.isdigit() and len(q) == 6:
                        del_code = q
                    elif q.lower() in name_cache:
                        del_code, _ = name_cache[q.lower()]
                    else:
                        del_code = None
                    if not del_code:
                        send_message(chat_id, f"❌ '{query}' 종목을 찾을 수 없습니다.")
                        continue
                    today_str_now = datetime.now().strftime("%Y%m%d")
                    removed, del_name = delete_from_history(del_code, chat_id, today_str_now)
                    if removed > 0:
                        send_message(chat_id, f"🗑️ {del_name} ({del_code}) 추적 목록에서 삭제됐습니다.")
                        print(f"   🗑️ [{chat_id}] {del_name}({del_code}) 삭제", flush=True)
                    elif removed == 0:
                        send_message(chat_id, f"⚠️ {del_code} 종목이 내 추적 목록에 없습니다.")
                    else:
                        send_message(chat_id, "❌ 삭제 중 오류가 발생했습니다.")
                    continue

                # /users — 방문자 목록 (등록 사용자만 조회 가능)
                if text == "/users":
                    if not visitors:
                        send_message(chat_id, "아직 방문자가 없습니다.")
                    else:
                        lines = ["👥 방문자 목록\n"]
                        for vid, info in visitors.items():
                            status = "✅ 등록" if info["known"] else "🚫 미등록"
                            lines.append(
                                f"{status} {info['name']}(@{info['username']})\n"
                                f"  ID: {vid}\n"
                                f"  마지막: {info['last_seen']}  ({info['count']}회)"
                            )
                        send_message(chat_id, "\n\n".join(lines))
                    continue
                send_message(chat_id, f"⏳ [{text}] 분석 중...")

                code, is_etf_hint = find_by_query(text, name_cache)

                if not code:
                    send_message(chat_id, f"❌ '{text}' 종목을 찾을 수 없습니다.\n종목코드 6자리 또는 정확한 종목명을 입력해주세요.")
                    continue

                result, err = analyze(code, models, max_lb)

                if err:
                    send_message(chat_id, f"❌ {err}")
                else:
                    send_message(chat_id, format_result(result))
                    _save_search_log(
                        result['code'], result['name'], result['curr'], result['cap'],
                        result['change_pct'], result['total_score'],
                        result['s_hits'], result['d_hits'],
                        result['kind'] in ('ETF', 'ETN'), result['probs'],
                        chat_id,
                    )
                    print(f"   ✅ [{code}] {result['name']} 전송 완료", flush=True)

        except KeyboardInterrupt:
            print("\n🛑 종료")
            break
        except Exception as e:
            print(f"⚠️ 루프 오류: {e}")
            time.sleep(5)


if __name__ == "__main__":
    run()