# kis_api/kiwoom_auth.py
import requests
import json
import os
import sys
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import secrets

KIWOOM_URL_BASE  = "https://api.kiwoom.com"
KIWOOM_TOKEN_FILE = "kiwoom_token.dat"

# 이 프로세스가 마지막으로 반환한 토큰 — 8005(Token 무효) 복구 시 "파일 토큰이
# 방금 실패한 토큰과 같은지" 판별용(재발급 핑퐁 방지, refresh_after_auth_fail 참조)
_last_returned = None


def _read_token_file(token_path):
    """토큰 파일 읽기 → (token, expires_dt) 또는 (None, None). 만료 검사는 안 함."""
    try:
        with open(token_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('token'), data.get('expires_dt', '')
    except Exception:
        return None, None


def _is_alive(expires_dt):
    """expires_dt("YYYYMMDDHHMMSS") 가 아직 미래인가"""
    try:
        return datetime.now() < datetime.strptime(expires_dt, "%Y%m%d%H%M%S")
    except Exception:
        return False


def get_access_token(force_new=False):
    global _last_returned
    token_path = os.path.join(os.path.dirname(__file__), KIWOOM_TOKEN_FILE)

    if not force_new:
        token, exp_dt = _read_token_file(token_path)
        if token and _is_alive(exp_dt):
            _last_returned = token
            return token
        if token is None and os.path.exists(token_path):
            print("⚠️ [키움토큰] 기존 파일 읽기 실패")

    print("🔄 [키움토큰] 신규 발급 중...")
    url  = f"{KIWOOM_URL_BASE}/oauth2/token"
    body = {
        "grant_type": "client_credentials",
        "appkey":     secrets.KIWOOM_APP_KEY,
        "secretkey":  secrets.KIWOOM_APP_SECRET,
    }
    try:
        res = requests.post(
            url,
            headers={"content-type": "application/json;charset=UTF-8"},
            json=body,
            timeout=5,
        )
        if res.status_code == 200:
            d = res.json()
            if d.get("return_code") == 0:
                token  = d["token"]
                exp_dt = d["expires_dt"]
                # ── 최신 발급 승자(newest-wins) 기록 ──────────────────────────
                # 키움은 신규 발급 시 기존 토큰을 서버에서 폐기한다. 여러 프로세스가
                # 만료 시점(매일 08:00 경)에 동시 재발급하면, 나중에 발급된 토큰만
                # 유효한데 먼저 발급받은 쪽이 파일을 나중에 덮어써 "폐기된 토큰"이
                # 남는 사고가 남(2026-08-31 8005 장애). 파일에 더 늦게 발급된
                # 토큰(expires_dt 가 더 큼)이 이미 있으면 그쪽을 쓴다.
                cur_token, cur_exp = _read_token_file(token_path)
                if cur_token and cur_exp > exp_dt:
                    print(f"↩️ [키움토큰] 더 늦게 발급된 파일 토큰 사용 (만료: {cur_exp})")
                    _last_returned = cur_token
                    return cur_token
                with open(token_path, 'w', encoding='utf-8') as f:
                    json.dump({"token": token, "expires_dt": exp_dt}, f)
                print(f"✅ [키움토큰] 발급 완료 (만료: {exp_dt})")
                _last_returned = token
                return token
        print(f"❌ [키움토큰] 발급 실패: {res.text}")
    except Exception as e:
        print(f"❌ [키움토큰] 오류: {e}")
    return None


def refresh_after_auth_fail():
    """API 가 return_code=3(8005 Token 무효)을 반환했을 때 호출.
    ① 파일에 '방금 실패한 것과 다른' 유효기간 내 토큰이 있으면 그것을 사용
       (다른 프로세스가 더 늦게 발급한 유효 토큰 — 무조건 재발급하면 서로의
       토큰을 폐기하는 핑퐁이 나므로 파일 우선).
    ② 없으면 강제 재발급.
    반환: 토큰 또는 None"""
    global _last_returned
    token_path = os.path.join(os.path.dirname(__file__), KIWOOM_TOKEN_FILE)
    token, exp_dt = _read_token_file(token_path)
    if token and token != _last_returned and _is_alive(exp_dt):
        _last_returned = token
        return token
    return get_access_token(force_new=True)
