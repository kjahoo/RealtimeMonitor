---
name: stock-60-ai-eval-day
description: 평일 08:00~16:30 15분마다 60점+ 종목 AI 평가(13항목). 스크립트 실행은 scheduler.py 데몬 전담 — Claude 실행 의무 없음.
---

60점+ 종목 AI 평가 사이클(무인, Windows). 자율 실행하고 질문하지 말 것. 작업폴더 C:\Projects\RealtimeMonitor, 오늘 LOCAL 날짜를 YYYYMMDD로 사용.

[구조 — 2026-07-07 변경: 파이썬 스크립트는 scheduler.py 데몬이 전담]
- build_pending.py → promote_evaluated.py → auto_buy.py 3스크립트는 상시 데몬 scheduler.py(바탕화면 Run_System.bat 기동, 로그인 시 자동시작 등록됨)가 평일 08:00~16:30, 10분마다 백그라운드로 자동 실행한다.
- **이 Claude 작업은 위 3스크립트를 실행하지 않는다.** 예약(무인) 세션에는 Windows-MCP 서버가 연결되지 않고 computer-use 승인도 불가한 플랫폼 제약이 확인됨(2026-07-07). 따라서 Windows-MCP ToolSearch 재시도 루프·computer-use fallback 모두 시도하지 말 것(시간 낭비 금지). 스크립트 실행·텔레그램 발송·Search_History 수정·plan 기록은 전적으로 데몬이 담당한다.
- 각 스크립트 역할(참고):
  build_pending.py(60점+ 신규 탐지 → logs/{날짜}_claude_pending.csv, 동시에 60점+ 전체를 promising=Search_History.csv 에 자동 등록 — 기등록 스킵)
  → promote_evaluated.py(results.json 후처리: 텔레그램 요약 + BUY promising 추가 + done 기록, results.json 없으면 no-op)
  → auto_buy.py(주문을 직접 내지 않음. BUY 종목별 목표수량+sweep 기준가만 계산해 logs/{날짜}_autobuy_plan.json 에 기록).
    ※ auto_buy 집행 게이트 = 실시간 total_score×100 ≥ 60점(SCORE_MIN=60). 비중 구간제(1안): 60~69=5% · 70~79=10% · 80~89=15% · 90~99=20% · 100=25%. 집행 게이트·평가 대상·execution_monitor 재확인 모두 60점+로 통일(2026-07-22).
- scheduler.py 가 시작과 동시에 상시 데몬 execution_monitor.py 도 기동: plan 을 읽어 KRX 정규장 09:00~15:30에 4초마다 매도호가(ka10004)를 실시간 감시 → promising 지정가 이하 호가 잔량만큼 sweep 매수, target_qty 채울 때까지 반복. 체결현황 텔레그램 보고도 monitor 담당.

[이 작업이 하는 일 — AI 평가만]
- 파일 읽기/쓰기는 일반 파일 도구로 한다. 쓰기는 logs 폴더에만.
- 오늘자 60점+ 종목 AI 평가 → logs/{날짜}_claude_results.json 저장 → 결과 한 줄 요약 보고 후 종료. (이후 처리는 데몬의 promote_evaluated/auto_buy 가 10분 내 자동 수행.)
- 평가할 60점+ 종목이 0개(또는 전부 기평가)라 results.json 을 새로 쓰지 않은 경우: 한 줄 보고 후 즉시 종료(다른 작업 불필요).

(평가 대상 선정) **반드시 오늘자 logs/{날짜}_Stock_V3.csv 원본에서 score_total >= 0.60(=60점+) 종목을 직접 골라내어 평가한다.**
- 우선순위: (1) logs/{날짜}_Stock_V3.csv 를 읽어 score_total>=0.60 종목을 모두 추출(헤더 컬럼: code,name,close_price,market_cap,score_total,net_hits,surge_hits,drop_hits,time,target1,target5,target20,drop1,drop5,drop20). (2) logs/{날짜}_claude_pending.csv 가 있으면 참고하되, pending 이 비어있거나 일부만 담겨 있어도 Stock_V3 의 60점+ 전체를 평가 대상으로 한다(pending 만 보고 누락시키지 말 것).
- Stock_V3.csv 가 없거나 60점+ 행이 0개면: 평가할 60점+ 없음 → 아무 작업도 하지 말고 한 줄 보고 후 종료.
- 결과 JSON 의 v3_score=score_total, v3_score100=score_total*100 은 Stock_V3 값을 그대로 복사한다.
- **주가(close_price)는 promising = logs/{날짜}_Search_History.csv 의 current_price 를 기준으로 한다.** 해당 code 가 Search_History 에 있으면 current_price(있으면 market_cap 도 그 값)를 close_price 로 쓰고, 없으면 Stock_V3 의 close_price 로 폴백한다. 밸류에이션도 이 기준가로 한다.
- **시세분석(가격·수급·차트·시장분위기 등)은 V3 스코어링 모델이 이미 정량 반영했으므로 AI 평가에서 별도로 하지 않는다.** 기술적/차트 분석 항목은 제외하고, 펀더멘털(사업·실적·성장·밸류에이션·리스크) 중심으로만 평가한다.

(평가 수행) C:\Projects\RealtimeMonitor\ai_eval_prompt.txt 를 읽어 그 지침을 그대로 따라 각 종목을 web_search(최근 2026 자료, 한국어)로 평가해 logs/{날짜}_claude_results.json 에 스키마대로 저장한다. **저장까지만** 하면 된다(이후 처리는 데몬의 promote/auto_buy 담당).
- **각 종목 analysis 는 아래 13항목을 모두 빠짐없이 채운다(누락 금지):** 1 business(주요 사업·제품별 매출 비중) 2 customers(주요 고객사·고객별 매출 비중) 3 financials(작년 vs 올해 최근 분기까지 비교) 4 growth(최신 정보 기반 성장성) 5 competition(경쟁사 대비 장단점) 6 valuation(밸류에이션+peer 비교, 최근 분기 실적·재무 중심, PER/PBR 등) 7 invest_points(최신 투자 포인트+최신 주요 뉴스) 8 gossip(웹상의 의견) 9 dilution(메자닌 CB/BW 등 희석 요인 — **단, 대략 6개월 전~6개월 후 사이에 실제로 출회·전환되는 물량이 아니거나, 규모가 유통주식/시총 대비 미미하면 감점요인으로 보지 않고 사실만 기재**. 임박·대규모 오버행만 리스크로 반영) 10 risks(리스크) 11 verdict(종합 판단+매수/관망/회피 근거) 12 report(6개월 내 애널리스트 분석보고서가 있는지, 보고서 내용 긍정적인지) 13 extra(이외 도움될 사항). 시세·차트(기술적) 분석 항목은 두지 않는다(스코어링 모델이 이미 수행). 확인 안 되는 항목은 "자료 부족"으로 명시(추측 금지).
- **위 13항목 종합으로 투자 매력도를 100점 만점

순수 펀더멘털 절대평가 — 각 종목을 그 자체의 사업·실적·성장·밸류에이션·리스크만으로 독립 채점한다. ① 같은 사이클의 다른 종목과 비교해 등급을 매기지 말 것(상대비교 금지). ② 주가가 며칠새 급등했다는 이유(수백% 급등·단기과열)만으로 감점·추격회피 하지 말 것 — 시세·수급·급등률은 V3 점수와 매수집행 50점 게이트가 담당하므로 claude_score 는 시세와 무관하게 펀더멘털 매력도만 반영하며, 동일 펀더멘털이면 실행이 달라도 등급이 흔들리지 않아야 한다. ③ 단, 6개월 내 투자경고종목·거래정지 이력, 자본잠식·순수 적자 테마주, 임박(±6개월)·대규모 희석(CB/BW 등) 오버행은 시세와 무관한 재무·구조 리스크이므로 회피/감점 사유로 유지한다. claude_score 밴드: 85+ 강력매수/BUY · 70~84 매수/BUY · 55~69·40~54 관망/HOLD · 40미만 회피/AVOID(70+ BUY, 40~69 HOLD, 40미만 AVOID).
(claude_score 정수)으로 기재**하고 grade(강력매수/매수/관망/회피)·recommendation(BUY/HOLD/AVOID)도 함께 채운다. 단기 시세·수급은 이미 V3 점수에 반영됐으므로 claude_score 는 펀더멘털 매력도에 집중한다. 6개월 내 거래정지가 있거나, 투자경고종목·순수 적자 테마주는 펀더멘털 부실로 회피.

직접 텔레그램 발송이나 Search_History 수정은 하지 말 것(데몬 스크립트가 처리). 상세: C:\Projects\RealtimeMonitor\CLAUDE_EVAL_WORKFLOW.md, 분석 스키마: ai_eval_prompt.txt.

[롤백] 되돌리려면: logs\scheduler.py.bak_* 로 scheduler.py 복원, logs\CLAUDE.md.bak_* / logs\SKILL_stock60_bak_*.md 복원, 시작프로그램 RealtimeMonitor_Scheduler.lnk 삭제. (2026-07-07 이전 = Claude 도 스크립트 3종 실행하던 버전: logs\CLAUDE.md.bak_20260707_1414 / logs\SKILL_stock60_bak_20260707_1414.md)
