import pandas as pd
from pathlib import Path

ROOT     = Path(r"C:\Projects\RealtimeMonitor")
DATA_DIR = ROOT / "Data" / "Stock"

df = pd.read_pickle(str(ROOT / "cache_prod_full_2006_2026_scores.pkl"))
df["date"] = pd.to_datetime(df["date"])

hi = df[(df["date"].dt.year == 2021) & (df["score"] >= 0.70)]
top_codes = hi["code"].value_counts().head(20)
print("=== 2021년 score>=0.70 상위 20종목 ===")
print(top_codes)
print()

print("=== 2021년 주가 수익률 ===")
for code in top_codes.index[:20]:
    fpath = DATA_DIR / f"A{code}.csv"
    if not fpath.exists():
        continue
    px = pd.read_csv(fpath, encoding="utf-8-sig", usecols=["date", "close"])
    px["date"] = pd.to_datetime(px["date"])
    yr = px[px["date"].dt.year == 2021].sort_values("date")
    if len(yr) < 2:
        continue
    s0, s1 = int(yr["close"].iloc[0]), int(yr["close"].iloc[-1])
    ret = (s1 / s0 - 1) * 100
    max_dd = ((yr["close"] - yr["close"].cummax()) / yr["close"].cummax()).min() * 100
    print(f"  {code}: {ret:>+8.1f}%  ({s0:,} -> {s1:,})  MDD={max_dd:.1f}%")

# 2022, 2023도 확인
for yr_n in [2022, 2023]:
    hi2 = df[(df["date"].dt.year == yr_n) & (df["score"] >= 0.70)]
    tc2 = hi2["code"].value_counts().head(5)
    print(f"\n=== {yr_n}년 score>=0.70 상위 5종목 주가 수익률 ===")
    for code in tc2.index:
        fpath = DATA_DIR / f"A{code}.csv"
        if not fpath.exists():
            continue
        px = pd.read_csv(fpath, encoding="utf-8-sig", usecols=["date", "close"])
        px["date"] = pd.to_datetime(px["date"])
        yr = px[px["date"].dt.year == yr_n].sort_values("date")
        if len(yr) < 2:
            continue
        s0, s1 = int(yr["close"].iloc[0]), int(yr["close"].iloc[-1])
        ret = (s1 / s0 - 1) * 100
        print(f"  {code}: {ret:>+8.1f}%  ({s0:,} -> {s1:,})")