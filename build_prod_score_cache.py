"""
build_prod_score_cache.py
=========================
프로덕션 모델(Model/)로 전 종목 스코어 캐시를 재생성.
excluded_stocks.json 에 등재된 종목은 건너뜀.

출력:
  cache_prod_full_2006_2026_scores.pkl   (date, code, score)

사용:
  python -X utf8 build_prod_score_cache.py
  python -X utf8 build_prod_score_cache.py --start 20070101 --end 20260101
  python -X utf8 build_prod_score_cache.py --exclude-file excluded_stocks.json
  python -X utf8 build_prod_score_cache.py --sample 100   # 빠른 테스트
"""

import os, sys, argparse, pickle, warnings, time, json
import numpy as np
import pandas as pd
from pathlib import Path

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
warnings.filterwarnings("ignore")

import tensorflow as tf
from tensorflow.keras.models import load_model


def _setup_gpu():
    gpus = tf.config.list_physical_devices('GPU')
    if not gpus:
        return False, 1024
    try:
        for gpu in gpus:
            tf.config.experimental.set_memory_growth(gpu, True)
        return True, 4096
    except Exception as e:
        if "cannot be modified after being initialized" in str(e):
            return True, 4096
        return False, 1024


_USE_GPU, _BATCH_SZ = _setup_gpu()
if _USE_GPU:
    print(f"   GPU 사용  배치: {_BATCH_SZ}")
else:
    print(f"   CPU 모드  배치: {_BATCH_SZ}")

ROOT      = Path(r"C:\Projects\RealtimeMonitor")
DATA_DIR  = ROOT / "Data" / "Stock"
MODEL_DIR = ROOT / "Model"
OUT_FILE  = ROOT / "cache_prod_full_2006_2026_scores.pkl"

sys.path.insert(0, str(ROOT))

# 프로덕션 모델 피처 (rsi, bb_p, bb_w, adx — _v3 없음)
V3_FEATURES = [
    'change_pct', 'volume_ratio', 'vol_power',
    'prog_net_ratio', 'prog_ratio_vol',
    'disparity_5', 'disparity_20',
    'rsi', 'bb_p', 'bb_w', 'adx',
    'kospi_change', 'kosdaq_change',
]

MODEL_SETTINGS = {
    "target1":  {"lb": 65, "thr": 0.5256, "weight": 0.1775, "type": "surge"},
    "target5":  {"lb": 55, "thr": 0.6484, "weight": 0.3639, "type": "surge"},
    "target20": {"lb": 95, "thr": 0.9197, "weight": 0.4586, "type": "surge"},
    "drop1":    {"lb": 80, "thr": 0.4018, "weight": 0.2544, "type": "drop"},
    "drop5":    {"lb": 85, "thr": 0.5041, "weight": 0.3376, "type": "drop"},
    "drop20":   {"lb": 85, "thr": 0.5723, "weight": 0.4079, "type": "drop"},
}


# ══════════════════════════════════════════════════════════════════════════════
# 제외 종목 로드
# ══════════════════════════════════════════════════════════════════════════════

def load_excluded_codes(path: Path) -> set:
    if not path.exists():
        print(f"   제외 목록 없음: {path}")
        return set()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    codes = set(data.get("excluded", {}).keys())
    print(f"   제외 종목: {len(codes)}개 ({path.name})")
    return codes


# ══════════════════════════════════════════════════════════════════════════════
# 모델 로드
# ══════════════════════════════════════════════════════════════════════════════

def load_models():
    models  = {}
    best_f1 = {}
    print("모델 로딩...")
    for m_name, cfg in MODEL_SETTINGS.items():
        h5  = MODEL_DIR / f"{m_name}_lstm_v3.h5"
        scl = MODEL_DIR / f"{m_name}_lstm_v3.scaler"
        log = MODEL_DIR / f"log_{m_name}_v3_v3_unified.csv"
        if not (h5.exists() and scl.exists()):
            print(f"   없음: {m_name}")
            continue
        with open(scl, 'rb') as f:
            scaler = pickle.load(f)
        model     = load_model(str(h5), compile=False)
        actual_lb = model.input_shape[1]

        if log.exists():
            log_df          = pd.read_csv(log)
            best_row        = log_df.loc[log_df['f1'].idxmax()]
            threshold       = float(best_row['threshold'])
            best_f1[m_name] = float(best_row['f1'])
        else:
            threshold = cfg["thr"]

        models[m_name] = {
            "model":     model,
            "scaler":    scaler,
            "lookback":  actual_lb,
            "threshold": threshold,
            "weight":    cfg["weight"],
            "type":      cfg["type"],
        }
        print(f"   {m_name:<10} lb={actual_lb}  thr={threshold:.4f}")

    # F1 비례 가중치
    for group in ("surge", "drop"):
        names = [n for n, c in MODEL_SETTINGS.items() if c["type"] == group and n in models]
        f1s   = [best_f1.get(n) for n in names]
        if all(f is not None for f in f1s):
            total = sum(f1s)
            for n, f in zip(names, f1s):
                models[n]["weight"] = f / total

    return models


# ══════════════════════════════════════════════════════════════════════════════
# 단일 종목 스코어 계산
# ══════════════════════════════════════════════════════════════════════════════

def _compute_stock_scores(feat_data: np.ndarray, models: dict) -> dict:
    n      = len(feat_data)
    n_feat = feat_data.shape[1]
    day_probs = {}

    for m_name, info in models.items():
        lb = info["lookback"]
        if n < lb:
            continue
        n_win = n - lb + 1

        idx  = np.arange(n_win)[:, None] + np.arange(lb)[None, :]
        wins = feat_data[idx].astype(np.float32)
        flat = info["scaler"].transform(wins.reshape(-1, n_feat)).astype(np.float32)
        wins = tf.constant(flat.reshape(n_win, lb, n_feat))

        preds = np.concatenate([
            info["model"](wins[s: s + _BATCH_SZ], training=False).numpy().flatten()
            for s in range(0, n_win, _BATCH_SZ)
        ])

        for i, prob in enumerate(preds):
            day_probs.setdefault(lb - 1 + i, {})[m_name] = float(prob)

        del wins, flat, preds

    result = {}
    for di, probs in day_probs.items():
        s, d = 0.0, 0.0
        for m_name, prob in probs.items():
            info = models[m_name]
            if prob > info["threshold"]:
                if info["type"] == "surge": s += prob * info["weight"]
                else:                       d += prob * info["weight"]
        result[di] = round(s - d, 4)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="프로덕션 스코어 캐시 재생성")
    parser.add_argument("--start",        default="20070101",
                        help="캐시 저장 시작일 (기본: 20070101)")
    parser.add_argument("--end",          default=None,
                        help="캐시 저장 종료일 (기본: 없음 = 전체)")
    parser.add_argument("--exclude-file", default=str(ROOT / "excluded_stocks.json"),
                        help="제외 종목 JSON 경로")
    parser.add_argument("--out",          default=str(OUT_FILE),
                        help="출력 pkl 경로")
    parser.add_argument("--sample",       type=int, default=None,
                        help="N개 종목만 처리 (테스트용)")
    args = parser.parse_args()

    excluded = load_excluded_codes(Path(args.exclude_file))
    models   = load_models()
    if not models:
        print("모델 로드 실패.")
        sys.exit(1)

    csv_files = sorted(DATA_DIR.glob("A*.csv"))
    csv_files = [f for f in csv_files if f.stem[1:] not in excluded]
    if args.sample:
        csv_files = csv_files[:args.sample]
        print(f"샘플 모드: {args.sample}개 종목")

    start_ts = pd.Timestamp(args.start)
    end_ts   = pd.Timestamp(args.end) if args.end else None
    end_label = f" ~ {end_ts.date()}" if end_ts else ""

    print(f"\n{'='*64}")
    print(f"스코어 캐시 재생성")
    print(f"  대상 종목   : {len(csv_files)}개 (제외 {len(excluded)}개)")
    print(f"  저장 기간   : {start_ts.date()}{end_label}")
    print(f"  출력 파일   : {args.out}")
    print(f"{'='*64}")

    records = []
    t0      = time.time()

    for i, fpath in enumerate(csv_files):
        code = fpath.stem[1:]
        try:
            df = pd.read_csv(fpath, encoding='utf-8-sig', usecols=['date'] + V3_FEATURES)
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date').reset_index(drop=True)
            df[V3_FEATURES] = df[V3_FEATURES].fillna(0)

            scores = _compute_stock_scores(df[V3_FEATURES].values, models)

            for di, score in scores.items():
                date = df['date'].iloc[di]
                if date < start_ts:
                    continue
                if end_ts and date >= end_ts:
                    continue
                records.append({"date": date, "code": code, "score": score})

        except Exception:
            pass

        if (i + 1) % 100 == 0 or i == len(csv_files) - 1:
            elapsed = time.time() - t0
            rate    = (i + 1) / elapsed
            remain  = (len(csv_files) - i - 1) / rate if rate > 0 else 0
            print(f"   [{i+1:4d}/{len(csv_files)}]  "
                  f"{elapsed/60:.1f}분 경과  남은 {remain/60:.1f}분  "
                  f"레코드 {len(records):,}")

    print(f"\n저장 중... ({len(records):,} rows)")
    cache_df = pd.DataFrame(records)
    cache_df['date'] = pd.to_datetime(cache_df['date'])
    cache_df.to_pickle(args.out)
    print(f"완료 → {args.out}")
    print(f"  날짜 범위: {cache_df['date'].min().date()} ~ {cache_df['date'].max().date()}")
    print(f"  종목 수  : {cache_df['code'].nunique():,}")
    print(f"  레코드   : {len(cache_df):,}")


if __name__ == "__main__":
    main()