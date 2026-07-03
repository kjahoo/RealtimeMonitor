# -*- coding: utf-8 -*-
"""
analyze_holdout.py
==================
holdout_comparison.py 가 생성한 holdout_caches/scores_<tag>_<year>.pkl 들을
읽어 26개 모델(Expanding 13 + Rolling 12 + Production)을 두 holdout 기간에서
"자세히" 비교 분석한다.

  Holdout-A : 2000~2006  (전 모델 학습 前 구간)
  Holdout-B : 2026       (전 모델 OOS 後 구간, 연초~오늘)

특징
  - TensorFlow 를 import 하지 않는다. (점수는 이미 캐시됨 → GPU 추론 불필요)
    → 돌고 있는 holdout_comparison 시뮬레이션 / 라이브 봇과 GPU 충돌 없음.
  - 캐시가 부분적으로만 쌓인 상태에서도 동작 (현재 존재하는 캐시만 분석, 커버리지 표시).
  - 시뮬레이션/메트릭/가격로딩/품질필터 로직과 전략 파라미터는 holdout_comparison.py 와
    "동일"하게 복제되어 있다. holdout_comparison.py 의 전략값을 바꾸면 아래도 같이 맞출 것.

산출물 (모두 analysis_holdout/ 폴더)
  metrics_detail.csv        모델×연도 상세 지표 (시뮬 재계산 결과 캐시)
  summary_by_holdout.csv    모델×구간 요약 (평균/중앙/누적/일관성/KOSPI초과 등)
  ranking_composite.csv     복합 강건성 점수 랭킹
  stability_cross.csv       Holdout-A ↔ Holdout-B 안정성 (순위상관)
  family_comparison.csv     Expanding vs Rolling vs Production 계열 비교
  signal_diagnostics.csv    캐시 기반 시그널 진단 (매수신호 빈도/폭/점수분포)
  REPORT.md                 위 내용을 종합한 텍스트 리포트 + 추천 모델
  *.png                     히트맵 / 랭킹 / 안정성 산점도 / 상위모델 자산곡선

실행
  python -X utf8 analyze_holdout.py            # 전체 (시뮬 재계산 → 캐시 → 분석/차트)
  python -X utf8 analyze_holdout.py --refresh  # 메트릭 캐시 무시하고 강제 재계산
  python -X utf8 analyze_holdout.py --quick     # 메트릭 캐시만 사용 (시뮬 스킵, 차트/리포트만)
  python -X utf8 analyze_holdout.py --no-charts # 차트 생성 생략
"""

import os, sys, gc, time, argparse, warnings
from pathlib import Path
from datetime import date as dtdate

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ── 경로 ────────────────────────────────────────────────────────────────────────
ROOT      = Path(r"C:\Projects\RealtimeMonitor")
DATA_DIR  = ROOT / "Data" / "Stock"
CACHE_DIR = ROOT / "holdout_caches"
OUT_DIR   = ROOT / "analysis_holdout"
OUT_DIR.mkdir(exist_ok=True)

# ── 전략 파라미터 (holdout_comparison.py 와 반드시 동일하게 유지) ────────────────
INITIAL_CAPITAL = 1_000_000_000
BUY_THRESH      = 0.55
ALLOC_PER_STOCK = 0.10
SELL_THRESH     = 0.50
BUY_FEE_RATE    = 0.00015
SELL_FEE_RATE   = 0.00195

HOLDOUT_A = list(range(2000, 2007))   # 2000~2006
HOLDOUT_B = [2026]
TODAY     = pd.Timestamp(dtdate.today())

# 복합 강건성 점수 가중치 (합 1.0). 방향: 클수록 좋게 정규화한 뒤 가중합.
COMPOSITE_WEIGHTS = {
    "수익률(%)":  0.25,   # 평균 연수익
    "Sharpe":     0.30,   # 위험조정수익
    "MDD(%)":     0.15,   # 낙폭 (작을수록↑ → 부호반전)
    "KOSPI승률":  0.15,   # KOSPI 초과 연도 비율
    "일관성":     0.15,   # 플러스 수익 연도 비율
}


# ══════════════════════════════════════════════════════════════════════════════
# 품질 필터 (holdout_comparison.py 와 동일)
# ══════════════════════════════════════════════════════════════════════════════
def _max_consecutive(bool_series: pd.Series) -> int:
    if bool_series.empty:
        return 0
    g = (bool_series != bool_series.shift()).cumsum()
    return int(bool_series.groupby(g).sum().max())


def _is_bad_stock(close: pd.Series, volume: pd.Series = None) -> bool:
    if len(close) < 2:
        return True
    if _max_consecutive(close.diff() == 0) >= 14:
        return True
    if (close.pct_change().abs() > 0.40).any():
        return True
    if volume is not None and _max_consecutive(volume == 0) >= 10:
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# 포트폴리오 시뮬레이션 (holdout_comparison.py 와 동일 로직)
# ══════════════════════════════════════════════════════════════════════════════
class _Portfolio:
    def __init__(self):
        self.cash      = float(INITIAL_CAPITAL)
        self.positions = {}
        self.trade_log = []

    def _price(self, code, prices):
        p = prices.get(code)
        if p and p > 0:
            return p
        pos = self.positions.get(code)
        return pos["avg_price"] if pos else 0.0

    def total_assets(self, prices):
        mv = sum(pos["qty"] * self._price(code, prices)
                 for code, pos in self.positions.items())
        return self.cash + mv

    def market_value(self, code, prices):
        pos = self.positions.get(code)
        return pos["qty"] * self._price(code, prices) if pos else 0.0

    def buy(self, code, amount, price, date):
        qty  = int(amount / price)
        cost = qty * price * (1 + BUY_FEE_RATE)
        if qty == 0 or cost > self.cash:
            return False
        self.cash -= cost
        if code in self.positions:
            pos     = self.positions[code]
            new_qty = pos["qty"] + qty
            pos["avg_price"] = (pos["qty"] * pos["avg_price"] + qty * price) / new_qty
            pos["qty"]       = new_qty
        else:
            self.positions[code] = {"qty": qty, "avg_price": price}
        self.trade_log.append({"date": date, "action": "BUY", "code": code,
                               "qty": qty, "price": price, "amount": qty * price})
        return True

    def sell_all(self, code, price, date):
        pos = self.positions.pop(code, None)
        if not pos:
            return
        qty = pos["qty"]
        self.cash += qty * price * (1 - SELL_FEE_RATE)
        self.trade_log.append({"date": date, "action": "SELL", "code": code,
                               "qty": qty, "price": price, "amount": qty * price})


def run_simulation(score_df, year, price_pivot, end_cap=None):
    y_start = pd.Timestamp(f"{year}-01-01")
    y_end   = pd.Timestamp(f"{year+1}-01-01")
    if year == 2026:
        y_end = min(TODAY + pd.Timedelta(days=1), y_end)
    # 모델 간 공정 비교용: 공통 종료일(end_cap)까지로 시뮬 구간 제한
    if end_cap is not None:
        y_end = min(y_end, pd.Timestamp(end_cap) + pd.Timedelta(days=1))

    scores_by_date = (score_df.groupby('date')
                      .apply(lambda g: dict(zip(g['code'], g['score'])))
                      .to_dict())
    mask         = (price_pivot.index >= y_start) & (price_pivot.index < y_end)
    trading_days = price_pivot.loc[mask].index.tolist()

    portfolio = _Portfolio()
    history   = []
    for date in trading_days:
        prices       = price_pivot.loc[date].dropna().to_dict()
        today_scores = scores_by_date.get(date, {})

        for code in list(portfolio.positions.keys()):
            score = today_scores.get(code)
            price = prices.get(code)
            if score is None or not price or price <= 0:
                continue
            if score < SELL_THRESH:
                portfolio.sell_all(code, price, date)

        total      = portfolio.total_assets(prices)
        candidates = []
        for code, score in today_scores.items():
            price = prices.get(code)
            if not price or price <= 0 or score < BUY_THRESH:
                continue
            cur_alloc = portfolio.market_value(code, prices) / total if total > 0 else 0.0
            if ALLOC_PER_STOCK > cur_alloc + 0.01:
                candidates.append((code, score, ALLOC_PER_STOCK - cur_alloc))

        for code, score, incr in sorted(candidates, key=lambda x: x[1], reverse=True):
            buy_amount = incr * total
            if portfolio.cash < buy_amount:
                continue
            portfolio.buy(code, buy_amount, prices[code], date)

        history.append({"date": date, "total_assets": portfolio.total_assets(prices)})

    return pd.DataFrame(history), pd.DataFrame(portfolio.trade_log)


def calc_metrics(history_df, trade_df):
    if history_df.empty or len(history_df) < 5:
        return None
    ta        = history_df.set_index("date")["total_assets"]
    ret       = (ta.iloc[-1] / ta.iloc[0] - 1) * 100
    daily_ret = ta.pct_change().dropna()
    sharpe    = (daily_ret.mean() / daily_ret.std() * np.sqrt(252)
                 if daily_ret.std() > 0 else 0.0)
    max_dd    = ((ta - ta.cummax()) / ta.cummax()).min() * 100
    win_rate  = (daily_ret > 0).sum() / len(daily_ret) * 100
    n_days    = len(ta)
    avg_asset = ta.mean()
    if not trade_df.empty and avg_asset > 0:
        turnover = (trade_df['amount'].sum() / avg_asset / 2) * (252 / n_days) * 100
    else:
        turnover = 0.0
    return {
        "수익률(%)": round(ret, 2),
        "Sharpe":    round(sharpe, 2),
        "MDD(%)":    round(max_dd, 2),
        "일승률(%)": round(win_rate, 1),
        "회전율(%)": round(turnover, 1),
        "거래횟수":  len(trade_df) if not trade_df.empty else 0,
        "거래일수":  n_days,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 가격 / KOSPI 로딩 (holdout_comparison.py 와 동일)
# ══════════════════════════════════════════════════════════════════════════════
def load_prices(years):
    y_min, y_max = min(years), max(years)
    h_start = pd.Timestamp(f"{y_min}-01-01")
    h_end   = pd.Timestamp(f"{y_max+1}-01-01")
    if y_max == 2026:
        h_end = min(TODAY + pd.Timedelta(days=1), h_end)

    frames, excl = [], 0
    files = sorted(DATA_DIR.glob("A*.csv"))
    t0 = time.time()
    for i, fpath in enumerate(files):
        code = fpath.stem[1:]
        try:
            df = pd.read_csv(fpath, encoding='utf-8-sig',
                             usecols=lambda c: c in ['date', 'close', 'volume'],
                             dtype={'close': float})
            df['date'] = pd.to_datetime(df['date'])
            df = df[(df['date'] >= h_start) & (df['date'] <= h_end)] \
                 .sort_values('date').reset_index(drop=True)
            if df.empty:
                continue
            close  = df['close'].astype(float)
            volume = df['volume'].astype(float) if 'volume' in df.columns else None
            if _is_bad_stock(close, volume):
                excl += 1
                continue
            frames.append(df[['date', 'close']].assign(code=code))
        except Exception:
            pass
        if (i + 1) % 400 == 0 or i == len(files) - 1:
            print(f"\r   가격 로딩 [{i+1}/{len(files)}]  {(time.time()-t0)/60:.1f}분  "
                  f"유효 {len(frames)}  제외 {excl}", end="", flush=True)
    print()
    if not frames:
        return pd.DataFrame()
    all_df = pd.concat(frames, ignore_index=True)
    return all_df.pivot_table(index='date', columns='code', values='close').sort_index()


def get_kospi_by_year(years):
    result = {}
    ref_path = DATA_DIR / "A005930.csv"
    if not ref_path.exists():
        cands = sorted(DATA_DIR.glob("A*.csv"))
        if not cands:
            return result
        ref_path = cands[0]
    try:
        ref = pd.read_csv(str(ref_path), encoding='utf-8-sig', usecols=['date', 'kospi_change'])
        ref['date'] = pd.to_datetime(ref['date'])
        ref = ref.set_index('date').sort_index()
        for year in years:
            period = ref[ref.index.year == year]
            if not period.empty:
                result[year] = round(((1 + period['kospi_change']).prod() - 1) * 100, 2)
    except Exception:
        pass
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 캐시 탐색
# ══════════════════════════════════════════════════════════════════════════════
def _label_for(tag: str) -> str:
    if tag.startswith("exp_"):
        return f"Exp-{tag.split('_')[1]}"
    if tag.startswith("roll_"):
        return f"Roll-{tag.split('_')[1]}"
    if tag == "production":
        return "Production"
    return tag


def _family_for(tag: str) -> str:
    if tag.startswith("exp_"):
        return "Expanding"
    if tag.startswith("roll_"):
        return "Rolling7y"
    return "Production"


def discover_caches():
    """holdout_caches/scores_<tag>_<year>.pkl → {tag: {year: path}}"""
    out = {}
    for p in sorted(CACHE_DIR.glob("scores_*_*.pkl")):
        stem = p.stem[len("scores_"):]      # e.g. exp_01_2003  /  production_2026
        try:
            year = int(stem[-4:])
            tag  = stem[:-5]                 # 끝 '_YYYY' 제거
        except ValueError:
            continue
        out.setdefault(tag, {})[year] = p
    return out


def common_2026_cutoff(caches):
    """모든 모델의 2026 점수 캐시가 '공통으로' 커버하는 마지막 거래일(교집합 종료일).

    WF 모델은 feather(_prep_wf_v3, 2026-05-29까지)를, Production 은 raw CSV
    (Data/Stock, 더 최신)를 쓰므로 2026 종료일이 다르다. 공정 비교를 위해
    전 모델의 2026 max date 중 '최소값'(=모두가 가진 마지막 날)으로 통일한다.
    """
    maxes = []
    for tag, yr_map in caches.items():
        p = yr_map.get(2026)
        if p is None:
            continue
        try:
            d = pd.to_datetime(pd.read_pickle(str(p))['date']).max()
            maxes.append(d)
        except Exception:
            pass
    return min(maxes) if maxes else None


# ══════════════════════════════════════════════════════════════════════════════
# 시그널 진단 (캐시 자체에서 — 시뮬 없이 빠름)
# ══════════════════════════════════════════════════════════════════════════════
def signal_diagnostics(score_df: pd.DataFrame) -> dict:
    n_dates = score_df['date'].nunique()
    buy  = score_df[score_df['score'] >= BUY_THRESH]
    sig_per_day = len(buy) / n_dates if n_dates else 0.0
    breadth     = buy['code'].nunique()
    return {
        "평균점수":      round(score_df['score'].mean(), 4),
        "점수표준편차":  round(score_df['score'].std(), 4),
        "점수p90":       round(score_df['score'].quantile(0.90), 4),
        "매수신호/일":   round(sig_per_day, 1),
        "매수종목수(폭)": breadth,
        "관측종목수":    score_df['code'].nunique(),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 메트릭 테이블 구축 (시뮬 재계산 + 캐싱)
# ══════════════════════════════════════════════════════════════════════════════
METRICS_CACHE = OUT_DIR / "metrics_detail.csv"


def build_metrics_table(caches, kospi_dict, refresh: bool, quick: bool):
    # 기존 캐시 로드
    prev = pd.DataFrame()
    if METRICS_CACHE.exists() and not refresh:
        prev = pd.read_csv(METRICS_CACHE, dtype={'태그': str})
        prev['mtime'] = prev.get('mtime', 0)

    if quick:
        if prev.empty:
            print("⚠ --quick 모드인데 메트릭 캐시가 없습니다. 먼저 일반 실행이 필요합니다.")
            sys.exit(1)
        return prev

    # 어떤 (tag, year) 를 (재)계산할지 결정 — 파일 mtime 변동 시만
    todo = []
    for tag, yr_map in caches.items():
        for year, path in yr_map.items():
            mt = path.stat().st_mtime
            if not prev.empty:
                hit = prev[(prev['태그'] == tag) & (prev['연도'] == year)]
                if not hit.empty and abs(float(hit['mtime'].iloc[0]) - mt) < 1:
                    continue
            todo.append((tag, year, path, mt))

    if not todo:
        print("   (변경된 캐시 없음 — 기존 메트릭 재사용)")
        return prev

    # 2026 공통 종료일 (모델 간 공정 비교) — 전 모델이 공통으로 가진 마지막 거래일
    b_cutoff = common_2026_cutoff(caches)
    if b_cutoff is not None:
        print(f"\n[2026 공통 종료일] {b_cutoff.date()} 로 통일 (모델 간 동일 구간 비교)")

    years_needed = sorted({y for _, y, _, _ in todo})
    print(f"\n[가격 로딩] 필요한 연도: {years_needed}")
    price_pivot = load_prices(years_needed)
    if price_pivot.empty:
        print("❌ 가격 데이터 없음")
        sys.exit(1)
    print(f"   종목 {price_pivot.shape[1]:,} · 거래일 {price_pivot.shape[0]:,}")

    rows = []
    for i, (tag, year, path, mt) in enumerate(todo, 1):
        print(f"   [{i}/{len(todo)}] 시뮬 {tag} {year} ...", end=" ", flush=True)
        score_df = pd.read_pickle(str(path))
        score_df['date'] = pd.to_datetime(score_df['date'])
        cap = None
        if year == 2026 and b_cutoff is not None:
            score_df = score_df[score_df['date'] <= b_cutoff]   # 공통 구간으로 클립
            cap = b_cutoff
        diag = signal_diagnostics(score_df)
        hist, trades = run_simulation(score_df, year, price_pivot, end_cap=cap)
        stats = calc_metrics(hist, trades)
        if not stats:
            print("거래 없음 → 스킵")
            continue
        kospi  = kospi_dict.get(year, float('nan'))
        excess = round(stats['수익률(%)'] - kospi, 2) if not np.isnan(kospi) else float('nan')
        rows.append({
            "구분":  "Holdout-A" if year in HOLDOUT_A else "Holdout-B",
            "태그":  tag, "모델": _label_for(tag), "계열": _family_for(tag),
            "연도":  year,
            **stats,
            "KOSPI(%)": kospi, "초과(%p)": excess,
            **diag, "mtime": mt,
        })
        print(f"수익 {stats['수익률(%)']:+.1f}%  Sh {stats['Sharpe']:.2f}")
        del score_df, hist, trades
        gc.collect()

    new_df = pd.DataFrame(rows)
    if not prev.empty:
        # 갱신된 (tag, year) 는 prev 에서 제거 후 합침
        key_new = set(zip(new_df['태그'], new_df['연도'])) if not new_df.empty else set()
        keep = prev[~prev.apply(lambda r: (r['태그'], r['연도']) in key_new, axis=1)]
        merged = pd.concat([keep, new_df], ignore_index=True)
    else:
        merged = new_df
    merged = merged.sort_values(['계열', '태그', '연도']).reset_index(drop=True)
    merged.to_csv(METRICS_CACHE, index=False, encoding='utf-8-sig')
    print(f"\n💾 {METRICS_CACHE.name} ({len(merged)} 행)")
    return merged


# ══════════════════════════════════════════════════════════════════════════════
# 요약 / 랭킹 / 안정성 / 계열
# ══════════════════════════════════════════════════════════════════════════════
def summarize_by_holdout(df):
    rows = []
    for (tag, part_tag), g in df.groupby(['태그', '구분']):
        valid = g.dropna(subset=['KOSPI(%)'])
        rows.append({
            "모델":   _label_for(tag), "태그": tag, "계열": _family_for(tag),
            "구분":   part_tag, "연수": len(g),
            "평균수익(%)":  round(g['수익률(%)'].mean(), 2),
            "중앙수익(%)":  round(g['수익률(%)'].median(), 2),
            "최저수익(%)":  round(g['수익률(%)'].min(), 2),
            "수익표준편차": round(g['수익률(%)'].std(), 2) if len(g) > 1 else 0.0,
            "누적수익(%)":  round(((1 + g['수익률(%)']/100).prod() - 1) * 100, 2),
            "평균Sharpe":   round(g['Sharpe'].mean(), 2),
            "평균MDD(%)":   round(g['MDD(%)'].mean(), 2),
            "최악MDD(%)":   round(g['MDD(%)'].min(), 2),
            "일관성":       round((g['수익률(%)'] > 0).mean(), 3),     # 플러스 연도 비율
            "KOSPI승률":    round((valid['초과(%p)'] > 0).mean(), 3) if not valid.empty else float('nan'),
            "평균회전율(%)": round(g['회전율(%)'].mean(), 1),
            "평균거래":     round(g['거래횟수'].mean(), 0),
        })
    return pd.DataFrame(rows).sort_values(['구분', '평균Sharpe'], ascending=[True, False])


def _minmax_norm(s):
    s = s.astype(float)
    lo, hi = s.min(), s.max()
    if hi - lo < 1e-12:
        return pd.Series(0.5, index=s.index)
    return (s - lo) / (hi - lo)


def composite_rank(summary):
    """구간별 복합 강건성 점수 (0~100). MDD 는 부호반전(작을수록↑)."""
    out = []
    for part_tag, g in summary.groupby('구분'):
        g = g.copy()
        comp = pd.Series(0.0, index=g.index)
        for col, w in COMPOSITE_WEIGHTS.items():
            if col == "수익률(%)":
                norm = _minmax_norm(g["평균수익(%)"])
            elif col == "Sharpe":
                norm = _minmax_norm(g["평균Sharpe"])
            elif col == "MDD(%)":
                norm = _minmax_norm(-g["평균MDD(%)"])     # MDD 는 음수 → 부호반전
            elif col == "KOSPI승률":
                norm = _minmax_norm(g["KOSPI승률"].fillna(0))
            elif col == "일관성":
                norm = _minmax_norm(g["일관성"])
            comp = comp + w * norm
        g["복합점수"] = (comp * 100).round(1)
        out.append(g[["모델", "태그", "계열", "구분", "복합점수",
                      "평균수익(%)", "평균Sharpe", "평균MDD(%)", "KOSPI승률", "일관성"]])
    res = pd.concat(out, ignore_index=True)

    # 두 구간 모두 있는 모델은 평균 복합점수로 종합 랭킹
    both = (res.groupby(['모델', '태그', '계열'])['복합점수']
            .agg(['mean', 'count']).reset_index()
            .rename(columns={'mean': '종합복합점수', 'count': '구간수'}))
    both['종합복합점수'] = both['종합복합점수'].round(1)
    both = both.sort_values('종합복합점수', ascending=False).reset_index(drop=True)
    both.insert(0, '순위', both.index + 1)
    return res.sort_values(['구분', '복합점수'], ascending=[True, False]), both


def cross_holdout_stability(summary):
    """Holdout-A vs Holdout-B 평균수익/Sharpe 의 모델별 대응 + 순위상관."""
    a = summary[summary['구분'] == 'Holdout-A'].set_index('태그')
    b = summary[summary['구분'] == 'Holdout-B'].set_index('태그')
    common = sorted(set(a.index) & set(b.index))
    rows = []
    for tag in common:
        rows.append({
            "모델": _label_for(tag), "태그": tag, "계열": _family_for(tag),
            "A_평균수익(%)": a.loc[tag, '평균수익(%)'],
            "B_수익(%)":     b.loc[tag, '평균수익(%)'],
            "A_Sharpe":      a.loc[tag, '평균Sharpe'],
            "B_Sharpe":      b.loc[tag, '평균Sharpe'],
        })
    df = pd.DataFrame(rows)
    corr = {}
    if len(df) >= 3:
        corr['수익_Spearman'] = round(df['A_평균수익(%)'].corr(df['B_수익(%)'], method='spearman'), 3)
        corr['Sharpe_Spearman'] = round(df['A_Sharpe'].corr(df['B_Sharpe'], method='spearman'), 3)
    return df, corr


def family_comparison(summary):
    rows = []
    for (fam, part_tag), g in summary.groupby(['계열', '구분']):
        rows.append({
            "계열": fam, "구분": part_tag, "모델수": len(g),
            "평균수익(%)":  round(g['평균수익(%)'].mean(), 2),
            "평균Sharpe":   round(g['평균Sharpe'].mean(), 2),
            "평균MDD(%)":   round(g['평균MDD(%)'].mean(), 2),
            "평균일관성":   round(g['일관성'].mean(), 3),
            "평균KOSPI승률": round(g['KOSPI승률'].mean(), 3),
        })
    return pd.DataFrame(rows).sort_values(['구분', '평균Sharpe'], ascending=[True, False])


# ══════════════════════════════════════════════════════════════════════════════
# 차트
# ══════════════════════════════════════════════════════════════════════════════
def _setup_mpl():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    for fam in ["Malgun Gothic", "NanumGothic", "AppleGothic"]:
        try:
            matplotlib.rcParams['font.family'] = fam
            break
        except Exception:
            continue
    matplotlib.rcParams['axes.unicode_minus'] = False
    return plt


def _heatmap(plt, pivot, title, fname, fmt="{:.0f}", cmap="RdYlGn", center0=True):
    if pivot.empty:
        return
    fig, ax = plt.subplots(figsize=(max(8, 0.7*pivot.shape[1]+3), max(4, 0.35*pivot.shape[0]+2)))
    data = pivot.values.astype(float)
    vmax = np.nanmax(np.abs(data)) if center0 else np.nanmax(data)
    vmin = -vmax if center0 else np.nanmin(data)
    im = ax.imshow(data, aspect='auto', cmap=cmap, vmin=vmin, vmax=vmax)
    ax.set_xticks(range(pivot.shape[1])); ax.set_xticklabels(pivot.columns, rotation=0)
    ax.set_yticks(range(pivot.shape[0])); ax.set_yticklabels(pivot.index, fontsize=8)
    for r in range(pivot.shape[0]):
        for c in range(pivot.shape[1]):
            v = data[r, c]
            if not np.isnan(v):
                ax.text(c, r, fmt.format(v), ha='center', va='center', fontsize=7)
    ax.set_title(title); fig.colorbar(im, ax=ax, shrink=0.7)
    fig.tight_layout(); fig.savefig(OUT_DIR / fname, dpi=120); plt.close(fig)


def make_charts(detail, summary, ranking_both, stability):
    plt = _setup_mpl()

    # 1) 연도별 수익률/Sharpe 히트맵 (모델×연도) — 전체 비교연도 2000~2006 + 2026
    if not detail.empty:
        piv = detail.pivot_table(index='모델', columns='연도', values='수익률(%)')
        _heatmap(plt, piv, "연도별 수익률(%)  (Holdout-A 2000~2006 + Holdout-B 2026)",
                 "heatmap_return.png", "{:+.0f}")
        pivs = detail.pivot_table(index='모델', columns='연도', values='Sharpe')
        _heatmap(plt, pivs, "연도별 Sharpe  (Holdout-A 2000~2006 + Holdout-B 2026)",
                 "heatmap_sharpe.png", "{:.1f}")

    # 2) 종합 복합점수 랭킹 막대
    if not ranking_both.empty:
        top = ranking_both.head(15).iloc[::-1]
        fig, ax = plt.subplots(figsize=(8, max(4, 0.4*len(top)+1)))
        colors = {'Expanding': '#4C72B0', 'Rolling7y': '#DD8452', 'Production': '#55A868'}
        ax.barh(top['모델'], top['종합복합점수'],
                color=[colors.get(f, '#888') for f in top['계열']])
        ax.set_xlabel("종합 복합 강건성 점수"); ax.set_title("모델 종합 랭킹 (상위 15)")
        for i, v in enumerate(top['종합복합점수']):
            ax.text(v, i, f" {v:.1f}", va='center', fontsize=8)
        fig.tight_layout(); fig.savefig(OUT_DIR / "ranking_composite.png", dpi=120); plt.close(fig)

    # 3) 안정성 산점도 (A 평균수익 vs B 수익)
    if not stability.empty and len(stability) >= 2:
        fig, ax = plt.subplots(figsize=(7, 6))
        colors = {'Expanding': '#4C72B0', 'Rolling7y': '#DD8452', 'Production': '#55A868'}
        for fam, g in stability.groupby('계열'):
            ax.scatter(g['A_평균수익(%)'], g['B_수익(%)'], label=fam,
                       color=colors.get(fam, '#888'), s=60)
        for _, r in stability.iterrows():
            ax.annotate(r['모델'], (r['A_평균수익(%)'], r['B_수익(%)']), fontsize=7,
                        xytext=(3, 3), textcoords='offset points')
        ax.axhline(0, color='gray', lw=0.7); ax.axvline(0, color='gray', lw=0.7)
        ax.set_xlabel("Holdout-A 평균수익(%)"); ax.set_ylabel("Holdout-B(2026) 수익(%)")
        ax.set_title("구간 간 안정성: 과거(A) ↔ 미래(B)"); ax.legend()
        fig.tight_layout(); fig.savefig(OUT_DIR / "stability_scatter.png", dpi=120); plt.close(fig)

    print(f"🖼  차트 저장: {OUT_DIR}")


# ══════════════════════════════════════════════════════════════════════════════
# 리포트
# ══════════════════════════════════════════════════════════════════════════════
def _to_md(df: pd.DataFrame) -> str:
    """tabulate 없이 GitHub 마크다운 테이블 생성."""
    if df is None or df.empty:
        return "_(데이터 없음)_"
    cols = [str(c) for c in df.columns]
    def fmt(v):
        if isinstance(v, float):
            return f"{v:.2f}"
        return "" if v is None else str(v)
    lines = ["| " + " | ".join(cols) + " |",
             "| " + " | ".join("---" for _ in cols) + " |"]
    for _, row in df.iterrows():
        lines.append("| " + " | ".join(fmt(row[c]) for c in df.columns) + " |")
    return "\n".join(lines)


def write_report(detail, summary, rank_part, rank_both, stability, corr, family, caches):
    L = []
    L.append("# Holdout 비교 분석 리포트\n")
    L.append(f"- 생성일: {TODAY.date()}")
    L.append(f"- 전략: BUY≥{BUY_THRESH} / SELL<{SELL_THRESH} / 종목당 {ALLOC_PER_STOCK*100:.0f}%")
    L.append(f"- Holdout-A: {HOLDOUT_A[0]}~{HOLDOUT_A[-1]} (학습 前) · Holdout-B: 2026 (OOS 後)\n")

    # 커버리지
    L.append("## 1. 데이터 커버리지")
    exp_years = len(HOLDOUT_A) + len(HOLDOUT_B)
    done = sum(len(v) for v in caches.values())
    L.append(f"- 발견된 모델: {len(caches)}개 · 캐시 파일: {done}개 (모델당 최대 {exp_years}년)")
    incomplete = [(_label_for(t), len(v)) for t, v in sorted(caches.items()) if len(v) < exp_years]
    if incomplete:
        L.append(f"- ⚠ 미완성 모델: " +
                 ", ".join(f"{m}({n}/{exp_years})" for m, n in incomplete))
    else:
        L.append("- ✅ 전 모델 전 연도 캐시 완비")
    L.append("")

    # 종합 랭킹
    L.append("## 2. 종합 강건성 랭킹 (두 구간 평균 복합점수)")
    if not rank_both.empty:
        L.append(_to_md(rank_both.head(10)))
        best = rank_both.iloc[0]
        L.append(f"\n**추천: `{best['모델']}` (종합 {best['종합복합점수']}점, 계열 {best['계열']})**")
    L.append("")

    # 구간별 Top
    L.append("## 3. 구간별 상위 모델")
    for tag in ["Holdout-A", "Holdout-B"]:
        part = rank_part[rank_part['구분'] == tag]
        if part.empty:
            continue
        L.append(f"\n### {tag}")
        L.append(_to_md(part.head(5)[["모델", "복합점수", "평균수익(%)", "평균Sharpe",
                                      "평균MDD(%)", "KOSPI승률", "일관성"]]))
    L.append("")

    # 안정성
    L.append("## 4. 구간 간 안정성 (과거 A ↔ 미래 B)")
    if corr:
        L.append(f"- 수익 순위상관(Spearman): **{corr.get('수익_Spearman','N/A')}** · "
                 f"Sharpe 순위상관: **{corr.get('Sharpe_Spearman','N/A')}**")
        L.append("  - 1에 가까울수록 '과거 잘하던 모델이 미래에도 잘함' (일반화 양호)")
    if not stability.empty:
        both_pos = stability[(stability['A_평균수익(%)'] > 0) & (stability['B_수익(%)'] > 0)]
        L.append(f"- 두 구간 모두 플러스: {len(both_pos)}/{len(stability)} 모델 — "
                 + ", ".join(both_pos['모델'].tolist()))
    L.append("")

    # 계열 비교
    L.append("## 5. 계열 비교 (Expanding vs Rolling vs Production)")
    if not family.empty:
        L.append(_to_md(family))
    L.append("")

    # 주의
    L.append("## 6. 해석 주의")
    L.append("- Holdout-A(2000~2006)는 IMF 회복·카드사태 등 한국시장 특수 레짐 → 절대수익보다 "
             "**KOSPI 초과·일관성·MDD** 위주로 볼 것.")
    L.append("- Holdout-B는 2026 연초~오늘로 표본기간이 짧아 단일 연도 노이즈 큼.")
    L.append("- 복합점수는 모델 간 상대 정규화(min-max) 결과이므로 '순위' 용도이지 절대 품질이 아님.")

    (OUT_DIR / "REPORT.md").write_text("\n".join(L), encoding="utf-8")
    print(f"📄 리포트: {OUT_DIR / 'REPORT.md'}")


# ══════════════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="Holdout 캐시 기반 모델 비교 분석")
    ap.add_argument("--refresh",   action="store_true", help="메트릭 캐시 무시하고 강제 재계산")
    ap.add_argument("--quick",     action="store_true", help="메트릭 캐시만 사용 (시뮬 스킵)")
    ap.add_argument("--no-charts", action="store_true", help="차트 생략")
    args = ap.parse_args()

    print("=" * 72)
    print("📊 Holdout 비교 분석")
    print("=" * 72)

    caches = discover_caches()
    if not caches:
        print(f"❌ 캐시 없음: {CACHE_DIR}")
        return
    exp_years = len(HOLDOUT_A) + len(HOLDOUT_B)
    print(f"발견 모델 {len(caches)}개:")
    for tag in sorted(caches):
        n = len(caches[tag])
        flag = "✅" if n >= exp_years else f"… {n}/{exp_years}"
        print(f"  {_label_for(tag):<14} {flag}")

    all_years = sorted({y for v in caches.values() for y in v})
    kospi_dict = get_kospi_by_year(all_years)

    detail = build_metrics_table(caches, kospi_dict, args.refresh, args.quick)
    if detail.empty:
        print("❌ 메트릭 없음")
        return

    summary = summarize_by_holdout(detail)
    rank_part, rank_both = composite_rank(summary)
    stability, corr = cross_holdout_stability(summary)
    family = family_comparison(summary)

    # 시그널 진단 요약 (모델×구간 평균)
    diag_cols = ["평균점수", "점수표준편차", "점수p90", "매수신호/일", "매수종목수(폭)", "관측종목수"]
    have_diag = [c for c in diag_cols if c in detail.columns]
    if have_diag:
        sig = (detail.groupby(['모델', '구분'])[have_diag].mean().round(2).reset_index())
        sig.to_csv(OUT_DIR / "signal_diagnostics.csv", index=False, encoding='utf-8-sig')

    summary.to_csv(OUT_DIR / "summary_by_holdout.csv", index=False, encoding='utf-8-sig')
    rank_part.to_csv(OUT_DIR / "ranking_by_holdout.csv", index=False, encoding='utf-8-sig')
    rank_both.to_csv(OUT_DIR / "ranking_composite.csv", index=False, encoding='utf-8-sig')
    stability.to_csv(OUT_DIR / "stability_cross.csv", index=False, encoding='utf-8-sig')
    family.to_csv(OUT_DIR / "family_comparison.csv", index=False, encoding='utf-8-sig')

    if not args.no_charts:
        try:
            make_charts(detail, summary, rank_both, stability)
        except Exception as e:
            print(f"⚠ 차트 생성 실패(분석은 정상): {e}")

    write_report(detail, summary, rank_part, rank_both, stability, corr, family, caches)

    # 콘솔 요약
    print("\n" + "=" * 72)
    print("🏆 종합 랭킹 (상위 10)")
    print("=" * 72)
    if not rank_both.empty:
        print(rank_both.head(10).to_string(index=False))
    if corr:
        print(f"\n구간 간 안정성  수익 ρ={corr.get('수익_Spearman')}  "
              f"Sharpe ρ={corr.get('Sharpe_Spearman')}")
    print(f"\n✅ 완료 — 산출물: {OUT_DIR}")


if __name__ == "__main__":
    main()
