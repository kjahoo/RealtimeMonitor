import os
import shutil
import time
from kis_api import inquiry, auth

# ====== 설정 ======
BASE_DIR = r"C:\Projects\RealtimeMonitor\Data"
STOCK_DIR = os.path.join(BASE_DIR, "Stock")
ETF_DIR = os.path.join(BASE_DIR, "ETF")


def organize_files():
    print("🧹 [데이터 정리] 파일을 종류별로 자동 분류합니다...")

    # 토큰 확보
    if not auth.get_access_token():
        print("❌ 토큰 발급 실패")
        return

    # csv 파일 목록 가져오기 (이미 폴더에 들어간 파일은 제외)
    files = [f for f in os.listdir(BASE_DIR) if f.startswith("A") and f.endswith(".csv")]
    total = len(files)

    print(f"📂 총 {total}개 파일 분류 시작 (약 {total * 0.1 / 60:.1f}분 소요 예상)")

    count_stock = 0
    count_etf = 0
    errors = 0

    for idx, filename in enumerate(files, 1):
        code = filename[1:7]  # 'A005930.csv' -> '005930'
        src_path = os.path.join(BASE_DIR, filename)

        try:
            # API로 종류 확인
            kind = inquiry.fetch_stock_kind(code)

            if kind in ["ETF", "ETN"]:
                dst_path = os.path.join(ETF_DIR, filename)
                shutil.move(src_path, dst_path)
                count_etf += 1
                # print(f"   🚚 [ETF] {filename} 이동 완료")
            else:
                dst_path = os.path.join(STOCK_DIR, filename)
                shutil.move(src_path, dst_path)
                count_stock += 1
                # print(f"   🚚 [Stock] {filename} 이동 완료")

            # 진행률 표시 (10개마다)
            if idx % 10 == 0:
                print(f"   ⏳ {idx}/{total} 분류 중... (주식: {count_stock}, ETF: {count_etf})")

            # API 호출 제한 방지 (0.05초 대기)
            time.sleep(0.05)

        except Exception as e:
            print(f"   ⚠️ {filename} 이동 실패: {e}")
            errors += 1

    print(f"\n✅ 정리 완료!")
    print(f"   - 주식: {count_stock}개 -> Data/Stock")
    print(f"   - ETF : {count_etf}개 -> Data/ETF")
    print(f"   - 실패: {errors}개")


if __name__ == "__main__":
    # 안전장치: 폴더가 없으면 만듦
    os.makedirs(STOCK_DIR, exist_ok=True)
    os.makedirs(ETF_DIR, exist_ok=True)

    organize_files()