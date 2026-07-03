"""
build_universe_filter.py
========================
학습 및 스코어 캐시 대상 종목을 필터링해 excluded_stocks.json 생성.

제외 기준 (하나라도 해당하면 제외):
  1. 2025 일평균 거래대금  < 20억
  2. 2025 평균 시가총액    < 1,000억  (--fetch-market-cap 플래그 필요, KIS API)
  3. 전 기간 중 20일 이상 연속 종가 동일
  4. 전 기간 중 20일 이상 연속 거래량 0
  5. 일 종가 변동 50% 초과

결과:
  excluded_stocks.json  → {"excluded": {code: [reasons...]}, "passed": [codes...]}

사용:
  python -X utf8 build_universe_filter.py
  python -X utf8 build_universe_filter.py --fetch-market-cap   # KIS API 시가총액 체크 추가
  python -X utf8 build_universe_filter.py --summary            # 결과만 요약 출력
"""

import os, sys, json, time, warnings, argparse
import pandas as pd
import numpy as np
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings("ignore")

ROOT     = Path(r"C:\Projects\RealtimeMonitor")
DATA_DIR = ROOT / "Data" / "Stock"
OUT_FILE = ROOT / "excluded_stocks.json"

sys.path.insert(0, str(ROOT))

# ── 필터 임계값 ───────────────────────────────────────────────────────────────
TRADING_VALUE_MIN   = 2_000_000_000   # 20억 (close × volume 일평균)
MARKET_CAP_MIN      = 1_000           # 1,000억 (inquiry 단위: 억원)
CONSEC_SAME_CLOSE   = 20              # 연속 종가 동일 임계
CONSEC_ZERO_VOL     = 20              # 연속 거래량 0 임계
DAILY_MOVE_MAX      = 0.50            # 일 종가 변동 50%


# ══════════════════════════════════════════════════════════════════════════════
# 유틸: 연속 True 최대 길이
# ══════════════════════════════════════════════════════════════════════════════
def _max_consecutive(bool_series: pd.Series) -> int:
    if bool_series.empty or not bool_series.any():
        return 0
    groups = (bool_series != bool_series.shift()).cumsum()
    return int(bool_series.groupby(groups).sum().max())


# ══════════════════════════════════════════════════════════════════════════════
# 단일 종목 CSV 검사
# ══════════════════════════════════════════════════════════════════════════════
def check_stock_csv(fpath: Path):
    """
    반환: (code, reasons_list)
    reasons가 비어있으면 필터 통과.
    """
    code    = fpath.stem[1:]   # A005930 → 005930
    reasons = []

    try:
        df = pd.read_csv(fpath, encoding="utf-8-sig",
                         usecols=["date", "close", "volume"])
        df["date"]   = pd.to_datetime(df["date"])
        df["close"]  = pd.to_numeric(df["close"],  errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
        df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)

        if df.empty:
            return code, ["no_data"]

        # ── 필터 1: 2025 일평균 거래대금 ────────────────────────────────────
        df_2025 = df[df["date"].dt.year == 2025]
        if df_2025.empty:
            reasons.append("no_2025_data")
        else:
            avg_val = (df_2025["close"] * df_2025["volume"]).mean()
            if avg_val < TRADING_VALUE_MIN:
                reasons.append(f"low_trading_value({avg_val/1e8:.1f}억)")

        # ── 필터 3: 20일 이상 연속 종가 동일 ────────────────────────────────
        same_close = df["close"].diff().fillna(1) == 0
        run_close  = _max_consecutive(same_close)
        if run_close >= CONSEC_SAME_CLOSE:
            reasons.append(f"consecutive_same_close({run_close}일)")

        # ── 필터 4: 20일 이상 연속 거래량 0 ─────────────────────────────────
        zero_vol  = df["volume"] == 0
        run_vol   = _max_consecutive(zero_vol)
        if run_vol >= CONSEC_ZERO_VOL:
            reasons.append(f"consecutive_zero_volume({run_vol}일)")

        # ── 필터 5: 일 종가 변동 50% 초과 ───────────────────────────────────
        pct_chg = df["close"].pct_change().abs()
        max_chg = pct_chg.max()
        if pd.notna(max_chg) and max_chg > DAILY_MOVE_MAX:
            reasons.append(f"large_daily_move({max_chg*100:.0f}%)")

    except Exception as e:
        reasons.append(f"read_error({e})")

    return code, reasons


# ══════════════════════════════════════════════════════════════════════════════
# KIS API 시가총액 체크 (선택)
# ══════════════════════════════════════════════════════════════════════════════
def fetch_market_caps_batch(codes: list, delay: float = 0.06) -> dict:
    """
    KIS API로 시가총액을 일괄 조회.
    반환: {code: cap_억원}
    delay: 호출 간격(초) — 초당 약 16회 (KIS 기본 제한 20회)
    """
    try:
        from kis_api import inquiry
    except ImportError:
        print("   ⚠️  kis_api 임포트 실패 — 시가총액 체크 건너뜀")
        return {}

    caps = {}
    n    = len(codes)
    print(f"\n   KIS API 시가총액 조회: {n}개 종목 (예상 {n*delay/60:.1f}분)")
    t0 = time.time()

    for i, code in enumerate(codes):
        try:
            cap = inquiry.fetch_market_cap(code)
            caps[code] = cap
        except Exception:
            caps[code] = 0

        time.sleep(delay)
        if (i + 1) % 200 == 0 or i == n - 1:
            el  = time.time() - t0
            eta = (n - i - 1) * delay
            print(f"   [{i+1:4d}/{n}]  {el/60:.1f}분 경과  잔여 {eta/60:.1f}분")

    return caps


# ══════════════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="학습 종목 필터링")
    parser.add_argument("--fetch-market-cap", action="store_true",
                        help="KIS API로 시가총액 확인 (장 중 실행 필요)")
    parser.add_argument("--summary", action="store_true",
                        help="기존 excluded_stocks.json 요약만 출력")
    parser.add_argument("--workers", type=int, default=8,
                        help="병렬 처리 스레드 수 (기본: 8)")
    args = parser.parse_args()

    # ── 기존 결과 요약만 보기 ─────────────────────────────────────────────────
    if args.summary and OUT_FILE.exists():
        with open(OUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        excl = data.get("excluded", {})
        passed = data.get("passed", [])
        print(f"\n📋 {OUT_FILE.name}")
        print(f"   제외: {len(excl)}개  통과: {len(passed)}개")
        reason_counts = {}
        for reasons in excl.values():
            for r in reasons:
                key = r.split("(")[0]
                reason_counts[key] = reason_counts.get(key, 0) + 1
        print("\n  사유별 건수:")
        for r, n in sorted(reason_counts.items(), key=lambda x: -x[1]):
            print(f"    {r:<30}: {n}개")
        return

    # ── CSV 기반 필터 실행 ────────────────────────────────────────────────────
    csv_files = sorted(DATA_DIR.glob("A*.csv"))
    print(f"\n{'='*64}")
    print(f"📊 종목 필터링  ({len(csv_files)}개 CSV)")
    print(f"{'='*64}")
    print(f"  필터 기준:")
    print(f"    거래대금  : 2025 일평균 < {TRADING_VALUE_MIN/1e8:.0f}억")
    print(f"    연속동일가: {CONSEC_SAME_CLOSE}일 이상")
    print(f"    연속거래0 : {CONSEC_ZERO_VOL}일 이상")
    print(f"    일변동폭  : {DAILY_MOVE_MAX*100:.0f}% 초과")
    if args.fetch_market_cap:
        print(f"    시가총액  : 2025 평균 < {MARKET_CAP_MIN}억 (KIS API)")

    print(f"\n[1] CSV 필터 검사 ({args.workers}스레드)...")
    t0 = time.time()

    excluded = {}
    passed   = []

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(check_stock_csv, f): f for f in csv_files}
        done = 0
        for fut in as_completed(futures):
            code, reasons = fut.result()
            done += 1
            if reasons:
                excluded[code] = reasons
            else:
                passed.append(code)
            if done % 500 == 0 or done == len(csv_files):
                print(f"   [{done:4d}/{len(csv_files)}]  제외: {len(excluded)}  통과: {len(passed)}")

    el = time.time() - t0
    print(f"\n   CSV 필터 완료: {el:.1f}초")

    # ── 사유별 집계 ──────────────────────────────────────────────────────────
    reason_counts = {}
    for reasons in excluded.values():
        for r in reasons:
            key = r.split("(")[0]
            reason_counts[key] = reason_counts.get(key, 0) + 1

    print(f"\n  사유별 제외 건수:")
    for r, n in sorted(reason_counts.items(), key=lambda x: -x[1]):
        print(f"    {r:<32}: {n}개")

    # ── 시가총액 체크 (선택) ──────────────────────────────────────────────────
    if args.fetch_market_cap:
        print(f"\n[2] KIS API 시가총액 체크 ({len(passed)}개 종목)...")
        caps = fetch_market_caps_batch(passed)

        low_cap_codes = [c for c, cap in caps.items() if 0 < cap < MARKET_CAP_MIN]
        zero_cap_codes = [c for c, cap in caps.items() if cap == 0]
        print(f"\n   시가총액 결과:")
        print(f"    조회 성공 : {sum(1 for c in caps.values() if c > 0)}개")
        print(f"    조회 실패 : {len(zero_cap_codes)}개 (API 오류)")
        print(f"    1000억 미만: {len(low_cap_codes)}개")

        for code in low_cap_codes:
            excluded[code] = excluded.get(code, []) + [f"low_market_cap({caps[code]}억)"]
            if code in passed:
                passed.remove(code)

    # ── 저장 ──────────────────────────────────────────────────────────────────
    result = {
        "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_stocks": len(csv_files),
        "excluded_count": len(excluded),
        "passed_count": len(passed),
        "thresholds": {
            "trading_value_min_KRW": TRADING_VALUE_MIN,
            "market_cap_min_억": MARKET_CAP_MIN,
            "consecutive_same_close": CONSEC_SAME_CLOSE,
            "consecutive_zero_volume": CONSEC_ZERO_VOL,
            "daily_move_max_pct": DAILY_MOVE_MAX * 100,
        },
        "excluded": {k: v for k, v in sorted(excluded.items())},
        "passed": sorted(passed),
    }

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*64}")
    print(f"💾 저장: {OUT_FILE}")
    print(f"   전체: {len(csv_files)}개  →  통과: {len(passed)}개  제외: {len(excluded)}개")
    print(f"   제외율: {len(excluded)/len(csv_files)*100:.1f}%")
    print(f"{'='*64}")
    print()
    print(f"  다음 단계:")
    if not args.fetch_market_cap:
        print(f"  (선택) python -X utf8 build_universe_filter.py --fetch-market-cap")
    print(f"  (학습) python -X utf8 walk_forward.py --data-start 20070101 --data-end 20260101 --rebuild-prep")
    print(f"  (학습) python -X utf8 walk_forward.py --data-start 20070101 --data-end 20260101 --rolling 7")
    print(f"  (캐시) python -X utf8 build_prod_score_cache.py")


if __name__ == "__main__":
    main()