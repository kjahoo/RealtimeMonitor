"""
자동 매도 기능 테스트 스크립트
- 토큰 / 잔고 조회 / 로직 계산은 실제로 실행
- 실제 주문(place_sell_order)은 DRY-RUN으로 대체 (주문 미제출)
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from kis_api import auth, trading
from config import secrets

SEPARATOR = "-" * 55


# ====================================================
# 1. 토큰 확인
# ====================================================
def test_token():
    print(f"\n{'='*55}")
    print("[ 1 ] 토큰 확인")
    print(SEPARATOR)
    token = auth.get_access_token()
    if token:
        print(f"  ✅ 토큰 정상 (앞 20자: {token[:20]}...)")
    else:
        print("  ❌ 토큰 발급 실패")
    return bool(token)


# ====================================================
# 2. 잔고 조회
# ====================================================
def test_holdings(codes):
    print(f"\n{'='*55}")
    print("[ 2 ] 잔고 조회 (현금/담보 구분)")
    print(SEPARATOR)
    any_found = False
    for code in codes:
        holdings = trading.fetch_stock_holdings(code)
        if holdings:
            any_found = True
            for h in holdings:
                label = f"[{h['order_type']}]"
                if h["loan_dt"]:
                    label += f" 대출일:{h['loan_dt']} 대출금:{h['loan_amt']:,}원"
                print(f"  ✅ [{code}] {label}")
                print(f"       보유:{h['qty']}주 | 매입평균가:{h['avg_buy_price']:,.0f}원 | "
                      f"매입금액:{h['purchase_amount']:,}원 | 매도가능:{h['sell_possible_qty']}주")
        else:
            print(f"  ─  [{code}] 미보유")
    if not any_found:
        print("  ℹ️  조회 종목 중 보유 종목 없음")
    return any_found


# ====================================================
# 3. 점수 → 보유 목표 금액 로직
# ====================================================
def test_keep_amount_logic():
    print(f"\n{'='*55}")
    print("[ 3 ] 점수별 보유 목표 금액 계산")
    print(SEPARATOR)
    test_scores = [0.42, 0.39, 0.34, 0.29, 0.24, 0.10]
    for score in test_scores:
        keep = trading.get_keep_amount(score)
        if keep is None:
            label = "매도 불필요"
        elif keep == 0:
            label = "전량매도"
        else:
            label = f"{keep:,}원 보유"
        print(f"  점수 {score:.2f}  →  {label}")


# ====================================================
# 4. 매도 수량 계산 (가상 잔고)
# ====================================================
def test_calc_sell_qty():
    print(f"\n{'='*55}")
    print("[ 4 ] 매도 수량 계산 (가상 잔고 시뮬레이션 — 현금+담보 혼합)")
    print(SEPARATOR)
    # 현금 600주 + 담보 400주 합산 30,000,000원 시나리오
    mock_holdings = [
        {"qty": 600, "sell_possible_qty": 600, "avg_buy_price": 30000.0,
         "purchase_amount": 18_000_000, "loan_dt": "",         "order_type": "현금"},
        {"qty": 400, "sell_possible_qty": 400, "avg_buy_price": 30000.0,
         "purchase_amount": 12_000_000, "loan_dt": "20250101", "order_type": "담보"},
    ]
    total_amt = sum(h["purchase_amount"] for h in mock_holdings)
    print(f"  가상 보유: 현금 600주 + 담보 400주 = 합계 {total_amt:,}원")
    print()
    import math
    for score in [0.39, 0.34, 0.29, 0.24]:
        keep = trading.get_keep_amount(score)
        label = "전량매도" if keep == 0 else f"{keep:,}원 보유"
        remaining = total_amt - keep
        print(f"  점수 {score:.2f} ({label}):")
        # 담보 먼저
        sorted_h = sorted(mock_holdings, key=lambda h: (not bool(h["loan_dt"]), h["loan_dt"]))
        for h in sorted_h:
            if remaining <= 0:
                break
            avg_p = h["avg_buy_price"]
            sell_qty = min(math.ceil(remaining / avg_p), h["sell_possible_qty"]) if keep > 0 else h["sell_possible_qty"]
            print(f"    [{h['order_type']}] 매도 {sell_qty}주")
            remaining -= sell_qty * avg_p
        print()


# ====================================================
# 5. auto_sell DRY-RUN (실제 주문 미제출)
# ====================================================
def test_auto_sell_dryrun(codes):
    print(f"\n{'='*55}")
    print("[ 5 ] auto_sell DRY-RUN (잔고 조회만, 주문 미제출)")
    print(SEPARATOR)

    original_place = trading.place_sell_order

    def mock_place(code, qty, price, loan_dt=""):
        order_type = "담보" if loan_dt else "현금"
        print(f"       🔕 [DRY-RUN] 실제 주문 미제출 — [{order_type}] {code} {qty}주 × {price:,}원")
        return {"rt_cd": "0", "output": {"ODNO": f"DRY-RUN-{order_type}"}}

    trading.place_sell_order = mock_place

    # 매도 신호 발생 시나리오별 테스트
    scenarios = [
        (0.39, "0.40선 이탈 → 2000만 보유"),
        (0.34, "0.35선 이탈 → 1500만 보유"),
        (0.29, "0.30선 이탈 → 1000만 보유"),
        (0.22, "0.25선 이탈 → 전량매도"),
    ]

    for code in codes:
        holdings = trading.fetch_stock_holdings(code)
        if not holdings:
            print(f"  ─  [{code}] 미보유, DRY-RUN 생략")
            continue
        total_amt = sum(h["purchase_amount"] for h in holdings)
        types = " + ".join(f"{h['order_type']} {h['qty']}주" for h in holdings)
        print(f"\n  종목 [{code}] — 실제 잔고: {types} | 매입금액 합계: {total_amt:,}원")
        for score, label in scenarios:
            print(f"    시나리오: {label} (점수={score})")
            result = trading.auto_sell(code, f"종목{code}", score, 50000)
            if result:
                print(f"      status : {result['status']}")
                for line in result["msg"].split("\n"):
                    print(f"      {line}")
            print()

    trading.place_sell_order = original_place


# ====================================================
# 메인
# ====================================================
if __name__ == "__main__":
    # 오늘 Search_History에 있는 종목 코드로 테스트
    TARGET_CODES = ["039560", "264450", "389030", "101490"]

    ok = test_token()
    if not ok:
        print("\n❌ 토큰 실패 — 이후 테스트 중단")
        sys.exit(1)

    test_holdings(TARGET_CODES)
    test_keep_amount_logic()
    test_calc_sell_qty()
    test_auto_sell_dryrun(TARGET_CODES)

    print(f"\n{'='*55}")
    print("테스트 완료")
    print("  ⚠️  실제 주문은 발생하지 않았습니다.")
    print(f"{'='*55}\n")