# ─────────────────────────────────────────────────────────────────────
# run_ai_eval.ps1 — 60점+ 종목 30분 자동 AI 평가 (로컬 스케줄)
#   1) 오늘자 Stock_V3.csv 에서 60점+ 신규(평가완료 제외) → pending 생성
#   2) Claude Code 헤드리스로 종목별 웹검색 분석 → 결과 JSON 작성
#   3) 후처리: 본인 텔레그램 발송 + 매수종목 promising 추가 + 통합리포트 갱신
#   * Anthropic API 미사용 (구독 Claude Code). main_stock 과 독립 실행.
# ─────────────────────────────────────────────────────────────────────
$ErrorActionPreference = 'Continue'
$Proj   = 'C:\Projects\RealtimeMonitor'
$Py     = 'C:\Users\JH_Signature\miniconda3\envs\trading_env\python.exe'
$Claude = 'C:\Users\JH_Signature\.local\bin\claude.exe'
$env:PYTHONIOENCODING = 'utf-8'
Set-Location $Proj

$LogFile = Join-Path $Proj 'logs\ai_eval_scheduler.log'
function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Tee-Object -FilePath $LogFile -Append }

# ── 장 시간 가드: 평일 09:00~15:40 만 실행 ──────────────────────────
$now = Get-Date
if ($now.DayOfWeek -in 'Saturday','Sunday') { exit 0 }
$mins = $now.Hour * 60 + $now.Minute
if ($mins -lt 540 -or $mins -gt 940) { exit 0 }   # 540=09:00, 940=15:40

$DateStr = Get-Date -Format 'yyyyMMdd'
Log "▶ AI 평가 시작 ($DateStr)"

# ── 1) pending 생성 (60점+ 신규) ────────────────────────────────────
$out = & $Py -c "from claude_eval_pipeline import build_pending_from_v3; print(build_pending_from_v3('$DateStr'))" 2>&1
$cnt = ($out | Select-Object -Last 1).ToString().Trim()
if ($cnt -notmatch '^\d+$' -or [int]$cnt -eq 0) {
    Log "신규 60점+ 종목 없음 (cnt=$cnt) — 종료"
    exit 0
}
Log "신규 60점+ $cnt 개 → Claude 분석 시작"

# ── 2) Claude 헤드리스 분석 ─────────────────────────────────────────
$prompt = (Get-Content (Join-Path $Proj 'ai_eval_prompt.txt') -Raw) -replace '\{DATE\}', $DateStr
& $Claude -p $prompt --permission-mode bypassPermissions --model opus --add-dir $Proj *> (Join-Path $Proj "logs\ai_eval_claude_$DateStr.log")
Log "Claude 분석 완료 (claude exit=$LASTEXITCODE)"

# ── 3) 후처리: 텔레그램 + promising + 통합리포트 ───────────────────
& $Py promote_evaluated.py $DateStr 2>&1 | Tee-Object -FilePath $LogFile -Append
Log "◀ AI 평가 종료"
