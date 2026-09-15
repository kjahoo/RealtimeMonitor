# -*- coding: utf-8 -*-
"""
viewer_v3_web.py — 오늘자 Stock_V3.csv 실시간 Streamlit 뷰어
=============================================================
logs/{오늘}_Stock_V3.csv 를 주기적으로(기본 1초) 읽어 score 내림차순 테이블 표시.
읽기 전용(자동매매 무관). 별도 가상환경(viewer_env)에서 실행 — trading_env 불사용.

(2026-09-14) 장후(15:30~20:00, NXT·KRX 애프터마켓) 보기 추가:
  장후 점수는 logs/{오늘}_Stock_V3_AfterMkt.csv 에 따로 기록된다(정규장 Stock_V3 보존).
  보기 '자동'이면 평일 15:30~20:00 에 장후 파일을 표시하고, 정규장 마감 점수 대비 차이(±) 열을 붙인다.
  장후 점수는 KRX 시세 기준, 현재가는 통합(SOR) 마지막 체결가.

실행:  viewer_env\Scripts\streamlit.exe run viewer_v3_web.py
       (또는 Run_Viewer.bat)
"""
import os
from datetime import datetime, time as dtime

import pandas as pd
import streamlit as st

LOG_DIR = r"C:\Projects\RealtimeMonitor\logs"
POST_CLOSE_START = dtime(15, 30)   # 정규장 마감 → 장후 시작
AFTER_MKT_START  = dtime(16, 0)    # KRX 애프터마켓 시작
POST_CLOSE_END   = dtime(20, 0)

st.set_page_config(page_title="Stock V3 뷰어", page_icon="📊", layout="wide")

# ── 사이드바 옵션 ────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 옵션")
    session  = st.radio("보기", ["자동", "정규장", "장후"], horizontal=True,
                        help="자동: 평일 15:30~20:00 장후, 그 외 정규장")
    interval = st.slider("갱신 주기(초)", 1, 10, 1)
    top_n    = st.slider("표시 종목 수", 10, 200, 50, step=10)
    only60   = st.checkbox("60점+ 만 보기", value=False)
    st.caption("읽기 전용 뷰어 — 자동매매와 무관")

st.title("📊 Stock V3 실시간 스코어")
_slot = st.container()


@st.cache_data(ttl=0.5, show_spinner=False)
def load_csv(path, mtime_ns):
    """mtime 이 바뀔 때만 실제 재파싱 (같은 파일이면 캐시)."""
    df = pd.read_csv(path, encoding="utf-8-sig",
                     dtype={"code": str, "name": str, "time": str},
                     on_bad_lines="skip")
    return df.dropna(subset=["score_total"])


def _is_post_close(now):
    return now.weekday() < 5 and POST_CLOSE_START < now.time() < POST_CLOSE_END


def _regular_close_scores(day):
    """정규장(09:00~15:30) 시각에 계산된 행의 점수 {code: score} — 장후 '마감대비' 기준."""
    path = os.path.join(LOG_DIR, f"{day}_Stock_V3.csv")
    try:
        df = load_csv(path, os.stat(path).st_mtime_ns)
    except Exception:
        return {}
    df = df[df["time"].fillna("").between("09:00:00", "15:30:59")]
    return dict(zip(df["code"].str.split(".").str[0].str.zfill(6), df["score_total"]))


@st.fragment(run_every=f"{interval}s")
def table():
    now = datetime.now()
    day = now.strftime("%Y%m%d")
    after = _is_post_close(now) if session == "자동" else session == "장후"

    if after:
        label = "애프터마켓" if now.time() >= AFTER_MKT_START else "NXT 장후"
        st.markdown(f"#### 🌙 장후 · {label} — 점수 KRX 기준 · 현재가 통합(SOR) · 자동매매 미적용")
        path = os.path.join(LOG_DIR, f"{day}_Stock_V3_AfterMkt.csv")
    else:
        st.markdown("#### ☀️ 정규장")
        path = os.path.join(LOG_DIR, f"{day}_Stock_V3.csv")

    if not os.path.exists(path):
        st.info(f"⏳ {os.path.basename(path)} 파일 대기 중... "
                f"(조회 {now:%H:%M:%S})")
        return
    try:
        mt = os.stat(path).st_mtime_ns
        df = load_csv(path, mt)
    except Exception as e:
        st.warning(f"읽기 재시도 중: {e}")
        return

    df = df.sort_values("score_total", ascending=False).reset_index(drop=True)
    total = len(df)
    n60 = int((df["score_total"] >= 0.60).sum())
    if only60:
        df = df[df["score_total"] >= 0.60]
    view = df.head(top_n).copy()
    view.insert(0, "순위", range(1, len(view) + 1))
    view["점수"] = (view["score_total"] * 100).round(1)
    cols  = ["순위", "code", "name", "close_price", "점수"]
    names = ["순위", "코드", "종목명", "현재가(통합)" if after else "현재가", "점수"]
    if after:
        base = _regular_close_scores(day)
        st.caption("마감대비 = 장후 점수 − 정규장(09:00~15:30) 마지막 점수 · '-' = 오늘 정규장 점수 기록 없음")
        def _diff_txt(c, s):
            # 문자열로 만든다 — st.dataframe 은 결측(NaN/None)을 Styler 서식과 무관하게 'None' 으로 그림
            if c not in base:
                return "-"
            d = (s - base[c]) * 100
            return f"{d:+.1f}" if abs(d) >= 0.05 else "0.0"
        view["마감대비"] = [_diff_txt(c, s) for c, s in
                          zip(view["code"].str.split(".").str[0].str.zfill(6), view["score_total"])]
        cols.append("마감대비"); names.append("마감대비")
    cols  += ["net_hits", "surge_hits", "drop_hits", "time"]
    names += ["net", "급등", "급락", "시각"]
    view = view[cols]
    view.columns = names
    price_col = names[3]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("전체 종목", f"{total:,}")
    c2.metric("60점+ ", f"{n60}")
    c3.metric("파일 갱신", datetime.fromtimestamp(mt / 1e9).strftime("%H:%M:%S"))
    c4.metric("조회 시각", now.strftime("%H:%M:%S"))

    def _hl(row):
        if row["점수"] >= 60:
            return ["background-color:#0f5132;color:#d1e7dd"] * len(row)
        if row["점수"] >= 40:
            return ["background-color:#664d03;color:#fff3cd"] * len(row)
        return [""] * len(row)

    st.dataframe(
        view.style.apply(_hl, axis=1).format({price_col: "{:,.0f}", "점수": "{:.1f}"}),
        width="stretch", height=min(38 * (len(view) + 1) + 4, 900), hide_index=True,
    )


with _slot:
    table()
