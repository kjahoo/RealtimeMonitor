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
from kis_api import auth, inquiry, indicators
from tensorflow.keras.models import load_model

# ====================================================
# ⚙️ 설정
# ====================================================
MODEL_DIR      = secrets.V3_MODEL_DIR
DATA_DIR_STOCK = os.path.join(PROJECT_DIR, "Data", "Stock")
DATA_DIR_ETF   = os.path.join(PROJECT_DIR, "Data", "ETF")
LOG_DIR        = os.path.join(PROJECT_DIR, "logs")

MODEL_SETTINGS = {
    "target1":  {"lb": 21,  "thr": 0.4974, "weight": 0.1384},
    "target5":  {"lb": 50,  "thr": 0.6327, "weight": 0.3099},
    "target20": {"lb": 60,  "thr": 0.9046, "weight": 0.5517},
    "drop1":    {"lb": 10,  "thr": 0.4349, "weight": 0.2411},
    "drop5":    {"lb": 94,  "thr": 0.4314, "weight": 0.3714},
    "drop20":   {"lb": 98,  "thr": 0.4686, "weight": 0.3875},
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
            return None, f"데이터 파일(A{code}.csv)이 없습니다."

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

    df.to_csv(file_path, index=False, encoding="utf-8-sig")

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


def _save_search_log(code, name, curr, cap, change_pct, total_score, s_hits, d_hits, is_etf, probs):
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)

    today_str  = datetime.now().strftime("%Y%m%d")
    hist_path  = os.path.join(LOG_DIR, f"{today_str}_Search_History.csv")
    hist_exists = os.path.exists(hist_path)
    hist_fields = ["timestamp", "code", "name", "current_price", "market_cap",
                   "change_pct", "total_score", "net_hits", "surge_hits", "drop_hits", "signal"]

    type_str   = "ETF" if is_etf else "Stock"
    v3_path    = os.path.join(LOG_DIR, f"{today_str}_{type_str}_V3.csv")
    v3_exists  = os.path.exists(v3_path)
    v3_fields  = ["code", "name", "close_price", "market_cap", "score_total",
                  "net_hits", "surge_hits", "drop_hits", "time",
                  "target1", "target5", "target20", "drop1", "drop5", "drop20"]

    try:
        with open(hist_path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=hist_fields, extrasaction="ignore")
            if not hist_exists:
                w.writeheader()
            w.writerow({
                "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "code": code, "name": name,
                "current_price": curr, "market_cap": cap,
                "change_pct":    round(change_pct * 100, 2),
                "total_score":   total_score,
                "net_hits":      s_hits - d_hits,
                "surge_hits":    s_hits, "drop_hits": d_hits,
                "signal":        f"Target {s_hits} / Drop {d_hits}",
            })
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

                # /start, /help
                if text.startswith("/") and text.split()[0] not in ("/users",):
                    send_message(chat_id,
                        "📱 종목 조회 봇\n\n"
                        "종목코드(6자리) 또는 종목명을 입력하세요.\n"
                        "예) 005930  /  삼성전자  /  카카오\n\n"
                        "/users  봇 방문자 목록"
                    )
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
                    print(f"   ✅ [{code}] {result['name']} 전송 완료", flush=True)

        except KeyboardInterrupt:
            print("\n🛑 종료")
            break
        except Exception as e:
            print(f"⚠️ 루프 오류: {e}")
            time.sleep(5)


if __name__ == "__main__":
    run()