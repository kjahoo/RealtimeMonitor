# 60점+ 종목 Claude 평가 워크플로우

60점(score_total ≥ 0.60) 이상 종목을 **Anthropic API 없이** 구독 Claude로 평가한다.
분석 관점 = **단기(1~20거래일) 트레이딩**, 원본 '트레이딩 종목 분석' **13항목 핵심압축 + 100점 투자매력도**.
main_stock 과는 분리(매 사이클 훅 없음).

## 자동 실행 메커니즘 (현재 = 앱 스케줄러, 2026-06-29 이관)
**Claude 앱(Desktop) 스케줄러** 2개 작업이 평일 08:00~16:00 KST 30분마다 실행(앱 떠 있으면 무인,
세션 창 불필요, fresh 실행이라 컨텍스트 누적 없음):
- `stock-60-ai-eval-day`  cron `0,30 8-15 * * 1-5` (08:00~15:30)
- `stock-60-ai-eval-1600` cron `0 16 * * 1-5` (16:00 마지막)
정의: `~/.claude/scheduled-tasks/<id>/SKILL.md`. 앱 사이드바 "Scheduled(예정됨)"에서 관리.
> 과거 이 세션 /loop(CronCreate) 방식은 컨텍스트 누적·7일 만료·창 유지 필요로 은퇴(2026-06-29).
> 단독 `claude -p`(Windows 작업 스케줄러)는 구독 인증이 앱에 묶여 401 실패 → 앱 스케줄러만 가능.
> 둘(앱·세션)을 동시에 켜면 이중 발화하므로 하나만 가동.

매 실행 4단계(build_pending → 평가 → promote → auto_buy)를 수행한다.

## 매 사이클 3단계
1. **pending 생성**: `python build_pending.py`
   → 오늘 `logs/{날짜}_Stock_V3.csv`의 60점+ 신규(평가완료 done 제외)를
     `logs/{날짜}_claude_pending.csv` 로 기록 (대기 수 출력, 0이면 종료·발송금지)
2. **종목별 분석**: pending 각 행을, **`ai_eval_prompt.txt` 지침 그대로** 13항목(핵심압축)으로
   web_search(최근 2026 자료) 평가하고 100점 투자매력도 산정 → `logs/{날짜}_claude_results.json` 저장.
   과열·투자경고·적자테마는 추격 회피.
3. **후처리**: `python promote_evaluated.py`
   → 종목별 13항목 **풀리포트** `logs/{날짜}_{코드}_{종목명}.md` 생성
   + 본인 텔레그램 **요약**(점수·종합판단·요약·리포트경로) 발송
   + 매수(BUY)만 promising(Search_History, 본인 ID) 추가
   + 통합리포트 `logs/{날짜}_AI리포트.md` 갱신 + done 기록

## 결과 JSON 스키마 (logs/{날짜}_claude_results.json — 리스트)
```json
[
  {
    "code": "083450", "name": "GST",
    "close_price": 49750, "market_cap": 9169,
    "v3_score": 0.6117, "v3_score100": 61.2,
    "claude_score": 72, "grade": "매수",
    "recommendation": "BUY",
    "summary": "한 줄 요약",
    "analysis": {
      "business": "1 주요 사업·제품별 매출비중",
      "customers": "2 주요 고객사·비중",
      "financials": "3 재무(작년~최근 분기 비교)",
      "growth": "4 성장성",
      "competition": "5 경쟁사 대비 장단점",
      "valuation": "6 밸류에이션·peer 비교",
      "invest_points": "7 최신 투자포인트·뉴스",
      "technical": "8 기술적(차트) 분석",
      "gossip": "9 gossip·웹 의견",
      "dilution": "10 메자닌 등 희석요인",
      "risks": "11 리스크",
      "verdict": "12 종합 판단",
      "extra": "13 이외 도움 사항"
    }
  }
]
```
- `recommendation`: BUY | HOLD | AVOID. `claude_score` 0~100(투자매력도). `grade` 한글 라벨(강력매수/매수/관망/회피).
- `code/name/close_price/market_cap/v3_score/v3_score100`은 pending CSV 값 그대로.
- `analysis` 각 값은 한국어 1~2문장 압축. (canonical 지침: `ai_eval_prompt.txt`)

## 자동매수 (auto_buy.py, 키움 실거래)
매 사이클 마지막에 `python auto_buy.py` 실행(항상). `secrets.AUTO_BUY_ENABLED`로 ON/OFF(현재 ON).
- **A) 체결현황 보고**: 추적 중인 미체결 매수주문을 점검해 체결/부분체결 시 본인 텔레그램 보고.
- **B) 신규 매수**(이번 사이클 새 결과 + 정규장 09:00~15:30일 때):
  - 대상: 결과 중 `recommendation==BUY` & `v3_score100(Total score)≥60`
  - 정렬: `claude_score`(AI점수) 높은 순
  - 비중: `alloc% = min(25, 5 + (TotalScore100−60)×0.5)` (2점당 1%, 60=5%·100=25%)
  - 기준금액: **전일 총자산(추정예탁자산) 스냅샷**(당일 고정, `{날짜}_autobuy_base.json`)
  - 종목당 목표 = 기준금액×alloc%, 단 **주문가능현금(ord_alow_amt, 미수 제외)** 한도 내
  - 매수가: 산출가(close_price) **지정가**. 이미 보유 종목은 패스. 현금 소진 시 가능분만 매수 후 중단.
  - 미체결은 그대로 두고 다음 사이클에 체결현황만 보고.
- 상태파일: `{날짜}_autobuy_base.json`(기준금액)·`_autobuy_orders.json`(발주추적)·`_autobuy_state.json`(중복방지 워터마크).
- 키움 함수: `place_buy_order`(kt10000)·`fetch_total_assets`(kt00018)·`fetch_order_cash`(kt00001)·`fetch_open_buy_orders`(ka10075 매수).
- 안전: 최초 활성화 시 기존 결과는 매수 제외(워터마크 초기화), 장외 결과는 추격 안 함, 종목 1주 미만이면 패스.

## 핵심 규칙
- **텔레그램은 본인(`TELEGRAM_CHAT_ID`)에게만** (친구 알림 아님). 13항목 전문은 풀리포트 파일에, 텔레그램엔 요약만.
- **promising 추가는 BUY 판정만.**
- done 기록으로 같은 종목 하루 1회.
- 경로/스키마/렌더링은 `claude_eval_pipeline.py`, 분석지침은 `ai_eval_prompt.txt` 에 정의.
- 무인 권한: `.claude/settings.local.json` 에 logs 쓰기 + build_pending/promote_evaluated 실행 + WebSearch 허용됨.
