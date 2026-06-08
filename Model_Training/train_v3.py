import os
import sys
import shutil

# ---------------------------------------------------------
# 🔥 [핵심] GPU 라이브러리 경로 강제 등록
# ---------------------------------------------------------
try:
    env_path = os.path.dirname(sys.executable)
    dll_path = os.path.join(env_path, 'Library', 'bin')
    if os.path.exists(dll_path):
        os.add_dll_directory(dll_path)
        os.environ['PATH'] = dll_path + os.pathsep + os.environ['PATH']
        print(f"✅ GPU 라이브러리 경로 등록 완료: {dll_path}")
except Exception:
    pass

os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
os.environ['TF_FORCE_GPU_ALLOW_GROWTH'] = 'true'

import gc
import json
import pickle
import random
import numpy as np
import pandas as pd
from pathlib import Path
from tqdm import tqdm

import tensorflow as tf
from tensorflow.keras.models import Sequential
from tensorflow.keras.layers import Dense, Dropout, Input, LSTM, BatchNormalization
from tensorflow.keras.optimizers import Adam
from tensorflow.keras.callbacks import EarlyStopping, ReduceLROnPlateau
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import RobustScaler
from sklearn.utils.class_weight import compute_class_weight
from sklearn.metrics import precision_recall_curve
from numpy.lib.stride_tricks import sliding_window_view

# ==========================================
# 1. 설정 및 경로 (프로젝트 구조에 맞춤)
# ==========================================
# 현재 파일 위치: C:\Projects\RealtimeMonitor\Model_Training\train_v3.py
# 상위 폴더(BASE_DIR): C:\Projects\RealtimeMonitor
BASE_DIR = Path(__file__).resolve().parent.parent


class DefaultSettings:
    # 모델이 저장될 폴더 (Model_Training/best_enhanced_v3)
    OUTPUT_DIR = Path(__file__).resolve().parent / "best_enhanced_v3"

    # 데이터가 있는 폴더 (Data)
    LEARNING_DATA_DIR = BASE_DIR / "Data"

    MODELS = {
        "LSTM": {
            "SURGE": [
                {"name": "target1", "lookback_min": 5, "lookback_max": 80},
                {"name": "target5", "lookback_min": 20, "lookback_max": 100},
                {"name": "target20", "lookback_min": 40, "lookback_max": 100},
            ],
            "DROP": [
                {"name": "drop1", "lookback_min": 5, "lookback_max": 80},
                {"name": "drop5", "lookback_min": 20, "lookback_max": 100},
                {"name": "drop20", "lookback_min": 40, "lookback_max": 100},
            ]
        }
    }


settings = DefaultSettings()

random.seed(42)
np.random.seed(42)
tf.random.set_seed(42)

try:
    import pyarrow

    CACHE_FORMAT = 'feather'
    print("✅ 'pyarrow' 라이브러리 감지됨 (고속 캐싱 사용)")
except ImportError:
    CACHE_FORMAT = 'pickle'
    print("⚠️ 'pyarrow' 미설치 -> 'pickle' 방식으로 전환합니다.")


def _get_df_cached(path: str):
    path = str(path)
    try:
        if path.endswith('.feather'):
            return pd.read_feather(path)
        else:
            return pd.read_pickle(path)
    except Exception:
        return pd.DataFrame()


# ==========================================
# 2. 기술적 지표 생성 함수 (v3 고도화)
# ==========================================
def calculate_adx_v3(df, window=14):
    try:
        high, low, close = df['high'], df['low'], df['close']
        plus_dm = high.diff()
        minus_dm = low.diff()
        plus_dm[plus_dm < 0] = 0
        minus_dm[minus_dm > 0] = 0
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window).mean()
        plus_di = 100 * (plus_dm.ewm(alpha=1 / window).mean() / atr)
        minus_di = abs(100 * (minus_dm.ewm(alpha=1 / window).mean() / atr))
        dx = (abs(plus_di - minus_di) / abs(plus_di + minus_di)) * 100
        adx = dx.rolling(window).mean()
        return adx.fillna(0) / 100.0
    except:
        return pd.Series(0, index=df.index)


def add_indicators_v3(df):
    df['ma5'] = df['close'].rolling(window=5).mean()
    df['ma20'] = df['close'].rolling(window=20).mean()
    df['disparity_5'] = (df['close'] / df['ma5'] - 1).fillna(0)
    df['disparity_20'] = (df['close'] / df['ma20'] - 1).fillna(0)

    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / (loss + 1e-9)
    df['rsi_v3'] = (100 - (100 / (1 + rs))).fillna(50) / 100.0

    std20 = df['close'].rolling(window=20).std()
    upper = df['ma20'] + (std20 * 2)
    lower = df['ma20'] - (std20 * 2)
    df['bb_w_v3'] = ((upper - lower) / df['ma20']).fillna(0)
    df['bb_p_v3'] = ((df['close'] - lower) / (upper - lower)).fillna(0.5)
    df['adx_v3'] = calculate_adx_v3(df)
    return df


# ==========================================
# 3. [공통] 전역 캐싱 함수 (폴더 구조 반영 수정)
# ==========================================
def run_global_caching(data_dir, prep_dir, feature_cols, train_start=None, train_end=None):
    # 🔥 [필터 기준]
    START_DT = pd.Timestamp(train_start) if train_start else pd.Timestamp("2021-01-04")
    END_DT   = pd.Timestamp(train_end)   if train_end   else pd.Timestamp("2025-12-31")
    REQUIRED_DAYS = 200  # 최소 데이터 개수 (완화)
    MIN_AVG_AMOUNT = 500_000_000  # 평균 거래대금 5억 이상 (완화)

    print(f"\n🔍 [Global Cache] 데이터 전처리 시작")
    stock_dir = data_dir / "Stock"
    print(f"   - 대상 폴더: {stock_dir}")
    print(f"   - 조건: {REQUIRED_DAYS}일 이상 & 거래대금 {MIN_AVG_AMOUNT // 100000000}억 이상")

    if not prep_dir.exists(): prep_dir.mkdir(parents=True)

    # Stock 폴더의 A*.csv 만 (ETF 제외)
    all_csv = list(stock_dir.glob("A*.csv"))

    print(f"   - 발견된 파일: {len(all_csv)}개")
    cached_files = []

    # 이미 캐시가 있으면 재사용
    ext = '.feather' if CACHE_FORMAT == 'feather' else '.pkl'
    existing = list(prep_dir.glob(f"*{ext}"))
    if len(existing) > len(all_csv) * 0.8:  # 80% 이상 캐시가 있으면 재사용
        print(f"📦 기존 캐시 {len(existing)}개를 재사용합니다.")
        return [str(p) for p in existing]

    reject_reasons = {"period_short": 0, "low_liquidity": 0, "error": 0, "empty": 0}

    for p in tqdm(all_csv, desc="Caching"):
        try:
            df = pd.read_csv(p)
            if df.empty or 'date' not in df.columns:
                reject_reasons["empty"] += 1
                continue

            df['date'] = pd.to_datetime(df['date'], errors='coerce')
            mask = (df['date'] >= START_DT) & (df['date'] <= END_DT)
            df_sub = df.loc[mask].copy()

            if len(df_sub) < REQUIRED_DAYS:
                reject_reasons["period_short"] += 1
                continue

            cols_numeric = ['open', 'high', 'low', 'close', 'volume', 'change_pct', 'prog_net_qty', 'kospi_change',
                            'kosdaq_change']
            for c in cols_numeric:
                if c in df_sub.columns: df_sub[c] = pd.to_numeric(df_sub[c], errors='coerce').fillna(0)

            df_sub['amount'] = df_sub['close'] * df_sub['volume']
            if df_sub['amount'].mean() < MIN_AVG_AMOUNT:
                reject_reasons["low_liquidity"] += 1
                continue

            # 지표 생성
            df_sub['volume_ratio'] = df_sub['volume'] / (df_sub['volume'].shift(1) + 1e-9)
            df_sub['vol_power'] = df_sub['change_pct'] * df_sub['volume_ratio']

            if 'prog_net_qty' in df_sub.columns:
                df_sub['prog_net_ratio'] = df_sub.apply(
                    lambda x: x['prog_net_qty'] / x['volume'] if x['volume'] > 0 else 0, axis=1)
            else:
                df_sub['prog_net_ratio'] = 0.0

            df_sub = add_indicators_v3(df_sub)
            df_sub.replace([np.inf, -np.inf], np.nan, inplace=True)
            df_sub.dropna(subset=feature_cols, inplace=True)

            if len(df_sub) > REQUIRED_DAYS - 50:
                fp = prep_dir / f"{p.stem}{ext}"
                if CACHE_FORMAT == 'feather':
                    df_sub.reset_index(drop=True).to_feather(fp)
                else:
                    df_sub.to_pickle(fp)
                cached_files.append(str(fp))
            else:
                reject_reasons["error"] += 1
        except:
            reject_reasons["error"] += 1
            continue

    print(f"✅ 캐싱 완료: {len(cached_files)}개 생성")
    return cached_files


# ==========================================
# 4. 샘플 추출 및 트레이너
# ==========================================
def extract_samples_v3(trainer_type, feather_path, target_name, lookback, feature_cols, thresholds, train_end=None):
    try:
        df = _get_df_cached(feather_path)
        if df.empty: return [], []
        if train_end is not None:
            df = df[df['date'] <= pd.Timestamp(train_end)].copy()

        for col in feature_cols:
            if col not in df.columns: df[col] = 0.0

        if 'ma20' not in df.columns: df['ma20'] = df['close'].rolling(window=20).mean()

        trend_mask = (df['close'] > df['ma20']) if trainer_type == 'SURGE' else pd.Series(True, index=df.index)
        amount_mask = (df['close'] * df['volume']) > 500_000_000  # 학습시엔 좀 더 관대하게
        prog_mask = (df['prog_net_ratio'] > -0.05) if trainer_type == 'SURGE' else pd.Series(True, index=df.index)

        valid_condition = trend_mask & amount_mask & prog_mask

        Xmat = df[feature_cols].to_numpy(dtype=np.float32)
        closes = df['close'].to_numpy(np.float32)
        conditions = valid_condition.to_numpy()

        if trainer_type == "SURGE":
            offset = {"target1": 1, "target5": 5, "target20": 20}.get(target_name, 5)
            thresh = thresholds.get(target_name, 0.05)
        else:
            offset = {"drop1": 1, "drop5": 5, "drop20": 20}.get(target_name, 5)
            thresh = thresholds.get(target_name, -0.05)

        N, F = Xmat.shape
        if N <= lookback + offset: return [], []

        win = sliding_window_view(Xmat, (lookback, F))[:, 0, :, :]
        valid_len = N - lookback - offset + 1
        base_idx = np.arange(lookback - 1, lookback - 1 + valid_len)
        base_close = closes[base_idx]

        mask = np.isfinite(base_close) & (base_close != 0) & conditions[base_idx]
        if not mask.any(): return [], []

        future = np.vstack([closes[base_idx + d] for d in range(1, offset + 1)])
        valid_mask = mask & np.isfinite(future).all(axis=0)
        if not valid_mask.any(): return [], []

        returns = (future[:, valid_mask] / base_close[valid_mask]) - 1.0

        if trainer_type == "SURGE":
            y = (np.max(returns, axis=0) >= thresh).astype(np.int8)
        else:
            y = (np.min(returns, axis=0) <= thresh).astype(np.int8)

        return [*win[:valid_len][valid_mask]], [*y]
    except:
        return [], []


class EnhancedTrainerV3:
    def __init__(self, model_spec, asof_tag, cached_files, output_dir=None, train_end=None):
        self.spec = model_spec
        self.asof_tag = asof_tag
        self.cached_files = cached_files
        self.target_name = model_spec['name']
        self.train_end = train_end

        self.model_dir = Path(output_dir) if output_dir else settings.OUTPUT_DIR
        self.model_dir.mkdir(parents=True, exist_ok=True)

        self.model_name = f"{self.target_name}_lstm_v3.h5"
        self.scaler_name = f"{self.target_name}_lstm_v3.scaler"
        self.model_path = self.model_dir / self.model_name
        self.scaler_path = self.model_dir / self.scaler_name
        self.log_path = self.model_dir / f"log_{self.target_name}_v3.csv"

        self.feature_cols = self._get_feature_cols()
        self.thresholds = self._get_thresholds()

        self.lookback_min = model_spec.get("lookback_min", 30)
        self.lookback_max = model_spec.get("lookback_max", 80)

    @property
    def trainer_type(self):
        return "BASE"

    def _get_thresholds(self):
        return {}

    def _get_feature_cols(self):
        return ['change_pct', 'volume_ratio', 'vol_power', 'prog_net_ratio', 'prog_ratio_vol',
                'disparity_5', 'disparity_20', 'rsi_v3', 'bb_p_v3', 'bb_w_v3', 'adx_v3',
                'kospi_change', 'kosdaq_change']

    def build_model(self, input_shape):
        m = Sequential([
            Input(shape=input_shape),
            LSTM(128, return_sequences=True), BatchNormalization(), Dropout(0.3),
            LSTM(64, return_sequences=False), BatchNormalization(), Dropout(0.3),
            Dense(32, activation='relu'), Dense(16, activation='relu'),
            Dense(1, activation='sigmoid')
        ])
        m.compile(optimizer=Adam(learning_rate=0.001), loss='binary_crossentropy', metrics=['accuracy'])
        return m

    def run(self):
        print(f"\n🚀 [v3] 학습 시작: {self.target_name} (LB {self.lookback_min}~{self.lookback_max})")

        best_f1, best_lb, best_thr = -1.0, None, None
        checked_lbs = set()

        if self.log_path.exists():
            try:
                log_df = pd.read_csv(self.log_path)
                best_row = log_df.loc[log_df['f1'].idxmax()]
                best_f1, best_lb, best_thr = best_row['f1'], int(best_row['lookback']), best_row['threshold']
                checked_lbs = set(log_df['lookback'].astype(int).unique())
                print(f"   📜 기존 최고 기록: LB={best_lb}, F1={best_f1:.4f}")
            except:
                pass

        search_queue = sorted(list(set(range(self.lookback_min, self.lookback_max + 1, 5)) - checked_lbs))
        for lb in search_queue:
            res = self._execute_step(lb)
            if res and res[0] > best_f1:
                best_f1, best_thr, best_lb = res[0], res[1], lb
                self._save_best(res[4], res[5], lb, res[1])
                print(f"      🎉 New Best! F1={best_f1:.4f} (LB={lb})")

        print(f"🏆 [v3] {self.target_name} 완료: LB={best_lb}, F1={best_f1:.4f}")

    def _execute_step(self, lb):
        X_all, y_all = [], []
        for fp in self.cached_files:
            Xp, yp = extract_samples_v3(self.trainer_type, fp, self.target_name, lb, self.feature_cols, self.thresholds, train_end=self.train_end)
            X_all.extend(Xp);
            y_all.extend(yp)

        if len(X_all) < 1000 or np.sum(y_all) / len(y_all) < 0.005: return None
        X, y = np.array(X_all, dtype=np.float32), np.array(y_all, dtype=np.int8)
        del X_all, y_all  # 원본 리스트 즉시 해제

        try:
            f1, thr, p, r, model, scaler = self._train_one(X, y, lb)
            pd.DataFrame([{"lookback": lb, "f1": f1, "precision": p, "recall": r, "threshold": thr}]).to_csv(
                self.log_path, mode='a', header=not self.log_path.exists(), index=False)
            return f1, thr, p, r, model, scaler
        except Exception as e:
            print(f"   ⚠️  LB={lb} 학습 실패: {type(e).__name__}: {e}")
            return None
        finally:
            del X, y  # 학습 데이터 즉시 해제
            tf.keras.backend.clear_session()
            gc.collect()
            gc.collect()  # 순환참조까지 2회 GC

    def _train_one(self, X, y, lb):
        N, T, F = X.shape
        X_tr, X_va, y_tr, y_va = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
        scaler = RobustScaler()
        X_tr_sc = scaler.fit_transform(X_tr.reshape(-1, F)).reshape(X_tr.shape)
        X_va_sc = scaler.transform(X_va.reshape(-1, F)).reshape(X_va.shape)

        model = self.build_model((T, F))
        cw = compute_class_weight('balanced', classes=np.unique(y_tr), y=y_tr)
        cw_dict = dict(zip(np.unique(y_tr), cw))
        es = EarlyStopping(monitor='val_loss', patience=7, restore_best_weights=True)
        model.fit(X_tr_sc, y_tr, validation_data=(X_va_sc, y_va), epochs=100, batch_size=4096, class_weight=cw_dict,
                  callbacks=[es], verbose=0)

        pred = model.predict(X_va_sc, batch_size=8192, verbose=0).ravel()
        P, R, Th = precision_recall_curve(y_va, pred)
        f1 = np.divide(2 * P * R, P + R + 1e-7, out=np.zeros_like(P), where=(P + R) != 0)
        idx = np.argmax(f1)
        return f1[idx], Th[idx], P[idx], R[idx], model, scaler

    def _save_best(self, model, scaler, lb, thr):
        model.save(self.model_path)
        with open(self.scaler_path, 'wb') as f: pickle.dump(scaler, f)


class EnhancedSurgeTrainerV3(EnhancedTrainerV3):
    @property
    def trainer_type(self): return "SURGE"

    def _get_thresholds(self): return {"target1": 0.03, "target5": 0.07, "target20": 0.30}


class EnhancedDropTrainerV3(EnhancedTrainerV3):
    @property
    def trainer_type(self): return "DROP"

    def _get_thresholds(self): return {"drop1": -0.01, "drop5": -0.04, "drop20": -0.10}


if __name__ == "__main__":
    gpus = tf.config.list_physical_devices('GPU')
    if gpus:
        for gpu in gpus: tf.config.experimental.set_memory_growth(gpu, True)

    print("=" * 60)
    print("🚀 [Version 3.1] 주식/ETF 통합 학습 시스템")
    print("=" * 60)

    # 설정 객체 사용
    data_dir = settings.LEARNING_DATA_DIR
    prep_dir = data_dir / "_prep_enhanced_v3"
    feature_cols = ['change_pct', 'volume_ratio', 'vol_power', 'prog_net_ratio', 'prog_ratio_vol', 'disparity_5',
                    'disparity_20', 'rsi_v3', 'bb_p_v3', 'bb_w_v3', 'adx_v3', 'kospi_change', 'kosdaq_change']

    # 1. 데이터 캐싱 (주식+ETF 통합)
    cached_files = run_global_caching(data_dir, prep_dir, feature_cols)
    if not cached_files:
        print("❌ 학습 데이터가 없습니다. Data 폴더를 확인하세요.")
        sys.exit()

    asof = "v3_unified"
    # 2. 모델 학습
    for spec in settings.MODELS["LSTM"]["SURGE"]: EnhancedSurgeTrainerV3(spec, asof, cached_files).run()
    for spec in settings.MODELS["LSTM"]["DROP"]: EnhancedDropTrainerV3(spec, asof, cached_files).run()