# kis_api/auth.py
import requests
import json
import os
import sys
from datetime import datetime

# 상위 폴더의 설정을 불러오기 위해 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import secrets

TOKEN_FILE = "token.dat"  # 토큰을 잠시 저장해둘 파일


def get_access_token():
    """
    한국투자증권 접속 토큰을 발급받거나, 유효한 기존 토큰을 불러옵니다.
    """
    # 1. 기존에 발급받은 토큰이 있는지 확인
    token_path = os.path.join(os.path.dirname(__file__), TOKEN_FILE)

    if os.path.exists(token_path):
        try:
            with open(token_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # 토큰 만료 시간 확인 (여유 있게 1시간 전까지만 재사용)
            expire_time = datetime.strptime(data['expired'], "%Y-%m-%d %H:%M:%S")
            if datetime.now() < expire_time:
                # print(f"✅ [토큰] 기존 토큰 유효함 (만료: {expire_time})")
                return data['access_token']
        except Exception as e:
            print(f"⚠️ [토큰] 기존 토큰 파일 읽기 실패, 재발급 시도: {e}")

    # 2. 토큰 재발급 요청 (API 호출)
    print("🔄 [토큰] 신규 발급 요청 중...")
    url = f"{secrets.URL_BASE}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": secrets.APP_KEY,
        "appsecret": secrets.APP_SECRET
    }

    try:
        res = requests.post(url, headers=headers, data=json.dumps(body))

        if res.status_code == 200:
            res_data = res.json()
            access_token = res_data['access_token']
            expired_str = res_data['access_token_token_expired']  # 예: 2025-01-20 12:00:00

            # 파일에 저장
            save_data = {
                'access_token': access_token,
                'expired': expired_str
            }
            with open(token_path, 'w', encoding='utf-8') as f:
                json.dump(save_data, f)

            print(f"✅ [토큰] 신규 발급 완료 (만료: {expired_str})")
            return access_token
        else:
            print(f"❌ [토큰] 발급 실패: {res.text}")
            return None

    except Exception as e:
        print(f"❌ [토큰] 요청 중 오류 발생: {e}")
        return None