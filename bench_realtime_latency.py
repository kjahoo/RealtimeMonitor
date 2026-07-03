# bench_realtime_latency.py
# 한투(KIS) vs 키움 REST 실시간 시세 수신 속도(왕복 지연) 실측 비교.
# 읽기 전용(현재가 조회)만 수행. 토큰은 사전 발급해 타이밍에서 제외.
#   KIS   : inquire-price (FHKST01010100)  GET
#   키움  : ka10001 주식기본정보요청        POST  → cur_prc
import time
import statistics
import requests
import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from config import secrets
from kis_api import auth, kiwoom_auth

SYMBOLS = ["005930", "000660", "035720", "247540", "042700"]  # 삼성전자/SK하이닉스/카카오/에코프로비엠/한미반도체
N = 20  # 종목당 반복

KIS_URL    = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-price"
KIWOOM_URL = "https://api.kiwoom.com/api/dostk/stkinfo"


def kis_call(token, code):
    headers = {
        "authorization": f"Bearer {token}",
        "appkey":        secrets.APP_KEY,
        "appsecret":     secrets.APP_SECRET,
        "tr_id":         "FHKST01010100",
        "custtype":      "P",
    }
    params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code}
    t0 = time.perf_counter()
    res = requests.get(KIS_URL, headers=headers, params=params, timeout=10)
    dt = (time.perf_counter() - t0) * 1000
    ok = res.status_code == 200 and res.json().get("rt_cd") == "0"
    return dt, ok


def kiwoom_call(token, code):
    headers = {
        "authorization": f"Bearer {token}",
        "api-id":        "ka10001",
        "content-type":  "application/json;charset=UTF-8",
    }
    body = {"stk_cd": code}
    t0 = time.perf_counter()
    res = requests.post(KIWOOM_URL, headers=headers, json=body, timeout=10)
    dt = (time.perf_counter() - t0) * 1000
    ok = res.status_code == 200 and res.json().get("return_code") == 0
    return dt, ok


def stats(label, samples):
    if not samples:
        print(f"  {label:8s}: 측정 실패 (성공 응답 0건)")
        return
    samples_sorted = sorted(samples)
    p95 = samples_sorted[min(len(samples_sorted) - 1, int(len(samples_sorted) * 0.95))]
    print(f"  {label:8s}: n={len(samples):3d}  "
          f"min={min(samples):6.1f}  avg={statistics.mean(samples):6.1f}  "
          f"median={statistics.median(samples):6.1f}  p95={p95:6.1f}  max={max(samples):6.1f}  (ms)")


def main():
    print("🔑 토큰 발급 중 (타이밍 제외)...")
    kis_token    = auth.get_access_token()
    kiwoom_token = kiwoom_auth.get_access_token()
    if not kis_token or not kiwoom_token:
        print(f"❌ 토큰 발급 실패  KIS={bool(kis_token)} 키움={bool(kiwoom_token)}")
        return

    # 워밍업 (연결 수립/DNS 캐시 — 측정 제외)
    for code in SYMBOLS[:1]:
        kis_call(kis_token, code)
        kiwoom_call(kiwoom_token, code)

    kis_all, kiwoom_all = [], []
    kis_fail = kiwoom_fail = 0

    print(f"\n⏱️  벤치마크 시작: {len(SYMBOLS)}종목 × {N}회 (인터리브)\n")
    for code in SYMBOLS:
        kis_s, kw_s = [], []
        for _ in range(N):
            dt, ok = kis_call(kis_token, code)
            if ok: kis_s.append(dt); kis_all.append(dt)
            else:  kis_fail += 1

            dt, ok = kiwoom_call(kiwoom_token, code)
            if ok: kw_s.append(dt); kiwoom_all.append(dt)
            else:  kiwoom_fail += 1
        print(f"[{code}]")
        stats("KIS", kis_s)
        stats("키움", kw_s)
        print()

    print("=" * 60)
    print(f"📊 전체 집계  (KIS 실패 {kis_fail} / 키움 실패 {kiwoom_fail})")
    stats("KIS", kis_all)
    stats("키움", kiwoom_all)
    if kis_all and kiwoom_all:
        diff = statistics.mean(kis_all) - statistics.mean(kiwoom_all)
        faster = "키움" if diff > 0 else "KIS"
        print(f"\n➡️  평균 기준 {faster}가 {abs(diff):.1f}ms 더 빠름 "
              f"({abs(diff)/max(statistics.mean(kis_all),statistics.mean(kiwoom_all))*100:.0f}% 차)")


if __name__ == "__main__":
    main()
