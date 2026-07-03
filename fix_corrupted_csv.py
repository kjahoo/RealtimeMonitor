# -*- coding: utf-8 -*-
"""
손상된 종목 CSV 복구 스크립트.
append 중 줄바꿈 유실로 두 일봉 레코드가 한 줄에 엉겨붙은 파일을 복구한다.
- 엉겨붙은 줄에서 완전한 30-필드 레코드는 정규식으로 분리해 복원
- 복구 불가한 잘린 조각/머리 잃은 꼬리는 폐기
- 날짜 기준 중복 제거(마지막 유지) 후 정렬, 원자적 저장
원본은 .corrupt.bak 으로 백업한다.
"""
import os
import re
import io
import shutil
import pandas as pd

DATA_DIR_STOCK = r"C:\Projects\RealtimeMonitor\Data\Stock"
DATA_DIR_ETF   = r"C:\Projects\RealtimeMonitor\Data\ETF"

EXPECTED_FIELDS = 30
# 레코드 시작 패턴: 날짜,코드(4~6자리),이름첫글자(영문/한글)
RECORD_START = re.compile(r"(\d{4}-\d{2}-\d{2},\d{4,6},[A-Za-z가-힣])")

TARGETS = ["A000500", "A001820", "A008490", "A010120", "A018670"]


def recover_records_from_line(line):
    """한 줄에서 완전한 30-필드 레코드 목록을 추출. 조각은 버림."""
    fields = line.rstrip("\n").split(",")
    if len(fields) == EXPECTED_FIELDS:
        return [line.rstrip("\n")]

    # 엉겨붙은 줄: 레코드 시작 위치마다 분할
    parts = RECORD_START.split(line.rstrip("\n"))
    # split 결과: [head, sep1, body1, sep2, body2, ...]
    chunks = []
    i = 1
    while i < len(parts):
        rec = parts[i] + parts[i + 1] if i + 1 < len(parts) else parts[i]
        chunks.append(rec)
        i += 2

    recovered = []
    for c in chunks:
        c = c.strip(",")
        if len(c.split(",")) == EXPECTED_FIELDS:
            recovered.append(c)
    return recovered


def fix_file(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        lines = f.readlines()

    header = lines[0].rstrip("\n")
    ncol = len(header.split(","))
    if ncol != EXPECTED_FIELDS:
        print(f"  ⚠️ 헤더 필드 {ncol}개 (기대 {EXPECTED_FIELDS}) — 건너뜀")
        return

    good, recovered, dropped = [], 0, 0
    for ln in lines[1:]:
        if not ln.strip():
            continue
        recs = recover_records_from_line(ln)
        if len(ln.rstrip("\n").split(",")) == EXPECTED_FIELDS:
            good.extend(recs)
        else:
            recovered += len(recs)
            # 줄 안의 레코드 후보 수 대비 복구 못한 조각
            dropped += 1
            good.extend(recs)

    # DataFrame 으로 정리 (날짜 중복 제거 + 정렬)
    buf = io.StringIO("\n".join([header] + good))
    df = pd.read_csv(buf, dtype={"code": str, "name": str})
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    before = len(df)
    df = df.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    df["code"] = df["code"].astype(str).str.split(".").str[0].str.zfill(6)

    # 백업 후 원자적 저장
    bak = path + ".corrupt.bak"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    tmp = path + ".tmp"
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)

    print(f"  ✅ 복구 완료: 유효행 {len(df)} (중복제거 {before - len(df)}), "
          f"엉김줄 {dropped}개에서 레코드 {recovered}개 복원 → 백업 {os.path.basename(bak)}")


def main():
    for stem in TARGETS:
        for base in (DATA_DIR_STOCK, DATA_DIR_ETF):
            p = os.path.join(base, stem + ".csv")
            if os.path.exists(p):
                print(f"[{stem}] {p}")
                # 손상 줄 사전 점검
                with open(p, "r", encoding="utf-8-sig") as f:
                    bad = sum(1 for i, l in enumerate(f)
                              if i > 0 and l.strip() and len(l.rstrip("\n").split(",")) != EXPECTED_FIELDS)
                print(f"  손상 줄: {bad}개")
                fix_file(p)
                break
        else:
            print(f"[{stem}] 파일 없음 — 건너뜀")


if __name__ == "__main__":
    main()
