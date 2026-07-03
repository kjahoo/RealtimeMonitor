# -*- coding: utf-8 -*-
"""
run_retrain_supervised.py
=========================
walk_forward.py (Rolling 7y, 필터링 적용) 재학습을 감시하며 자동 재시작.

- walk_forward.py 는 폴드 단위로 idempotent: 6개 모델이 다 있는 폴드는 학습 skip.
  → 크래시/재부팅 후 같은 명령을 재실행하면 완료 폴드를 건너뛰고 이어서 학습.
- prep 캐시는 기존 것 재사용(--rebuild-prep 미사용).
- excluded_stocks.json 자동 적용(walk_forward.py 기본값).
- 24/7 운용: 장중 GPU 경쟁으로 크래시해도 계속 재시작(완료 폴드 누적).

실행(작업 스케줄러에서 conda/GPU 로):
  conda run -n trading_env --no-capture-output python -u run_retrain_supervised.py
"""
import subprocess, sys, time
from pathlib import Path

ROOT      = Path(r"C:\Projects\RealtimeMonitor")
SCRIPT    = ROOT / "walk_forward.py"
WF_OUT    = ROOT / "walk_forward_rolling7y"
MODELS    = ["target1", "target5", "target20", "drop1", "drop5", "drop20"]

CMD = [sys.executable, "-u", str(SCRIPT),
       "--data-start", "20070101", "--data-end", "20260101", "--rolling", "7"]

MAX_ATTEMPTS   = 150
SLEEP_BETWEEN  = 30            # 재시작 전 대기(초)
NOPROG_ABORT   = 12           # 진전 없는 크래시 N회 연속 → 중단(장중 경쟁 고려해 넉넉히)


def completed_folds() -> int:
    """6개 모델 .h5 가 모두 있는 fold 수."""
    if not WF_OUT.exists():
        return 0
    n = 0
    for fd in WF_OUT.glob("fold_*"):
        mdir = fd / "models"
        if all((mdir / f"{m}_lstm_v3.h5").exists() for m in MODELS):
            n += 1
    return n


def main():
    print(f"[retrain] 시작  완료 폴드 {completed_folds()}개", flush=True)
    no_progress = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        before = completed_folds()
        print(f"\n[retrain] === 시도 {attempt}/{MAX_ATTEMPTS}  완료폴드 {before} ===", flush=True)
        rc = subprocess.run(CMD).returncode
        after = completed_folds()
        print(f"[retrain] 시도 {attempt} 종료 rc={rc}  완료폴드 {before}→{after}", flush=True)

        if rc == 0:
            print("[retrain] ✅ walk_forward 정상 완료.", flush=True)
            break

        if after > before:
            no_progress = 0
        else:
            no_progress += 1
            print(f"[retrain] ⚠ 진전 없는 크래시 {no_progress}/{NOPROG_ABORT}", flush=True)
            if no_progress >= NOPROG_ABORT:
                print("[retrain] ❌ 진전 없는 크래시 누적 → 중단(사람 개입 필요)", flush=True)
                break
        print(f"[retrain] {SLEEP_BETWEEN}초 후 재시작(완료 폴드 skip 재개)...", flush=True)
        time.sleep(SLEEP_BETWEEN)
    else:
        print(f"[retrain] ❌ 최대 시도({MAX_ATTEMPTS}) 도달, 중단.", flush=True)

    print(f"[retrain] 종료. 최종 완료 폴드 {completed_folds()}개", flush=True)


if __name__ == "__main__":
    main()
