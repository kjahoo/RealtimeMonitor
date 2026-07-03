# -*- coding: utf-8 -*-
"""
build_stock_master.py
=====================
키움 ka10099(종목정보 리스트)로 코스피·코스닥 **전체 상장 보통주** 목록을 만들고,
Data\\Stock 에 아직 CSV 가 없는 신규 상장 종목의 과거 이력을 백필한다.

산출:
  logs\\stock_master.json  →  {
      "generated_at": "...", "count": N,
      "stocks": { code: {name, market, listCount, lastPrice, auditInfo,
                         state, companyClassName, marketCap_eok}, ... }
  }
  (build_buylist.py 가 이 스냅샷만 읽어 API 재호출 없이 매수리스트를 만든다)

보통주 판정(응답 필드 기준 — 요청 mrkt_tp 와 무관하게 응답 marketName 이 실제 유형):
  - marketName ∈ {거래소, 코스닥} 만 통과   (ETF/ETN/리츠/뮤추얼펀드/인프라 제외)
  - companyClassName=='스팩' 또는 이름에 '스팩' 포함  → 제외
  - 우선주 정규식  \\d*우[A-C]?(\\(전환\\))?$          → 제외 (전환우선주 포함)
  - 이름에 '리츠' 포함                                → 제외 (안전)

사용:
  python -X utf8 build_stock_master.py                # 목록 생성 + 신규종목 백필
  python -X utf8 build_stock_master.py --no-backfill  # 목록만 생성(백필 생략)
  python -X utf8 build_stock_master.py --limit 20     # 백필 최대 20종목(초기 점검용)
"""

import os
import re
import sys
import json
import argparse
from datetime import datetime

import pandas as pd
import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from kis_api import kiwoom_inquiry
from kis_api import indicators
# 신규종목 백필은 Update_Data_All 의 검증된 수집 함수를 그대로 재사용
import Update_Data_All as UDA

DATA_DIR   = os.path.join(ROOT, "Data", "Stock")
LOG_DIR    = os.path.join(ROOT, "logs")
MASTER_OUT = os.path.join(LOG_DIR, "stock_master.json")

# ── 보통주 판정 ────────────────────────────────────────────────────────────────
COMMON_MARKETS = ("거래소", "코스닥")
PREF_RE        = re.compile(r"\d*우[A-C]?(\(전환\))?$")   # 우/우B/2우B/우(전환) 등 우선주


def is_common_stock(row: dict) -> bool:
    if row.get("marketName") not in COMMON_MARKETS:
        return False
    name = row.get("name", "")
    if row.get("companyClassName") == "스팩" or "스팩" in name:
        return False
    if PREF_RE.search(name):
        return False
    if "리츠" in name:
        return False
    return True


def _to_int(v) -> int:
    try:
        return int(str(v).replace(",", "").replace("+", "").split(".")[0])
    except (ValueError, TypeError):
        return 0


# ══════════════════════════════════════════════════════════════════════════════
# 1) 마스터 목록 생성
# ══════════════════════════════════════════════════════════════════════════════
def build_master() -> dict:
    rows = kiwoom_inquiry.fetch_stock_list("0") + kiwoom_inquiry.fetch_stock_list("10")
    print(f"  ka10099 응답: {len(rows)}개(전체) ", end="")

    stocks = {}
    for r in rows:
        if not is_common_stock(r):
            continue
        code = r.get("code", "").strip()
        if not code:
            continue
        list_cnt   = _to_int(r.get("listCount"))
        last_price = _to_int(r.get("lastPrice"))
        stocks[code] = {
            "name":             r.get("name", "").strip(),
            "market":           r.get("marketName", ""),
            "regDay":           r.get("regDay", ""),
            "listCount":        list_cnt,
            "lastPrice":        last_price,
            "auditInfo":        r.get("auditInfo", ""),
            "state":            r.get("state", ""),
            "companyClassName": r.get("companyClassName", ""),
            "marketCap_eok":    round(list_cnt * last_price / 1e8, 1),  # 시가총액(억원)
        }
    print(f"→ 보통주 {len(stocks)}개")
    return stocks


def save_master(stocks: dict):
    os.makedirs(LOG_DIR, exist_ok=True)
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(stocks),
        "stocks": dict(sorted(stocks.items())),
    }
    tmp = MASTER_OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MASTER_OUT)
    print(f"  💾 저장: {MASTER_OUT}")


# ══════════════════════════════════════════════════════════════════════════════
# 2) 신규 상장 종목 백필  (Update_Data_All 파이프라인을 신규 파일용으로 재현)
# ══════════════════════════════════════════════════════════════════════════════
def _build_new_csv(code: str, name: str, start_date: str, end_date: str,
                   df_kospi: pd.DataFrame, df_kosdaq: pd.DataFrame) -> bool:
    """상장 이후 전체 이력으로 A{code}.csv 신규 생성. 성공 시 True."""
    df_price = UDA.fetch_chart_data_chunked(code, start_date, end_date)
    if df_price is None or df_price.empty:
        return False

    df_prog = UDA.fetch_program_history(code, start_date, end_date)
    if df_prog is not None and not df_prog.empty:
        df = pd.merge(df_price, df_prog, on="date", how="left")
        df["prog_net_qty"] = df["prog_net_qty"].fillna(0)
        df["prog_buy"]     = df["prog_buy"].fillna(0)
        df["prog_sell"]    = df["prog_sell"].fillna(0)
    else:
        df = df_price.copy()
        df["prog_net_qty"] = 0
        df["prog_buy"]     = 0
        df["prog_sell"]    = 0

    df["prog_ratio_vol"] = np.where(df["volume"] > 0,
                                    (df["prog_buy"] + df["prog_sell"]) / df["volume"], 0.0)
    df["prog_net_ratio"] = np.where(df["volume"] > 0,
                                    df["prog_net_qty"] / df["volume"], 0.0)
    df["code"] = code
    df["name"] = name

    # 지수 병합
    df.set_index("date", inplace=True)
    df["kospi_change"]  = np.nan
    df["kosdaq_change"] = np.nan
    if df_kospi is not None and not df_kospi.empty:
        df["kospi_change"]  = df_kospi["kospi_change"].combine_first(df["kospi_change"])
    if df_kosdaq is not None and not df_kosdaq.empty:
        df["kosdaq_change"] = df_kosdaq["kosdaq_change"].combine_first(df["kosdaq_change"])
    df.reset_index(inplace=True)
    df[["kospi_change", "kosdaq_change"]] = df[["kospi_change", "kosdaq_change"]].fillna(0)

    # 파생 지표 + Target
    df["change_pct"] = df["close"].pct_change().fillna(0)
    df["target1"]  = df["close"].shift(-1)  / df["close"] - 1
    df["target5"]  = df["close"].shift(-5)  / df["close"] - 1
    df["target20"] = df["close"].shift(-20) / df["close"] - 1
    df = indicators.calculate_indicators_v3_save(df)

    if "ma60" not in df.columns:
        df["ma60"] = df["close"].rolling(window=60).mean()
    if "disparity_60" not in df.columns:
        df["disparity_60"] = (df["close"] / df["ma60"] - 1).fillna(0)
    if "bb_pos" not in df.columns:
        df["bb_pos"] = 0.0

    for col in UDA.FINAL_COLUMNS:
        if col not in df.columns:
            df[col] = 0
    df_final = df[UDA.FINAL_COLUMNS].fillna(0)

    out_path = os.path.join(DATA_DIR, f"A{code}.csv")
    tmp = out_path + ".tmp"
    df_final.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, out_path)
    return True


def backfill_new_listings(stocks: dict, limit: int = 0):
    existing = {f[1:7] for f in os.listdir(DATA_DIR)
                if f.startswith("A") and f.endswith(".csv")}
    missing = sorted(c for c in stocks if c not in existing)
    if not missing:
        print("  신규 상장 백필 대상 없음.")
        return
    if limit and len(missing) > limit:
        print(f"  신규 상장 {len(missing)}개 중 {limit}개만 백필(--limit).")
        missing = missing[:limit]
    else:
        print(f"  신규 상장 백필 대상: {len(missing)}개")

    end_date = datetime.now().strftime("%Y%m%d")
    # 지수는 가장 오래된 상장일부터 한 번만 수집해 재사용
    reg_days = [stocks[c].get("regDay", "") for c in missing if stocks[c].get("regDay", "").isdigit()]
    start_date = min(reg_days) if reg_days else "20000101"
    print(f"  지수 데이터 수집({start_date}~{end_date})...")
    df_kospi  = UDA.fetch_market_index_history("0001", start_date, end_date)
    df_kosdaq = UDA.fetch_market_index_history("1001", start_date, end_date)
    if df_kospi is not None and not df_kospi.empty:
        df_kospi = df_kospi.set_index("date")
    if df_kosdaq is not None and not df_kosdaq.empty:
        df_kosdaq = df_kosdaq.set_index("date")

    ok = fail = 0
    for i, code in enumerate(missing, 1):
        info = stocks[code]
        reg = info.get("regDay", "")
        s = reg if reg.isdigit() and len(reg) == 8 else "20000101"
        try:
            if _build_new_csv(code, info["name"], s, end_date, df_kospi, df_kosdaq):
                ok += 1
                print(f"    [{i}/{len(missing)}] ✅ A{code}.csv ({info['name']}) 생성")
            else:
                fail += 1
                print(f"    [{i}/{len(missing)}] ⚠️ {code} ({info['name']}) 데이터 없음 — 스킵")
        except Exception as e:
            fail += 1
            print(f"    [{i}/{len(missing)}] ❌ {code} ({info['name']}) 실패: {e}")
    print(f"  백필 완료: 성공 {ok} / 실패·스킵 {fail}")


def _lookup_master(code):
    """stock_master.json 에서 (name, regDay) 조회. 없으면 (None, None)."""
    try:
        with open(MASTER_OUT, "r", encoding="utf-8") as f:
            s = json.load(f).get("stocks", {}).get(code)
        if s:
            return s.get("name"), s.get("regDay")
    except Exception:
        pass
    return None, None


def ensure_stock_csv(code, name=None):
    """
    A{code}.csv 가 이미 있으면 그 경로를 반환.
    없으면 상장 이후(또는 조회 가능 범위) 이력을 즉시 백필해 생성하고 경로 반환.
    실패 시 None. (텔레그램 수동 추가·Promising 추적에서 신규/미보유 종목을 위해 호출)
    """
    from datetime import timedelta
    code = str(code).split(".")[0].strip().zfill(6)
    for d in (DATA_DIR, os.path.join(ROOT, "Data", "ETF")):
        p = os.path.join(d, f"A{code}.csv")
        if os.path.exists(p):
            return p

    m_name, reg = _lookup_master(code)
    if not name:
        name = m_name
    if not name:
        # 마스터에 없는 종목(우선주 등) → 실제 종목명 조회
        try:
            from kis_api import inquiry as _kis_inq
            name = (_kis_inq.fetch_stock_name(code) or "").strip() or code
        except Exception:
            name = code
    if reg and reg.isdigit() and len(reg) == 8:
        start_date = reg
    else:
        # 상장일 미상 → 지표·룩백 워밍업에 충분한 과거(약 3년) 확보
        start_date = (datetime.now() - timedelta(days=1200)).strftime("%Y%m%d")
    end_date = datetime.now().strftime("%Y%m%d")

    try:
        df_kospi  = UDA.fetch_market_index_history("0001", start_date, end_date)
        df_kosdaq = UDA.fetch_market_index_history("1001", start_date, end_date)
        if df_kospi is not None and not df_kospi.empty:
            df_kospi = df_kospi.set_index("date")
        if df_kosdaq is not None and not df_kosdaq.empty:
            df_kosdaq = df_kosdaq.set_index("date")
        if _build_new_csv(code, name, start_date, end_date, df_kospi, df_kosdaq):
            return os.path.join(DATA_DIR, f"A{code}.csv")
    except Exception as e:
        print(f"⚠️ ensure_stock_csv({code}) 실패: {e}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="상장 보통주 마스터 생성 + 신규종목 백필")
    ap.add_argument("--no-backfill", action="store_true", help="목록만 생성(백필 생략)")
    ap.add_argument("--limit", type=int, default=0, help="백필 최대 종목 수(0=무제한)")
    args = ap.parse_args()

    print("=" * 60)
    print(f"📋 종목 마스터 생성  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    stocks = build_master()
    if not stocks:
        print("❌ 마스터가 비었음(ka10099 응답 실패?) — 중단, 기존 파일 보존")
        return
    save_master(stocks)

    if args.no_backfill:
        print("  (--no-backfill) 백필 생략")
    else:
        backfill_new_listings(stocks, limit=args.limit)

    print("=" * 60)


if __name__ == "__main__":
    main()
