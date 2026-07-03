# -*- coding: utf-8 -*-
"""
build_buylist.py
================
EOD 스냅샷(logs\\stock_master.json)과 개별 종목 CSV 만으로 **다음 거래일 매수리스트
(= 장중 스캔 유효 유니버스)** 를 만든다. API 재호출 없음.

제외 기준(하나라도 해당하면 제외):
  1. 시가총액 < 1,000억                         (stock_master.marketCap_eok)
  2. 감리구분 != '정상'  (관리종목·거래정지·투자경고·투자위험·투자주의환기 등)
  3. state 에 '거래정지' 포함                    (중복 안전장치)
  4. 거래일수(CSV 행수) < 95   (모델 최대 lookback 미만 → 분석 불가. 신규 상장 등)
  5. 최근 20거래일 일평균 거래대금(close×volume) < 20억   (CSV)
  6. 최근 ~6개월(120거래일) 내 연속 동일종가 ≥ 10일       (비공식 정지/초저유동 방어, CSV)

  ※ promising·키움 보유종목은 조건 미달이어도 강제 포함(감시 유지) — 아래 강제 포함 참고.

산출:
  logs\\{다음거래일}_buylist.json  및  logs\\buylist_latest.json
    → {"generated_at","for_date","count","thresholds","codes":[...]}

사용:
  python -X utf8 build_buylist.py
"""

import os
import sys
import glob
import json
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import numpy as np

ROOT     = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
DATA_DIR = os.path.join(ROOT, "Data", "Stock")
LOG_DIR  = os.path.join(ROOT, "logs")
MASTER   = os.path.join(LOG_DIR, "stock_master.json")

# ── 임계값 (튜닝 지점) ─────────────────────────────────────────────────────────
MARKET_CAP_MIN_EOK   = 1_000            # 시가총액 최소(억원)
TRADING_VALUE_MIN    = 2_000_000_000    # 일평균 거래대금 최소(원) = 20억
TV_WINDOW            = 20               # 거래대금 평균 창(거래일)
HALT_WINDOW          = 120              # 거래정지 근사 관찰 창(≈6개월)
CONSEC_SAME_CLOSE    = 10               # 연속 동일종가 임계(일)
MIN_TRADING_DAYS     = 95               # 최소 거래일수 = 모델 최대 lookback(MODEL_SETTINGS target20 lb=95)
                                        #   미만이면 main_stock/Update_Promising 이 len(df)<max_lb 로 스킵 → 분석 불가
GOOD_AUDIT           = {"정상"}          # 통과 감리구분


def _max_consecutive(bool_series: pd.Series) -> int:
    """연속 True 최대 길이 (build_universe_filter 와 동일 로직)"""
    if bool_series.empty or not bool_series.any():
        return 0
    groups = (bool_series != bool_series.shift()).cumsum()
    return int(bool_series.groupby(groups).sum().max())


def _promising_codes() -> set:
    """promising(가장 최근 Search_History.csv)에 등록된 종목코드 집합."""
    files = glob.glob(os.path.join(LOG_DIR, "*_Search_History.csv"))
    if not files:
        return set()
    latest = max(files, key=os.path.getmtime)
    try:
        df = pd.read_csv(latest, encoding="utf-8-sig", dtype=str, on_bad_lines="skip")
        if "code" in df.columns:
            return {str(c).split(".")[0].strip().zfill(6)
                    for c in df["code"].dropna() if str(c).strip()}
    except Exception as e:
        print(f"  ⚠️ Search_History 읽기 실패: {e}")
    return set()


def _holding_codes() -> set:
    """키움 계좌 전 보유종목 코드 집합 (조회 실패 시 빈 집합)."""
    try:
        from kis_api import kiwoom_trading as trading
        hs = trading.fetch_all_holdings()
        if not hs:
            return set()
        return {str(h["code"]).split(".")[0].strip().zfill(6)
                for h in hs if h.get("code")}
    except Exception as e:
        print(f"  ⚠️ 보유종목 조회 실패: {e}")
        return set()


def next_trading_day(base: datetime) -> str:
    """다음 평일(월~금) YYYYMMDD. 공휴일은 미고려(main_stock 이 폴백 처리)."""
    d = base + timedelta(days=1)
    while d.weekday() >= 5:   # 5=토, 6=일
        d += timedelta(days=1)
    return d.strftime("%Y%m%d")


def csv_ok(code: str):
    """CSV 기반 필터. 통과 시 (code, True, avg_val), 아니면 (code, False, 사유)."""
    fpath = os.path.join(DATA_DIR, f"A{code}.csv")
    if not os.path.exists(fpath):
        return code, False, "no_csv"
    try:
        df = pd.read_csv(fpath, encoding="utf-8-sig", usecols=["close", "volume"])
        df["close"]  = pd.to_numeric(df["close"],  errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
        df = df.dropna(subset=["close"])
        if df.empty:
            return code, False, "empty"

        # 거래일수 부족(신규 상장 등) → 모델 분석 불가하므로 제외
        if len(df) < MIN_TRADING_DAYS:
            return code, False, "short_history"

        # 거래대금 (최근 TV_WINDOW 거래일 평균)
        recent = df.tail(TV_WINDOW)
        avg_val = float((recent["close"] * recent["volume"]).mean())
        if avg_val < TRADING_VALUE_MIN:
            return code, False, f"low_value({avg_val/1e8:.1f}억)"

        # 거래정지 근사 (최근 HALT_WINDOW 내 연속 동일종가)
        win = df["close"].tail(HALT_WINDOW)
        same = win.diff().fillna(1) == 0
        if _max_consecutive(same) >= CONSEC_SAME_CLOSE:
            return code, False, "flatline"

        return code, True, avg_val
    except Exception as e:
        return code, False, f"read_error({e})"


def main():
    print("=" * 60)
    print(f"🧺 매수리스트 생성  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    if not os.path.exists(MASTER):
        print(f"❌ {MASTER} 없음 — build_stock_master.py 를 먼저 실행하세요. 중단.")
        return
    with open(MASTER, "r", encoding="utf-8") as f:
        stocks = json.load(f).get("stocks", {})
    print(f"  마스터 보통주: {len(stocks)}개")

    # ── 1차: 마스터 필드(시총·감리·상태) 필터 — API 0콜 ─────────────────────────
    cand, drop_cap, drop_audit, drop_state = [], 0, 0, 0
    for code, s in stocks.items():
        if s.get("marketCap_eok", 0) < MARKET_CAP_MIN_EOK:
            drop_cap += 1;   continue
        if s.get("auditInfo", "") not in GOOD_AUDIT:
            drop_audit += 1; continue
        if "거래정지" in str(s.get("state", "")):
            drop_state += 1; continue
        cand.append(code)
    print(f"  1차 통과(시총·감리·상태): {len(cand)}개  "
          f"[시총미달 {drop_cap} / 감리 {drop_audit} / 정지 {drop_state}]")

    # ── 2차: CSV 거래일수·거래대금·플랫라인 필터 ───────────────────────────────
    passed, drop_val, drop_flat, drop_short, drop_misc = [], 0, 0, 0, 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(csv_ok, c): c for c in cand}
        for fut in as_completed(futs):
            code, ok, info = fut.result()
            if ok:
                passed.append(code)
            elif isinstance(info, str) and info.startswith("low_value"):
                drop_val += 1
            elif info == "flatline":
                drop_flat += 1
            elif info == "short_history":
                drop_short += 1
            else:
                drop_misc += 1
    print(f"  2차 통과(거래일수·거래대금·플랫라인): {len(passed)}개  "
          f"[거래대금미달 {drop_val} / 플랫라인 {drop_flat} / 거래일부족 {drop_short} / 기타 {drop_misc}]")

    # ── 강제 포함: promising + 키움 보유종목 (조건 미달이어도 익일 감시 대상) ──────────
    passed_set = set(passed)
    promising  = _promising_codes()
    holdings   = _holding_codes()
    forced     = (promising | holdings) - passed_set
    forced_added = []
    if forced:
        try:
            import build_stock_master as _bsm
        except Exception:
            _bsm = None
        for code in sorted(forced):
            # 스캔 가능하도록 CSV 보장(없으면 상장이후 이력 백필)
            if _bsm is not None:
                try:
                    _bsm.ensure_stock_csv(code)
                except Exception:
                    pass
            passed_set.add(code)
            forced_added.append(code)
    print(f"  강제 포함: promising {len(promising)}개 + 보유 {len(holdings)}개 "
          f"→ 조건미달 신규 편입 {len(forced_added)}개")

    passed = sorted(passed_set)
    for_date = next_trading_day(datetime.now())
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "for_date": for_date,
        "count": len(passed),
        "thresholds": {
            "market_cap_min_eok": MARKET_CAP_MIN_EOK,
            "trading_value_min_KRW": TRADING_VALUE_MIN,
            "tv_window": TV_WINDOW,
            "halt_window": HALT_WINDOW,
            "consec_same_close": CONSEC_SAME_CLOSE,
            "good_audit": sorted(GOOD_AUDIT),
        },
        "forced_included": sorted(forced_added),   # promising/보유 중 조건미달 강제편입
        "codes": passed,
    }
    os.makedirs(LOG_DIR, exist_ok=True)
    for out in (os.path.join(LOG_DIR, f"{for_date}_buylist.json"),
                os.path.join(LOG_DIR, "buylist_latest.json")):
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, out)

    print(f"  💾 저장: logs\\{for_date}_buylist.json  (+ buylist_latest.json)")
    print(f"  ✅ 매수리스트 {len(passed)}개 (다음거래일 {for_date})")
    print("=" * 60)


if __name__ == "__main__":
    main()
