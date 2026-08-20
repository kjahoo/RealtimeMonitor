"""
scheduler.py - RealtimeMonitor 자동 스케줄러
=============================================
[시간대별 동작]
  평일 08:00~09:00  : NXT 모드 → main_stock + Update_Promising 실행
  평일 09:00~15:30  : KRX 모드 → main_stock + Update_Promising 실행
  평일 15:30~20:00  : NXT 모드 → main_stock + Update_Promising 실행
  평일 20:00~       : 봇 종료 → Update_Data_All → build_stock_master(신규상장 편입)
                       → build_buylist(다음거래일 매수리스트) 자동 실행 → 익일 08:00 대기
  주말              : 대기

  ※ 장중 main_stock 은 logs/{today}_buylist.json(없으면 buylist_latest.json)의
     종목만 스캔한다. buylist 가 없으면 전체 종목으로 폴백.

  ※ main_etf.py 는 당분간 실행하지 않음
  ※ Search_Stock_V3.py 는 스케줄러 기동 시 항상 별도 콘솔로 실행되며,
     비정상 종료 시 자동 재시작됩니다.
  ※ telegram_chat.py 는 스케줄러 기동 시 항상 별도 콘솔로 실행되며,
     텔레그램으로 종목명/코드 입력 시 V3 분석 결과를 응답합니다.
     비정상 종료 시 자동 재시작됩니다.

[사용법]
  python scheduler.py
"""

import os
import sys
import time
import shutil
import subprocess
import signal
import threading
from datetime import datetime, timedelta, time as dtime

# ====================================================
# ⚙️ 설정 (필요 시 수정)
# ====================================================
PYTHON_EXE = r"C:\Users\JH_Signature\miniconda3\envs\trading_env\python.exe"
PROJECT_DIR = r"C:\Projects\RealtimeMonitor"

# 시간대 설정
TIME_NXT_START   = dtime(8,  0)   # NXT 프리마켓 시작
TIME_KRX_START   = dtime(9,  0)   # KRX 정규장 시작
TIME_KRX_END     = dtime(15, 30)  # KRX 정규장 종료
TIME_NXT_END     = dtime(20,  0)  # NXT 애프터마켓 종료
TIME_NEXT_START  = dtime(8,  0)   # 다음날 재시작 시각

# 폴링 간격 (초)
POLL_INTERVAL = 10

# ====================================================
# 📋 상태 관리
# ====================================================
# "stock"  : main_stock.py
# "update" : Update_Promising_Stocks.py
# "search" : Search_Stock_V3.py  ← 항상 실행 (시장 모드 무관)
running_procs = {}
current_mode  = None  # "NXT" | "KRX" | "CLOSED" | "WAITING" | "WEEKEND"


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ====================================================
# 🕐 시간대 판단
# ====================================================
def get_market_mode():
    """현재 시각 기준 마켓 모드 반환"""
    now = datetime.now()

    # 주말 체크 (0=월 ... 4=금, 5=토, 6=일)
    if now.weekday() >= 5:
        return "WEEKEND"

    # 공휴일(하드코딩 휴일집합)도 휴장 → WEEKEND 처리(시장봇 정지, 휴일 데이터 기록 방지)
    try:
        from kis_api import kiwoom_trading as _kt
        if now.strftime("%Y%m%d") in _kt.KRX_HOLIDAYS:
            return "WEEKEND"
    except Exception:
        pass

    t = now.time().replace(second=0, microsecond=0)

    if TIME_NXT_START <= t < TIME_KRX_START:
        return "NXT"
    elif TIME_KRX_START <= t < TIME_KRX_END:
        return "KRX"
    elif TIME_KRX_END <= t < TIME_NXT_END:
        return "NXT"
    elif t >= TIME_NXT_END:
        return "CLOSED"
    else:
        # 자정~08시
        return "WAITING"


def seconds_until(target_time):
    """오늘(또는 내일) target_time 까지 남은 초"""
    now = datetime.now()
    target_dt = datetime.combine(now.date(), target_time)
    if target_dt <= now:
        target_dt += timedelta(days=1)
    return max(0, (target_dt - now).total_seconds())


def next_weekday_start():
    """다음 평일 08:00 까지 남은 초"""
    now = datetime.now()
    days_ahead = 1
    while True:
        candidate = now + timedelta(days=days_ahead)
        if candidate.weekday() < 5:  # 평일
            target = datetime.combine(candidate.date(), TIME_NEXT_START)
            return max(0, (target - now).total_seconds())
        days_ahead += 1


# ====================================================
# 📴 공휴일 감지 — 하드코딩 휴일목록(kt.KRX_HOLIDAYS)만으로 결정적 판정
#    ※ 삼성 시세 유무로 휴장을 단정하지 않는다: 아침 토큰/API 일시 실패에 거래일을
#      휴장으로 오판(2026-07-21 사례) → 하루 매매·데이터 통째 손실. fail-safe = 개장.
# ====================================================
def is_market_open_today():
    """오늘 개장 여부. 주말·KRX_HOLIDAYS 만으로 판정하고, 그 외엔 개장(True)으로 간주."""
    try:
        from kis_api import kiwoom_trading as kt
        now = datetime.now()
        if now.weekday() >= 5 or now.strftime("%Y%m%d") in kt.KRX_HOLIDAYS:
            log(f"   📴 {now:%Y%m%d} 주말/공휴일(KRX_HOLIDAYS) → 휴장")
            return False
    except Exception as e:
        log(f"   ⚠️ 휴일 확인 오류: {e} → 개장으로 간주")
    return True   # 목록에 없으면 개장. 시세 실패로 휴장 단정하지 않음(거래일 오판 방지)


# ====================================================
# 🔍 Search_Stock_V3 상시 실행 관리
# ====================================================
def start_search_bot():
    """
    Search_Stock_V3.py 를 별도 콘솔 창으로 실행합니다.
    이 봇은 시장 모드와 무관하게 항상 살아있어야 합니다.
    MARKET_MODE 환경변수는 주입하지 않습니다 (수동 검색 도구이므로 불필요).
    """
    script_path = os.path.join(PROJECT_DIR, "Search_Stock_V3.py")
    try:
        proc = subprocess.Popen(
            [PYTHON_EXE, script_path],
            cwd=PROJECT_DIR,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        running_procs["search"] = proc
        log(f"🔍 Search_Stock_V3.py 시작 (PID: {proc.pid})")
    except Exception as e:
        log(f"   ❌ Search_Stock_V3.py 시작 실패: {e}")


def ensure_search_bot_alive():
    """
    Search_Stock_V3.py 가 살아있는지 확인하고, 종료됐으면 재시작합니다.
    메인 루프의 모든 폴링 주기마다 호출됩니다.
    """
    proc = running_procs.get("search")
    if proc is None or proc.poll() is not None:
        log("⚠️ Search_Stock_V3.py 종료 감지 → 재시작")
        start_search_bot()


def start_telegram_bot():
    """telegram_chat.py 를 별도 콘솔 창으로 실행합니다."""
    script_path = os.path.join(PROJECT_DIR, "kis_api", "telegram_chat.py")
    try:
        proc = subprocess.Popen(
            [PYTHON_EXE, script_path],
            cwd=PROJECT_DIR,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        running_procs["telegram"] = proc
        log(f"📱 telegram_chat.py 시작 (PID: {proc.pid})")
    except Exception as e:
        log(f"   ❌ telegram_chat.py 시작 실패: {e}")


def ensure_telegram_bot_alive():
    proc = running_procs.get("telegram")
    if proc is None or proc.poll() is not None:
        log("⚠️ telegram_chat.py 종료 감지 → 재시작")
        start_telegram_bot()


def start_exec_monitor():
    """execution_monitor.py 를 별도 콘솔로 실행합니다.
    실시간 호가 sweep 매수 집행 데몬 — 시장 모드와 무관하게 항상 살아있어야 하며,
    내부에서 정규장(09:00~15:30)에만 실제 주문을 냅니다."""
    script_path = os.path.join(PROJECT_DIR, "execution_monitor.py")
    try:
        proc = subprocess.Popen(
            [PYTHON_EXE, script_path],
            cwd=PROJECT_DIR,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        running_procs["monitor"] = proc
        log(f"🛒 execution_monitor.py 시작 (PID: {proc.pid})")
    except Exception as e:
        log(f"   ❌ execution_monitor.py 시작 실패: {e}")


def ensure_exec_monitor_alive():
    proc = running_procs.get("monitor")
    if proc is None or proc.poll() is not None:
        log("⚠️ execution_monitor.py 종료 감지 → 재시작")
        start_exec_monitor()


# ====================================================
# 🚀 시장 연동 봇 관리 (main_stock + Update_Promising)
# ====================================================
def start_bots(mode):
    """
    mode에 따라 시장 연동 봇을 실행합니다.
      KRX / NXT : main_stock + Update_Promising_Stocks
      ※ main_etf.py 는 실행하지 않습니다.
    """
    global running_procs

    stop_market_bots()  # 기존 시장 연동 봇 먼저 종료

    env = os.environ.copy()
    env["MARKET_MODE"] = mode  # inquiry.py 에서 KRX/NXT 분기에 사용

    scripts = {
        "stock":  "main_stock.py",
        "update": "Update_Promising_Stocks.py",
    }
    log(f"🚀 봇 시작 (모드: {mode}) — 프로덕션 2개")

    for key, script in scripts.items():
        script_path = os.path.join(PROJECT_DIR, script)
        try:
            proc = subprocess.Popen(
                [PYTHON_EXE, script_path],
                cwd=PROJECT_DIR,
                env=env,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            running_procs[key] = proc
            log(f"   ✅ {script} 시작 (PID: {proc.pid})")
            time.sleep(1)  # 순차 시작 (토큰 충돌 방지)
        except Exception as e:
            log(f"   ❌ {script} 시작 실패: {e}")


def stop_market_bots():
    """시장 연동 봇(stock, update)만 종료합니다. search 봇은 건드리지 않습니다."""
    global running_procs

    market_keys = [k for k in ("stock", "update") if k in running_procs]
    if not market_keys:
        return

    log("🛑 시장 연동 봇 종료 중...")
    for key in market_keys:
        proc = running_procs.pop(key, None)
        try:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                log(f"   ✅ {key} 종료 완료")
        except Exception as e:
            log(f"   ⚠️ {key} 종료 중 오류: {e}")


def stop_bots():
    """모든 봇(search 포함) 종료 — 스케줄러 자체가 종료될 때만 사용합니다."""
    global running_procs

    if not running_procs:
        return

    log("🛑 전체 봇 종료 중...")
    for key, proc in running_procs.items():
        try:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                log(f"   ✅ {key} 종료 완료")
        except Exception as e:
            log(f"   ⚠️ {key} 종료 중 오류: {e}")

    running_procs = {}


def check_market_bots_alive():
    """시장 연동 봇이 비정상 종료됐으면 재시작합니다."""
    global running_procs, current_mode

    if current_mode not in ("NXT", "KRX"):
        return

    script_map = {
        "stock":  "main_stock.py",
        "update": "Update_Promising_Stocks.py",
    }

    for key in list(script_map.keys()):
        proc = running_procs.get(key)
        if proc and proc.poll() is not None:  # 종료됨
            script = script_map[key]
            log(f"⚠️ {script} 비정상 종료 감지 → 재시작")

            env = os.environ.copy()
            env["MARKET_MODE"] = current_mode
            try:
                new_proc = subprocess.Popen(
                    [PYTHON_EXE, os.path.join(PROJECT_DIR, script)],
                    cwd=PROJECT_DIR,
                    env=env,
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
                running_procs[key] = new_proc
                log(f"   ✅ {script} 재시작 완료 (PID: {new_proc.pid})")
            except Exception as e:
                log(f"   ❌ 재시작 실패: {e}")


# ====================================================
# 📋 Search_History.csv 다음 거래일 파일 준비
# ====================================================
LOGS_DIR = os.path.join(PROJECT_DIR, "logs")


def get_next_trading_day():
    """다음 거래일(평일) 날짜 반환"""
    candidate = datetime.now().date() + timedelta(days=1)
    while candidate.weekday() >= 5:  # 5=토, 6=일
        candidate += timedelta(days=1)
    return candidate


def copy_search_history_to_next_trading_day():
    """
    오늘자 YYYYMMDD_Search_History.csv 를 다음 거래일 파일명으로 복사합니다.
    장 종료(20:00) 직전 CLOSED 전환 시 호출합니다.
    """
    today_str = datetime.now().strftime("%Y%m%d")
    today_file = os.path.join(LOGS_DIR, f"{today_str}_Search_History.csv")

    if not os.path.exists(today_file):
        log(f"⚠️ 오늘자 Search_History.csv 없음 ({today_str}) → 복사 건너뜀")
        return

    next_day = get_next_trading_day()
    next_str = next_day.strftime("%Y%m%d")
    next_file = os.path.join(LOGS_DIR, f"{next_str}_Search_History.csv")

    try:
        shutil.copy2(today_file, next_file)
        log(f"📋 Search_History.csv 복사 완료: {today_str} → {next_str}")
    except Exception as e:
        log(f"❌ Search_History.csv 복사 실패: {e}")


# ====================================================
# 📊 Update_Data_All 자동 실행
# ====================================================
def run_update_data_all():
    """
    장 종료 후 Update_Data_All.py 를 자동 실행합니다.
    기존 코드의 수동 입력 부분을 stdin 으로 자동 주입합니다.
    """
    log("📊 Update_Data_All.py 자동 실행 시작...")

    today_str = datetime.now().strftime("%Y%m%d")

    # 두 번의 input() 에 자동 응답:
    #   1) "👉 시작 날짜를 입력하세요" -> 오늘 날짜
    #   2) "데이터 업데이트를 진행하시겠습니까? (y/n)" -> y
    auto_input = f"{today_str}\ny\n".encode("utf-8")

    script_path = os.path.join(PROJECT_DIR, "Update_Data_All.py")

    try:
        proc = subprocess.Popen(
            [PYTHON_EXE, script_path],
            cwd=PROJECT_DIR,
            stdin=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        proc.stdin.write(auto_input)
        proc.stdin.flush()
        proc.stdin.close()

        log(f"   ✅ Update_Data_All.py 실행 중 (PID: {proc.pid})")
        log(f"   📅 업데이트 기준일: {today_str}")

        # 완료까지 대기 (최대 3시간)
        try:
            proc.wait(timeout=10800)
            log("   ✅ Update_Data_All.py 완료!")
        except subprocess.TimeoutExpired:
            log("   ⚠️ 3시간 초과 → 강제 종료")
            proc.kill()

    except Exception as e:
        log(f"   ❌ Update_Data_All.py 실행 실패: {e}")


def _run_blocking(script_name, timeout_sec, label):
    """PROJECT_DIR 의 스크립트를 별도 콘솔로 실행하고 완료까지 대기(공통 헬퍼)."""
    script_path = os.path.join(PROJECT_DIR, script_name)
    log(f"🧺 {label} 실행 시작... ({script_name})")
    try:
        proc = subprocess.Popen(
            [PYTHON_EXE, script_path],
            cwd=PROJECT_DIR,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        log(f"   ✅ {script_name} 실행 중 (PID: {proc.pid})")
        try:
            proc.wait(timeout=timeout_sec)
            log(f"   ✅ {label} 완료!")
        except subprocess.TimeoutExpired:
            log(f"   ⚠️ {label} {timeout_sec//60}분 초과 → 강제 종료")
            proc.kill()
    except Exception as e:
        log(f"   ❌ {label} 실행 실패: {e}")


def run_build_stock_master():
    """전체 상장 보통주 마스터 생성 + 신규상장 CSV 백필 (최대 2시간)."""
    _run_blocking("build_stock_master.py", 7200, "종목 마스터 생성/백필")


def run_build_buylist():
    """다음 거래일 매수리스트(장중 스캔 유니버스) 생성 (최대 30분)."""
    _run_blocking("build_buylist.py", 1800, "매수리스트 생성")


# ====================================================
# 📈 60점+ AI 평가 파이프라인 (build_pending → promote → auto_buy)
#    - 평일 08:00~16:30, 10분 간격으로 1사이클 (EVAL_INTERVAL 참조)
#    - auto_buy 는 내부적으로 KRX 정규장(09:00~15:30)에만 실주문 → NXT 세션 미주문
#    - 별도 스레드로 실행해 메인 폴링 루프를 막지 않음
#    - AI 평가(2단계)는 Cowork(Claude) 작업이 pending CSV를 읽어 results.json 저장,
#      promote 가 그 결과를 받아간다. (이 스케줄러는 결정적 파이썬 단계만 담당)
# ====================================================
EVAL_START    = dtime(8,  0)
EVAL_END      = dtime(16, 30)
EVAL_INTERVAL = 10 * 60            # 10분(초)
_eval_last_run = None
_eval_lock     = threading.Lock()


def _run_eval_script(script, label, timeout=600):
    """평가 파이프라인 단일 스크립트를 블로킹 실행하고 마지막 출력 한 줄을 로깅."""
    path = os.path.join(PROJECT_DIR, script)
    try:
        log(f"   ▶ {label} ({script})")
        r = subprocess.run(
            [PYTHON_EXE, path], cwd=PROJECT_DIR,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
        tail = ""
        if r.stdout:
            lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
            if lines:
                tail = lines[-1][:120]
        log(f"   ✅ {label} rc={r.returncode} | {tail}")
    except subprocess.TimeoutExpired:
        log(f"   ⚠️ {label} 타임아웃({timeout}s)")
    except Exception as e:
        log(f"   ❌ {label} 실패: {e}")


def _eval_pipeline_worker():
    """build_pending → promote → auto_buy 순차 실행(백그라운드 스레드)."""
    try:
        log("📈 60+ 평가 파이프라인 사이클 시작")
        _run_eval_script("build_pending.py",     "build_pending(신규탐지)")
        maybe_spawn_claude_eval()  # 미평가 60점+ 있으면 Claude CLI 즉시 기동(이벤트 구동)
        _run_eval_script("promote_evaluated.py", "promote(후처리)")
        _run_eval_script("auto_buy.py",          "auto_buy(체결/매수)")
        log("📈 60+ 평가 파이프라인 사이클 종료")
    finally:
        _eval_lock.release()


# ====================================================
# 🤖 Claude CLI 이벤트 구동 AI 평가 (2026-08-19 추가)
#    - build_pending 직후 미평가 60점+ 감지 시 Claude Code CLI 헤드리스(-p)로
#      즉시 평가 기동 → logs/{날짜}_claude_results.json 저장 → promote 가 후처리.
#    - Cowork 15분 폴링 예약작업은 백업으로 축소됨. CLI 우선.
#    - 락(중복실행 방지) + 30분 타임아웃 + 동일 종목셋 30분 재시도 쿨다운.
# ====================================================
CLAUDE_EXE            = r"C:\Users\JH_Signature\.local\bin\claude.exe"
CLAUDE_EVAL_TIMEOUT   = 30 * 60   # CLI 평가 최대 30분
CLAUDE_RETRY_COOLDOWN = 30 * 60   # 동일 종목셋 재시도 최소 간격(실패 루프 방지)
_claude_lock       = threading.Lock()
_claude_last_codes = None
_claude_last_start = None


def _today_str():
    return datetime.now().strftime("%Y%m%d")


def _unevaluated_pending_codes():
    """오늘자 pending 중 eval_done/results 에 없는 미평가 코드 집합."""
    import csv as _csv
    import json as _json
    day = _today_str()
    logs = os.path.join(PROJECT_DIR, "logs")
    pend = os.path.join(logs, f"{day}_claude_pending.csv")
    if not os.path.exists(pend):
        return set()
    try:
        with open(pend, encoding="utf-8-sig") as f:
            codes = {str(r["code"]).strip().zfill(6)
                     for r in _csv.DictReader(f) if r.get("code")}
    except Exception:
        return set()
    done = set()
    p = os.path.join(logs, f"{day}_claude_eval_done.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                done |= {str(c).zfill(6) for c in _json.load(f).get("codes", [])}
        except Exception:
            pass
    p = os.path.join(logs, f"{day}_claude_results.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                d = _json.load(f)
            items = d.get("results", d) if isinstance(d, dict) else d
            done |= {str(it.get("code", "")).zfill(6) for it in items}
        except Exception:
            pass
    return codes - done


def _claude_eval_worker(codes):
    """Claude Code CLI 헤드리스로 AI 평가 실행(별도 스레드, 락 보유 상태로 진입)."""
    try:
        day = _today_str()
        log(f"🤖 Claude CLI 평가 시작: {len(codes)}종목 {sorted(codes)}")
        prompt = (
            f"오늘({day}) 60점+ 종목 AI 평가를 CLAUDE.md 지침 그대로 수행하라. "
            f"대상: logs/{day}_Stock_V3.csv 의 score_total>=0.60 중 미평가 종목"
            f"(참고 pending: {', '.join(sorted(codes))}). "
            f"결과는 logs/{day}_claude_results.json 에 스키마대로 저장 후 종료. "
            f"전부 기평가면 아무 파일도 쓰지 말고 한 줄 보고 후 종료."
        )
        logf = os.path.join(PROJECT_DIR, "logs", f"{day}_claude_cli_eval.log")
        with open(logf, "a", encoding="utf-8") as lf:
            lf.write(f"\n===== {datetime.now()} codes={sorted(codes)} =====\n")
            lf.flush()
            r = subprocess.run(
                [CLAUDE_EXE, "-p", prompt,
                 "--permission-mode", "acceptEdits",
                 "--allowedTools", "Read,Write,Edit,Glob,Grep,WebSearch,WebFetch"],
                cwd=PROJECT_DIR, stdout=lf, stderr=subprocess.STDOUT,
                timeout=CLAUDE_EVAL_TIMEOUT,
            )
        log(f"🤖 Claude CLI 평가 종료 rc={r.returncode}")
    except subprocess.TimeoutExpired:
        log(f"⚠️ Claude CLI 평가 타임아웃({CLAUDE_EVAL_TIMEOUT}s)")
    except Exception as e:
        log(f"❌ Claude CLI 평가 실패: {e}")
    finally:
        _claude_lock.release()


def maybe_spawn_claude_eval():
    """build_pending 직후 호출: 미평가 60점+ 있으면 Claude CLI 평가를 비동기 기동."""
    global _claude_last_codes, _claude_last_start
    if not os.path.exists(CLAUDE_EXE):
        return
    codes = _unevaluated_pending_codes()
    if not codes:
        return
    now = datetime.now()
    if (_claude_last_codes == codes and _claude_last_start
            and (now - _claude_last_start).total_seconds() < CLAUDE_RETRY_COOLDOWN):
        return
    if not _claude_lock.acquire(blocking=False):
        log("⏳ Claude CLI 평가 진행 중 → 이번 틱 건너뜀")
        return
    _claude_last_codes = set(codes)
    _claude_last_start = now
    threading.Thread(target=_claude_eval_worker, args=(codes,), daemon=True).start()


_openkick_day = None


def run_autobuy_open_kick():
    """개장 직후(09:00~09:10) auto_buy 만 1회 즉시 실행해 plan 을 바로 생성한다.
    평가 파이프라인은 10분 주기 + 선행단계(build_pending·promote)가 ~10분 걸려
    개장 후 ~15분간 plan 부재 → execution_monitor 매수가 늦던 문제 해소.
    (매수 안전은 monitor 의 '매수직전 정규장 점수 재확인'이 그대로 담당 — NXT 스파이크 차단 유지)"""
    global _openkick_day
    now = datetime.now()
    if now.weekday() >= 5:
        return
    day = now.strftime("%Y%m%d")
    if _openkick_day == day:
        return
    if not (dtime(9, 0) <= now.time() <= dtime(9, 10)):
        return
    try:
        from kis_api import kiwoom_trading as _kt
        if day in _kt.KRX_HOLIDAYS:
            _openkick_day = day
            return
    except Exception:
        pass
    _openkick_day = day
    log("⚡ 개장 킥: auto_buy 즉시 실행(plan 선생성)")
    threading.Thread(target=_run_eval_script,
                     args=("auto_buy.py", "auto_buy(개장킥)"), daemon=True).start()


def run_eval_pipeline_if_due():
    """평일 08:00~16:30, 10분마다(EVAL_INTERVAL) 평가 파이프라인을 백그라운드로 1회 기동."""
    global _eval_last_run
    now = datetime.now()
    if now.weekday() >= 5:
        return
    # 공휴일(휴장)엔 평가 파이프라인(build_pending→promote→auto_buy) 미실행 — 휴일 데이터 생성 방지
    try:
        from kis_api import kiwoom_trading as _kt
        if now.strftime("%Y%m%d") in _kt.KRX_HOLIDAYS:
            return
    except Exception:
        pass
    if not (EVAL_START <= now.time() <= EVAL_END):
        return
    if _eval_last_run and (now - _eval_last_run).total_seconds() < EVAL_INTERVAL - 30:
        return
    if not _eval_lock.acquire(blocking=False):
        log("⏳ 평가 파이프라인 이전 사이클 진행 중 → 이번 틱 건너뜀")
        return
    _eval_last_run = now
    threading.Thread(target=_eval_pipeline_worker, daemon=True).start()


# ====================================================
# 🔄 메인 루프
# ====================================================
def main():
    global current_mode

    log("=" * 55)
    log("🤖 RealtimeMonitor 자동 스케줄러 시작")
    log(f"   Python  : {PYTHON_EXE}")
    log(f"   프로젝트: {PROJECT_DIR}")
    log("=" * 55)

    # Ctrl+C 핸들러
    def on_exit(sig, frame):
        log("\n🛑 스케줄러 종료 요청 → 전체 봇 정리 중...")
        stop_bots()
        sys.exit(0)

    signal.signal(signal.SIGINT,  on_exit)
    signal.signal(signal.SIGTERM, on_exit)

    # ── Search_Stock_V3, telegram_chat, execution_monitor 는 시작과 동시에 항상 기동
    start_search_bot()
    start_telegram_bot()
    start_exec_monitor()

    data_updated_today = False  # 하루에 1번만 Update_Data_All 실행
    holiday_today      = False  # 공휴일 플래그 — WAITING/WEEKEND 복귀 시 해제

    while True:
        mode = get_market_mode()

        # ── 날짜가 바뀌면 업데이트 플래그 초기화
        if mode in ("NXT", "KRX") and data_updated_today:
            data_updated_today = False

        # ── 공휴일 대기 중: search/telegram 봇만 유지, 시장봇 건드리지 않음
        #    WAITING(자정~08시) 또는 WEEKEND 복귀 시 플래그 해제
        if holiday_today:
            if mode in ("WAITING", "WEEKEND"):
                holiday_today  = False
                current_mode   = mode
                log(f"📌 공휴일 종료 → 모드 복귀: {mode}")
            ensure_search_bot_alive()
            ensure_telegram_bot_alive()
            ensure_exec_monitor_alive()
            time.sleep(POLL_INTERVAL)
            continue

        # ── 모드가 바뀔 때만 시장 연동 봇을 재시작
        if mode != current_mode:
            log(f"📌 모드 전환: {current_mode} → {mode}")
            current_mode = mode

            if mode in ("NXT", "KRX"):
                # NXT 아침 첫 전환(08:00)이면 공휴일 체크
                if mode == "NXT" and datetime.now().time() < TIME_KRX_START:
                    log("🔍 NXT 개장 — 삼성전자 시세로 공휴일 여부 확인 중...")
                    if not is_market_open_today():
                        holiday_today = True
                        stop_market_bots()
                        secs = next_weekday_start()
                        h, m = divmod(int(secs) // 60, 60)
                        log(f"📴 공휴일 — 다음 평일 08:00까지 대기 ({h}시간 {m}분)")
                        time.sleep(POLL_INTERVAL)
                        continue
                start_bots(mode)

            elif mode == "CLOSED":
                copy_search_history_to_next_trading_day()
                stop_market_bots()
                if not data_updated_today:
                    run_update_data_all()
                    # 전체 이력 갱신 직후: 신규상장 편입 → 다음 거래일 매수리스트 생성
                    run_build_stock_master()
                    run_build_buylist()
                    data_updated_today = True
                    secs = next_weekday_start()
                    h, m = divmod(int(secs) // 60, 60)
                    log(f"😴 다음 평일 08:00까지 대기 ({h}시간 {m}분)")

            elif mode in ("WAITING", "WEEKEND"):
                stop_market_bots()
                secs = next_weekday_start()
                h, m = divmod(int(secs) // 60, 60)
                log(f"😴 다음 평일 08:00까지 대기 ({h}시간 {m}분)")

        # ── 봇 생존 확인
        check_market_bots_alive()    # 시장 연동 봇 (stock, update)
        ensure_search_bot_alive()    # Search_Stock_V3 (항상)
        ensure_telegram_bot_alive()  # telegram_chat (항상)
        ensure_exec_monitor_alive()  # execution_monitor (항상)

        # ── 60점+ 평가 파이프라인 (평일 08:00~16:30, 10분마다 / 백그라운드)
        run_eval_pipeline_if_due()
        run_autobuy_open_kick()      # 개장(09:00) 직후 auto_buy 1회 즉시 실행 → plan 선생성

        # ── 현재 상태 주기적 출력 (폴링 주기 내 첫 번째 틱)
        if datetime.now().second < POLL_INTERVAL:
            if mode in ("NXT", "KRX"):
                market_alive = sum(
                    1 for k in ("stock", "update")
                    if running_procs.get(k) and running_procs[k].poll() is None
                )
                search_alive = (
                    running_procs.get("search") and
                    running_procs["search"].poll() is None
                )
                tg_alive = (
                    running_procs.get("telegram") and
                    running_procs["telegram"].poll() is None
                )
                mon_alive = (
                    running_procs.get("monitor") and
                    running_procs["monitor"].poll() is None
                )
                log(f"💓 [{mode}] 프로덕션: {market_alive}/2 | 검색봇: {'✅' if search_alive else '❌'} | 텔레봇: {'✅' if tg_alive else '❌'} | 집행봇: {'✅' if mon_alive else '❌'}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()