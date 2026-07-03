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


def get_access_token():
    token_path = os.path.join(os.path.dirname(__file__), KIWOOM_TOKEN_FILE)

    if os.path.exists(token_path):
        try:
            with open(token_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # expires_dt 형식: "20241107083713" (YYYYMMDDHHMMSS)
            expire_time = datetime.strptime(data['expires_dt'], "%Y%m%d%H%M%S")
            if datetime.now() < expire_time:
                return data['token']
        except Exception as e:
            print(f"⚠️ [키움토큰] 기존 파일 읽기 실패: {e}")

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
                with open(token_path, 'w', encoding='utf-8') as f:
                    json.dump({"token": token, "expires_dt": exp_dt}, f)
                print(f"✅ [키움토큰] 발급 완료 (만료: {exp_dt})")
                return token
        print(f"❌ [키움토큰] 발급 실패: {res.text}")
    except Exception as e:
        print(f"❌ [키움토큰] 오류: {e}")
    return None