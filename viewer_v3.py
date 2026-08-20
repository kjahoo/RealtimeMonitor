# -*- coding: utf-8 -*-
"""
viewer_v3.py — 오늘자 Stock_V3.csv 실시간 콘솔 뷰어
====================================================
logs/{오늘}_Stock_V3.csv 를 1초마다 읽어 score_total 내림차순 테이블로 표시.
읽기 전용(자동매매 무관). Ctrl+C 로 종료.

사용:  python viewer_v3.py [표시행수]   (기본 30, 0=전체)
"""
import os
import sys
import time
import unicodedata
from datetime import datetime

import pandas as pd

LOG_DIR  = r"C:\Projects\RealtimeMonitor\logs"
INTERVAL = 1.0
TOP_N    = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 30

# ANSI 색 (Windows 10+ VT 활성화)
os.system("")
RESET, BOLD = "\x1b[0m", "\x1b[1m"
GREEN, YELLOW, CYAN, DIM = "\x1b[92m", "\x1b[93m", "\x1b[96m", "\x1b[2m"
CLEAR_HOME = "\x1b[H\x1b[J"


def _w(s):
    """동아시아 폭 반영 표시폭."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(s))


def _pad(s, width, right=False):
    s = str(s)
    gap = max(0, width - _w(s))
    return (" " * gap + s) if right else (s + " " * gap)


def render(df, total, n60, path, mtime):
    rows = []
    hdr = (f"{_pad('#',3,True)} {_pad('코드',6)} {_pad('종목명',16)} "
           f"{_pad('현재가',9,True)} {_pad('점수',6,True)} "
           f"{_pad('net',4,True)} {_pad('급등',4,True)} {_pad('급락',4,True)} {_pad('시각',8)}")
    rows.append(BOLD + hdr + RESET)
    rows.append(DIM + "-" * _w(hdr) + RESET)
    for i, r in enumerate(df.itertuples(index=False), 1):
        sc100 = r.score_total * 100
        color = GREEN if sc100 >= 60 else (YELLOW if sc100 >= 40 else "")
        line = (f"{_pad(i,3,True)} {_pad(r.code,6)} {_pad(str(r.name)[:8],16)} "
                f"{_pad(f'{int(r.close_price):,}',9,True)} {_pad(f'{sc100:.1f}',6,True)} "
                f"{_pad(int(r.net_hits),4,True)} {_pad(int(r.surge_hits),4,True)} "
                f"{_pad(int(r.drop_hits),4,True)} {_pad(r.time,8)}")
        rows.append(color + line + (RESET if color else ""))
    head = (f"{BOLD}{CYAN}📊 Stock_V3 실시간 뷰어{RESET}  {os.path.basename(path)}  "
            f"(파일갱신 {mtime:%H:%M:%S} · 조회 {datetime.now():%H:%M:%S})\n"
            f"   전체 {total}종목 중 상위 {len(df)} 표시 · "
            f"{GREEN}60점+ {n60}개{RESET} · 1초 갱신 · Ctrl+C 종료\n")
    print(CLEAR_HOME + head + "\n".join(rows), flush=True)


def main():
    last_sig = None
    while True:
        day = datetime.now().strftime("%Y%m%d")
        path = os.path.join(LOG_DIR, f"{day}_Stock_V3.csv")
        try:
            if not os.path.exists(path):
                print(CLEAR_HOME + f"⏳ {path} 대기 중... ({datetime.now():%H:%M:%S})", flush=True)
            else:
                st = os.stat(path)
                sig = (path, st.st_mtime_ns, st.st_size, TOP_N)
                if sig != last_sig:            # 변경 없으면 재렌더 생략(깜빡임 방지)
                    df = pd.read_csv(path, encoding="utf-8-sig",
                                     dtype={"code": str, "name": str, "time": str},
                                     on_bad_lines="skip")
                    df = df.dropna(subset=["score_total"])
                    df = df.sort_values("score_total", ascending=False)
                    total = len(df)
                    n60 = int((df["score_total"] >= 0.60).sum())
                    if TOP_N > 0:
                        df = df.head(TOP_N)
                    render(df, total, n60, path, datetime.fromtimestamp(st.st_mtime))
                    last_sig = sig
        except KeyboardInterrupt:
            raise
        except Exception as e:
            # 쓰기 도중 read 등 일시 오류 → 직전 화면 유지, 다음 틱 재시도
            print(f"{DIM}(재시도: {e}){RESET}", flush=True)
        try:
            time.sleep(INTERVAL)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    print("\n👋 뷰어 종료")
