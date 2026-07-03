"""
backfill_history.py
===================
각 종목 CSV의 가장 이른 날짜보다 과거 OHLCV + 프로그램 매매 데이터를
2000-01-01 부터 소급하여 채워넣는 스크립트.

흐름:
  1) KOSPI / KOSDAQ 지수를 전 기간 한 번에 수집
  2) 각 종목 CSV마다
     - 현재 최초 날짜 확인
     - 이미 backfill된 경우 스킵 (--target-start 이전 데이터 존재 시)
     - (최초날짜 - 1일) 까지 OHLCV + 프로그램 수집
     - 기존 데이터와 prepend 후 전체 지표 재계산
     - 동일 경로에 저장

사용:
  python -X utf8 backfill_history.py
  python -X utf8 backfill_history.py --target-start 20000101 --stock-dir "Data/Stock"
  python -X utf8 backfill_history.py --sample 50          # 테스트용 50종목
  python -X utf8 backfill_history.py --code 005930        # 특정 종목
  python -X utf8 backfill_history.py --skip-prog          # 프로그램 매매 생략 (속도↑)
"""

import os, sys, time, argparse, traceback
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from pathlib import Path

ROOT = Path(r"C:\Projects\RealtimeMonitor")
sys.path.insert(0, str(ROOT))

from kis_api import auth, indicators, common
from config import secrets

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


# ──────────────────────────────────────────────────────────────────────────────
# API 헬퍼
# ──────────────────────────────────────────────────────────────────────────────

def _call(url, headers, params, delay=0.04):
    """공통 API 호출 + 지연."""
    time.sleep(delay)
    return common.call_api(url, params, headers)


def _base_headers(tr_id):
    token = auth.get_access_token()
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": secrets.APP_KEY,
        "appsecret": secrets.APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
    }


def fetch_index_history(market_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """KOSPI(0001) 또는 KOSDAQ(1001) 일별 등락률 수집."""
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
    col = 'kospi_change' if market_code == '0001' else 'kosdaq_change'

    start_dt = datetime.strptime(start_date, "%Y%m%d")
    end_dt   = datetime.strptime(end_date,   "%Y%m%d")

    # 첫 날 등락률 계산을 위해 15일 앞당겨 수집
    fetch_start = start_dt - timedelta(days=15)

    rows, curr = [], fetch_start
    while curr <= end_dt:
        nxt = min(curr + timedelta(days=60), end_dt)
        hdrs = _base_headers("FHKUP03500100")
        params = {
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": market_code,
            "FID_INPUT_DATE_1": curr.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": nxt.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D",
        }
        res = _call(url, hdrs, params, delay=0.05)
        if res and "output2" in res:
            for item in res["output2"]:
                d = item.get("stck_bsop_date", "")
                if d:
                    rows.append({"date": pd.to_datetime(d),
                                 "close": float(item["bstp_nmix_prpr"])})
        curr = nxt + timedelta(days=1)

    if not rows:
        return pd.DataFrame(columns=["date", col])

    df = (pd.DataFrame(rows)
            .sort_values("date")
            .drop_duplicates("date")
            .reset_index(drop=True))
    df[col] = df["close"].pct_change().fillna(0)
    df = df[df["date"] >= start_dt].reset_index(drop=True)
    return df[["date", col]]


def fetch_ohlcv_history(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """종목 일봉 수집 (수정주가, 3개월 단위 청킹)."""
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    start_dt = datetime.strptime(start_date, "%Y%m%d")
    end_dt   = datetime.strptime(end_date,   "%Y%m%d")

    rows, curr = [], start_dt
    while curr <= end_dt:
        nxt = min(curr + relativedelta(months=3), end_dt)
        hdrs = _base_headers("FHKST03010100")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": curr.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": nxt.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "1",   # 수정주가
        }
        res = _call(url, hdrs, params, delay=0.03)
        if res and "output2" in res:
            for item in res["output2"]:
                d = item.get("stck_bsop_date", "")
                if not d:
                    continue
                try:
                    rows.append({
                        "date":   pd.to_datetime(d),
                        "open":   int(item["stck_oprc"]),
                        "high":   int(item["stck_hgpr"]),
                        "low":    int(item["stck_lwpr"]),
                        "close":  int(item["stck_clpr"]),
                        "volume": int(item["acml_vol"]),
                    })
                except Exception:
                    pass
        curr = nxt + timedelta(days=1)

    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
              .sort_values("date")
              .drop_duplicates("date")
              .reset_index(drop=True))


def fetch_program_history(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """프로그램 매매 수집 (40일 단위 청킹)."""
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/program-trade-by-stock-daily"
    start_dt = datetime.strptime(start_date, "%Y%m%d")
    end_dt   = datetime.strptime(end_date,   "%Y%m%d")

    rows, curr = [], start_dt
    while curr <= end_dt:
        nxt = min(curr + timedelta(days=40), end_dt)
        hdrs = _base_headers("FHPPG04650201")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": nxt.strftime("%Y%m%d"),
        }
        res = _call(url, hdrs, params, delay=0.03)
        if res and "output" in res:
            lo = curr.strftime("%Y%m%d")
            hi = nxt.strftime("%Y%m%d")
            for item in res["output"]:
                d = item.get("stck_bsop_date", "")
                if not d or not (lo <= d <= hi):
                    continue
                try:
                    rows.append({
                        "date":         pd.to_datetime(d),
                        "prog_net_qty": int(item["whol_smtn_ntby_qty"]),
                        "prog_buy":     int(item["whol_smtn_shnu_vol"]),
                        "prog_sell":    int(item["whol_smtn_seln_vol"]),
                    })
                except Exception:
                    pass
        curr = nxt + timedelta(days=1)

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df.drop_duplicates("date")


# ──────────────────────────────────────────────────────────────────────────────
# 지표 계산 / 저장 헬퍼
# ──────────────────────────────────────────────────────────────────────────────

def _build_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """전체 df에 대해 지표 재계산."""
    df = df.sort_values("date").reset_index(drop=True)
    df["change_pct"] = df["close"].pct_change().fillna(0)
    df["prog_ratio_vol"] = np.where(
        df["volume"] > 0,
        (df["prog_buy"].fillna(0) + df["prog_sell"].fillna(0)) / df["volume"], 0.0)
    df["prog_net_ratio"] = np.where(
        df["volume"] > 0, df["prog_net_qty"].fillna(0) / df["volume"], 0.0)

    df["target1"]  = df["close"].shift(-1)  / df["close"] - 1
    df["target5"]  = df["close"].shift(-5)  / df["close"] - 1
    df["target20"] = df["close"].shift(-20) / df["close"] - 1

    df = indicators.calculate_indicators_v3_save(df)

    if "ma60" not in df.columns:
        df["ma60"] = df["close"].rolling(60).mean()
    if "disparity_60" not in df.columns:
        df["disparity_60"] = (df["close"] / df["ma60"] - 1).fillna(0)
    if "bb_pos" not in df.columns:
        df["bb_pos"] = df.get("bb_p", 0.0)

    return df


def _save(df: pd.DataFrame, path: Path):
    for col in FINAL_COLUMNS:
        if col not in df.columns:
            df[col] = 0
    # 원자적 저장 (temp + os.replace) — 동시 쓰기로 인한 줄바꿈 유실/파일 손상 방지
    tmp_path = path.with_name(path.name + ".tmp")
    df[FINAL_COLUMNS].fillna(0).to_csv(tmp_path, index=False, encoding="utf-8-sig")
    os.replace(tmp_path, path)


# ──────────────────────────────────────────────────────────────────────────────
# 단일 종목 처리
# ──────────────────────────────────────────────────────────────────────────────

def backfill_one(fpath: Path, target_start_dt: pd.Timestamp,
                 df_kospi: pd.DataFrame, df_kosdaq: pd.DataFrame,
                 skip_prog: bool) -> str:
    """
    Returns:
      "ok"       – 정상 backfill
      "skip"     – 이미 충분한 과거 데이터 존재
      "no_data"  – API에서 데이터 없음
      "error"    – 예외 발생
    """
    code = fpath.stem[1:]
    df_old = pd.DataFrame()
    try:
        df_old = pd.read_csv(fpath, encoding="utf-8-sig")
        if df_old.empty or "date" not in df_old.columns:
            raise ValueError("empty")
        df_old["date"] = pd.to_datetime(df_old["date"])
        df_old = df_old.sort_values("date").reset_index(drop=True)
    except Exception:
        df_old = pd.DataFrame()

    name = df_old["name"].iloc[0] if not df_old.empty and "name" in df_old.columns else code

    if not df_old.empty:
        first_date = df_old["date"].iloc[0]
        # 이미 target_start_dt 이전 데이터가 있으면 스킵
        if first_date <= target_start_dt:
            return "skip"
        # 수집할 기간: target_start_dt ~ first_date - 1일
        fetch_end_dt = first_date - timedelta(days=1)
    else:
        # 빈 CSV — 전체 기간 수집
        fetch_end_dt = pd.Timestamp.now()

    fetch_start = target_start_dt.strftime("%Y%m%d")
    fetch_end   = fetch_end_dt.strftime("%Y%m%d")

    # ── OHLCV 수집 ─────────────────────────────────────────────────────────
    df_price = fetch_ohlcv_history(code, fetch_start, fetch_end)
    if df_price.empty:
        return "no_data"

    # ── 프로그램 매매 수집 ──────────────────────────────────────────────────
    if skip_prog:
        df_price["prog_net_qty"] = 0
        df_price["prog_buy"]     = 0
        df_price["prog_sell"]    = 0
    else:
        df_prog = fetch_program_history(code, fetch_start, fetch_end)
        if not df_prog.empty:
            df_price = df_price.merge(df_prog, on="date", how="left")
        df_price["prog_net_qty"] = df_price.get("prog_net_qty", pd.Series(dtype=float)).fillna(0)
        df_price["prog_buy"]     = df_price.get("prog_buy",     pd.Series(dtype=float)).fillna(0)
        df_price["prog_sell"]    = df_price.get("prog_sell",    pd.Series(dtype=float)).fillna(0)

    # ── 지수 병합 ───────────────────────────────────────────────────────────
    df_price = df_price.merge(df_kospi,  on="date", how="left")
    df_price = df_price.merge(df_kosdaq, on="date", how="left")
    df_price["kospi_change"]  = df_price["kospi_change"].fillna(0)
    df_price["kosdaq_change"] = df_price["kosdaq_change"].fillna(0)

    df_price["code"] = code
    df_price["name"] = name

    # ── 기존 데이터와 합치기 ────────────────────────────────────────────────
    # df_old 의 prog_buy / prog_sell 컬럼이 없을 수 있으므로 보정
    for c in ("prog_buy", "prog_sell"):
        if c not in df_old.columns:
            df_old[c] = 0

    df_combined = (pd.concat([df_price, df_old], ignore_index=True)
                     .sort_values("date")
                     .drop_duplicates("date")
                     .reset_index(drop=True))

    # ── 지표 재계산 + 저장 ──────────────────────────────────────────────────
    df_combined = _build_indicators(df_combined)
    _save(df_combined, fpath)
    return "ok"


# ──────────────────────────────────────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="종목 CSV 과거 데이터 소급 수집")
    parser.add_argument("--target-start", default="20000101",
                        help="소급 시작일 (기본: 20000101)")
    parser.add_argument("--stock-dir",    default=str(ROOT / "Data" / "Stock"),
                        help="종목 CSV 디렉터리")
    parser.add_argument("--sample", type=int, default=None,
                        help="N개 종목만 처리 (테스트)")
    parser.add_argument("--code", default=None,
                        help="특정 종목코드 하나만 처리 (예: 005930)")
    parser.add_argument("--skip-prog", action="store_true",
                        help="프로그램 매매 수집 생략 (속도 향상, 해당 컬럼은 0)")
    args = parser.parse_args()

    if not auth.get_access_token():
        print("토큰 발급 실패.")
        sys.exit(1)

    target_start_dt = pd.Timestamp(args.target_start)
    stock_dir       = Path(args.stock_dir)
    today_str       = datetime.now().strftime("%Y%m%d")

    # ── 지수 데이터 한 번 수집 ────────────────────────────────────────────
    print(f"KOSPI / KOSDAQ 지수 수집 중 ({args.target_start} ~ {today_str})...")
    df_kospi  = fetch_index_history("0001", args.target_start, today_str)
    df_kosdaq = fetch_index_history("1001", args.target_start, today_str)
    print(f"   KOSPI  {len(df_kospi):,}일  /  KOSDAQ {len(df_kosdaq):,}일")

    # ── 대상 파일 목록 ───────────────────────────────────────────────────
    if args.code:
        files = [stock_dir / f"A{args.code}.csv"]
        files = [f for f in files if f.exists()]
    else:
        files = sorted(stock_dir.glob("A*.csv"))

    if args.sample:
        files = files[:args.sample]

    total = len(files)
    print(f"\n대상 종목: {total}개  (skip_prog={args.skip_prog})")
    print("=" * 64)

    results = {"ok": 0, "skip": 0, "no_data": 0, "error": 0}
    t0 = time.time()

    for i, fpath in enumerate(files, 1):
        code = fpath.stem[1:]
        status = backfill_one(
            fpath, target_start_dt,
            df_kospi, df_kosdaq,
            skip_prog=args.skip_prog,
        )

        key = status.split(":")[0]
        results[key] = results.get(key, 0) + 1

        elapsed = time.time() - t0
        rate    = i / elapsed
        remain  = (total - i) / rate if rate > 0 else 0

        marker = {"ok": "✓", "skip": "-", "no_data": "∅"}.get(key, "!")
        print(f"[{i:4d}/{total}] {marker} {code}  {status:<12}  "
              f"{elapsed/60:.1f}분 경과  잔여 {remain/60:.1f}분  "
              f"ok={results['ok']} skip={results['skip']} "
              f"no_data={results.get('no_data',0)} err={results.get('error',0)}")

    print("\n" + "=" * 64)
    print(f"완료  ok={results['ok']}  skip={results['skip']}  "
          f"no_data={results.get('no_data',0)}  error={results.get('error',0)}")
    print(f"소요: {(time.time()-t0)/60:.1f}분")

    # 파이프라인 감지용 마커 파일
    (ROOT / "backfill_done.marker").write_text("done")


if __name__ == "__main__":
    main()