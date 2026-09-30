"""
scheduler.py - RealtimeMonitor 자동 스케줄러
=============================================
[시간대별 동작]
  평일 08:00~09:00  : NXT 모드 → main_stock + Update_Promising 실행
  평일 09:00~15:30  : KRX 모드 → main_stock + Update_Promising 실행
  평일 15:30~20:00  : AFTER 모드(장후 — NXT 15:30~ · KRX 애프터마켓 16:00~, 2026-09-14 신설)
                       → main_stock + Update_Promising 실행 (모니터링·알림만)
                       ※ 대상: 전일 build_buylist 필터링 종목(buylist)만
                       ※ 점수는 KRX 시세(학습 데이터와 동일 기준), 알림 현재가·손절 판정은 통합(SOR) 현재가
                       ※ 자동매매 미적용(execution_monitor 는 정규장만 집행),
                         일봉 CSV·Stock_V3·promising·매도전략 상태는 정규장 기준으로 보존하고
                         장후 점수는 logs/{날짜}_Stock_V3_AfterMkt.csv 에 따로 기록
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
import json
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

# 새 콘솔 창 제목을 스크립트 파일명으로 표시 (기본은 python.exe 경로라 창 구분 불가).
#   python.exe 가 먼저 SetConsoleTitleW 후 runpy 로 스크립트/모듈 실행 → 같은 PID 유지(poll/terminate 영향 없음).
#   sys.path[0] 은 'python script.py' 와 같게 스크립트 폴더로 맞춘다(kis_api/telegram_chat.py 의 형제 import).
_TITLE_LAUNCHER = (
    "import ctypes,os,runpy,sys;"
    "t=sys.argv[1];a=sys.argv[2:];"
    "ctypes.windll.kernel32.SetConsoleTitleW(t);"
    "m=a[1] if a[0]=='-m' else None;"
    "sys.argv=[m]+a[2:] if m else a;"
    "sys.path[0]=os.getcwd() if m else os.path.dirname(os.path.abspath(a[0]));"
    "runpy.run_module(m,run_name='__main__',alter_sys=True) if m else runpy.run_path(a[0],run_name='__main__')"
)


def titled_cmd(args, python=None, title=None):
    """[python, *args] 와 동일하게 실행하되 콘솔 창 제목을 파일명(또는 title)으로 설정하는 명령 리스트."""
    if title is None:
        title = os.path.basename(args[0]) if args[0] != "-m" else args[1]
    return [python or PYTHON_EXE, "-c", _TITLE_LAUNCHER, title, *args]

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
current_mode  = None  # "NXT" | "KRX" | "AFTER" | "CLOSED" | "WAITING" | "WEEKEND"
MARKET_MODES  = ("NXT", "KRX", "AFTER")   # 시장 연동 봇(main_stock·Update_Promising)이 도는 모드


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
        return "AFTER"      # 장후 모니터링(자동매매 미적용)
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
            titled_cmd([script_path]),
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
            titled_cmd([script_path]),
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
            titled_cmd([script_path]),
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


def start_paper_trader():
    """paper_trader.py(prod 무후보 시 BULL 대체 전략 가상매매 기록)를 별도 콘솔로 실행합니다.
    실주문 없음 — 파일 읽기·가상체결·본인 텔레그램만. 중복 기동 시 내부 잠금으로 대기합니다."""
    script_path = os.path.join(PROJECT_DIR, "paper_trader.py")
    try:
        proc = subprocess.Popen(
            titled_cmd([script_path]),
            cwd=PROJECT_DIR,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        running_procs["paper"] = proc
        log(f"🧪 paper_trader.py 시작 (PID: {proc.pid})")
    except Exception as e:
        log(f"   ❌ paper_trader.py 시작 실패: {e}")


def ensure_paper_trader_alive():
    proc = running_procs.get("paper")
    if proc is None or proc.poll() is not None:
        log("⚠️ paper_trader.py 종료 감지 → 재시작")
        start_paper_trader()


# ====================================================
# 📊 Stock_V3 웹 뷰어 (Streamlit, 읽기 전용 — 항시 실행)
# ====================================================
VIEWER_PORT = 8501

def _viewer_port_in_use():
    """8501 리슨 여부 — 수동(Run_Viewer.bat) 인스턴스가 이미 떠 있으면 중복 기동 금지.
    (streamlit 은 포트 사용 중이면 즉시 종료 → poll() 재시작 무한루프 방지)"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", VIEWER_PORT)) == 0


def start_viewer():
    """viewer_v3_web.py(Streamlit) 를 별도 콘솔로 실행. 읽기 전용 — 자동매매 무관,
    항시 실행(장외엔 파일 대기 표시). 접속: http://localhost:8501
    viewer_env(전용 conda prefix 환경) 미구축이면 조용히 생략(선택 기능)."""
    viewer_py = os.path.join(PROJECT_DIR, "viewer_env", "python.exe")
    app_path  = os.path.join(PROJECT_DIR, "viewer_v3_web.py")
    if not (os.path.exists(viewer_py) and os.path.exists(app_path)):
        return
    try:
        proc = subprocess.Popen(
            titled_cmd(["-m", "streamlit", "run", app_path,
             "--server.headless", "true", "--browser.gatherUsageStats", "false",
             "--server.port", str(VIEWER_PORT)], python=viewer_py, title="viewer_v3_web.py"),
            cwd=PROJECT_DIR,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        running_procs["viewer"] = proc
        log(f"📊 Stock_V3 웹 뷰어 시작 (PID: {proc.pid}) — http://localhost:{VIEWER_PORT}")
    except Exception as e:
        log(f"   ❌ 웹 뷰어 시작 실패: {e}")


def ensure_viewer_alive():
    proc = running_procs.get("viewer")
    if proc is not None and proc.poll() is None:
        return
    if _viewer_port_in_use():
        return              # 외부(수동) 인스턴스 생존 → 중복 기동/포트충돌 루프 방지
    if proc is not None:
        log("⚠️ 웹 뷰어 종료 감지 → 재시작")
    start_viewer()


# ====================================================
# 🚀 시장 연동 봇 관리 (main_stock + Update_Promising)
# ====================================================
def _write_krx_session_marker():
    """정규장 재평가 경계(logs/{날짜}_krx_session.json)를 당일 1회 기록.
    이 시각 이후 계산된 Stock_V3 점수만 매수 판단·비중에 쓰인다(buy_gate.krx_since).
    - 첫 기록 = 지금(NXT 봇 종료 직후) 시각. 늦게 기동돼도(절전 복귀·루프 지연) 그때까지 돈
      NXT 봇 행을 확실히 배제한다. 대가는 첫 KRX 재계산(UPS ~1분)까지의 짧은 대기뿐.
    - 이미 있으면 유지(장중 scheduler 재시작이 경계를 뒤로 밀어 매수가 멈추지 않게)"""
    try:
        import buy_gate
        now = datetime.now()
        day = now.strftime("%Y%m%d")
        path = buy_gate.krx_session_path(LOGS_DIR, day)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                if json.load(f).get("date") == day:
                    return
        hhmmss = now.strftime("%H:%M:%S")
        since = hhmmss
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"date": day, "since": since, "written": hhmmss}, f, ensure_ascii=False)
        os.replace(tmp, path)
        log(f"🕘 정규장 재평가 경계 기록: {since} 이후 계산된 점수만 매수에 사용")
    except Exception as e:
        log(f"   ⚠️ 정규장 재평가 경계 기록 실패(기본 09:01:00 적용): {e}")


def start_bots(mode):
    """
    mode에 따라 시장 연동 봇을 실행합니다.
      KRX / NXT / AFTER : main_stock + Update_Promising_Stocks
      (AFTER 는 inquiry·kiwoom_inquiry 에서 KRX 시장코드로 조회된다 — NXT 만 _NX/NX 분기.
       통합(SOR) 현재가는 각 봇이 kiwoom_inquiry.fetch_sor_price 로 별도 조회)
      ※ main_etf.py 는 실행하지 않습니다.
    """
    global running_procs

    stop_market_bots()  # 기존 시장 연동 봇 먼저 종료
    if mode == "KRX":
        _write_krx_session_marker()   # NXT 봇 종료 직후 = 정규장 재평가 경계

    env = os.environ.copy()
    env["MARKET_MODE"] = mode  # inquiry.py 에서 KRX/NXT 분기에 사용 (AFTER=KRX 시세)

    scripts = {
        "stock":  "main_stock.py",
        "update": "Update_Promising_Stocks.py",
    }
    log(f"🚀 봇 시작 (모드: {mode}) — 프로덕션 2개")

    for key, script in scripts.items():
        script_path = os.path.join(PROJECT_DIR, script)
        try:
            proc = subprocess.Popen(
                titled_cmd([script_path]),
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

    if current_mode not in MARKET_MODES:
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
                    titled_cmd([os.path.join(PROJECT_DIR, script)]),
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
            titled_cmd([script_path]),
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
            titled_cmd([script_path]),
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


# promote(텔레그램·리포트) → auto_buy 는 사이클 스레드와 Claude CLI 종료 스레드 양쪽에서
# 호출되므로, 동시 중복 실행을 막기 위해 전용 락으로 직렬화한다.
_promote_lock = threading.Lock()


def _run_promote_and_buy(tag):
    """promote → auto_buy 를 직렬 실행. 이미 다른 스레드가 돌고 있으면 이번 호출은 건너뛴다.
    (두 호출은 같은 results.json 을 처리하므로 중복 실행은 불필요하고, 대기하면
     사이클 스레드가 최대 20분 묶여 다음 사이클까지 밀린다.)"""
    if not _promote_lock.acquire(blocking=False):
        log(f"⏳ promote 진행 중 → {tag} 호출 건너뜀")
        return
    try:
        _run_eval_script("promote_evaluated.py", f"promote(후처리·{tag})")
        _run_eval_script("auto_buy.py",          f"auto_buy(체결/매수·{tag})")
    finally:
        _promote_lock.release()


# build_pending 은 10분 사이클과 즉시 트리거(run_fast_claude_trigger) 양쪽에서 호출되므로
# 동시 실행 시 Search_History 60+자동등록 행이 중복 append 되지 않도록 직렬화한다.
_pending_lock = threading.Lock()


def _run_build_pending(tag):
    with _pending_lock:
        _run_eval_script("build_pending.py", f"build_pending(신규탐지·{tag})")


def _eval_pipeline_worker():
    """build_pending → promote → auto_buy 순차 실행(백그라운드 스레드)."""
    try:
        log("📈 60+ 평가 파이프라인 사이클 시작")
        _run_build_pending("사이클")
        maybe_spawn_claude_eval()  # 미평가 60점+ 있으면 Claude CLI 즉시 기동(이벤트 구동)
        _run_promote_and_buy("사이클")
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
CLAUDE_EVAL_MODEL     = "claude-opus-5"   # 미지정 시 계정 기본값(2026-09-14 확인: opus-4-8)으로 돌아 명시 고정
CLAUDE_EVAL_EFFORT    = "high"
CLAUDE_EVAL_TIMEOUT   = 30 * 60   # CLI 평가 최대 30분
CLAUDE_RETRY_COOLDOWN = 30 * 60   # 동일 종목셋 재시도 최소 간격(실패 루프 방지)
_claude_lock       = threading.Lock()
_claude_last_codes = None
_claude_last_start = None


def _today_str():
    return datetime.now().strftime("%Y%m%d")


def _pending_rows(day):
    """오늘자 pending CSV → {code6: row dict}. 없거나 읽기 실패 시 {}."""
    import csv as _csv
    pend = os.path.join(PROJECT_DIR, "logs", f"{day}_claude_pending.csv")
    if not os.path.exists(pend):
        return {}
    try:
        with open(pend, encoding="utf-8-sig") as f:
            return {str(r["code"]).strip().zfill(6): r
                    for r in _csv.DictReader(f) if r.get("code")}
    except Exception:
        return {}


def _unevaluated_pending_codes():
    """오늘자 pending 중 eval_done/results 에 없는 미평가 코드 집합."""
    codes = set(_pending_rows(_today_str()))
    if not codes:
        return set()
    return codes - _evaluated_codes(_today_str())


def _evaluated_codes(day):
    """오늘자 eval_done.json + claude_results.json 에 기록된 평가완료 코드 집합."""
    import json as _json
    logs = os.path.join(PROJECT_DIR, "logs")
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
    return done


def _results_sig(day):
    """오늘자 claude_results.json 의 (mtime, size). 없으면 None."""
    p = os.path.join(PROJECT_DIR, "logs", f"{day}_claude_results.json")
    try:
        st = os.stat(p)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def _claude_eval_worker(codes):
    """Claude Code CLI 헤드리스로 AI 평가 실행(별도 스레드, 락 보유 상태로 진입).

    CLI 종료 후 results.json 이 갱신됐으면 다음 10분 사이클을 기다리지 않고
    곧바로 promote(텔레그램·리포트) → auto_buy 를 실행한다.
    """
    day = _today_str()
    before_sig = _results_sig(day)
    rc = None
    try:
        log(f"🤖 Claude CLI 평가 시작: {len(codes)}종목 {sorted(codes)} "
            f"[{CLAUDE_EVAL_MODEL}/{CLAUDE_EVAL_EFFORT}]")
        # 기준점 돌파 시점의 V3 점수(pending 스냅샷)를 프롬프트에 박아 둔다.
        # V3 점수는 수 분 내 급락할 수 있어(2026-09-14 루닛 66.5→39, 3분 만에),
        # CLI 가 Stock_V3 를 다시 읽는 시점 점수로 대상에서 누락시키지 않게 한다.
        prow = _pending_rows(day)
        targets = "; ".join(
            f"{c} {prow.get(c, {}).get('name', '')} score_total={prow.get(c, {}).get('score_total', '?')}"
            for c in sorted(codes))
        prompt = (
            f"오늘({day}) 60점+ 종목 AI 평가를 CLAUDE.md 지침 그대로 수행하라. "
            f"대상: logs/{day}_Stock_V3.csv 의 score_total>=0.60 중 미평가 종목"
            f"(60점+ 가 0개면 CLAUDE.md 보조매수 규칙대로 score_total>=0.40 중 미평가 종목). "
            f"필수 평가 종목(pending 스냅샷 — 기준점 돌파 시점 점수): {targets}. "
            f"필수 평가 종목은 평가 시점 Stock_V3 의 score_total 이 기준 미만으로 떨어졌어도 기평가가 아니면 "
            f"반드시 평가하고, 그 경우 v3_score/v3_score100 은 위 스냅샷 score_total 값을 사용하라. "
            f"결과는 logs/{day}_claude_results.json 에 스키마대로 저장 후 종료. "
            f"전부 기평가면 아무 파일도 쓰지 말고 한 줄 보고 후 종료."
        )
        logf = os.path.join(PROJECT_DIR, "logs", f"{day}_claude_cli_eval.log")
        with open(logf, "a", encoding="utf-8") as lf:
            lf.write(f"\n===== {datetime.now()} codes={sorted(codes)} =====\n")
            lf.flush()
            r = subprocess.run(
                [CLAUDE_EXE, "-p", prompt,
                 "--model", CLAUDE_EVAL_MODEL,
                 "--effort", CLAUDE_EVAL_EFFORT,
                 "--permission-mode", "acceptEdits",
                 "--allowedTools", "Read,Write,Edit,Glob,Grep,WebSearch,WebFetch"],
                cwd=PROJECT_DIR, stdout=lf, stderr=subprocess.STDOUT,
                timeout=CLAUDE_EVAL_TIMEOUT,
            )
        rc = r.returncode
        log(f"🤖 Claude CLI 평가 종료 rc={rc}")
    except subprocess.TimeoutExpired:
        log(f"⚠️ Claude CLI 평가 타임아웃({CLAUDE_EVAL_TIMEOUT}s)")
    except Exception as e:
        log(f"❌ Claude CLI 평가 실패: {e}")
    finally:
        # 락은 promote 전에 반드시 해제(promote 가 수 분 걸려도 다음 평가 기동을 막지 않도록)
        _claude_lock.release()

    # ── CLI 평가 직후 즉시 후처리 (다음 사이클 대기 없음)
    #    이전에는 promote 가 10분 주기 사이클에서만 돌아, CLI 완료 → 텔레그램까지
    #    최대 10분 지연됐고 EVAL_END(16:30) 이후 완료분은 아예 발송되지 않았다.
    if rc == 0 and _results_sig(day) != before_sig:
        log("🤖 CLI 평가 결과 갱신 감지 → promote 즉시 실행")
        _run_promote_and_buy("CLI직후")
    elif rc == 0:
        log("🤖 CLI 평가 결과 변경 없음 → promote 생략(다음 사이클에 위임)")

    # 평가 도중 즉시 트리거가 새 종목을 pending 에 넣었지만 락 때문에 기동 못 했을 수 있다
    # → 다음 사이클(최대 10분)을 기다리지 않고 바로 이어서 기동. 동일 종목셋은 쿨다운이 막는다.
    maybe_spawn_claude_eval()


def maybe_spawn_claude_eval():
    """build_pending 직후 호출: 미평가 60점+ 있으면 Claude CLI 평가를 비동기 기동."""
    global _claude_last_codes, _claude_last_start
    if not os.path.exists(CLAUDE_EXE):
        return
    codes = _unevaluated_pending_codes()
    if not codes:
        return
    # 사이클·즉시트리거·CLI 종료 스레드에서 동시에 불리므로 쿨다운 판정·기록도 락 안에서 한다.
    if not _claude_lock.acquire(blocking=False):
        log("⏳ Claude CLI 평가 진행 중 → 이번 틱 건너뜀")
        return
    now = datetime.now()
    if (_claude_last_codes == codes and _claude_last_start
            and (now - _claude_last_start).total_seconds() < CLAUDE_RETRY_COOLDOWN):
        _claude_lock.release()
        return
    _claude_last_codes = set(codes)
    _claude_last_start = now
    threading.Thread(target=_claude_eval_worker, args=(codes,), daemon=True).start()


# ====================================================
# ⚡ 즉시 트리거 (2026-09-14 추가)
#    10분 사이클만으로는 60점 돌파 → CLI 기동까지 최대 10분 대기(09-14 루닛: 09:38 알림 → 09:44:41 기동).
#    메인 루프(10초)마다 Stock_V3.csv mtime 을 보고, 바뀌었을 때만 읽어 기준점(60, 60점+ 0개면 40)
#    이상 미평가 신규 종목이 있으면 build_pending → CLI 기동을 즉시 수행한다.
#    한 번 넘긴 종목은 당일 다시 즉시트리거하지 않는다(실패 재시도는 10분 사이클 + 쿨다운 담당).
# ====================================================
_fast_v3_mtime  = None
_fast_seen      = set()
_fast_day       = None
_fast_busy      = threading.Lock()


def _v3_codes_over_threshold(path):
    """Stock_V3.csv 에서 pending 기준점(buy_gate: 60, 60점+ 0개면 40) 이상 코드 집합."""
    import csv as _csv
    rows = []
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in _csv.DictReader(f):
                if not r.get("code"):
                    continue
                try:
                    s = float(r.get("score_total") or 0)
                except (TypeError, ValueError):
                    continue
                rows.append((str(r["code"]).split(".")[0].strip().zfill(6), s))
    except Exception:
        return set()   # 다른 프로세스가 쓰는 중일 수 있음 → 다음 mtime 변경 때 재시도
    scores = [s for _, s in rows]
    try:
        import buy_gate
        thr = buy_gate.LOW_MIN if buy_gate.low_mode(scores) else buy_gate.PRIMARY_MIN
    except Exception:
        thr = 0.60
    return {c for c, s in rows if s >= thr}


def _fast_trigger_worker(new_codes):
    try:
        log(f"⚡ 즉시 트리거: 신규 기준점+ {sorted(new_codes)} → build_pending → CLI 기동")
        _run_build_pending("즉시")
        maybe_spawn_claude_eval()
    finally:
        _fast_busy.release()


def run_fast_claude_trigger():
    global _fast_v3_mtime, _fast_seen, _fast_day
    now = datetime.now()
    if now.weekday() >= 5 or not (EVAL_START <= now.time() <= EVAL_END):
        return
    day = now.strftime("%Y%m%d")
    if day != _fast_day:
        _fast_day, _fast_seen, _fast_v3_mtime = day, set(), None
    path = os.path.join(PROJECT_DIR, "logs", f"{day}_Stock_V3.csv")
    try:
        m = os.stat(path).st_mtime
    except OSError:
        return
    if m == _fast_v3_mtime or _fast_busy.locked():
        return
    new_codes = _v3_codes_over_threshold(path) - _evaluated_codes(day) - _fast_seen
    _fast_v3_mtime = m
    if not new_codes:
        return
    try:
        from kis_api import kiwoom_trading as _kt
        if day in _kt.KRX_HOLIDAYS:
            return
    except Exception:
        pass
    if not _fast_busy.acquire(blocking=False):
        return
    _fast_seen |= new_codes
    threading.Thread(target=_fast_trigger_worker, args=(new_codes,), daemon=True).start()




OPENKICK_END      = dtime(9, 20)   # 개장 킥 반복 종료(첫 정규장 스캔·40점 보조모드 판정까지 커버)
OPENKICK_INTERVAL = 60             # 개장 킥 반복 간격(초)
_openkick_last    = 0.0
_openkick_busy    = threading.Event()


def _openkick_worker():
    # 동시 auto_buy 직렬화는 auto_buy 내부 파일락이 담당(프로세스 간). promote 락은 잡지 않는다
    # — 잡으면 CLI직후/10분 사이클 promote 가 통째로 건너뛰어진다.
    try:
        _run_eval_script("auto_buy.py", "auto_buy(개장킥)")
    finally:
        _openkick_busy.clear()


def run_autobuy_open_kick():
    """개장 직후(09:00:30~09:20) auto_buy 를 60초마다 실행해 '정규장 재평가' 점수가 나오는 즉시
    plan 에 반영한다(2026-09-29 개정 — 1회 킥은 재평가 전이라 장전 점수로 plan 이 만들어졌음).
    auto_buy 는 재평가 전 종목을 보류(⏳)하므로, 장전 60점+ 종목은 재평가 60점+ 유지 시에만
    그 시점 점수 비중으로 편입된다. 평가 파이프라인(10분 주기)만으론 개장 후 ~15분 plan 부재였던
    문제도 함께 막는다. auto_buy 는 종목구성·비중·보류사유가 바뀔 때만 알리므로(가격 변동에 따른
    목표수량 미세변동은 무알림) 반복해도 텔레그램 도배 없음. 동시 실행은 auto_buy 파일락이 직렬화."""
    global _openkick_last
    now = datetime.now()
    if now.weekday() >= 5:
        return
    if not (dtime(9, 0, 30) <= now.time() <= OPENKICK_END):
        return
    try:
        from kis_api import kiwoom_trading as _kt
        if now.strftime("%Y%m%d") in _kt.KRX_HOLIDAYS:
            return
    except Exception:
        pass
    if _openkick_busy.is_set() or time.time() - _openkick_last < OPENKICK_INTERVAL:
        return
    _openkick_busy.set()
    _openkick_last = time.time()
    log("⚡ 개장 킥: auto_buy 실행(정규장 재평가 반영 plan 갱신)")
    threading.Thread(target=_openkick_worker, daemon=True).start()


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
    start_paper_trader()

    data_updated_today = False  # 하루에 1번만 Update_Data_All 실행
    holiday_today      = False  # 공휴일 플래그 — WAITING/WEEKEND 복귀 시 해제

    while True:
        mode = get_market_mode()

        # ── 날짜가 바뀌면 업데이트 플래그 초기화
        if mode in MARKET_MODES and data_updated_today:
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
            ensure_paper_trader_alive()
            time.sleep(POLL_INTERVAL)
            continue

        # ── 모드가 바뀔 때만 시장 연동 봇을 재시작
        if mode != current_mode:
            log(f"📌 모드 전환: {current_mode} → {mode}")
            current_mode = mode

            if mode in MARKET_MODES:
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
        ensure_paper_trader_alive()  # paper_trader (항상, 가상매매 기록)
        ensure_viewer_alive()        # Stock_V3 웹 뷰어 (항상, 읽기 전용)

        # ── 60점+ 평가 파이프라인 (평일 08:00~16:30, 10분마다 / 백그라운드)
        run_eval_pipeline_if_due()
        run_fast_claude_trigger()    # Stock_V3 에 신규 60점+ 등장 시 10분 사이클 기다리지 않고 즉시 CLI 평가
        run_autobuy_open_kick()     # 개장(09:00) 직후 auto_buy 1회 즉시 실행 → plan 선생성

        # ── 현재 상태 주기적 출력 (폴링 주기 내 첫 번째 틱)
        if datetime.now().second < POLL_INTERVAL:
            if mode in MARKET_MODES:
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