# -*- coding: utf-8 -*-
"""
run_holdout_supervised.py
=========================
holdout_comparison.py 를 감시하며 자동 재시작하는 래퍼.

배경: RTX 50 시리즈에서 cuDNN LSTM 추론이 간헐적으로 하드 크래시(프로세스 사망,
Python 트레이스백 없음). holdout_comparison.py 는 연도별 캐시(scores_<tag>_<year>.pkl)
를 저장하므로, 죽어도 재시작하면 캐시된 지점부터 자동 재개된다.

전략:
  - 자식(holdout_comparison.py)을 subprocess 로 실행.
  - rc==0(정상 완료) → 종료.
  - 크래시(rc!=0) → 캐시부터 재개하며 재시작.
  - 같은 지점에서 진전 없이 반복 크래시(=결정적 cuDNN 크기 문제로 추정) →
    HOLDOUT_GPU_CHUNK 를 단계적으로 낮춰(2048→1024→512→256) 돌파 시도.
  - 가장 작은 배치로도 진전이 없으면 중단(사람 개입 필요).

실행 (반드시 conda 환경에서 — GPU 사용):
  conda run -n trading_env --no-capture-output python -u run_holdout_supervised.py
"""
import subprocess, sys, os, time
from pathlib import Path

ROOT      = Path(r"C:\Projects\RealtimeMonitor")
SCRIPT    = ROOT / "holdout_comparison.py"
CACHE_DIR = ROOT / "holdout_caches"
RESULTS   = ROOT / "holdout_comparison_results.csv"

MAX_ATTEMPTS      = 80
SLEEP_BETWEEN     = 20          # 재시작 전 대기(초) — GPU 메모리 해제 여유
# 2004년 등 일부 연도가 배치 2048에서 반복 크래시(CUDA Unexpected Event) →
# 1024 가 안정적으로 통과 확인됨. 처음부터 1024 로 시작해 불필요한 크래시 최소화.
CHUNK_LADDER      = [1024, 512, 256, 128]
NOPROG_TO_ESCALATE = 2          # 진전 없는 크래시 N회 → 배치 한 단계 축소


def cache_count() -> int:
    return len(list(CACHE_DIR.glob("scores_*_*.pkl")))


def main():
    print(f"[supervisor] 시작  현재 캐시 {cache_count()}개", flush=True)
    chunk_idx   = 0
    no_progress = 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        env = os.environ.copy()
        chunk = CHUNK_LADDER[chunk_idx]
        if chunk is not None:
            env["HOLDOUT_GPU_CHUNK"] = str(chunk)
        chunk_label = chunk if chunk is not None else "기본(2048)"

        before = cache_count()
        print(f"\n[supervisor] === 시도 {attempt}/{MAX_ATTEMPTS}  "
              f"캐시 {before}/208  GPU_CHUNK={chunk_label} ===", flush=True)

        rc = subprocess.run([sys.executable, "-u", str(SCRIPT)], env=env).returncode
        after = cache_count()

        print(f"[supervisor] 시도 {attempt} 종료 rc={rc}  캐시 {before}→{after}", flush=True)

        if rc == 0:
            print("[supervisor] ✅ holdout_comparison 정상 완료.", flush=True)
            break

        if after > before:
            no_progress = 0            # 진전 있었음 → 카운터 리셋
        else:
            no_progress += 1
            print(f"[supervisor] ⚠ 진전 없는 크래시 {no_progress}회", flush=True)
            if no_progress >= NOPROG_TO_ESCALATE:
                if chunk_idx < len(CHUNK_LADDER) - 1:
                    chunk_idx += 1
                    no_progress = 0
                    print(f"[supervisor] ↓ GPU_CHUNK 축소 → {CHUNK_LADDER[chunk_idx]}",
                          flush=True)
                else:
                    print("[supervisor] ❌ 최소 배치로도 진전 없음 → 중단(사람 개입 필요)",
                          flush=True)
                    break

        print(f"[supervisor] {SLEEP_BETWEEN}초 후 재시작(캐시부터 재개)...", flush=True)
        time.sleep(SLEEP_BETWEEN)
    else:
        print(f"[supervisor] ❌ 최대 시도({MAX_ATTEMPTS}) 도달, 중단.", flush=True)

    done = RESULTS.exists()
    print(f"[supervisor] 종료. 최종 캐시 {cache_count()}/208  결과CSV={'있음' if done else '없음'}",
          flush=True)


if __name__ == "__main__":
    main()
