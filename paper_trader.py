# -*- coding: utf-8 -*-
"""
paper_trader.py — 'prod 무후보 시 BULL 대체' 전략 실시간 가상매매 기록 데몬 (실주문 없음)
==========================================================================================
배경: regime_analysis/drought_*.py 백테스트에서 유력했던 전략을 실시간으로 가상 체결·기록해 검증.

가상 포트폴리오 (각 INIT_CAPITAL, PORTFOLIOS 에서 조정)
  BULL_K1_keep : 오늘 prod 0.60+ 후보가 0개면 BULL 모델이 매수
  BULL_K3_keep : 오늘 포함 3거래일 연속 prod 무후보면 BULL 모델이 매수
  PROD         : 같은 규칙의 prod 단독 — 기준선 (실계좌는 Claude 평가·체결 방식이 달라 직접 비교 불가)
  keep = 산 모델의 점수·규칙으로 청산까지 보유, 다른 모델이 산 종목엔 추가매수 안 함

데이터 (API 호출 없음 — main_stock 이 쓴 파일만 읽음)
  prod 점수 : logs/{오늘}_Stock_V3.csv  (정규장 09:00~15:30 에 계산된 행만)
  BULL 점수 : Data/Stock/A{code}.csv 오늘 행(main_stock 이 매 사이클 지표 포함 갱신) → Model_regime/BULL
  가격      : 같은 CSV 의 오늘 행 close (main_stock 이 받은 현재가)
  과거 점수 : 매일 장전, 전일까지 확정된 CSV 로 prod·BULL 종가 점수 재계산
              → 평활 청산 이력(최근 2일 raw, 평활<0.10 연속일) · prod 무후보 연속일

매매규칙 (라이브와 동일: auto_buy.py / kis_api/sell_strategy_b.py 상수 사용)
  매수: 점수 ≥ 0.60, 구간 비중 60점대 5% … 100점 25% (전일 평가자산 기준 목표수량, 증액만),
        점수 높은 순, 현금 한도 내. 당일 매도 종목 재매수 안 함
  매도: ① 평단 −14% 손절(raw ≥ 0.60 면제) ② raw < 0 30분 연속(15:00 이후 즉시)
        ③ raw < 0.10 & 3일 평활 < 0.10 & 2일 연속
  체결: 판정 시점 현재가, 수수료 매수 0.015% / 매도 0.195% (백테스트와 동일)
  main_stock 의 정규장 첫 전종목 스캔(유니버스 CSV 80% 이상 갱신) 완료 후부터 체결
  ※ 라이브의 Claude AI 'BUY' 게이트는 적용하지 않음(백테스트와 같은 순수 모델 규칙)

알림(본인만): 장중 가상 체결 즉시 · 장 마감 후 전체 정리
기록: logs/paper_trader/  trades.csv · daily.csv · holdings.csv · state.json · paper_trader.log

실행: python paper_trader.py                       (scheduler 가 상시 기동, 중복 실행 시 대기)
      python paper_trader.py --selftest [YYYYMMDD]   (해당일 데이터로 장중 2틱+마감 모의,
                                                     텔레그램 대신 출력, logs/paper_trader_selftest/)
"""
import os
import sys

os.environ["CUDA_VISIBLE_DEVICES"] = "-1"      # GPU 는 main_stock 전용 — 여기선 CPU
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"

import argparse
import io
import json
import pickle
import shutil
import time
import traceback
import warnings
from datetime import datetime, time as dtime, timedelta

import numpy as np
import pandas as pd
import requests
import tensorflow as tf
from tensorflow.keras.models import load_model

warnings.filterwarnings("ignore")
tf.config.threading.set_intra_op_parallelism_threads(4)
tf.config.threading.set_inter_op_parallelism_threads(2)

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from config import secrets                        # noqa: E402
from kis_api import kiwoom_trading as kt          # noqa: E402  휴장 판정만 사용(API 호출 없음)
from kis_api import sell_strategy_b as sb         # noqa: E402  매도 규칙 상수만 사용

# ── 설정 ──────────────────────────────────────────────────────────────────────
INIT_CAPITAL = 100_000_000
PORTFOLIOS = {
    "BULL_K1_keep": {"fallback": "BULL", "K": 1, "label": "BULL K1"},
    "BULL_K3_keep": {"fallback": "BULL", "K": 3, "label": "BULL K3"},
    "PROD":         {"fallback": None,   "K": None, "label": "PROD(기준선)"},
}
BUY_MIN = 0.60            # auto_buy.SCORE_MIN / 100
ALLOC_MAX = 25.0          # auto_buy.ALLOC_MAX
BUY_FEE = 0.00015
SELL_FEE = 0.00195
MIN_AVG_TRADE_VALUE = 1_000_000_000   # main_stock: 최근 20일 평균 거래대금 10억 미만 제외
TICK_SEC = 60
READY_RATIO = 0.8
HIST_DAYS = 20
PREP_TIME = dtime(7, 30)
MARKET_OPEN, MARKET_CLOSE = kt.MARKET_OPEN, kt.MARKET_CLOSE

DATA_DIR = os.path.join(ROOT, "Data", "Stock")
LOG_DIR = os.path.join(ROOT, "logs")
MODEL_DIRS = {"prod": secrets.V3_MODEL_DIR, "BULL": os.path.join(ROOT, "Model_regime", "BULL")}
V3_FEATURES = ["change_pct", "volume_ratio", "vol_power", "prog_net_ratio", "prog_ratio_vol",
               "disparity_5", "disparity_20", "rsi", "bb_p", "bb_w", "adx",
               "kospi_change", "kosdaq_change"]
NAMES = ["target1", "target5", "target20", "drop1", "drop5", "drop20"]
REASON_TXT = {"stop": "−14% 손절", "raw_neg": "raw<0 30분", "raw_neg_late": "raw<0(15시 이후)",
              "score": "평활<10 2일"}

OUT_DIR = os.path.join(LOG_DIR, "paper_trader")
SELFTEST = False


# ══════════════════════════════════════════════════════════════════════════════
# 공통 유틸
# ══════════════════════════════════════════════════════════════════════════════
def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(OUT_DIR, "paper_trader.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def send_owner(msg):
    """본인에게만 텔레그램 (auto_buy._send_owner 와 동일 방식). 셀프테스트는 출력만."""
    chunks, cur = [], ""
    for line in msg.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        if SELFTEST or not secrets.TELEGRAM_BOT_TOKEN:
            print("\n----- [텔레그램 미발송: 셀프테스트] -----\n" + c)
            continue
        try:
            requests.post(f"https://api.telegram.org/bot{secrets.TELEGRAM_BOT_TOKEN}/sendMessage",
                          data={"chat_id": str(secrets.TELEGRAM_CHAT_ID), "text": c}, timeout=5)
        except Exception as e:
            log(f"⚠️ 텔레그램 전송 실패: {e}")


def alloc_pct(score):
    """auto_buy._alloc_pct 와 동일 (점수 0~1)."""
    bucket = int(np.floor(score * 100 + 1e-6) // 10)
    return max(0.0, min(ALLOC_MAX, (bucket - 5) * 5.0))


def append_csv(name, rows):
    if not rows:
        return
    p = os.path.join(OUT_DIR, name)
    pd.DataFrame(rows).to_csv(p, mode="a", header=not os.path.exists(p), index=False, encoding="utf-8-sig")


def won(x):
    return f"{x:,.0f}"


def trading_days_between(d0, d1):
    """d0(YYYYMMDD) 다음 거래일부터 d1 까지 거래일 수."""
    hol = [datetime.strptime(h, "%Y%m%d").date() for h in kt.KRX_HOLIDAYS]
    a = datetime.strptime(d0, "%Y%m%d").date()
    b = datetime.strptime(d1, "%Y%m%d").date()
    return int(np.busday_count(a, b, holidays=hol))


def read_tail(path, n):
    """큰 CSV 의 헤더 + 마지막 n행만 읽음 (종목 CSV 는 수 MB)."""
    with open(path, "rb") as f:
        header = f.readline().decode("utf-8-sig").strip()
        f.seek(0, 2)
        end = f.tell()
        size = min(end, max(8192, n * 600))
        while True:
            f.seek(end - size)
            chunk = f.read(size)
            if chunk.count(b"\n") > n + 1 or size >= end:
                break
            size = min(end, size * 2)
    lines = chunk.decode("utf-8", errors="ignore").splitlines()[1:]   # 첫 줄은 잘렸거나 헤더
    lines = [ln for ln in lines if ln.strip()][-n:]
    return pd.read_csv(io.StringIO(header + "\n" + "\n".join(lines)), dtype={"code": str})


def load_code(code, n):
    p = os.path.join(DATA_DIR, f"A{code}.csv")
    if not os.path.exists(p):
        return None
    try:
        df = read_tail(p, n)
    except Exception:
        return None
    if df.empty or "date" not in df.columns:
        return None
    df["date"] = df["date"].astype(str).str[:10]
    for c in V3_FEATURES + ["close", "volume"]:
        if c not in df.columns:
            df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df[V3_FEATURES] = df[V3_FEATURES].fillna(0)
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 모델
# ══════════════════════════════════════════════════════════════════════════════
def load_model_set(tag):
    """백테스트(build_scores.load_model_set)와 동일: lookback=모델 입력, threshold=로그 최고 F1 행,
    가중치=급등/하락 그룹 내 F1 비례. prod 는 main_stock 하드코딩 값과 같아야 함(로드 시 출력)."""
    d = MODEL_DIRS[tag]
    out, f1 = {}, {}
    for name in NAMES:
        model = load_model(os.path.join(d, f"{name}_lstm_v3.h5"), compile=False)
        with open(os.path.join(d, f"{name}_lstm_v3.scaler"), "rb") as f:
            scaler = pickle.load(f)
        thr = fs = None
        for ln in (f"log_{name}_v3.csv", f"log_{name}_v3_v3_unified.csv"):
            lp = os.path.join(d, ln)
            if os.path.exists(lp):
                lg = pd.read_csv(lp)
                br = lg.loc[lg["f1"].idxmax()]
                thr, fs = float(br["threshold"]), float(br["f1"])
                break
        if thr is None:
            meta = json.load(open(os.path.join(d, f"{name}_lstm_v3.meta.json"), encoding="utf-8"))
            thr, fs = float(meta["threshold"]), float(meta.get("f1", 1.0))
        out[name] = {"model": model, "scaler": scaler, "lb": int(model.input_shape[1]), "thr": thr,
                     "type": "surge" if name.startswith("target") else "drop"}
        f1[name] = fs
    for grp in ("surge", "drop"):
        ns = [n for n in NAMES if out[n]["type"] == grp]
        tot = sum(f1[n] for n in ns)
        for n in ns:
            out[n]["w"] = f1[n] / tot
    log(f"모델 로드 [{tag}] " + " · ".join(f"{n} lb{i['lb']} thr{i['thr']:.4f} w{i['w']:.4f}"
                                           for n, i in out.items()))
    return out


def score_windows(models, mats):
    """mats: 2D 배열 목록(행×피처, 마지막 행 = 예측 시점). 반환: total_score (행 부족 시 nan).
    main_stock.process_stock 과 동일 공식: 확률이 threshold 초과한 모델만 가중합, 급등 − 하락."""
    n = len(mats)
    if n == 0:
        return np.array([])
    max_lb = max(i["lb"] for i in models.values())
    # 창에 inf/nan 이 있으면 main_stock 은 scaler 에서 예외 → 그 종목 점수 없음. 동일하게 제외
    ok = np.array([m.shape[0] >= max_lb and np.isfinite(m[-max_lb:]).all() for m in mats])
    idx = np.nonzero(ok)[0]
    out = np.full(n, np.nan)
    if len(idx) == 0:
        return out
    s = np.zeros(len(idx))
    d = np.zeros(len(idx))
    for info in models.values():
        lb = info["lb"]
        X = np.stack([mats[k][-lb:] for k in idx]).astype(np.float32)
        F = X.shape[2]
        Xs = info["scaler"].transform(X.reshape(-1, F)).reshape(X.shape).astype(np.float32)
        prob = np.concatenate([info["model"](Xs[a:a + 2048], training=False).numpy().ravel()
                               for a in range(0, len(Xs), 2048)])
        c = np.where(prob > info["thr"], prob * info["w"], 0.0)
        if info["type"] == "surge":
            s += c
        else:
            d += c
    out[idx] = np.round(s - d, 4)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 가상매매 엔진
# ══════════════════════════════════════════════════════════════════════════════
class PaperTrader:
    def __init__(self, models):
        self.models = models
        self.max_lb = max(i["lb"] for ms in models.values() for i in ms.values())
        self.state_path = os.path.join(OUT_DIR, "state.json")
        self.state = self._load_state()
        self.bull_cache = {}        # code -> (mtime, raw, price, kospi_chg, kosdaq_chg)
        self.boot = None            # 장전 재계산 결과(셀프테스트 검증용)

    # ── 상태 ──
    def _load_state(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            st = {}
        st.setdefault("portfolios", {})
        for p in PORTFOLIOS:
            st["portfolios"].setdefault(p, {
                "cash": float(INIT_CAPITAL), "positions": {}, "equity_prev": float(INIT_CAPITAL),
                "base": float(INIT_CAPITAL), "start": None, "sold_today": [],
                "closed": 0, "wins": 0, "ret_sum": 0.0, "today": {}})
        st.setdefault("last_price", {})
        st.setdefault("names", {})
        return st

    def save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    # ── 유니버스 ──
    def universe(self, today):
        for fn in (f"{today}_buylist.json", "buylist_latest.json"):
            p = os.path.join(LOG_DIR, fn)
            if os.path.exists(p):
                try:
                    d = json.load(open(p, encoding="utf-8"))
                    codes = [str(c).zfill(6) for c in d.get("codes", [])]
                    if codes:
                        return codes
                except Exception:
                    pass
        return []

    def held_codes(self):
        return {c for pf in self.state["portfolios"].values() for c in pf["positions"]}

    # ── 장전 준비: 과거 종가 점수 재계산 ──
    def prepare_day(self, today):
        t0 = time.time()
        today_dash = f"{today[:4]}-{today[4:6]}-{today[6:]}"
        codes = sorted(set(self.universe(today)) | self.held_codes())
        n_tail = HIST_DAYS + self.max_lb + 25
        entries, mats = [], []
        for code in codes:
            df = load_code(code, n_tail)
            if df is None:
                continue
            df = df[df["date"] < today_dash].reset_index(drop=True)
            if len(df) < self.max_lb:
                continue
            if "name" in df.columns and code not in self.state["names"]:
                self.state["names"][code] = str(df["name"].iloc[-1])
            feats = df[V3_FEATURES].to_numpy(np.float64)
            amt = (df["close"] * df["volume"]).to_numpy()
            for t in range(max(self.max_lb - 1, len(df) - HIST_DAYS), len(df)):
                elig = np.nanmean(amt[max(0, t - 19):t + 1]) >= MIN_AVG_TRADE_VALUE
                entries.append((df["date"].iloc[t], code, bool(elig), float(df["close"].iloc[t])))
                mats.append(feats[:t + 1])
            self.state["last_price"].setdefault(code, [float(df["close"].iloc[-1]), df["date"].iloc[-1]])
        sc = {tag: score_windows(self.models[tag], mats) for tag in ("prod", "BULL")}
        boot = pd.DataFrame(entries, columns=["date", "code", "elig", "close"])
        boot["prod"], boot["BULL"] = sc["prod"], sc["BULL"]
        self.boot = boot

        days = sorted(boot["date"].unique())[-HIST_DAYS:]
        cnt = {d: int(((boot["date"] == d) & boot["elig"] & (boot["prod"] >= BUY_MIN)).sum()) for d in days}
        streak = 0
        for d in reversed(days):
            if cnt[d] == 0:
                streak += 1
            else:
                break
        hist, below = {}, {}
        for tag in ("prod", "BULL"):
            hist[tag], below[tag] = {}, {}
            piv = boot.pivot_table(index="date", columns="code", values=tag, aggfunc="last").reindex(days)
            for code in piv.columns:
                v = piv[code].dropna().to_numpy()
                if len(v) == 0:
                    continue
                hist[tag][code] = [float(x) for x in v[-(sb.SMOOTH_N - 1):]]
                sm = [v[max(0, k - sb.SMOOTH_N + 1):k + 1].mean() for k in range(len(v))]
                b = 0
                for x in reversed(sm):
                    if x < sb.SELL_THRESH:
                        b += 1
                    else:
                        break
                below[tag][code] = b
        self.state.update({"hist_day": today, "zero_streak_prev": streak, "hist": hist, "below": below,
                           "prod_cnt_hist": [[d, cnt[d]] for d in days[-5:]]})
        for pf in self.state["portfolios"].values():
            pf["sold_today"] = []
            pf["base"] = pf["equity_prev"]
            pf["today"] = {"date": today, "fills": [], "fallback_on": None}
            for pos in pf["positions"].values():
                pos["raw_neg_since"] = None
        self.state["today"] = {"date": today, "ready": False, "ticked": False, "prod_cnt": None,
                               "kospi": None, "kosdaq": None}
        self.bull_cache = {}
        self.save()
        log(f"장전 준비 {today}: 종목 {len(codes)} · 창 {len(mats):,} · prod 무후보 연속 {streak}일 "
            f"(최근 {', '.join(f'{d[5:]}:{cnt[d]}' for d in days[-5:])}) · {time.time()-t0:.0f}초")

    # ── 장중 데이터 ──
    def read_prod(self, today, t_hi):
        p = os.path.join(LOG_DIR, f"{today}_Stock_V3.csv")
        if not os.path.exists(p):
            return {}
        try:
            df = pd.read_csv(p, encoding="utf-8-sig", dtype={"code": str}, on_bad_lines="skip")
        except Exception:
            return {}
        df["code"] = df["code"].astype(str).str.split(".").str[0].str.zfill(6)
        df["score_total"] = pd.to_numeric(df["score_total"], errors="coerce")
        df["close_price"] = pd.to_numeric(df["close_price"], errors="coerce")
        t = df["time"].astype(str)
        df = df[(t >= "09:00:00") & (t <= t_hi)].dropna(subset=["score_total"])
        out = {}
        for r in df.itertuples():
            out[r.code] = (float(r.score_total), float(r.close_price) if r.close_price == r.close_price else None)
            if isinstance(r.name, str):
                self.state["names"][r.code] = r.name
        return out

    def refresh_bull(self, codes, today, ts_open):
        today_dash = f"{today[:4]}-{today[4:6]}-{today[6:]}"
        need = []
        for code in codes:
            p = os.path.join(DATA_DIR, f"A{code}.csv")
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if mt < ts_open:
                continue
            c = self.bull_cache.get(code)
            if c is None or c[0] != mt:
                need.append((code, mt))
        mats, meta = [], []
        for code, mt in need:
            df = load_code(code, self.max_lb + 25)
            if df is None or df["date"].iloc[-1] != today_dash:
                continue
            price = float(df["close"].iloc[-1])
            k, q = float(df["kospi_change"].iloc[-1]), float(df["kosdaq_change"].iloc[-1])
            if (df["close"] * df["volume"]).tail(20).mean() < MIN_AVG_TRADE_VALUE:
                self.bull_cache[code] = (mt, np.nan, price, k, q)
                continue
            mats.append(df[V3_FEATURES].to_numpy(np.float64))
            meta.append((code, mt, price, k, q))
        scores = score_windows(self.models["BULL"], mats)
        for (code, mt, price, k, q), s in zip(meta, scores):
            self.bull_cache[code] = (mt, float(s), price, k, q)
        return len(meta)

    # ── 장중 틱 ──
    def tick(self, now):
        today = now.strftime("%Y%m%d")
        ts_open = datetime.combine(now.date(), MARKET_OPEN).timestamp()
        st_today = self.state["today"]
        st_today["ticked"] = True
        uni = self.universe(today)

        if not st_today.get("ready"):
            if uni:
                fresh = sum(1 for c in uni if os.path.exists(os.path.join(DATA_DIR, f"A{c}.csv"))
                            and os.path.getmtime(os.path.join(DATA_DIR, f"A{c}.csv")) >= ts_open)
                ready, desc = fresh / len(uni) >= READY_RATIO, f"{fresh}/{len(uni)} ({fresh/len(uni):.0%})"
            else:   # buylist 없음(main_stock 전체 폴백 스캔) → 정규장 계산 행 수로 판정
                n = len(self.read_prod(today, now.strftime("%H:%M:%S")))
                ready, desc = n >= 200, f"buylist 없음 · Stock_V3 정규장 {n}행"
            if not ready:
                log(f"대기: main_stock 정규장 첫 스캔 {desc}")
                self.save()
                return
            st_today["ready"] = True
            log(f"정규장 스캔 준비 완료 {desc} — 가상매매 시작")

        t_hi = min(now.strftime("%H:%M:%S"), MARKET_CLOSE.strftime("%H:%M:%S")) if not SELFTEST else "15:30:00"
        prod = self.read_prod(today, t_hi)
        codes = set(prod) | self.held_codes() | set(uni)
        n_new = self.refresh_bull(codes, today, ts_open)

        prod_raw = {c: v[0] for c, v in prod.items()}
        bull_raw = {c: v[1] for c, v in self.bull_cache.items() if v[1] == v[1]}
        price = {}
        for c, v in prod.items():
            if v[1]:
                price[c] = v[1]
        for c, v in self.bull_cache.items():
            price[c] = v[2]
        for c, p in price.items():
            self.state["last_price"][c] = [p, now.strftime("%Y%m%d %H:%M:%S")]
        if self.bull_cache:
            last = max(self.bull_cache.values(), key=lambda x: x[0])
            st_today["kospi"], st_today["kosdaq"] = last[3], last[4]

        prod_cnt = sum(1 for v in prod_raw.values() if v >= BUY_MIN)
        st_today["prod_cnt"] = prod_cnt
        ctx = {"now": now, "today": today, "raw": {"prod": prod_raw, "BULL": bull_raw}, "price": price,
               "today_zero": prod_cnt == 0 and len(prod_raw) > 0,
               "streak": self.state.get("zero_streak_prev", 0)}

        fills = []
        for pname, cfg in PORTFOLIOS.items():
            fills += self.step(pname, cfg, self.state["portfolios"][pname], ctx)
        self.save()
        if fills:
            append_csv("trades.csv", fills)
            lines = [f"🧪 [가상체결] {now:%H:%M} · prod 60+ {prod_cnt}개 · 무후보 연속 전일까지 {ctx['streak']}일"]
            for f in fills:
                nm = f"{f['name']}({f['code']})"
                if f["side"] == "BUY":
                    lines.append(f"{PORTFOLIOS[f['portfolio']]['label']} 매수 {nm} {f['qty']:,}주 @{won(f['price'])} "
                                 f"({f['model']} {f['score']*100:.0f}점·{f['alloc']:.0f}%)")
                else:
                    lines.append(f"{PORTFOLIOS[f['portfolio']]['label']} 매도 {nm} {f['qty']:,}주 @{won(f['price'])} "
                                 f"{f['ret_pct']:+.1f}% ({REASON_TXT.get(f['reason'], f['reason'])})")
            send_owner("\n".join(lines))
        log(f"틱 {now:%H:%M:%S}: prod {len(prod_raw)}종목(60+ {prod_cnt}) · BULL {len(bull_raw)}종목(갱신 {n_new}) · 체결 {len(fills)}")

    def step(self, pname, cfg, pf, ctx):
        now, today = ctx["now"], ctx["today"]
        if pf.get("start") is None:
            pf["start"] = today
        raw, price = ctx["raw"], ctx["price"]
        fills = []

        # 매도 — 산 모델(gov)의 점수로 판정
        for code in list(pf["positions"]):
            pos = pf["positions"][code]
            g = pos["gov"]
            r, p = raw[g].get(code), price.get(code)
            if r is None or p is None or not np.isfinite(r) or p <= 0:
                continue
            reason = None
            if r < sb.STOP_SCORE_KEEP and p / pos["avg"] - 1.0 <= sb.STOP_PCT:
                reason = "stop"
            elif r < sb.RAW_SELL_THRESH:
                if now.time() >= sb.LATE_RAW_NEG_START:
                    reason = "raw_neg_late"
                elif pos.get("raw_neg_since") is None:
                    pos["raw_neg_since"] = now.timestamp()
                elif now.timestamp() - pos["raw_neg_since"] >= sb.RAW_NEG_HOLD_SEC:
                    reason = "raw_neg"
            else:
                pos["raw_neg_since"] = None
            if reason is None:
                h = self.state["hist"].get(g, {}).get(code, [])
                sm = (sum(h) + r) / (len(h) + 1)
                if (r < sb.SELL_THRESH and sm < sb.SELL_THRESH
                        and self.state["below"].get(g, {}).get(code, 0) + 1 >= sb.CONFIRM_DAYS):
                    reason = "score"
            if reason is None:
                continue
            qty = pos["qty"]
            proceeds = qty * p * (1 - SELL_FEE)
            pnl = proceeds - pos["cost"]
            pf["cash"] += proceeds
            pf["closed"] += 1
            pf["wins"] += int(pnl > 0)
            pf["ret_sum"] += pnl / pos["cost"]
            pf["sold_today"].append(code)
            del pf["positions"][code]
            f = {"date": today, "time": now.strftime("%H:%M:%S"), "portfolio": pname, "side": "SELL",
                 "code": code, "name": self.state["names"].get(code, ""), "qty": qty, "price": p,
                 "amount": round(qty * p), "fee": round(qty * p * SELL_FEE), "model": g,
                 "score": round(r, 4), "alloc": np.nan, "reason": reason, "avg": round(pos["avg"], 2),
                 "pnl": round(pnl), "ret_pct": round(pnl / pos["cost"] * 100, 2),
                 "hold_days": trading_days_between(pos["entry"], today), "entry": pos["entry"]}
            pf["today"]["fills"].append(f)
            fills.append(f)

        # 매수 — 가뭄이면 대체모델, 아니면 prod
        active = "prod"
        if cfg["fallback"] and ctx["today_zero"] and ctx["streak"] >= cfg["K"] - 1:
            active = cfg["fallback"]
            if pf["today"].get("fallback_on") is None:
                pf["today"]["fallback_on"] = now.strftime("%H:%M")
        cand = sorted(((c, s) for c, s in raw[active].items()
                       if s is not None and np.isfinite(s) and s >= BUY_MIN and price.get(c)),
                      key=lambda x: -x[1])
        for code, s in cand:
            if code in pf["sold_today"]:
                continue
            pos = pf["positions"].get(code)
            if pos and pos["gov"] != active:
                continue                                  # keep: 다른 모델 보유분 동결
            p = price[code]
            a = alloc_pct(s)
            tq = int((pf["base"] * a / 100.0) // p)
            held = pos["qty"] if pos else 0
            need = min(tq - held, int(pf["cash"] // (p * (1 + BUY_FEE))))
            if need < 1:
                continue
            cost = need * p * (1 + BUY_FEE)
            pf["cash"] -= cost
            if pos:
                q0 = pos["qty"]
                pos["avg"] = (q0 * pos["avg"] + need * p) / (q0 + need)
                pos["qty"] = q0 + need
                pos["cost"] += cost
            else:
                pf["positions"][code] = {"qty": need, "avg": p, "cost": cost, "gov": active,
                                         "entry": today, "raw_neg_since": None}
            f = {"date": today, "time": now.strftime("%H:%M:%S"), "portfolio": pname, "side": "BUY",
                 "code": code, "name": self.state["names"].get(code, ""), "qty": need, "price": p,
                 "amount": round(need * p), "fee": round(need * p * BUY_FEE), "model": active,
                 "score": round(s, 4), "alloc": a, "reason": "가뭄대체" if active != "prod" else "prod",
                 "avg": np.nan, "pnl": np.nan, "ret_pct": np.nan, "hold_days": np.nan, "entry": today}
            pf["today"]["fills"].append(f)
            fills.append(f)
        return fills

    # ── 장 마감 정리 ──
    def finalize_day(self, today, now):
        st_today = self.state.get("today", {})
        if st_today.get("date") != today or not st_today.get("ticked"):
            log(f"마감 정리 생략 {today}: 오늘 장중 틱 없음")
            self.state["last_eod"] = today
            self.save()
            return
        lp = self.state["last_price"]
        prod_cnt = st_today.get("prod_cnt")
        streak_now = (self.state.get("zero_streak_prev", 0) + 1) if prod_cnt == 0 else 0
        k, q = st_today.get("kospi"), st_today.get("kosdaq")
        daily_rows, hold_rows = [], []
        head = [f"🧪 [가상매매] {today[:4]}-{today[4:6]}-{today[6:]} 장마감 정리",
                f"prod 60+ 마감 {prod_cnt if prod_cnt is not None else '-'}개 → 무후보 연속 {streak_now}일"
                + (f" | KOSPI {k*100:+.2f}% · KOSDAQ {q*100:+.2f}%" if k is not None else "")]
        body = []
        for pname, cfg in PORTFOLIOS.items():
            pf = self.state["portfolios"][pname]
            mv = 0.0
            hl = []
            for code, pos in pf["positions"].items():
                p = lp.get(code, [pos["avg"]])[0]
                val = pos["qty"] * p
                mv += val
                hl.append((code, pos, p, val))
            eq = pf["cash"] + mv
            dr = eq / pf["equity_prev"] - 1
            cum = eq / INIT_CAPITAL - 1
            fl = pf["today"].get("fills", [])
            nb = sum(1 for f in fl if f["side"] == "BUY")
            ns = sum(1 for f in fl if f["side"] == "SELL")
            rp = sum(f["pnl"] for f in fl if f["side"] == "SELL")
            daily_rows.append({"date": today, "portfolio": pname, "equity": round(eq), "cash": round(pf["cash"]),
                               "cash_pct": round(pf["cash"] / eq * 100, 2), "n_pos": len(pf["positions"]),
                               "daily_ret_pct": round(dr * 100, 3), "cum_ret_pct": round(cum * 100, 3),
                               "buys": nb, "sells": ns, "realized_today": round(rp),
                               "fallback_on": pf["today"].get("fallback_on") or "",
                               "prod_cnt_close": prod_cnt, "prod_zero_streak": streak_now,
                               "kospi_chg_pct": round(k * 100, 3) if k is not None else np.nan,
                               "kosdaq_chg_pct": round(q * 100, 3) if q is not None else np.nan})
            for code, pos, p, val in sorted(hl, key=lambda x: -x[3]):
                hold_rows.append({"date": today, "portfolio": pname, "code": code,
                                  "name": self.state["names"].get(code, ""), "qty": pos["qty"],
                                  "avg": round(pos["avg"], 2), "close": p, "value": round(val),
                                  "weight_pct": round(val / eq * 100, 2),
                                  "unreal_pct": round((val * (1 - SELL_FEE) / pos["cost"] - 1) * 100, 2),
                                  "model": pos["gov"], "entry": pos["entry"],
                                  "hold_days": trading_days_between(pos["entry"], today)})
            body.append("━━━━━━━━━━━━")
            fb = f" · 대체발동 {pf['today']['fallback_on']}~" if pf["today"].get("fallback_on") else ""
            body.append(f"▶ {cfg['label']}{fb}")
            body.append(f"평가 {won(eq)}원 | 오늘 {dr*100:+.2f}% | 누적 {cum*100:+.2f}% (시작 {pf.get('start') or today})")
            body.append(f"현금 {pf['cash']/eq*100:.0f}% | 보유 {len(pf['positions'])}종목 | 오늘 매수 {nb}·매도 {ns}"
                        + (f" | 실현 {won(rp)}원" if ns else ""))
            if fl:
                body.append("[오늘 체결]")
                for f in fl:
                    if f["side"] == "BUY":
                        body.append(f" {f['time'][:5]} 매수 {f['name']} {f['qty']:,}주 @{won(f['price'])} "
                                    f"({f['model']} {f['score']*100:.0f})")
                    else:
                        body.append(f" {f['time'][:5]} 매도 {f['name']} {f['qty']:,}주 @{won(f['price'])} "
                                    f"{f['ret_pct']:+.1f}% ({REASON_TXT.get(f['reason'], f['reason'])})")
            if hold_rows:
                mine = [r for r in hold_rows if r["portfolio"] == pname]
                if mine:
                    body.append("[보유]")
                    for r in mine:
                        body.append(f" {r['name']} {r['unreal_pct']:+.1f}% · {r['weight_pct']:.0f}% "
                                    f"({r['model']}, {r['hold_days']}일)")
            if pf["closed"]:
                body.append(f"[누적 청산] {pf['closed']}건 · 승률 {pf['wins']/pf['closed']*100:.0f}% · "
                            f"평균 {pf['ret_sum']/pf['closed']*100:+.1f}%")
            pf["equity_prev"] = eq
        append_csv("daily.csv", daily_rows)
        append_csv("holdings.csv", hold_rows)
        self.state["last_eod"] = today
        self.save()
        send_owner("\n".join(head + body))
        log(f"마감 정리 완료 {today}: " + ", ".join(f"{r['portfolio']} {r['cum_ret_pct']:+.2f}%" for r in daily_rows))


# ══════════════════════════════════════════════════════════════════════════════
# 실행
# ══════════════════════════════════════════════════════════════════════════════
def acquire_lock():
    """중복 실행 방지. 다른 인스턴스가 살아 있으면 종료하지 않고 대기(스케줄러 재기동 폭주 방지)."""
    import psutil
    lock = os.path.join(OUT_DIR, "paper_trader.lock")
    announced = False
    while True:
        try:
            pid = int(open(lock).read().strip())
        except Exception:
            pid = None
        if pid and pid != os.getpid() and psutil.pid_exists(pid):
            if not announced:
                log(f"다른 인스턴스 실행 중(PID {pid}) — 종료될 때까지 대기")
                announced = True
            time.sleep(60)
            continue
        with open(lock, "w") as f:
            f.write(str(os.getpid()))
        return


def run():
    acquire_lock()
    log("paper_trader 시작 — 포트폴리오: " + ", ".join(PORTFOLIOS))
    models = {tag: load_model_set(tag) for tag in ("prod", "BULL")}
    tr = PaperTrader(models)
    while True:
        try:
            now = datetime.now()
            today = now.strftime("%Y%m%d")
            if not kt.is_trading_day(now):
                time.sleep(600)
                continue
            t = now.time()
            if t < PREP_TIME:
                time.sleep(min(600, (datetime.combine(now.date(), PREP_TIME) - now).total_seconds() + 1))
                continue
            if tr.state.get("hist_day") != today:
                tr.prepare_day(today)
            if MARKET_OPEN <= t <= MARKET_CLOSE:
                tr.tick(now)
                time.sleep(TICK_SEC)
            elif t > MARKET_CLOSE and tr.state.get("last_eod") != today:
                tr.finalize_day(today, now)
            else:
                time.sleep(TICK_SEC if t < MARKET_OPEN else 600)
        except Exception as e:
            log(f"❌ 오류: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            time.sleep(60)


def selftest(date_str):
    """해당일 실데이터로 장전 준비 → 장중 2틱(14:00, 14:45) → 마감. 텔레그램은 출력만.
    장전 재계산 점수를 백테스트 캐시(regime_analysis/score_cache)와 대조해 BULL 점수 재현성 확인."""
    log(f"셀프테스트 {date_str} — 출력 폴더 {OUT_DIR}")
    models = {tag: load_model_set(tag) for tag in ("prod", "BULL")}
    tr = PaperTrader(models)
    tr.prepare_day(date_str)

    last = sorted(tr.boot["date"].unique())[-1]
    cache_dir = os.path.join(ROOT, "regime_analysis", "score_cache")
    for tag in ("prod", "BULL"):
        cp = os.path.join(cache_dir, f"scores_{tag}.pkl")
        if not os.path.exists(cp):
            continue
        c = pd.read_pickle(cp)
        c["date"] = pd.to_datetime(c["date"]).dt.strftime("%Y-%m-%d")
        c = c[c["date"] == last][["code", "score"]]
        m = tr.boot[tr.boot["date"] == last][["code", tag, "elig"]].merge(c, on="code").dropna()
        if len(m):
            log(f"검증 {last} [{tag}] 실시간 파이프라인 vs 백테스트 캐시: {len(m)}종목 · 상관 {m[tag].corr(m['score']):.4f} · "
                f"평균|차| {np.abs(m[tag]-m['score']).mean():.4f} · 60+ 개수 {int((m[tag]>=BUY_MIN).sum())} vs "
                f"{int((m['score']>=BUY_MIN).sum())}")
    d = datetime.strptime(date_str, "%Y%m%d")
    for hh, mm in ((14, 0), (14, 45)):
        tr.tick(d.replace(hour=hh, minute=mm))
    tr.finalize_day(date_str, d.replace(hour=15, minute=40))
    log("셀프테스트 종료 — 기록: " + ", ".join(sorted(os.listdir(OUT_DIR))))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", nargs="?", const=datetime.now().strftime("%Y%m%d"), default=None)
    args = ap.parse_args()
    if args.selftest:
        SELFTEST = True
        OUT_DIR = os.path.join(LOG_DIR, "paper_trader_selftest")
        shutil.rmtree(OUT_DIR, ignore_errors=True)
        os.makedirs(OUT_DIR, exist_ok=True)
        selftest(args.selftest)
    else:
        os.makedirs(OUT_DIR, exist_ok=True)
        run()
