# -*- coding: utf-8 -*-
"""
Claude CLI 로그인 상태 점검 (평일 아침 Windows 작업스케줄러 "ClaudeCLI_LoginCheck" 로 실행).
로그아웃 상태이면 텔레그램 + PC 토스트 알림을 발송한다.
사용법:  python check_claude_login.py [--test]   (--test = 로그인 상태여도 알림 강제 발송)
로그:   logs/claude_login_check.log
"""
import json
import subprocess
import sys
import datetime
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
CLAUDE_EXE = r"C:\Users\JH_Signature\.local\bin\claude.exe"
LOG_FILE = BASE / "logs" / "claude_login_check.log"

sys.path.insert(0, str(BASE))
from config import secrets  # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def log(msg: str):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def check_login():
    """claude auth status 를 실행해 (logged_in, detail) 을 반환한다."""
    try:
        r = subprocess.run(
            [CLAUDE_EXE, "auth", "status"],
            capture_output=True, text=True, timeout=60, encoding="utf-8",
        )
        out = (r.stdout or "") + (r.stderr or "")
        try:
            data = json.loads(out[out.index("{"): out.rindex("}") + 1])
            return bool(data.get("loggedIn")), out.strip()
        except Exception:
            return False, f"상태 출력 파싱 실패: {out.strip()[:200]}"
    except FileNotFoundError:
        return False, f"claude.exe 를 찾을 수 없음: {CLAUDE_EXE}"
    except Exception as e:
        return False, f"실행 오류: {e}"


def send_telegram(text: str):
    try:
        url = f"https://api.telegram.org/bot{secrets.TELEGRAM_BOT_TOKEN}/sendMessage"
        r = requests.post(
            url, data={"chat_id": secrets.TELEGRAM_CHAT_ID, "text": text}, timeout=15
        )
        ok = r.json().get("ok", False)
        log(f"텔레그램 발송 {'성공' if ok else '실패: ' + r.text[:200]}")
    except Exception as e:
        log(f"텔레그램 발송 오류: {e}")


def send_pc_alert(title: str, body: str):
    """PC 화면 알림. PowerShell 을 쓰지 않고 ctypes MessageBox 를 별도 프로세스로 띄운다
    (백신의 fileless PowerShell 탐지를 피하기 위함). 10분 후 자동으로 닫힌다."""
    code = (
        "import ctypes;"
        "ctypes.windll.user32.MessageBoxTimeoutW("
        f"0, {body!r}, {title!r}, 0x00051030, 0, 600000)"
    )  # MB_ICONWARNING|MB_SYSTEMMODAL|MB_SETFOREGROUND|MB_TOPMOST
    try:
        subprocess.Popen(
            [sys.executable, "-c", code],
            creationflags=0x08000008,  # CREATE_NO_WINDOW | DETACHED_PROCESS
        )
        log("PC 알림창 표시")
    except Exception as e:
        log(f"PC 알림 오류: {e}")


def main():
    force = "--test" in sys.argv
    logged_in, detail = check_login()
    log(f"점검 결과 loggedIn={logged_in} ({detail.splitlines()[0] if detail else ''})")

    if logged_in and not force:
        return

    prefix = "[테스트] " if (logged_in and force) else ""
    msg = (
        f"{prefix}⚠️ Claude CLI 로그아웃 감지\n"
        f"시각: {datetime.datetime.now():%Y-%m-%d %H:%M}\n"
        f"조치: PC에서 터미널을 열어 `claude auth login` 을 실행해 재로그인 필요.\n"
        f"참고: 재로그인 전까지는 백업 폴링(Cowork 예약작업)이 AI 평가를 대신 수행함."
    )
    send_telegram(msg)
    send_pc_alert(
        f"{prefix}Claude CLI 로그아웃 감지",
        "터미널에서 claude auth login 실행이 필요합니다.\n재로그인 전까지 백업 폴링이 평가를 대행합니다.",
    )


if __name__ == "__main__":
    main()
