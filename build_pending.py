# -*- coding: utf-8 -*-
"""
build_pending.py
오늘자 logs/{날짜}_Stock_V3.csv 에서 60점+ 신규(평가완료 제외) 종목으로
pending 큐를 만든다. 대기 종목 수를 출력한다 (0이면 분석할 신규 없음).

사용법:
  python build_pending.py            # 오늘 날짜
  python build_pending.py 20260625   # 특정 날짜
"""

import os
import sys
from datetime import datetime

# 콘솔 코드페이지(cp949 등)와 무관하게 이모지 출력이 죽지 않도록 UTF-8 강제
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 어느 작업 디렉터리에서 실행되어도 import 되도록 스크립트 폴더를 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from claude_eval_pipeline import build_pending_from_v3
from kis_api import kiwoom_trading as kt


def main():
    d = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y%m%d")
    if not kt.is_trading_day():
        print("📴 휴장일(주말/공휴일) — build_pending 스킵(pending/brief 미기록)")
        return
    print(build_pending_from_v3(d))


if __name__ == "__main__":
    main()
