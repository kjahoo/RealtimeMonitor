# -*- coding: utf-8 -*-
"""
promote_evaluated.py
─────────────────────────────────────────────────────────────────────
Claude(Cowork/세션)가 60점+ 종목을 평가한 뒤 결과를
  logs/{날짜}_claude_results.json
에 저장하고 이 스크립트를 실행하면:

  • 본인 텔레그램으로 평가 요약 발송
  • 'BUY' 종목만 Search_History.csv(본인 ID)에 자동 추가
  • 평가완료 종목 done 기록 (중복 방지)

사용법:
  python promote_evaluated.py            # 오늘 날짜 자동
  python promote_evaluated.py 20260625   # 특정 날짜 지정

결과 JSON 한 건의 스키마(리스트):
  {
    "code": "005930", "name": "삼성전자",
    "close_price": 71000, "market_cap": 4230000,
    "v3_score": 0.72, "v3_score100": 72.0,
    "claude_score": 78, "grade": "매수",
    "recommendation": "BUY",            # BUY | HOLD | AVOID
    "thesis": "...", "catalysts": "...", "risks": "...", "summary": "..."
  }
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

from claude_eval_pipeline import promote_from_file


def main():
    today = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y%m%d")
    print(f"▶ 평가 결과 후처리 시작 ({today})")
    res = promote_from_file(today)
    if res is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
