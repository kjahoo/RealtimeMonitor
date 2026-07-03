# -*- coding: utf-8 -*-
"""
claude_eval_pipeline.py
─────────────────────────────────────────────────────────────────────
main_stock 가 한 바퀴 돌고 나면 60점(score_total ≥ 0.60) 이상 신규 종목을
'핸드오프 파일'로 저장한다. 이후 Claude(Cowork/이 Claude Code 세션)가
그 파일을 읽어 웹검색 기반으로 종목을 평가하고, 결과를 다시 이 모듈의
promote_and_notify() 로 넘기면:

  ① 본인(TELEGRAM_CHAT_ID) 에게만 평가 요약을 텔레그램으로 발송
  ② 'BUY(매수)' 판정 종목을 Search_History.csv(본인 ID)에 추가
     → Update_Promising_Stocks 가 추적/매도전략 적용
  ③ 평가완료 종목을 done 셋에 기록 → 다음 사이클 핸드오프에서 제외(중복 방지)

[promising 자동등록] 매 사이클 build_pending(write_handoff) 단계에서 60점+
(score_total>=0.60) 종목 전체를 register_promising_from_v3() 로 Search_History.csv
(본인 ID)에 자동 등록한다(BUY 여부와 무관, 이미 등록된 종목은 스킵). 따라서
주가 기준(current_price)이 60점+ 전 종목에 대해 promising 에 확보된다.

[Anthropic API 미사용]  — 분석은 구독 Claude(Cowork/세션)가 수행한다.

생성 파일 (logs/ 아래, 날짜 YYYYMMDD):
  {날짜}_claude_pending.csv      분석 대기 큐 (기계 판독용)
  {날짜}_claude_brief.md          사람/Claude 읽기용 브리핑
  {날짜}_claude_eval_done.json    평가 완료 종목코드 (중복 방지)
  {날짜}_claude_results.json      Claude가 쓴 평가 결과 (promote_evaluated.py 입력)
"""

import os
import csv
import json
from datetime import datetime

import pandas as pd

from config import secrets

# ── 설정 ─────────────────────────────────────────────────────────────
SCORE_THRESHOLD = 0.60          # 60점 이상 (score_total 0~1 스케일)
LOG_DIR = secrets.LOCAL_DATA_PATH
OWNER_ID = str(secrets.TELEGRAM_CHAT_ID)

# 핸드오프 CSV 컬럼 (Claude가 읽을 종목 메타 + V3 모델 출력)
PENDING_COLS = [
    "code", "name", "close_price", "market_cap",
    "score_total", "score100", "net_hits", "surge_hits", "drop_hits",
    "target1", "target5", "target20", "drop1", "drop5", "drop20",
]

# Search_History.csv 스키마 (telegram_chat.py 와 동일하게 유지)
HIST_FIELDS = [
    "timestamp", "code", "name", "current_price", "market_cap",
    "change_pct", "total_score", "net_hits", "surge_hits", "drop_hits",
    "signal", "chat_id",
]


# ── 경로 헬퍼 ─────────────────────────────────────────────────────────
def _pending_path(today_str):
    return os.path.join(LOG_DIR, f"{today_str}_claude_pending.csv")


def _brief_path(today_str):
    return os.path.join(LOG_DIR, f"{today_str}_claude_brief.md")


def _done_path(today_str):
    return os.path.join(LOG_DIR, f"{today_str}_claude_eval_done.json")


def _results_path(today_str):
    return os.path.join(LOG_DIR, f"{today_str}_claude_results.json")


def _hist_path(today_str):
    return os.path.join(LOG_DIR, f"{today_str}_Search_History.csv")


def _fmt_code(x):
    s = str(x).strip().split(".")[0]
    return s.zfill(6) if s.isdigit() else s


# ── done 셋 (평가 완료 종목) ──────────────────────────────────────────
def load_done(today_str):
    p = _done_path(today_str)
    try:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == today_str:
                return set(_fmt_code(c) for c in data.get("codes", []))
    except Exception:
        pass
    return set()


def _atomic_write_json(path, obj, **dump_kw):
    """tmp 파일에 쓰고 fsync 후 os.replace 로 원자 교체.
    쓰기가 중간에 끊겨도 대상 파일이 손상되지 않게 한다.
    tmp 이름을 프로세스별로 고유하게 만들어 promote 가 동시에 2개 돌더라도
    같은 tmp 를 공유해 내용이 뒤섞이는 손상(2026-06-29 사례)을 방지한다."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, **dump_kw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def save_done(today_str, codes):
    try:
        _atomic_write_json(_done_path(today_str),
                           {"date": today_str, "codes": sorted(codes)},
                           ensure_ascii=False)
    except Exception as e:
        print(f"   ⚠️ done 저장 실패: {e}")


# ── ① 핸드오프 생성 (main_stock 가 매 사이클 끝에 호출) ───────────────
def write_handoff(today_results, today_str):
    """
    today_results: {code: result_dict}  (main_stock 의 today_results)
        result_dict 예) {'code','name','close_price','market_cap','score_total',
                         'net_hits','surge_hits','drop_hits',
                         'target1'..'drop20'}
    60점 이상 & 아직 평가하지 않은(done 아님) 종목을 pending CSV + brief MD 로 저장.
    반환: 대기 종목 수
    """
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        done = load_done(today_str)

        rows = []
        all60 = []          # 60점+ 전체(평가완료 포함) — promising 자동등록용
        for code, res in today_results.items():
            try:
                sc = float(res.get("score_total", 0) or 0)
            except (TypeError, ValueError):
                sc = 0.0
            code6 = _fmt_code(code)
            if sc >= SCORE_THRESHOLD:
                all60.append({
                    "code": code6,
                    "name": res.get("name", ""),
                    "close_price": res.get("close_price", ""),
                    "market_cap": res.get("market_cap", ""),
                    "score_total": round(sc, 4),
                    "net_hits": res.get("net_hits", ""),
                    "surge_hits": res.get("surge_hits", ""),
                    "drop_hits": res.get("drop_hits", ""),
                })
                if code6 not in done:
                    rows.append({
                        "code": code6,
                        "name": res.get("name", ""),
                        "close_price": res.get("close_price", ""),
                        "market_cap": res.get("market_cap", ""),
                        "score_total": round(sc, 4),
                        "score100": round(sc * 100, 1),
                        "net_hits": res.get("net_hits", ""),
                        "surge_hits": res.get("surge_hits", ""),
                        "drop_hits": res.get("drop_hits", ""),
                        "target1": res.get("target1", ""),
                        "target5": res.get("target5", ""),
                        "target20": res.get("target20", ""),
                        "drop1": res.get("drop1", ""),
                        "drop5": res.get("drop5", ""),
                        "drop20": res.get("drop20", ""),
                    })

        # 60점+ 종목은 매 사이클 promising(Search_History) 에 자동 등록(없는 종목만)
        register_promising_from_v3(all60, today_str)

        rows.sort(key=lambda r: r["score100"], reverse=True)

        # pending CSV (원자적 저장)
        df = pd.DataFrame(rows, columns=PENDING_COLS)
        tmp = _pending_path(today_str) + ".tmp"
        df.to_csv(tmp, index=False, encoding="utf-8-sig")
        os.replace(tmp, _pending_path(today_str))

        # brief MD
        _write_brief(rows, today_str)

        if rows:
            print(f"   📝 [Claude 평가대기] 60점+ 신규 {len(rows)}개 → {_pending_path(today_str)}")
        return len(rows)
    except Exception as e:
        print(f"   ⚠️ 핸드오프 생성 실패: {e}")
        return 0


def build_pending_from_v3(today_str):
    """오늘자 logs/{today}_Stock_V3.csv 에서 60점+ 신규 종목으로 pending 생성.
       (30분 스케줄러가 매 실행 시 호출 — main_stock 과 분리)
       반환: 대기 종목 수 (0이면 분석할 신규 없음)
    """
    v3 = os.path.join(LOG_DIR, f"{today_str}_Stock_V3.csv")
    if not os.path.exists(v3):
        print(f"   ℹ️ Stock_V3 없음: {v3}")
        write_handoff({}, today_str)
        return 0
    try:
        df = pd.read_csv(v3, encoding="utf-8-sig", dtype=str, on_bad_lines="skip")
    except Exception as e:
        print(f"   ⚠️ Stock_V3 읽기 실패: {e}")
        return 0
    if "code" not in df.columns:
        write_handoff({}, today_str)
        return 0
    keep = ["code", "name", "close_price", "market_cap", "net_hits",
            "surge_hits", "drop_hits", "target1", "target5", "target20",
            "drop1", "drop5", "drop20"]
    tr = {}
    for _, r in df.iterrows():
        c = _fmt_code(r.get("code", ""))
        if not c:
            continue
        d = {k: r.get(k, "") for k in keep}
        try:
            d["score_total"] = float(r.get("score_total", 0) or 0)
        except (TypeError, ValueError):
            d["score_total"] = 0.0
        tr[c] = d
    return write_handoff(tr, today_str)


def _write_brief(rows, today_str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# 60점+ 종목 AI 평가 대기 — {today_str}",
        "",
        f"- 생성: {now}",
        f"- 대기 종목: {len(rows)}개 (score_total ≥ {SCORE_THRESHOLD})",
        "",
        "각 종목을 웹검색(뉴스·실적·재무·수급·테마)으로 분석해 단기(1~20일) 트레이딩 관점에서",
        "0~100 점수와 매수/관망/회피를 판정하고, 결과를 `_claude_results.json` 으로 저장 후",
        "`promote_evaluated.py` 를 실행하세요. (스키마는 README/메모리 참고)",
        "",
    ]
    if not rows:
        lines.append("_평가 대기 종목 없음._")
    else:
        lines.append("| # | 종목 | 코드 | V3점수 | 현재가 | 시총(억) | net | tgt1 | tgt5 | tgt20 | drop1 | drop5 | drop20 |")
        lines.append("|---|------|------|--------|--------|----------|-----|------|------|-------|-------|-------|--------|")
        for i, r in enumerate(rows, 1):
            lines.append(
                f"| {i} | {r['name']} | {r['code']} | {r['score100']} | "
                f"{r['close_price']} | {r['market_cap']} | {r['net_hits']} | "
                f"{r['target1']} | {r['target5']} | {r['target20']} | "
                f"{r['drop1']} | {r['drop5']} | {r['drop20']} |"
            )
    try:
        tmp = _brief_path(today_str) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        os.replace(tmp, _brief_path(today_str))
    except Exception as e:
        print(f"   ⚠️ brief 저장 실패: {e}")


# ── pending 읽기 (분석 단계에서 사용) ─────────────────────────────────
def load_pending(today_str):
    p = _pending_path(today_str)
    if not os.path.exists(p):
        return []
    try:
        df = pd.read_csv(p, encoding="utf-8-sig", dtype=str, on_bad_lines="skip")
        done = load_done(today_str)
        records = df.to_dict("records")
        # 핸드오프 파일이 갱신되기 전이어도 이미 평가완료된 종목은 제외
        return [r for r in records if _fmt_code(r.get("code", "")) not in done]
    except Exception as e:
        print(f"   ⚠️ pending 읽기 실패: {e}")
        return []


# ── 텔레그램 ──────────────────────────────────────────────────────────
#   본인(_send_owner): AI평가 + 풀리포트 경로 + 매수(promising) 정보 전부
#   친구(_send_friends): AI평가 요약만. 풀리포트 로컬경로·실제 매수/매도 정보는 제외
def _send(chat_id, msg):
    if not secrets.TELEGRAM_BOT_TOKEN:
        return
    try:
        import requests
        url = f"https://api.telegram.org/bot{secrets.TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": str(chat_id), "text": msg}, timeout=5)
    except Exception as e:
        print(f"   ⚠️ 텔레그램 전송 실패({chat_id}): {e}")


def _send_owner(msg):
    _send(OWNER_ID, msg)


def _friend_ids():
    """AI평가 공개 대상 = TELEGRAM_NOTIFY_IDS 중 본인 제외(본인은 _send_owner 로 별도 발송)."""
    ids = [str(i) for i in getattr(secrets, "TELEGRAM_NOTIFY_IDS", [])]
    return [i for i in ids if i != OWNER_ID]


def _send_friends(msg):
    for cid in _friend_ids():
        _send(cid, msg)


# 원본 '트레이딩 종목 분석' 13항목 (핵심 압축) — 종목별 풀리포트 섹션
ANALYSIS_SECTIONS = [
    ("business",      "1. 주요 사업 및 제품별 매출 비중"),
    ("customers",     "2. 주요 고객사 및 고객사별 매출 비중"),
    ("financials",    "3. 재무상황 분석 (작년~최근 분기 비교)"),
    ("growth",        "4. 성장성 분석"),
    ("competition",   "5. 경쟁사 대비 장단점"),
    ("valuation",     "6. 밸류에이션 분석 및 peer 비교"),
    ("invest_points", "7. 최신 투자 포인트와 주요 뉴스"),
    ("technical",     "8. 기술적(차트) 분석"),
    ("gossip",        "9. Gossip 및 웹상의 의견"),
    ("dilution",      "10. 메자닌 등 주가 희석 요인"),
    ("risks",         "11. 리스크 요인"),
    ("verdict",       "12. 종합 판단"),
    ("extra",         "13. 이외 투자 도움 사항"),
]


def _safe_name(name):
    s = "".join(ch for ch in str(name) if ch.isalnum() or ch in (" ", "_", "-"))
    return s.strip().replace(" ", "")[:20]


def _write_stock_report(ev, today_str):
    """종목별 13항목 풀리포트를 logs/{날짜}_{코드}_{종목명}.md 로 저장. 경로 반환."""
    code6 = _fmt_code(ev.get("code", ""))
    name = str(ev.get("name", "")).strip()
    an = ev.get("analysis") or {}
    rec = str(ev.get("recommendation", "")).upper()
    nm = _safe_name(name)
    fname = f"{today_str}_{code6}_{nm}.md" if nm else f"{today_str}_{code6}.md"
    path = os.path.join(LOG_DIR, fname)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# {name} ({code6}) 투자분석",
        "",
        f"- 작성: {now}",
        f"- **투자매력도(클로드): {ev.get('claude_score','-')}/100**  ·  등급: {ev.get('grade','-')}  ·  판단: {_REC_KR.get(rec, rec)}",
        f"- V3 모델점수: {ev.get('v3_score100','-')}  ·  현재가: {ev.get('close_price','-')}  ·  시총: {ev.get('market_cap','-')}억",
        f"- 한줄요약: {ev.get('summary','')}",
        "",
    ]
    for key, title in ANALYSIS_SECTIONS:
        val = an.get(key) or ev.get(key) or ""
        lines += [f"## {title}", str(val).strip() or "_(자료 부족)_", ""]
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        os.replace(tmp, path)
        return path
    except Exception as e:
        print(f"   ⚠️ 종목리포트 저장 실패({code6}): {e}")
        return None


def _format_stock_msg(ev, added, report_path=None, public=False):
    """텔레그램용 요약 (점수 + 종합판단 + 요약). 13항목 전문은 파일에.
       public=True(친구용)는 풀리포트 로컬경로·실제 매수(promising 추가) 정보를 제외한다."""
    rec = str(ev.get("recommendation", "")).upper()
    an = ev.get("analysis") or {}
    verdict = an.get("verdict") or ev.get("verdict") or ev.get("thesis") or ""
    lines = [
        f"🤖 [AI평가] {ev.get('name','')} ({_fmt_code(ev.get('code',''))})",
        f"투자매력도 {ev.get('claude_score','-')}/100 · {ev.get('grade','-')} · {_REC_KR.get(rec, rec)}",
        f"V3 {ev.get('v3_score100','-')} · 현재가 {ev.get('close_price','-')}",
    ]
    if verdict:
        lines.append(f"종합: {verdict}")
    if ev.get("summary"):
        lines.append(f"요약: {ev['summary']}")
    if not public:                      # 본인 전용: 풀리포트 로컬경로 + 매수 추적 정보
        if report_path:
            lines.append(f"📄 풀리포트: {report_path}")
        if added:
            lines.append("➕ promising(본인)에 추가됨")
    return "\n".join(lines)


# ── promising 자동등록 (60점+ 전체, 매 사이클) ────────────────────────
def register_promising_from_v3(items, today_str):
    """60점+ 종목을 promising(Search_History.csv)에 본인 ID로 자동 등록.
       이미 등록된(본인 ID) 종목은 건너뛰고 신규만 추가한다. 가격(current_price)
       갱신은 Update_Promising_Stocks 가 담당하므로 여기서는 신규 행만 append.
       items: [{'code','name','close_price','market_cap','score_total',
                'net_hits','surge_hits','drop_hits'}, ...]
       반환: 신규 등록 개수.
    """
    if not items:
        return 0
    p = _hist_path(today_str)
    existing = set()
    if os.path.exists(p):
        try:
            df = pd.read_csv(p, encoding="utf-8-sig", dtype=str, on_bad_lines="skip")
            if "code" in df.columns and "chat_id" in df.columns:
                for c, cid in zip(df["code"].apply(_fmt_code),
                                  df["chat_id"].fillna("")):
                    if str(cid) == OWNER_ID:
                        existing.add(c)
        except Exception:
            pass
    os.makedirs(LOG_DIR, exist_ok=True)
    file_exists = os.path.exists(p)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    added = 0
    try:
        with open(p, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=HIST_FIELDS, extrasaction="ignore")
            if not file_exists:
                w.writeheader()
            for it in items:
                code6 = _fmt_code(it.get("code", ""))
                if not code6 or code6 in existing:
                    continue
                try:
                    sc = float(it.get("score_total", 0) or 0)
                except (TypeError, ValueError):
                    sc = 0.0
                w.writerow({
                    "timestamp": now,
                    "code": code6,
                    "name": it.get("name", ""),
                    "current_price": it.get("close_price", ""),
                    "market_cap": it.get("market_cap", ""),
                    "change_pct": "",
                    "total_score": round(sc, 4),
                    "net_hits": it.get("net_hits", ""),
                    "surge_hits": it.get("surge_hits", ""),
                    "drop_hits": it.get("drop_hits", ""),
                    "signal": "60+자동등록",
                    "chat_id": OWNER_ID,
                })
                existing.add(code6)
                added += 1
        if added:
            print(f"   ➕ [promising 자동등록] 60점+ {added}개 → {p}")
    except Exception as e:
        print(f"   ⚠️ promising 자동등록 실패: {e}")
    return added


# ── Search_History 추가 (BUY 종목만) ──────────────────────────────────
def _already_in_history(code6, today_str):
    p = _hist_path(today_str)
    if not os.path.exists(p):
        return False
    try:
        df = pd.read_csv(p, encoding="utf-8-sig", dtype=str, on_bad_lines="skip")
        if "code" not in df.columns or "chat_id" not in df.columns:
            return False
        c = df["code"].apply(_fmt_code)
        return bool(((c == code6) & (df["chat_id"].fillna("") == OWNER_ID)).any())
    except Exception:
        return False


def _append_to_history(ev, today_str):
    """BUY 종목을 Search_History.csv 에 본인 ID로 추가. 반환: 추가됨 여부"""
    code6 = _fmt_code(ev.get("code", ""))
    if not code6 or _already_in_history(code6, today_str):
        return False
    os.makedirs(LOG_DIR, exist_ok=True)
    p = _hist_path(today_str)
    exists = os.path.exists(p)
    row = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "code": code6,
        "name": ev.get("name", ""),
        "current_price": ev.get("close_price", ""),
        "market_cap": ev.get("market_cap", ""),
        "change_pct": "",
        "total_score": ev.get("v3_score", ""),
        "net_hits": "", "surge_hits": "", "drop_hits": "",
        "signal": "클로드평가",
        "chat_id": OWNER_ID,
    }
    try:
        with open(p, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=HIST_FIELDS, extrasaction="ignore")
            if not exists:
                w.writeheader()
            w.writerow(row)
        return True
    except Exception as e:
        print(f"   ⚠️ Search_History 추가 실패({code6}): {e}")
        return False


# ── 누적 결과 + 통합 리포트 (한 파일로 정리·갱신) ────────────────────
def _results_all_path(today_str):
    return os.path.join(LOG_DIR, f"{today_str}_claude_results_all.json")


def _report_path(today_str):
    return os.path.join(LOG_DIR, f"{today_str}_AI리포트.md")


def _load_results_all(today_str):
    p = _results_all_path(today_str)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


_REC_ORDER = {"BUY": 0, "HOLD": 1, "AVOID": 2}
_REC_KR = {"BUY": "✅매수", "HOLD": "⚪관망", "AVOID": "🔴회피"}


def _write_report(all_results, today_str):
    def _key(e):
        try:
            cs = float(e.get("claude_score", 0) or 0)
        except (TypeError, ValueError):
            cs = 0.0
        return (_REC_ORDER.get(str(e.get("recommendation", "")).upper(), 9), -cs)

    items = sorted(all_results, key=_key)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    buys = [e for e in items if str(e.get("recommendation", "")).upper() == "BUY"]

    lines = [
        f"# 60점+ 종목 AI 평가 리포트 — {today_str}",
        "",
        f"- 갱신: {now}",
        f"- 평가 종목: {len(items)}개 (매수 {len(buys)} / 관망·회피 {len(items) - len(buys)})",
        "",
        "| 판정 | 종목 | 코드 | V3 | 클로드 | 등급 | 요약 |",
        "|------|------|------|----|--------|------|------|",
    ]
    for e in items:
        rec = str(e.get("recommendation", "")).upper()
        lines.append(
            f"| {_REC_KR.get(rec, rec)} | {e.get('name','')} | {_fmt_code(e.get('code',''))} | "
            f"{e.get('v3_score100','')} | {e.get('claude_score','')} | {e.get('grade','')} | "
            f"{str(e.get('summary','')).replace('|','/')} |"
        )
    lines.append("")
    lines.append("### 종목별 종합판단 (풀리포트: logs/{날짜}_{코드}_{종목명}.md)")
    for e in items:
        rec = str(e.get("recommendation", "")).upper()
        an = e.get("analysis") or {}
        verdict = an.get("verdict") or e.get("verdict") or e.get("thesis") or e.get("summary", "")
        lines.append(
            f"- {_REC_KR.get(rec, rec)} **{e.get('name','')}({_fmt_code(e.get('code',''))})** "
            f"클로드 {e.get('claude_score','')}/100 · {e.get('grade','')} — {verdict}"
        )
    try:
        tmp = _report_path(today_str) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        os.replace(tmp, _report_path(today_str))
        print(f"   📄 통합 리포트 갱신: {_report_path(today_str)}")
    except Exception as e:
        print(f"   ⚠️ 리포트 저장 실패: {e}")


def _update_consolidated(results, today_str):
    """이번 실행 결과를 누적 저장하고 통합 리포트 1개 파일을 갱신."""
    by = {_fmt_code(e.get("code", "")): e for e in _load_results_all(today_str)}
    for ev in results:
        by[_fmt_code(ev.get("code", ""))] = ev   # 새 결과가 기존을 덮어씀
    merged = list(by.values())
    try:
        _atomic_write_json(_results_all_path(today_str), merged,
                           ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"   ⚠️ 누적 결과 저장 실패: {e}")
    _write_report(merged, today_str)


# ── ③ 결과 후처리: 텔레그램 + promising 추가 + done 기록 + 통합리포트 ──
def promote_and_notify(results, today_str):
    """
    results: [ {code,name,close_price,market_cap,v3_score,v3_score100,
                claude_score,grade,recommendation('BUY'|'HOLD'|'AVOID'),
                thesis,catalysts,risks,summary}, ... ]
    """
    if not results:
        print("ℹ️ 평가 결과 없음 — 후처리 생략")
        return {"sent": 0, "added": 0}

    done = load_done(today_str)
    added_cnt, sent_cnt = 0, 0

    # ── 발송 디덥: 이미 발송한(done) 종목은 제외 → 종목당 1회만 발송.
    #    results.json 은 사이클마다 남아있으므로, done 게이트가 없으면 30분마다
    #    같은 종목을 반복 발송(본인·친구 도배)하게 된다. done 에 없는 신규만 보낸다.
    fresh = [r for r in results if _fmt_code(r.get("code", "")) and
             _fmt_code(r.get("code", "")) not in done]
    if not fresh:
        print("ℹ️ 신규 발송 대상 없음(모두 발송 완료) — 텔레그램 생략")
        _update_consolidated(results, today_str)   # results_all 은 최신 유지(auto_buy plan 용)
        return {"sent": 0, "added": 0}

    buys = [r for r in fresh if str(r.get("recommendation", "")).upper() == "BUY"]
    header = (f"🤖 60점+ 종목 AI평가 {len(fresh)}건 "
              f"(매수 {len(buys)} / 관망·회피 {len(fresh) - len(buys)}) · {today_str}")
    _send_owner(header)
    _send_friends(header)          # 친구에게도 AI평가 공개 (요약 헤더)

    for ev in fresh:
        code6 = _fmt_code(ev.get("code", ""))
        is_buy = str(ev.get("recommendation", "")).upper() == "BUY"
        added = _append_to_history(ev, today_str) if is_buy else False
        if added:
            added_cnt += 1
        report_path = _write_stock_report(ev, today_str)   # 종목별 13항목 풀리포트
        _send_owner(_format_stock_msg(ev, added, report_path, public=False))
        _send_friends(_format_stock_msg(ev, added, report_path, public=True))
        sent_cnt += 1
        done.add(code6)            # 발송 완료 기록 → 다음 사이클 재발송 방지

    save_done(today_str, done)
    _update_consolidated(results, today_str)   # 통합 리포트 1개 파일 갱신
    print(f"✅ 텔레그램 {sent_cnt}건 발송(신규만) · promising 추가 {added_cnt}개 · done {len(done)}개 기록")
    return {"sent": sent_cnt, "added": added_cnt}


def promote_from_file(today_str):
    """logs/{today}_claude_results.json 을 읽어 promote_and_notify 실행."""
    p = _results_path(today_str)
    if not os.path.exists(p):
        print(f"❌ 결과 파일 없음: {p}")
        return None
    with open(p, encoding="utf-8") as f:
        results = json.load(f)
    return promote_and_notify(results, today_str)
