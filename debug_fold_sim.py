"""
debug_fold_sim.py
=================
특정 폴드의 시뮬레이션을 상세 진단.
  - 일별 자산 추이
  - 종목별 수익 기여도 TOP 20
  - 비정상적 단일 거래 탐지 (하루에 +100% 이상)

사용:
  python -X utf8 debug_fold_sim.py
  python -X utf8 debug_fold_sim.py --method Expanding --fold 11
  python -X utf8 debug_fold_sim.py --method Rolling7y --fold 3
"""

import argparse, pickle, warnings, sys, gc
import numpy as np
import pandas as pd
from pathlib import Path

warnings.filterwarnings("ignore")

import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

# select_best_model의 함수들 재사용
sys.path.insert(0, str(Path(r"C:\Projects\RealtimeMonitor")))
import select_best_model as sbm

ROOT     = Path(r"C:\Projects\RealtimeMonitor")
DATA_DIR = ROOT / "Data" / "Stock"
PREP_DIR = ROOT / "Data" / "_prep_wf_v3"


def run_simulation_verbose(score_df: pd.DataFrame,
                           h_start: pd.Timestamp, h_end: pd.Timestamp,
                           price_pivot: pd.DataFrame):
    """상세 거래 기록 포함 시뮬레이션."""
    scores_by_date = (
        score_df.groupby('date')
        .apply(lambda g: dict(zip(g['code'], g['score'])))
        .to_dict()
    )
    mask         = (price_pivot.index >= h_start) & (price_pivot.index < h_end)
    trading_days = price_pivot.loc[mask].index.tolist()
    portfolio    = sbm.Portfolio()
    history      = []
    trade_log    = []

    prev_total = sbm.INITIAL_CAPITAL
    for date in trading_days:
        prices_today = price_pivot.loc[date].dropna().to_dict()
        today_scores = scores_by_date.get(date, {})
        portfolio.update_prices(prices_today)
        total = portfolio.total_assets(prices_today)

        # 매도
        for code in list(portfolio.positions.keys()):
            score = today_scores.get(code)
            price = prices_today.get(code)
            if score is None or not price or price <= 0:
                continue
            sell_target = sbm._sell_target_ratio(score)
            if sell_target is None:
                continue
            if sell_target == 0.0:
                portfolio.sell_all(code, price, date)
            else:
                target_amount = sell_target * total
                cur_mv        = portfolio.market_value(code, prices_today)
                if cur_mv > target_amount:
                    sell_qty = int((cur_mv - target_amount) / price)
                    if sell_qty > 0:
                        portfolio.sell(code, sell_qty, price, date, alloc_ratio=sell_target)

        # 매수
        total      = portfolio.total_assets(prices_today)
        candidates = []
        for code, score in today_scores.items():
            price = prices_today.get(code)
            if not price or price <= 0:
                continue
            target_ratio = sbm._buy_target_ratio(score)
            if target_ratio is None:
                continue
            cur_mv    = portfolio.market_value(code, prices_today)
            cur_alloc = cur_mv / total if total > 0 else 0.0
            if target_ratio > cur_alloc + 0.01:
                incr_ratio = target_ratio - cur_alloc
                candidates.append((code, score, target_ratio, incr_ratio))

        for code, score, target_ratio, incr_ratio in sorted(candidates, key=lambda x: x[1], reverse=True):
            buy_amount = incr_ratio * total
            price      = prices_today[code]
            if portfolio.cash < buy_amount:
                continue
            portfolio.buy(code, buy_amount, price, date, alloc_ratio=target_ratio)

        total_after = portfolio.total_assets(prices_today)
        daily_ret   = (total_after / prev_total - 1) * 100 if prev_total > 0 else 0.0

        # 하루 +50% 이상이면 이상 거래 기록
        if daily_ret > 50:
            snap = {code: {"qty": pos["qty"],
                           "price": prices_today.get(code, pos["avg_price"]),
                           "avg": pos["avg_price"]}
                    for code, pos in portfolio.positions.items()}
            trade_log.append({"date": date, "daily_ret": daily_ret,
                               "total": total_after, "positions": snap})

        history.append({
            "date":         date,
            "total_assets": total_after,
            "n_positions":  len(portfolio.positions),
            "daily_ret":    daily_ret,
        })
        prev_total = total_after

    return pd.DataFrame(history), trade_log, portfolio._trade_log


def analyze_trades(trade_records: list, price_pivot: pd.DataFrame,
                   h_start: pd.Timestamp, h_end: pd.Timestamp):
    """종목별 수익 기여 분석."""
    if not trade_records:
        return pd.DataFrame()

    df = pd.DataFrame(trade_records)
    df['date'] = pd.to_datetime(df['date'])

    buys  = df[df['action'] == 'BUY'].copy()
    sells = df[df['action'] == 'SELL'].copy()

    # 종목별 총 매수 금액, 총 매도 금액
    buy_sum  = buys.groupby('code').apply(lambda g: (g['qty'] * g['price']).sum()).rename('buy_cost')
    sell_sum = sells.groupby('code').apply(lambda g: (g['qty'] * g['price']).sum()).rename('sell_proc')

    summary = pd.concat([buy_sum, sell_sum], axis=1).fillna(0)

    # 종료일 잔여 포지션은 마지막 가격으로 청산 가정
    last_prices = price_pivot.iloc[-1].dropna().to_dict()
    # (실제 잔여 포지션 계산 생략 — 마지막 날 가격으로 추정)

    summary['profit'] = summary['sell_proc'] - summary['buy_cost']
    summary['n_buys'] = buys.groupby('code').size()
    summary['n_sells'] = sells.groupby('code').size()
    return summary.sort_values('profit', ascending=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--method', default='Expanding', choices=['Expanding', 'Rolling7y'])
    parser.add_argument('--fold',   type=int, default=11)
    parser.add_argument('--start',  default='2022-01-01')
    parser.add_argument('--end',    default='2022-12-31')
    args = parser.parse_args()

    h_start = pd.Timestamp(args.start)
    h_end   = pd.Timestamp(args.end)

    wf_root  = ROOT / ("walk_forward" if args.method == "Expanding" else "walk_forward_rolling7y")
    fold_dir = wf_root / f"fold_{args.fold:02d}"

    print(f"\n{'='*65}")
    print(f"🔍 진단: {args.method} fold_{args.fold:02d}  |  {args.start} ~ {args.end}")
    print(f"{'='*65}")

    # ── 모델 로드 ──────────────────────────────────────────────────────────────
    print("\n[1] 모델 로딩...")
    models = sbm.load_fold_models(fold_dir)

    # ── 스코어 계산 ────────────────────────────────────────────────────────────
    cache_tag  = f"{h_start.strftime('%Y%m%d')}_{h_end.strftime('%Y%m%d')}"
    cache_path = fold_dir / f"holdout_{cache_tag}_scores.pkl"
    print(f"\n[2] 스코어 계산...")
    score_df = sbm.compute_scores(models, h_start, h_end, cache_path, no_cache=False)
    del models; gc.collect()

    if score_df.empty:
        print("❌ 스코어 없음")
        return

    print(f"   스코어 레코드: {len(score_df):,}건")
    print(f"   점수 분포: min={score_df['score'].min():.3f}  "
          f"mean={score_df['score'].mean():.3f}  max={score_df['score'].max():.3f}")
    print(f"   매수 트리거(≥0.5) 비율: "
          f"{(score_df['score'] >= 0.5).mean()*100:.1f}%")

    # ── 가격 데이터 ────────────────────────────────────────────────────────────
    print(f"\n[3] 가격 데이터 로딩...")
    price_pivot = sbm.load_prices(h_start, h_end)
    print(f"   종목 수: {price_pivot.shape[1]:,}  거래일: {price_pivot.shape[0]}")

    # ── 시뮬레이션 ─────────────────────────────────────────────────────────────
    print(f"\n[4] 시뮬레이션 실행...")
    history_df, anomalies, trade_records = run_simulation_verbose(
        score_df, h_start, h_end, price_pivot)

    # ── 결과 출력 ──────────────────────────────────────────────────────────────
    ta = history_df.set_index('date')['total_assets']
    total_ret = (ta.iloc[-1] / ta.iloc[0] - 1) * 100

    print(f"\n{'─'*65}")
    print(f"📊 시뮬레이션 결과")
    print(f"   초기 자산: {sbm.INITIAL_CAPITAL/1e8:.0f}억")
    print(f"   최종 자산: {ta.iloc[-1]/1e8:.1f}억")
    print(f"   누적 수익: {total_ret:+.2f}%")
    print(f"   Sharpe   : {sbm.analyze_history(history_df)['Sharpe']:.3f}")

    # 월별 수익
    print(f"\n{'─'*65}")
    print(f"📅 월별 수익률")
    monthly = history_df.set_index('date')['total_assets'].resample('ME').last()
    monthly_ret = monthly.pct_change().dropna() * 100
    for m, r in monthly_ret.items():
        bar = '█' * int(abs(r) / 5) if abs(r) < 200 else '█' * 40
        sign = '+' if r >= 0 else ''
        print(f"   {m.strftime('%Y-%m')}  {sign}{r:7.2f}%  {bar}")

    # 일별 수익 TOP 10
    print(f"\n{'─'*65}")
    print(f"📈 일별 수익률 TOP 10 (이상 여부 확인)")
    top_days = history_df.nlargest(10, 'daily_ret')[['date', 'daily_ret', 'total_assets', 'n_positions']]
    for _, row in top_days.iterrows():
        print(f"   {row['date'].date()}  +{row['daily_ret']:7.2f}%  "
              f"총자산 {row['total_assets']/1e8:8.1f}억  포지션 {int(row['n_positions'])}개")

    # 가격 데이터 없는 포지션이 있었던 날 탐지
    print(f"\n{'─'*65}")
    print(f"⚠️  +50% 이상 단일일 이상 거래 탐지: {len(anomalies)}건")
    for a in anomalies[:5]:
        print(f"   {a['date'].date()}  +{a['daily_ret']:.1f}%  "
              f"총자산 {a['total']:.0f}원  포지션 {len(a['positions'])}개")
        for code, info in sorted(a['positions'].items(),
                                  key=lambda x: x[1]['qty'] * x[1]['price'], reverse=True)[:5]:
            mv = info['qty'] * info['price']
            gain_pct = (info['price'] / info['avg'] - 1) * 100 if info['avg'] > 0 else 0
            print(f"      {code}: {info['qty']:,}주 × {info['price']:,.0f}원 = {mv/1e6:.1f}백만  "
                  f"평단 {info['avg']:,.0f}원 ({gain_pct:+.1f}%)")

    # 종목별 수익 기여 TOP 20
    print(f"\n{'─'*65}")
    print(f"💰 종목별 수익 기여 TOP 20 (매수원가 기준)")
    contrib = analyze_trades(trade_records, price_pivot, h_start, h_end)
    if not contrib.empty:
        print(f"   {'종목':>8}  {'이익(만원)':>12}  {'매수총액(만원)':>14}  {'매수횟수':>6}  {'매도횟수':>6}")
        for code, row in contrib.head(20).iterrows():
            print(f"   {code:>8}  {row['profit']/1e4:>12,.0f}  {row['buy_cost']/1e4:>14,.0f}  "
                  f"{int(row.get('n_buys',0)):>6}  {int(row.get('n_sells',0)):>6}")

    # 가격 이상 종목 탐지 (단일 종목이 하루에 2배 이상)
    print(f"\n{'─'*65}")
    print(f"🔎 가격 이상 탐지 (전일 대비 +100% 이상 상승한 종목/날짜)")
    mask = (price_pivot.index >= h_start) & (price_pivot.index < h_end)
    sub  = price_pivot.loc[mask]
    daily_chg = sub.pct_change()
    extreme = (daily_chg > 1.0)   # 100% 이상 상승
    n_extreme = extreme.sum().sum()
    print(f"   해당 종목-날짜 수: {n_extreme}")
    if n_extreme > 0 and n_extreme <= 50:
        stacked = daily_chg[extreme].stack().reset_index()
        stacked.columns = ['date', 'code', 'change']
        stacked = stacked.sort_values('change', ascending=False)
        for _, row in stacked.head(10).iterrows():
            prev_p = sub.loc[:row['date']].iloc[-2][row['code']] if len(sub.loc[:row['date']]) > 1 else 0
            cur_p  = sub.loc[row['date'], row['code']]
            print(f"   {row['date'].date()}  {row['code']}  {prev_p:,.0f} → {cur_p:,.0f}원  ({row['change']*100:+.1f}%)")


if __name__ == "__main__":
    main()