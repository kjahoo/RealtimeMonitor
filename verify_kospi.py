"""
verify_kospi.py
===============
prep 캐시에 저장된 kospi_change 누적값과 실제 KOSPI 지수를 비교 검증.

[실제 데이터 소스 우선순위]
  1. pykrx  : pip install pykrx  (KRX 공식 데이터, 가장 정확)
  2. yfinance: pip install yfinance  (Yahoo Finance, ^KS11)

사용:
  python -X utf8 verify_kospi.py
  python -X utf8 verify_kospi.py --start 2013 --end 2024
  python -X utf8 verify_kospi.py --detail        # 일별 상세 비교 출력
"""

import argparse
import warnings
import sys
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

ROOT     = Path(r"C:\Projects\RealtimeMonitor")
PREP_DIR = ROOT / "Data" / "_prep_wf_v3"


# ══════════════════════════════════════════════════════════════════════════════
# 1. prep 캐시에서 kospi_change 추출
# ══════════════════════════════════════════════════════════════════════════════

def load_kospi_from_cache() -> pd.Series:
    """
    prep 캐시 파일 하나에서 kospi_change 를 추출.
    모든 종목이 동일 날짜에 동일한 kospi_change 를 가짐.
    """
    feather = sorted(PREP_DIR.glob("*.feather"))
    prep_files = feather if feather else sorted(PREP_DIR.glob("*.pkl"))
    if not prep_files:
        print(f"❌ prep 캐시 없음: {PREP_DIR}")
        sys.exit(1)

    for fpath in prep_files:
        try:
            df = pd.read_feather(str(fpath)) if fpath.suffix == '.feather' else pd.read_pickle(str(fpath))
            df['date'] = pd.to_datetime(df['date'])
            if 'kospi_change' not in df.columns:
                continue
            s = df.set_index('date')['kospi_change'].dropna().sort_index()
            if len(s) > 100:
                print(f"   캐시 소스: {fpath.name}  ({len(s):,}일)")
                return s
        except Exception:
            continue

    print("❌ kospi_change 컬럼을 가진 캐시 파일을 찾을 수 없습니다.")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
# 2. 실제 KOSPI 일별 수익률 취득
# ══════════════════════════════════════════════════════════════════════════════

def fetch_kospi_pykrx(start: str, end: str):
    """pykrx로 KOSPI 일별 종가 수익률 취득."""
    try:
        from pykrx import stock
        print("   📡 pykrx로 KOSPI 실제 데이터 취득 중...")
        df = stock.get_index_ohlcv_by_date(start, end, "1001")  # 1001 = KOSPI
        if df is None or df.empty:
            return None
        df.index = pd.to_datetime(df.index)
        close = df['종가'].sort_index()
        daily_ret = close.pct_change().dropna()
        print(f"   ✅ pykrx: {len(daily_ret):,}일 ({daily_ret.index[0].date()} ~ {daily_ret.index[-1].date()})")
        return daily_ret
    except ImportError:
        print("   ℹ️  pykrx 미설치 → yfinance 시도")
        return None
    except Exception as e:
        print(f"   ⚠️  pykrx 오류: {e}")
        return None


def fetch_kospi_yfinance(start: str, end: str):
    """yfinance로 KOSPI 일별 종가 수익률 취득."""
    try:
        import yfinance as yf
        print("   📡 yfinance로 KOSPI 실제 데이터 취득 중...")
        df = yf.download("^KS11", start=start, end=end, progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        close = df['Close'].squeeze().dropna().sort_index()
        daily_ret = close.pct_change().dropna()
        print(f"   ✅ yfinance: {len(daily_ret):,}일 ({daily_ret.index[0].date()} ~ {daily_ret.index[-1].date()})")
        return daily_ret
    except ImportError:
        print("   ⚠️  yfinance 미설치")
        return None
    except Exception as e:
        print(f"   ⚠️  yfinance 오류: {e}")
        return None


def fetch_actual_kospi(start_year: int, end_year: int):
    start = f"{start_year}0101"
    end   = f"{end_year}1231"
    ret = fetch_kospi_pykrx(start, end)
    if ret is None:
        ret = fetch_kospi_yfinance(f"{start_year}-01-01", f"{end_year}-12-31")
    return ret


# ══════════════════════════════════════════════════════════════════════════════
# 3. 비교 분석
# ══════════════════════════════════════════════════════════════════════════════

def cumulative_return(daily_ret: pd.Series) -> float:
    """일별 수익률 시리즈 → 누적 수익률(%)."""
    return ((1 + daily_ret).prod() - 1) * 100


def yearly_table(cache_ret: pd.Series, actual_ret,
                 start_year: int, end_year: int) -> pd.DataFrame:
    rows = []
    for year in range(start_year, end_year + 1):
        y_str = str(year)
        c_period = cache_ret[cache_ret.index.year == year]
        c_cum    = cumulative_return(c_period) if len(c_period) > 0 else float('nan')

        a_cum = float('nan')
        if actual_ret is not None:
            a_period = actual_ret[actual_ret.index.year == year]
            if len(a_period) > 0:
                a_cum = cumulative_return(a_period)

        diff = round(c_cum - a_cum, 2) if not (np.isnan(c_cum) or np.isnan(a_cum)) else float('nan')

        rows.append({
            "연도":        year,
            "캐시(%)":     round(c_cum, 2) if not np.isnan(c_cum) else '-',
            "실제(%)":     round(a_cum, 2) if not np.isnan(a_cum) else '-',
            "차이(%p)":    diff if not np.isnan(diff) else '-',
            "캐시_거래일": len(c_period),
        })
    return pd.DataFrame(rows)


def daily_comparison(cache_ret: pd.Series, actual_ret: pd.Series) -> pd.DataFrame:
    """두 시리즈를 날짜 기준으로 inner join 후 비교."""
    df = pd.DataFrame({
        "cache":  cache_ret,
        "actual": actual_ret,
    }).dropna()
    df["diff"] = df["cache"] - df["actual"]
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 4. 메인
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="prep 캐시 KOSPI vs 실제 KOSPI 검증")
    parser.add_argument("--start",  type=int, default=2013, help="시작 연도 (기본: 2013)")
    parser.add_argument("--end",    type=int, default=2025, help="종료 연도 (기본: 2025)")
    parser.add_argument("--detail", action="store_true",    help="일별 상세 통계 출력")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"📊 KOSPI 데이터 검증  ({args.start} ~ {args.end})")
    print(f"{'='*60}")

    # ── 캐시 데이터 ────────────────────────────────────────────────────────────
    print(f"\n[1] prep 캐시에서 kospi_change 읽는 중...")
    cache_full = load_kospi_from_cache()
    cache_range = cache_full[
        (cache_full.index.year >= args.start) &
        (cache_full.index.year <= args.end)
    ]
    print(f"   기간 내 거래일: {len(cache_range):,}일  "
          f"({cache_range.index[0].date()} ~ {cache_range.index[-1].date()})")

    # ── 실제 KOSPI ─────────────────────────────────────────────────────────────
    print(f"\n[2] 실제 KOSPI 취득 중...")
    actual = fetch_actual_kospi(args.start, args.end)
    has_actual = actual is not None

    # ── 연도별 비교 테이블 ────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"{'연도':>5}  {'캐시(%)':>9}  {'실제(%)':>9}  {'차이(%p)':>9}  {'캐시_거래일':>10}")
    print(f"{'─'*60}")

    ytable = yearly_table(cache_range, actual, args.start, args.end)
    for _, r in ytable.iterrows():
        diff_str = f"{r['차이(%p)']:+.2f}" if isinstance(r['차이(%p)'], float) else str(r['차이(%p)'])
        a_str    = f"{r['실제(%)']:+.2f}"  if isinstance(r['실제(%)'],  float) else str(r['실제(%)'])
        c_str    = f"{r['캐시(%)']:+.2f}"  if isinstance(r['캐시(%)'],  float) else str(r['캐시(%)'])
        print(f"{int(r['연도']):>5}  {c_str:>9}  {a_str:>9}  {diff_str:>9}  {int(r['캐시_거래일']):>10}")

    print(f"{'─'*60}")

    # 전체 기간 누적
    c_total = cumulative_return(cache_range)
    print(f"{'합계':>5}  {c_total:+9.2f}%", end="")
    if has_actual:
        actual_range = actual[(actual.index.year >= args.start) & (actual.index.year <= args.end)]
        a_total = cumulative_return(actual_range)
        diff_total = c_total - a_total
        print(f"  {a_total:+9.2f}%  {diff_total:+9.2f}%p")
    else:
        print()

    # ── 일별 상관관계 분석 ──────────────────────────────────────────────────────
    if has_actual:
        print(f"\n[3] 일별 수익률 상관관계 분석")
        comp = daily_comparison(cache_range, actual)
        if not comp.empty:
            corr     = comp['cache'].corr(comp['actual'])
            mae      = comp['diff'].abs().mean() * 100   # 퍼센트로 변환
            max_diff = comp['diff'].abs().max() * 100
            n_mismatch = (comp['diff'].abs() > 0.005).sum()  # 0.5%p 이상 차이 나는 날

            print(f"   공통 거래일 : {len(comp):,}일")
            print(f"   일별 상관계수: {corr:.6f}  (1.0에 가까울수록 정확)")
            print(f"   평균 절대오차: {mae:.4f}%p/일")
            print(f"   최대 절대오차: {max_diff:.4f}%p")
            print(f"   0.5%p 이상 차이나는 날: {n_mismatch}일")

            if args.detail and n_mismatch > 0:
                print(f"\n   [차이 큰 날 TOP 10]")
                top10 = comp.nlargest(10, 'diff' if False else comp['diff'].abs().name)
                # abs 기준 정렬
                top10 = comp.assign(abs_diff=comp['diff'].abs()).nlargest(10, 'abs_diff')
                print(f"   {'날짜':^12}  {'캐시':>9}  {'실제':>9}  {'차이':>9}")
                for date, row in top10.iterrows():
                    print(f"   {str(date.date()):^12}  "
                          f"{row['cache']*100:+8.3f}%  "
                          f"{row['actual']*100:+8.3f}%  "
                          f"{row['diff']*100:+8.3f}%p")

            # 판정
            print(f"\n{'─'*60}")
            if corr > 0.999 and mae < 0.01:
                verdict = "✅ 매우 정확 — 벤치마크 계산 신뢰 가능"
            elif corr > 0.99 and mae < 0.05:
                verdict = "🟡 대체로 정확 — 누적 오차 주의 필요"
            else:
                verdict = "🔴 오차 큼 — 벤치마크 데이터 점검 필요"
            print(f"   판정: {verdict}")
            print(f"{'─'*60}")

    else:
        print(f"\n⚠️  실제 KOSPI 데이터를 취득하지 못했습니다.")
        print(f"   설치 후 재실행:")
        print(f"     pip install pykrx")
        print(f"   또는:")
        print(f"     pip install yfinance")

        # 실제 데이터 없이도 알 수 있는 참고값 출력
        print(f"\n[참고] 알려진 KOSPI 연간 수익률 (수동 비교용)")
        known = {
            2013: +0.7,  2014: -4.8,  2015: +2.4,  2016: +3.3,
            2017: +21.8, 2018: -17.3, 2019: +7.7,  2020: +30.8,
            2021: +3.6,  2022: -24.9, 2023: +18.7, 2024: -9.6,
            2025: None,  # 확인 필요 — 실제값 입력 후 검증
        }
        print(f"   {'연도':>5}  {'실제(참고)':>12}  {'캐시(%)':>10}  {'캐시_거래일':>10}")
        for year in range(args.start, args.end + 1):
            if year not in known:
                continue
            c_row  = ytable[ytable['연도'] == year]
            c_val  = c_row['캐시(%)'].values[0]      if not c_row.empty else '-'
            c_days = c_row['캐시_거래일'].values[0]  if not c_row.empty else 0
            c_str  = f"{c_val:+.2f}" if isinstance(c_val, float) else str(c_val)
            ref    = known[year]
            ref_str = f"{ref:>+11.1f}%" if ref is not None else "     미확인"
            flag   = ""
            if ref is not None and isinstance(c_val, float) and abs(c_val - ref) > 1.0:
                flag = "  ⚠️ 오차 큼"
            print(f"   {year:>5}  {ref_str}  {c_str:>10}%  {int(c_days):>10}{flag}")


if __name__ == "__main__":
    main()