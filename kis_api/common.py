import requests
import time
import json


def call_api(url, params, headers, post_data=None):
    """
    API를 호출하고 결과를 반환하는 공통 함수
    - GET/POST 자동 구분
    - 에러 처리 포함
    """
    try:
        # post_data가 있으면 POST 요청, 없으면 GET 요청
        if post_data:
            resp = requests.post(url, headers=headers, json=post_data)
        else:
            resp = requests.get(url, headers=headers, params=params)

        if resp.status_code == 200:
            data = resp.json()
            # API 응답 코드 확인 (0: 정상, 0이 아니면 에러)
            if data.get('rt_cd') == '0':
                return data
            else:
                # 에러 메시지가 있으면 출력 (단, 장 종료 후 조회 등은 에러가 아닐 수 있음)
                msg = data.get('msg1', 'Unknown Error')
                # print(f"   ⚠️ API 응답: {msg} ({data.get('msg_cd')})")
                return data  # 에러라도 내용은 반환 (호출부에서 처리)
        else:
            print(f"❌ API HTTP 실패: {resp.status_code}")
            return None

    except Exception as e:
        print(f"❌ API 연결 에러: {e}")
        return None