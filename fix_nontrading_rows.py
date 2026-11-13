# -*- coding: utf-8 -*-
"""
fix_nontrading_rows.py — 일봉 CSV(Data/Stock, Data/ETF)의 2026+ KRX 비거래일 행 일괄 제거 (2026-11-09)
=====================================================================================================
배경 : 휴장일에도 실시간 봇·수동 분석이 '오늘 행'을 기록 → 시세 API 가 돌려준 직전 거래일 값이 복제된 행이
       2026-05-01·05-16·05-25·06-03·06-06·06-27·07-17·08-22·10-05 에 남음(지수 등락률 이중 누적 등).
제거 : date >= 2026-01-01 이면서 kiwoom_trading.is_trading_day(date) == False (주말 + KRX_HOLIDAYS)
       + --extra 날짜(라이브 KRX_HOLIDAYS 반영 전 누락분 지정용, 예: 2026-05-01)
       ※ 1998-12 이전 토요일(반일장) 행은 정상 데이터라 대상 아님.
       --dedupe : 같은 날짜가 두 번 이상 있고 OHLCV·거래량이 동일한 행은 첫 행만 남김(값이 다르면 손대지 않음).
재계산: Update_Data_All 5~6단계와 동일(API 호출 없음) — change_pct=close.pct_change, target1/5/20,
       calculate_indicators_v3_save, FINAL_COLUMNS·fillna(0). 원시값(OHLCV·지수·프로그램)은 불변(검증).
안전장치:
  1) --apply 없으면 드라이런(읽기 전용)
  2) --apply 시 대상 원본 전부를 --backup-zip 에 먼저 압축·검증(CRC) 완료한 뒤에만 수정 시작
  3) 백업 시점 이후 원본이 바뀐 파일(라이브 봇 동시 쓰기)은 수정하지 않고 '재실행 필요'로 보고
  4) tmp + os.replace 원자적 저장, 저장 후 재검증(비거래일 0행·행 수·원시값 불변)
복원 : zip 안의 Data/Stock/A*.csv 를 원래 위치로 풀면 됨(파일 단위 덮어쓰기).
사용 :
  python fix_nontrading_rows.py --extra 2026-05-01 --dedupe                      # 드라이런
  python fix_nontrading_rows.py --extra 2026-05-01 --dedupe --apply \
         --backup-zip logs/Data_Stock_nontrading.bak_20261109.zip --report logs/20261109_nontrading_fix_report.csv
"""
import argparse
import os
import sys
import zipfile
from collections import Counter

import numpy as np
import pandas as pd

ROOT = r"C:\Projects\RealtimeMonitor"
sys.path.insert(0, ROOT)
from kis_api import kiwoom_trading as kt  # noqa: E402
from kis_api import indicators  # noqa: E402

DATA_DIRS = [os.path.join(ROOT, "Data", "Stock"), os.path.join(ROOT, "Data", "ETF")]
GUARD_FROM = pd.Timestamp("2026-01-01")      # KRX_HOLIDAYS 가 관리되는 첫 해
FINAL_COLUMNS = [
    'date', 'code', 'name', 'open', 'high', 'low', 'close', 'volume',
    'change_pct', 'kospi_change', 'kosdaq_change',
    'prog_net_qty', 'prog_ratio_vol',
    'ma5', 'ma20', 'ma60',
    'disparity_5', 'disparity_20', 'disparity_60',
    'volume_ratio', 'vol_power',
    'bb_w', 'bb_p', 'rsi', 'adx',
    'target1', 'target5', 'target20',
    'prog_net_ratio', 'bb_pos'
]
RAW_COLS = ['open', 'high', 'low', 'close', 'volume', 'kospi_change', 'kosdaq_change',
            'prog_net_qty', 'prog_ratio_vol', 'prog_net_ratio']
OHLCV = ['open', 'high', 'low', 'close', 'volume']


def non_trading_mask(dates, extra):
    d = pd.to_datetime(dates)
    return pd.Series([(x >= GUARD_FROM) and (not kt.is_trading_day(x.to_pydatetime())
                                             or x.strftime("%Y-%m-%d") in extra) for x in d], index=dates.index)


def rederive(df):
    """Update_Data_All 5~6단계와 동일한 파생값 재계산."""
    df = df.sort_values('date', kind='stable').reset_index(drop=True)
    df['change_pct'] = df['close'].pct_change().fillna(0)
    df['target1'] = df['close'].shift(-1) / df['close'] - 1
    df['target5'] = df['close'].shift(-5) / df['close'] - 1
    df['target20'] = df['close'].shift(-20) / df['close'] - 1
    df = indicators.calculate_indicators_v3_save(df)
    if 'ma60' not in df.columns:
        df['ma60'] = df['close'].rolling(window=60).mean()
    if 'disparity_60' not in df.columns:
        df['disparity_60'] = (df['close'] / df['ma60'] - 1).fillna(0)
    if 'bb_pos' not in df.columns:
        df['bb_pos'] = 0.0
    for c in FINAL_COLUMNS:
        if c not in df.columns:
            df[c] = 0
    return df[FINAL_COLUMNS].fillna(0)


def load(p):
    df = pd.read_csv(p, encoding="utf-8-sig", dtype={'code': str, 'name': str})
    df['date'] = pd.to_datetime(df['date'], errors='coerce').dt.normalize()
    return df


def plan_file(p, extra, dedupe):
    """(df, 제거 마스크, 비거래일 날짜 목록, 중복 제거 수, 중복값상이 날짜) — 대상 아니면 None."""
    df = load(p)
    if df['date'].isna().any():
        return "날짜파싱실패"
    bad = non_trading_mask(df['date'], extra)
    dup_rm = pd.Series(False, index=df.index)
    dup_conflict = []
    if dedupe:
        dmask = df['date'].duplicated(keep=False) & ~bad
        for d, g in df[dmask].groupby('date'):
            if (g[OHLCV].nunique() <= 1).all():
                dup_rm.loc[g.index[1:]] = True
            else:
                dup_conflict.append(d.strftime('%Y-%m-%d'))
    rm = bad | dup_rm
    if not rm.any():
        return None
    return df, rm, df.loc[bad, 'date'].dt.strftime('%Y-%m-%d').tolist(), int(dup_rm.sum()), dup_conflict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup-zip", default="")
    ap.add_argument("--extra", default="", help="추가 제거 날짜(YYYY-MM-DD, 콤마) — KRX_HOLIDAYS 누락분")
    ap.add_argument("--dedupe", action="store_true", help="값이 동일한 중복 날짜 행 제거")
    ap.add_argument("--only", default="", help="대상 코드 제한(6자리, 콤마)")
    ap.add_argument("--report", default="")
    ap.add_argument("--data-root", default=os.path.join(ROOT, "Data"), help="Stock/ETF 상위 폴더(테스트용)")
    a = ap.parse_args()
    global DATA_DIRS
    DATA_DIRS = [os.path.join(a.data_root, "Stock"), os.path.join(a.data_root, "ETF")]
    extra = {s.strip() for s in a.extra.split(",") if s.strip()}
    only = {s.strip().zfill(6) for s in a.only.split(",") if s.strip()}
    if a.apply and not a.backup_zip:
        sys.exit("--apply 에는 --backup-zip 경로가 필요합니다")
    if a.apply and os.path.exists(a.backup_zip):
        sys.exit(f"백업 zip 이 이미 있습니다(덮어쓰기 금지): {a.backup_zip}")

    # ── 1) 대상 선정(읽기 전용)
    targets = []   # (path, folder, fn, mtime, size)
    rows = []
    for ddir in DATA_DIRS:
        for fn in sorted(os.listdir(ddir)):
            if not (fn.startswith("A") and fn.endswith(".csv")):
                continue
            if only and fn[1:7] not in only:
                continue
            p = os.path.join(ddir, fn)
            st = os.stat(p)
            r = plan_file(p, extra, a.dedupe)
            if r is None:
                continue
            if isinstance(r, str):
                rows.append({"file": fn, "folder": os.path.basename(ddir), "status": r})
                continue
            df, rm, bad_dates, n_dup, dup_conflict = r
            targets.append((p, os.path.basename(ddir), fn, st.st_mtime_ns, st.st_size))
            rows.append({"file": fn, "folder": os.path.basename(ddir), "removed_nontrading": len(bad_dates),
                         "dates": ",".join(bad_dates), "removed_dup": n_dup,
                         "dup_value_conflict": ",".join(dup_conflict), "rows_before": len(df),
                         "rows_after": int((~rm).sum()), "status": "dry-run"})
    rep = pd.DataFrame(rows)
    tot_bytes = sum(t[4] for t in targets)
    print(f"대상 파일 {len(targets)}개({tot_bytes/1e6:.1f}MB), 비거래일 행 "
          f"{int(rep.get('removed_nontrading', pd.Series(dtype=int)).fillna(0).sum())}개, "
          f"중복 행 {int(rep.get('removed_dup', pd.Series(dtype=int)).fillna(0).sum())}개, extra={sorted(extra)}")
    if 'dates' in rep:
        cnt = Counter(d for s in rep['dates'].dropna() for d in s.split(",") if d)
        print("날짜별 비거래일 행:", dict(sorted(cnt.items())))
    if 'dup_value_conflict' in rep and rep['dup_value_conflict'].fillna('').str.len().gt(0).any():
        print("⚠️ 값이 다른 중복 날짜(손대지 않음):",
              rep.loc[rep['dup_value_conflict'].fillna('') != '', ['file', 'dup_value_conflict']].to_dict('records'))

    if a.apply and targets:
        # ── 2) 백업 zip 먼저 완성·검증
        os.makedirs(os.path.dirname(os.path.abspath(a.backup_zip)), exist_ok=True)
        with zipfile.ZipFile(a.backup_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for p, folder, fn, _, _ in targets:
                z.write(p, arcname=f"Data/{folder}/{fn}")
        with zipfile.ZipFile(a.backup_zip) as z:
            bad_member = z.testzip()
            names = set(z.namelist())
        if bad_member or len(names) != len(targets):
            sys.exit(f"❌ 백업 zip 검증 실패(손상 {bad_member}, {len(names)}/{len(targets)}) — 수정하지 않고 중단")
        print(f"✅ 백업 완료·검증: {a.backup_zip} ({os.path.getsize(a.backup_zip)/1e6:.1f}MB, {len(names)}파일)")

        # ── 3) 파일별 수정(백업 이후 원본 변경 시 스킵)
        status = {}
        for p, folder, fn, mt, sz in targets:
            st = os.stat(p)
            if st.st_mtime_ns != mt or st.st_size != sz:
                status[fn] = "백업후원본변경-스킵(재실행 필요)"
                continue
            df, rm, bad_dates, n_dup, _ = plan_file(p, extra, a.dedupe)
            keep = df[~rm].copy()
            new = rederive(keep.copy())
            old = keep.sort_values('date', kind='stable').reset_index(drop=True)
            raw_ok = all(np.allclose(pd.to_numeric(old[c], errors='coerce').fillna(0).values,
                                     pd.to_numeric(new[c], errors='coerce').fillna(0).values)
                         for c in RAW_COLS if c in old.columns)
            if not raw_ok:
                status[fn] = "원시값불일치-스킵"
                continue
            out = new.copy()
            out['date'] = pd.to_datetime(out['date']).dt.strftime('%Y-%m-%d')
            out['code'] = out['code'].astype(str).str.split('.').str[0].str.zfill(6)
            if os.stat(p).st_mtime_ns != mt:       # 재계산 중 변경 재확인
                status[fn] = "백업후원본변경-스킵(재실행 필요)"
                continue
            tmp = p + ".fixtmp"
            out.to_csv(tmp, index=False, encoding='utf-8-sig')
            os.replace(tmp, p)
            chk = load(p)
            ok = ((not non_trading_mask(chk['date'], extra).any()) and len(chk) == len(new)
                  and np.allclose(pd.to_numeric(chk['close']).values, pd.to_numeric(new['close']).values))
            status[fn] = "적용완료" if ok else "적용후검증실패"
        rep['status'] = rep.apply(lambda r: status.get(r['file'], r['status']), axis=1)

    if len(rep):
        print(rep['status'].value_counts().to_string())
    if a.report:
        rep.to_csv(a.report, index=False, encoding="utf-8-sig")
        print("보고서:", a.report)


if __name__ == "__main__":
    main()
