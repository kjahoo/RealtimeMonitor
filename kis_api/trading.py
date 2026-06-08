import math
import requests
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import secrets
from kis_api import auth


def _headers(tr_id):
    token = auth.get_access_token()
    return {
        "authorization": f"Bearer {token}",
        "appkey":        secrets.APP_KEY,
        "appsecret":     secrets.APP_SECRET,
        "tr_id":         tr_id,
        "custtype":      "P",
        "content-type":  "application/json; charset=utf-8",
    }


def _get_hashkey(body):
    url = f"{secrets.URL_BASE}/uapi/hashkey"
    headers = {
        "appkey":       secrets.APP_KEY,
        "appsecret":    secrets.APP_SECRET,
        "content-type": "application/json; charset=utf-8",
    }
    try:
        res = requests.post(url, headers=headers, json=body, timeout=5)
        if res.status_code == 200:
            return res.json().get("HASH")
    except Exception as e:
        print(f"   ❌ HASHKEY 발급 오류: {e}")
    return None


# ====================================================
# 💼 잔고 조회
#    같은 종목이 현금/담보로 나뉘어 있으면 각각 반환
# ====================================================
def fetch_stock_holdings(code):
    """
    반환: list of {
        "qty"              : 보유수량,
        "sell_possible_qty": 매도가능수량,
        "avg_buy_price"    : 매입평균가(float),
        "purchase_amount"  : 매입금액(int),
        "loan_dt"          : 대출일자 str ("" = 현금, "YYYYMMDD" = 담보),
        "loan_amt"         : 대출금액(int),
        "order_type"       : "현금" | "담보",
    }
    보유 없으면 None
    """
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-balance"
    params = {
        "CANO":                  secrets.CANO,
        "ACNT_PRDT_CD":          secrets.ACNT_PRDT_CD,
        "AFHR_FLPR_YN":          "N",
        "OFL_YN":                "N",
        "INQR_DVSN":             "02",
        "UNPR_DVSN":             "01",
        "FUND_STTL_ICLD_YN":     "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN":             "01",
        "CTX_AREA_FK100":        "",
        "CTX_AREA_NK100":        "",
    }
    try:
        res = requests.get(url, headers=_headers("TTTC8434R"), params=params, timeout=5)
        if res.status_code != 200:
            return None
        data = res.json()
        if data.get("rt_cd") != "0":
            return None

        holdings = []
        for item in data.get("output1", []):
            if item.get("pdno") != code:
                continue
            qty = int(item.get("hldg_qty", 0))
            if qty <= 0:
                continue
            loan_dt = item.get("loan_dt", "").strip()
            holdings.append({
                "qty":               qty,
                "sell_possible_qty": int(item.get("ord_psbl_qty", 0)),
                "avg_buy_price":     float(item.get("pchs_avg_pric", 0)),
                "purchase_amount":   int(item.get("pchs_amt", 0)),
                "loan_dt":           loan_dt,
                "loan_amt":          int(item.get("loan_amt", 0) or 0),
                "order_type":        "담보" if loan_dt else "현금",
            })
        return holdings if holdings else None
    except Exception as e:
        print(f"   ❌ 잔고 조회 오류 [{code}]: {e}")
    return None


# ====================================================
# 📉 지정가 매도 주문
#    loan_dt 있으면 담보대출 매도, 없으면 현금 매도
# ====================================================
def place_sell_order(code, qty, price, loan_dt=""):
    if qty <= 0:
        return None

    url  = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/trading/order-cash"
    body = {
        "CANO":         secrets.CANO,
        "ACNT_PRDT_CD": secrets.ACNT_PRDT_CD,
        "PDNO":         code,
        "ORD_DVSN":     "00",      # 지정가
        "ORD_QTY":      str(qty),
        "ORD_UNPR":     str(price),
    }
    if loan_dt:
        body["LOAN_DT"] = loan_dt

    hashkey = _get_hashkey(body)
    headers = _headers("TTTC0801U")
    if hashkey:
        headers["hashkey"] = hashkey

    try:
        res = requests.post(url, headers=headers, json=body, timeout=5)
        if res.status_code == 200:
            return res.json()
        print(f"   ❌ 매도 주문 HTTP 오류: {res.status_code}")
    except Exception as e:
        print(f"   ❌ 매도 주문 오류 [{code}]: {e}")
    return None


# ====================================================
# 📋 미체결 매도 주문 조회 (정정·취소 가능 주문)
# ====================================================
def fetch_open_sell_orders(code):
    """
    반환: list of {
        "order_no", "qty", "filled_qty", "remaining_qty", "price", "loan_dt"
    } or []
    """
    url = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
    params = {
        "CANO":           secrets.CANO,
        "ACNT_PRDT_CD":   secrets.ACNT_PRDT_CD,
        "CTX_AREA_FK200": "",
        "CTX_AREA_NK200": "",
        "INQR_DVSN_1":    "0",   # 전체 (코드 레벨에서 매도 필터)
        "INQR_DVSN_2":    "0",   # 전체
    }
    try:
        res = requests.get(url, headers=_headers("TTTC8036R"), params=params, timeout=5)
        if res.status_code != 200:
            print(f"   ❌ 미체결 주문 조회 HTTP 오류 [{code}]: {res.status_code}")
            return []
        data = res.json()
        if data.get("rt_cd") != "0":
            print(f"   ❌ 미체결 주문 조회 API 오류 [{code}]: {data.get('msg1', '')}")
            return []
        orders = []
        for item in data.get("output", []):
            if item.get("pdno") != code:
                continue
            # 매도 주문만 필터 (01 또는 1)
            if item.get("sll_buy_dvsn_cd", "01") not in ("01", "1"):
                continue
            remaining = int(item.get("rmn_qty", 0) or 0)
            if remaining <= 0:
                continue
            orders.append({
                "order_no":      item.get("odno", ""),
                "qty":           int(item.get("ord_qty", 0) or 0),
                "filled_qty":    int(item.get("tot_ccld_qty", 0) or 0),
                "remaining_qty": remaining,
                "price":         int(item.get("ord_unpr", 0) or 0),
                "loan_dt":       item.get("loan_dt", "").strip(),
            })
        return orders
    except Exception as e:
        print(f"   ❌ 미체결 주문 조회 오류 [{code}]: {e}")
    return []


# ====================================================
# ❌ 주문 취소
# ====================================================
def cancel_order(order_no, code, qty, loan_dt=""):
    """반환: True(성공) / False(실패)"""
    url  = f"{secrets.URL_BASE}/uapi/domestic-stock/v1/trading/order-rvsecncl"
    body = {
        "CANO":                secrets.CANO,
        "ACNT_PRDT_CD":        secrets.ACNT_PRDT_CD,
        "KRX_FWDG_ORD_ORGNO": "",
        "ORGN_ODNO":           order_no,
        "ORD_DVSN":            "00",
        "RVSE_CNCL_DVSN_CD":   "02",   # 02 = 취소
        "ORD_QTY":             str(qty),
        "ORD_UNPR":            "0",
        "QTY_ALL_ORD_YN":      "Y",
    }
    if loan_dt:
        body["LOAN_DT"] = loan_dt

    hashkey = _get_hashkey(body)
    headers = _headers("TTTC0803U")
    if hashkey:
        headers["hashkey"] = hashkey

    try:
        res = requests.post(url, headers=headers, json=body, timeout=5)
        if res.status_code == 200:
            return res.json().get("rt_cd") == "0"
        print(f"   ❌ 주문 취소 HTTP 오류: {res.status_code}")
    except Exception as e:
        print(f"   ❌ 주문 취소 오류 [{code}]: {e}")
    return False


# ====================================================
# 🔍 미체결 매도 주문 상태 확인 (정정요망 감지)
# ====================================================
def check_sell_order_status(code, curr_price):
    """
    반환:
      None  → 미체결 매도 주문 없음
      dict  → {"status": "정정요망"|"pending",
               "msg": str, "max_price": int, "orders": list}
    """
    orders = fetch_open_sell_orders(code)
    if not orders:
        return None
    max_price     = max(o["price"] for o in orders)
    order_summary = " / ".join(
        f"{o['remaining_qty']}주@{o['price']:,}원" for o in orders
    )
    if max_price > curr_price:
        msg = (f"⚠️ 정정요망\n"
               f"미체결 매도({order_summary})\n"
               f"주문가 {max_price:,}원 > 현재가 {curr_price:,}원")
        return {"status": "정정요망", "msg": msg, "max_price": max_price, "orders": orders}
    return {"status": "pending", "max_price": max_price, "orders": orders}


# ====================================================
# 🧮 점수 → 목표 보유 금액 (프로덕션 기준)
#    DROP_THRESHOLDS [0.40, 0.35, 0.30, 0.25, ...] 에 대응
# ====================================================
SELL_KEEP_TABLE = [
    (0.25,  0),           # score < 0.25 → 전량매도
    (0.30,  5_000_000),   # score < 0.30 → 500만원 보유
    (0.35,  10_000_000),  # score < 0.35 → 1000만원 보유
    (0.40,  20_000_000),  # score < 0.40 → 2000만원 보유
]


def get_keep_amount(total_score):
    """총점 → 매입금액 기준 남길 목표 금액 (None = 매도 불필요)"""
    for threshold, keep in SELL_KEEP_TABLE:
        if total_score < threshold:
            return keep
    return None


# ====================================================
# 🚀 자동 매도 실행
#    현금/담보 포지션을 분리해 각각 별도 주문
#    담보 포지션을 우선 매도 (이자 절감)
# ====================================================
def auto_sell(code, stock_name, total_score, curr_price, prev_open_orders=None):
    """
    반환 dict:
      status        : "ordered" | "already_pending" | "skipped" | "failed"
      msg           : 텔레그램/로그용 메시지 (already_pending 시 None)
      placed_orders : [{"order_no", "qty", "price", "order_type", "loan_dt"}, ...]
      keep_label    : "전량매도" | "N만원 보유"

    prev_open_orders: 레벨 변경 시 취소할 이전 주문 목록 (last_scores에서 전달)
    """
    keep_amount = get_keep_amount(total_score)
    if keep_amount is None:
        return None  # 매도 불필요

    # ── 레벨 변경 시 이전 주문 취소 (저장된 주문번호 사용) ─────────────
    cancelled_by_loan = {}   # {loan_dt: 취소 성공한 수량}
    cancel_lines = []
    for o in (prev_open_orders or []):
        ok = cancel_order(o["order_no"], code, o["qty"], o.get("loan_dt", ""))
        icon = "✅" if ok else "❌"
        cancel_lines.append(
            f"  {icon} 기존주문취소 {o['qty']}주×{o['price']:,}원"
        )
        if ok:
            loan = o.get("loan_dt", "")
            cancelled_by_loan[loan] = cancelled_by_loan.get(loan, 0) + o["qty"]

    all_holdings = fetch_stock_holdings(code)
    if not all_holdings:
        msg = f"📭 자동매도 건너뜀\n{stock_name}({code})\n보유 잔고 없음"
        if cancel_lines:
            msg += "\n" + "\n".join(cancel_lines)
        return {"status": "skipped", "code": code, "name": stock_name, "msg": msg}

    total_purchase_amt = sum(h["purchase_amount"] for h in all_holdings)
    if keep_amount > 0 and total_purchase_amt <= keep_amount:
        msg = (f"📭 자동매도 건너뜀\n{stock_name}({code})\n"
               f"이미 목표 이하 보유 ({total_purchase_amt:,.0f}원)")
        if cancel_lines:
            msg += "\n" + "\n".join(cancel_lines)
        return {"status": "skipped", "code": code, "name": stock_name, "msg": msg}

    keep_label = "전량매도" if keep_amount == 0 else f"{keep_amount // 10_000:,}만원 보유"

    # 담보 포지션 먼저, 이후 현금 순으로 매도
    sorted_holdings = sorted(all_holdings, key=lambda h: (not bool(h["loan_dt"]), h["loan_dt"]))

    remaining_to_sell = total_purchase_amt - keep_amount
    placed_orders = []
    failed_msgs   = []

    for h in sorted_holdings:
        if remaining_to_sell <= 0:
            break

        avg_p = h["avg_buy_price"]
        # 취소 성공한 수량을 가용수량에 합산 (API 갱신 지연 보정)
        avail = h["sell_possible_qty"] + cancelled_by_loan.get(h["loan_dt"], 0)
        if avg_p <= 0 or avail <= 0:
            continue

        if keep_amount == 0:
            sell_qty = avail
        else:
            sell_qty = min(math.ceil(remaining_to_sell / avg_p), avail)

        if sell_qty <= 0:
            continue

        result = place_sell_order(code, sell_qty, curr_price, h["loan_dt"])
        if result and result.get("rt_cd") == "0":
            order_no = result.get("output", {}).get("ODNO", "?")
            placed_orders.append({
                "order_no":   order_no,
                "qty":        sell_qty,
                "price":      curr_price,
                "order_type": h["order_type"],
                "loan_dt":    h["loan_dt"],
            })
            remaining_to_sell -= sell_qty * avg_p
        else:
            err = result.get("msg1", "응답 없음") if result else "응답 없음"
            failed_msgs.append(f"[{h['order_type']}] {err}")

    cancel_block = ("\n" + "\n".join(cancel_lines)) if cancel_lines else ""

    if not placed_orders:
        # 잔고는 있지만 매도가능수량이 0 → 기존 주문이 수량을 잠근 상태
        total_locked = sum(h["qty"] - h["sell_possible_qty"] for h in all_holdings)
        if total_locked > 0 and not failed_msgs:
            return {"status": "already_pending", "code": code, "name": stock_name,
                    "msg": None, "placed_orders": []}

        err_detail = " / ".join(failed_msgs) if failed_msgs else "응답 없음"
        return {"status": "failed", "code": code, "name": stock_name,
                "msg": f"❌ 자동매도 실패\n{stock_name}({code})\n{err_detail}{cancel_block}"}

    order_lines = "\n".join(
        f"  [{o['order_type']}] {o['qty']}주 × {o['price']:,}원  주문번호: {o['order_no']}"
        for o in placed_orders
    )
    msg = f"📤 매도 주문 접수\n{stock_name}({code})  [{keep_label}]\n{order_lines}{cancel_block}"
    if failed_msgs:
        msg += "\n⚠️ 일부 실패: " + " / ".join(failed_msgs)

    return {
        "status":        "ordered",
        "code":          code,
        "name":          stock_name,
        "msg":           msg,
        "placed_orders": placed_orders,
        "keep_label":    keep_label,
    }