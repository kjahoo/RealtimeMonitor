# kis_api/kiwoom_trading.py
# 주문 API: 키움증권 REST (kt10001/kt10007/kt10003/kt10009)
# 잔고 조회: kt00018, 미체결 조회: ka10075
import math
import datetime
import requests
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import secrets
from kis_api import kiwoom_auth

KIWOOM_URL_BASE = "https://api.kiwoom.com"


# ====================================================
# ⏰ 정규장 시간 (평일 09:00~15:30)
#    이 시간 외에는 매도 본주문을 내지 않고 체크만 수행
# ====================================================
MARKET_OPEN  = datetime.time(9, 0)
MARKET_CLOSE = datetime.time(15, 30)


def is_market_open(now=None):
    """평일 정규장 시간(09:00~15:30) 여부"""
    now = now or datetime.datetime.now()
    if now.weekday() >= 5:           # 토(5)·일(6) 휴장
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def _pint(v):
    """0-padding 된 문자열 또는 숫자 → int"""
    try:
        return int(v or 0)
    except (ValueError, TypeError):
        return 0


def _headers(api_id):
    token = kiwoom_auth.get_access_token()
    return {
        "api-id":        api_id,
        "authorization": "Bearer " + (token or ""),
        "content-type":  "application/json;charset=UTF-8",
    }


def _post(api_id, url_path, body):
    url = KIWOOM_URL_BASE + url_path
    try:
        res = requests.post(url, headers=_headers(api_id), json=body, timeout=5)
        if res.status_code == 200:
            data = res.json()
            rc = data.get("return_code", 0)
            if rc != 0:
                print(f"   ❌ [{api_id}] return_code={rc} msg={data.get('return_msg', '')}")
            return data
        print(f"   ❌ API HTTP 실패 [{api_id}]: {res.status_code} → {res.text[:200]}")
    except Exception as e:
        print(f"   ❌ API 오류 [{api_id}]: {e}")
    return None


# ====================================================
# 💼 잔고 조회 (kt00018 qry_tp=2 개별)
#    같은 종목이 현금/신용/담보로 나뉘어 있으면 각각 반환
# ====================================================
_CRD_TYPE_NAME = {"00": "현금", "01": "신용", "08": "담보"}

def fetch_stock_holdings(code):
    """
    반환: list of {
        "qty"              : 보유수량,
        "sell_possible_qty": 매매가능수량,
        "avg_buy_price"    : 매입가(float),
        "purchase_amount"  : 매입금액(int),
        "loan_dt"          : 대출일자 str ("" = 현금, "YYYYMMDD" = 신용/담보),
        "crd_type"         : 신용구분 ("00"=현금, "01"=신용, "08"=담보),
        "loan_amt"         : 0 (미제공),
        "order_type"       : "현금" | "신용" | "담보",
    }
    보유 없으면 None
    """
    # qry_tp=2(개별): 담보(crd_tp=08)·신용(crd_tp=01) 구분 및 대출일자 정확히 반환
    data = _post("kt00018", "/api/dostk/acnt", {"qry_tp": "2", "dmst_stex_tp": "KRX"})
    if not data or data.get("return_code") != 0:
        return None

    holdings = []
    for item in data.get("acnt_evlt_remn_indv_tot", []):
        raw_cd = item.get("stk_cd", "")
        # stk_cd: "A005930" (접두어 1자리 + 6자리)
        item_code = raw_cd[1:] if (len(raw_cd) == 7 and raw_cd[0].isalpha()) else raw_cd
        if item_code != code:
            continue
        qty = _pint(item.get("rmnd_qty"))
        if qty <= 0:
            continue
        loan_dt  = item.get("crd_loan_dt", "").strip()
        crd_type = item.get("crd_tp", "00").strip()
        holdings.append({
            "qty":               qty,
            "sell_possible_qty": _pint(item.get("trde_able_qty")),
            "avg_buy_price":     float(_pint(item.get("pur_pric"))),
            "purchase_amount":   _pint(item.get("pur_amt")),
            "loan_dt":           loan_dt,
            "crd_type":          crd_type,
            "loan_amt":          0,
            "order_type":        _CRD_TYPE_NAME.get(crd_type, "현금"),
        })
    return holdings if holdings else None


# ====================================================
# 📦 전 보유종목 조회 (kt00018 qry_tp=2)
#    동일 종목이 현금/신용/담보로 나뉘면 수량 합산해 종목당 1건으로 반환
# ====================================================
def fetch_all_holdings():
    """
    반환: list of {"code": 6자리, "name": 종목명, "qty": 총보유수량}
          조회 실패 시 None (빈 계좌의 [] 와 구분 — 호출부 오삭제 방지용)
    """
    data = _post("kt00018", "/api/dostk/acnt", {"qry_tp": "2", "dmst_stex_tp": "KRX"})
    if not data or data.get("return_code") != 0:
        return None

    agg = {}  # code -> {"code", "name", "qty"}
    for item in data.get("acnt_evlt_remn_indv_tot", []):
        raw_cd = item.get("stk_cd", "")
        code = raw_cd[1:] if (len(raw_cd) == 7 and raw_cd[0].isalpha()) else raw_cd
        qty = _pint(item.get("rmnd_qty"))
        if not code or qty <= 0:
            continue
        if code not in agg:
            agg[code] = {"code": code, "name": item.get("stk_nm", "").strip(), "qty": 0}
        agg[code]["qty"] += qty
    return list(agg.values())


# ====================================================
# 📉 지정가 매도 주문
#    loan_dt 있으면 kt10007(담보=crd_deal_tp:88 / 신용=33), 없으면 kt10001 현금 매도
# ====================================================
def place_sell_order(code, qty, price, loan_dt="", crd_type="00"):
    if qty <= 0:
        return None
    if loan_dt:
        # 담보(crd_type=08): crd_deal_tp="88", 신용융자(crd_type=01): crd_deal_tp="33"
        crd_deal_tp = "88" if crd_type == "08" else "33"
        body = {
            "dmst_stex_tp": "KRX",
            "stk_cd":       code,
            "ord_qty":      str(qty),
            "ord_uv":       str(price),
            "trde_tp":      "0",     # 지정가
            "crd_deal_tp":  crd_deal_tp,
            "crd_loan_dt":  loan_dt,
        }
        return _post("kt10007", "/api/dostk/crdordr", body)
    else:
        body = {
            "dmst_stex_tp": "KRX",
            "stk_cd":       code,
            "ord_qty":      str(qty),
            "ord_uv":       str(price),
            "trde_tp":      "0",     # 지정가
            "cond_uv":      "",
        }
        return _post("kt10001", "/api/dostk/ordr", body)


# ====================================================
# 📈 지정가 매수 주문 (현금, kt10000)
# ====================================================
def place_buy_order(code, qty, price):
    """현금 지정가 매수. 반환: API dict (return_code=0 성공) or None"""
    if qty <= 0 or price <= 0:
        return None
    body = {
        "dmst_stex_tp": "KRX",
        "stk_cd":       code,
        "ord_qty":      str(qty),
        "ord_uv":       str(price),
        "trde_tp":      "0",     # 지정가
        "cond_uv":      "",
    }
    return _post("kt10000", "/api/dostk/ordr", body)


# ====================================================
# 💰 총자산 / 주문가능현금 조회
# ====================================================
def fetch_total_assets():
    """추정예탁자산(예수금+주식평가) = 총자산. 실패 시 None"""
    d = _post("kt00018", "/api/dostk/acnt", {"qry_tp": "2", "dmst_stex_tp": "KRX"})
    if not d or d.get("return_code") != 0:
        return None
    return _pint(d.get("prsm_dpst_aset_amt"))


def fetch_order_cash():
    """미수 없이 현금으로 매수 가능한 금액 = D+2 추정예수금(d2_entra). 실패 시 None.
    HTS '주문가능금액 · 미수불가 100%' 값과 일치한다(실측: HTS 1,501,029 ≈ d2_entra 1,501,079).

    ※ qry_tp 는 반드시 '3'(추정조회)이어야 한다. '2'(일반조회)는 추정예수금을
      계산하지 않아 d1_entra/d2_entra 가 0으로 나온다(이게 값이 안 맞던 원인).
    ⚠️ Nstk_ord_alow_amt(20/40/50/100%종목주문가능금액)는 보유주식 대용(담보)을
       포함한 '증거금 기반 매수여력'이라 현금을 크게 초과한다(예: 56,265,387원).
       이걸로 주문하면 예수금 초과분이 전부 미수가 되므로 절대 매수 한도로 쓰지 않는다.
    d2_entra = 당일 매도대금까지 정산된 뒤의 추정 현금. 이 범위 내 매수는 미수 없음."""
    d = _post("kt00001", "/api/dostk/acnt", {"qry_tp": "3"})
    if not d or d.get("return_code") != 0:
        return None
    return max(0, _pint(d.get("d2_entra")))


# ====================================================
# 📊 매도호가 10단계 조회 (ka10004 주식호가요청)
#    실시간 sweep 매수 판단용. 최우선(가장 싼)부터 깊은 순으로 반환.
# ====================================================
def _pabs(v):
    """부호 접두('+70000'/'-2800') 포함 숫자 → 절대값 int (호가/가격용)"""
    try:
        return abs(int(str(v).replace(",", "").replace("+", "").split(".")[0]))
    except (ValueError, TypeError):
        return 0


def fetch_ask_book(code):
    """매도호가 10단계를 [(price:int, qty:int), ...] 로 반환 (best→deep, price 오름차순).
       price<=0 단계는 제외. 조회 실패 시 []."""
    data = _post("ka10004", "/api/dostk/mrkcond", {"stk_cd": code})
    if not data or data.get("return_code") != 0:
        return []
    levels = []
    p = _pabs(data.get("sel_fpr_bid"))      # 매도최우선호가
    q = _pint(data.get("sel_fpr_req"))      # 매도최우선잔량
    if p > 0:
        levels.append((p, q))
    for n in range(2, 11):                  # 매도 2~10차선
        p = _pabs(data.get(f"sel_{n}th_pre_bid"))
        q = _pint(data.get(f"sel_{n}th_pre_req"))
        if p > 0:
            levels.append((p, q))
    return levels


def ask_qty_at_or_below(code, limit_price):
    """매도호가 중 limit_price 이하(포함)에 쌓인 총 잔량과 최우선호가를 반환.
       반환: (avail_qty:int, best_ask:int). 호가 없으면 (0, 0)."""
    levels = fetch_ask_book(code)
    if not levels:
        return 0, 0
    best_ask = levels[0][0]
    avail = 0
    for price, qty in levels:        # price 오름차순 → limit 초과 시 중단
        if price > limit_price:
            break
        avail += qty
    return avail, best_ask


def fetch_bid_book(code):
    """매수호가 10단계를 [(price:int, qty:int), ...] 로 반환 (best→deep, price 내림차순).
       price<=0 단계는 제외. 조회 실패 시 []. (실시간 sweep 매도 판단용)"""
    data = _post("ka10004", "/api/dostk/mrkcond", {"stk_cd": code})
    if not data or data.get("return_code") != 0:
        return []
    levels = []
    p = _pabs(data.get("buy_fpr_bid"))      # 매수최우선호가
    q = _pint(data.get("buy_fpr_req"))      # 매수최우선잔량
    if p > 0:
        levels.append((p, q))
    for n in range(2, 11):                  # 매수 2~10차선
        p = _pabs(data.get(f"buy_{n}th_pre_bid"))
        q = _pint(data.get(f"buy_{n}th_pre_req"))
        if p > 0:
            levels.append((p, q))
    return levels


def bid_qty_at_or_above(code, limit_price):
    """매수호가 중 limit_price 이상(포함)에 쌓인 총 잔량과 최우선호가를 반환.
       = limit_price 로 지정가 매도 시 '즉시 체결될' 수량.
       반환: (avail_qty:int, best_bid:int). 호가 없으면 (0, 0)."""
    levels = fetch_bid_book(code)
    if not levels:
        return 0, 0
    best_bid = levels[0][0]
    avail = 0
    for price, qty in levels:        # price 내림차순 → limit 미만 시 중단
        if price < limit_price:
            break
        avail += qty
    return avail, best_bid


# ====================================================
# 📋 미체결 매수 주문 조회 (ka10075, trde_tp=2 매수)
# ====================================================
def fetch_open_buy_orders(code):
    """반환: list of {order_no, qty, remaining_qty, price}"""
    body = {
        "all_stk_tp": "1",   # 종목 지정
        "trde_tp":    "2",   # 매수
        "stk_cd":     code,
        "stex_tp":    "0",
    }
    data = _post("ka10075", "/api/dostk/acnt", body)
    if not data or data.get("return_code") != 0:
        return []
    orders = []
    for item in data.get("oso", []):
        remaining = _pint(item.get("oso_qty"))
        if remaining <= 0:
            continue
        if "매수" not in item.get("io_tp_nm", ""):
            continue
        orders.append({
            "order_no":      item.get("ord_no", ""),
            "qty":           _pint(item.get("ord_qty")),
            "remaining_qty": remaining,
            "price":         _pint(item.get("ord_pric")),
        })
    return orders


# ====================================================
# ❌ 주문 취소
#    loan_dt 있으면 신용/담보 취소(kt10009), 없으면 현금 취소(kt10003)
# ====================================================
def cancel_order(order_no, code, qty, loan_dt=""):
    """반환: True(성공) / False(실패)"""
    body = {
        "dmst_stex_tp": "KRX",
        "orig_ord_no":  order_no,
        "stk_cd":       code,
        "cncl_qty":     "0",     # 전량취소
    }
    if loan_dt:
        data = _post("kt10009", "/api/dostk/crdordr", body)
    else:
        data = _post("kt10003", "/api/dostk/ordr", body)
    return data is not None and data.get("return_code") == 0


# ====================================================
# ✏️ 주문 가격 정정
#    loan_dt 있으면 신용/담보 정정(kt10008), 없으면 현금 정정(kt10002)
# ====================================================
def amend_sell_order(order_no, code, new_price, loan_dt=""):
    """반환: True(성공) / False(실패)"""
    body = {
        "dmst_stex_tp": "KRX",
        "orig_ord_no":  order_no,
        "stk_cd":       code,
        "mdfy_qty":     "0",           # 잔량 전부 정정
        "mdfy_uv":      str(new_price),
        "mdfy_cond_uv": "",
    }
    if loan_dt:
        data = _post("kt10008", "/api/dostk/crdordr", body)
    else:
        data = _post("kt10002", "/api/dostk/ordr", body)
    return data is not None and data.get("return_code") == 0


# ====================================================
# 📋 미체결 매도 주문 조회 (ka10075)
# ====================================================
def fetch_open_sell_orders(code):
    """
    반환: list of {
        "order_no", "qty", "remaining_qty", "price", "loan_dt"
    }
    """
    body = {
        "all_stk_tp": "1",   # 종목 지정
        "trde_tp":    "1",   # 매도
        "stk_cd":     code,  # 6자리 (접두어 없음)
        "stex_tp":    "0",   # 통합
    }
    data = _post("ka10075", "/api/dostk/acnt", body)
    if not data or data.get("return_code") != 0:
        return []

    orders = []
    for item in data.get("oso", []):
        remaining = _pint(item.get("oso_qty"))
        if remaining <= 0:
            continue
        io_tp = item.get("io_tp_nm", "")
        if "매도" not in io_tp:
            continue
        orders.append({
            "order_no":      item.get("ord_no", ""),
            "qty":           _pint(item.get("ord_qty")),
            "remaining_qty": remaining,
            "price":         _pint(item.get("ord_pric")),
            "loan_dt":       "",   # ka10075 응답에 대출일 미포함 → 저장된 값 사용
        })
    return orders


# ====================================================
# 🧮 점수 → 목표 보유 금액
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
def auto_sell(code, stock_name, total_score, curr_price, prev_open_orders=None, keep_override=None):
    """
    반환 dict:
      status        : "ordered" | "already_pending" | "skipped" | "failed"
      msg           : 텔레그램/로그용 메시지 (already_pending 시 None)
      placed_orders : [{"order_no", "qty", "price", "order_type", "loan_dt"}, ...]
      keep_label    : "전량매도" | "N만원 보유"

    prev_open_orders: 레벨 변경 시 취소할 이전 주문 목록 (last_scores에서 전달)
    """
    # keep_override 가 주어지면(B 전략 등) 티어 재계산 대신 그 값을 사용 (0=전량)
    keep_amount = keep_override if keep_override is not None else get_keep_amount(total_score)
    if keep_amount is None:
        return None  # 매도 불필요

    # ── 정규장 시간(평일 09:00~15:30) 외에는 본주문/취소 없이 체크만 ──────
    #    sell_level을 진행시키지 않고 대기 → 정규장 개장 후 사이클에서 실제 주문
    if not is_market_open():
        keep_label = "전량매도" if keep_amount == 0 else f"{keep_amount // 10_000:,}만원 보유"
        return {
            "status": "market_closed",
            "code":   code,
            "name":   stock_name,
            "msg":    (f"⏸️ 정규장 시간 외 — 매도 대기\n{stock_name}({code})  [{keep_label}]\n"
                       f"09:00~15:30 정규장에 주문 실행"),
        }

    # ── 레벨 변경 시 이전 주문 취소 ───────────────────────────────────────
    cancelled_by_loan = {}
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

    # 담보(신용) 포지션 먼저, 이후 현금 순으로 매도
    sorted_holdings = sorted(all_holdings, key=lambda h: (not bool(h["loan_dt"]), h["loan_dt"]))

    remaining_to_sell = total_purchase_amt - keep_amount
    placed_orders = []
    failed_msgs   = []

    for h in sorted_holdings:
        if remaining_to_sell <= 0:
            break

        avg_p = h["avg_buy_price"]
        avail = h["sell_possible_qty"] + cancelled_by_loan.get(h["loan_dt"], 0)
        if avg_p <= 0 or avail <= 0:
            continue

        sell_qty = avail if keep_amount == 0 else min(math.ceil(remaining_to_sell / avg_p), avail)
        if sell_qty <= 0:
            continue

        result = place_sell_order(code, sell_qty, curr_price, h["loan_dt"], h.get("crd_type", "00"))
        if result and result.get("return_code") == 0:
            order_no = result.get("ord_no", "?")
            placed_orders.append({
                "order_no":   order_no,
                "qty":        sell_qty,
                "price":      curr_price,
                "order_type": h["order_type"],
                "loan_dt":    h["loan_dt"],
                "crd_type":   h.get("crd_type", "00"),
            })
            remaining_to_sell -= sell_qty * avg_p
        else:
            err = result.get("return_msg", "응답 없음") if result else "응답 없음"
            # 800033: 담보설정 종목 매도 불가 — REST API 미지원
            if result and "800033" in str(err):
                failed_msgs.append(f"[담보설정] HTS 대출매도상환 화면에서 직접 매도 필요")
            else:
                failed_msgs.append(f"[{h['order_type']}] {err}")

    cancel_block = ("\n" + "\n".join(cancel_lines)) if cancel_lines else ""

    # 담보설정 에러 여부 확인
    is_collateral_blocked = any("담보설정" in m for m in failed_msgs)

    if not placed_orders:
        total_locked = sum(h["qty"] - h["sell_possible_qty"] for h in all_holdings)
        if total_locked > 0 and not failed_msgs:
            return {"status": "already_pending", "code": code, "name": stock_name,
                    "msg": None, "placed_orders": []}

        err_detail = " / ".join(failed_msgs) if failed_msgs else "응답 없음"
        if is_collateral_blocked:
            return {
                "status": "collateral_blocked",
                "code":   code,
                "name":   stock_name,
                "msg":    (f"⚠️ 담보대출 종목 자동매도 불가\n{stock_name}({code})\n"
                           f"키움 HTS > 대출매도상환 화면에서 직접 매도하세요{cancel_block}"),
            }
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