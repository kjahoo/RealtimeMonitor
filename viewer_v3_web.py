# -*- coding: utf-8 -*-
"""
viewer_v3_web.py — 오늘자 Stock_V3.csv 실시간 Streamlit 뷰어
=============================================================
logs/{오늘}_Stock_V3.csv 를 주기적으로(기본 1초) 읽어 score 내림차순 테이블 표시.
읽기 전용(자동매매 무관). 별도 가상환경(viewer_env)에서 실행 — trading_env 불사용.

실행:  viewer_env\Scripts\streamlit.exe run viewer_v3_web.py
       (또는 Run_Viewer.bat)
"""
import os
from datetime import datetime

import pandas as pd
import streamlit as st

LOG_DIR = r"C:\Projects\RealtimeMonitor\logs"

st.set_page_config(page_title="Stock V3 뷰어", page_icon="📊", layout="wide")

# ── 사이드바 옵션 ────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 옵션")
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


@st.fragment(run_every=f"{interval}s")
def table():
    day = datetime.now().strftime("%Y%m%d")
    path = os.path.join(LOG_DIR, f"{day}_Stock_V3.csv")
    if not os.path.exists(path):
        st.info(f"⏳ {os.path.basename(path)} 파일 대기 중... "
                f"(조회 {datetime.now():%H:%M:%S})")
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
    view = view[["순위", "code", "name", "close_price", "점수",
                 "net_hits", "surge_hits", "drop_hits", "time"]]
    view.columns = ["순위", "코드", "종목명", "현재가", "점수",
                    "net", "급등", "급락", "시각"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("전체 종목", f"{total:,}")
    c2.metric("60점+ ", f"{n60}")
    c3.metric("파일 갱신", datetime.fromtimestamp(mt / 1e9).strftime("%H:%M:%S"))
    c4.metric("조회 시각", datetime.now().strftime("%H:%M:%S"))

    def _hl(row):
        if row["점수"] >= 60:
            return ["background-color:#0f5132;color:#d1e7dd"] * len(row)
        if row["점수"] >= 40:
            return ["background-color:#664d03;color:#fff3cd"] * len(row)
        return [""] * len(row)

    st.dataframe(
        view.style.apply(_hl, axis=1).format({"현재가": "{:,.0f}", "점수": "{:.1f}"}),
        width="stretch", height=min(38 * (len(view) + 1) + 4, 900), hide_index=True,
    )


with _slot:
    table()
