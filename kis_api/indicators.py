import pandas as pd
import numpy as np


def today_change_pct(rt, curr, df=None, target_date=None):
    """실시간 오늘 행의 change_pct = 현재가 / 전일종가 - 1.
    학습·과거 일봉(Update_Data_All 의 close.pct_change)과 같은 정의. (2026-09-30 수정: 기존 현재가/시가-1 은
    오늘 행만 정의가 달라 갭상승 종목 점수가 부풀려짐 — 60일 재현 비교에서 현행 방식에서만 60점+ 인 종목 D+5 -1.4%)
    전일종가 = 시세 응답의 기준가(stck_sdpr), 없으면 일봉 CSV 의 target_date 직전 종가."""
    try:
        prev = float(rt.get("stck_sdpr") or 0)
    except (TypeError, ValueError):
        prev = 0.0
    if prev <= 0 and df is not None and target_date is not None:
        try:
            past = df[pd.to_datetime(df['date']) < pd.Timestamp(target_date)]
            if not past.empty:
                prev = float(past['close'].iloc[-1])
        except Exception:
            prev = 0.0
    return (curr / prev - 1) if prev > 0 and curr > 0 else 0.0


# ====== v3용 지표 전체 계산 함수 ======
def calculate_indicators_v3_save(df):
    """
    기존 Realtime_Monitor.py의 로직을 그대로 이식함.
    """
    # 1. 이동평균 및 이격도
    df['ma5'] = df['close'].rolling(window=5).mean()
    df['ma20'] = df['close'].rolling(window=20).mean()
    df['ma60'] = df['close'].rolling(window=60).mean()
    df['disparity_5'] = (df['close'] / df['ma5'] - 1).fillna(0)
    df['disparity_20'] = (df['close'] / df['ma20'] - 1).fillna(0)
    df['disparity_60'] = (df['close'] / df['ma60'] - 1).fillna(0)

    # 2. 거래량 지표
    eps = 1e-9
    df['volume_ratio'] = df['volume'] / (df['volume'].shift(1) + eps)
    df['vol_power'] = df['change_pct'] * df['volume_ratio']

    # 3. RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / (loss + eps)
    df['rsi'] = (100 - (100 / (1 + rs))).fillna(50) / 100.0

    # 4. 볼린저 밴드 (bb_p, bb_w)
    std20 = df['close'].rolling(window=20).std()
    upper = df['ma20'] + (std20 * 2)
    lower = df['ma20'] - (std20 * 2)
    df['bb_p'] = ((df['close'] - lower) / (upper - lower + eps)).fillna(0.5)
    df['bb_w'] = ((upper - lower) / (df['ma20'] + eps)).fillna(0)

    # 5. ADX 계산
    try:
        window = 14
        plus_dm = df['high'].diff();
        minus_dm = df['low'].diff()
        plus_dm[plus_dm < 0] = 0;
        minus_dm[minus_dm > 0] = 0
        tr = pd.concat(
            [df['high'] - df['low'], abs(df['high'] - df['close'].shift(1)), abs(df['low'] - df['close'].shift(1))],
            axis=1).max(axis=1)
        atr = tr.rolling(window).mean()
        plus_di = 100 * (plus_dm.ewm(alpha=1 / window).mean() / atr)
        minus_di = abs(100 * (minus_dm.ewm(alpha=1 / window).mean() / atr))
        dx = (abs(plus_di - minus_di) / abs(plus_di + minus_di)) * 100
        df['adx'] = dx.rolling(window).mean().fillna(0) / 100.0
    except:
        df['adx'] = 0.0

    # 호환성 유지
    if 'bb_p' in df.columns:
        df['bb_pos'] = df['bb_p']

    return df


def fill_prog_net_ratio(df):
    """prog_net_ratio(프로그램 순매수/거래량) 결측 행을 채운다 — 실시간 봇이 붙인 '오늘 행' 용.
    과거 행은 Update_Data_All 이 같은 식으로 계산해 둔 값을 그대로 둔다.
    (2026-09-14 수정: 기존엔 'prog_net_ratio' 열이 이미 있으면 계산을 건너뛰어 오늘 행이 NaN→fillna(0)
     으로 항상 0 — 학습 데이터와 달리 실시간 V3 점수에서 프로그램 순매수 효과가 빠져 있었음)"""
    ratio = (df['prog_net_qty'] / df['volume'].where(df['volume'] > 0)).fillna(0)
    if 'prog_net_ratio' in df.columns:
        df['prog_net_ratio'] = df['prog_net_ratio'].fillna(ratio)
    else:
        df['prog_net_ratio'] = ratio
    return df