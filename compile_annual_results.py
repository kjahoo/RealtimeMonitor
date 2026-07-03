import pandas as pd
import numpy as np

files = {
    'Exp-01':    'expanding_fold01_annual.csv',
    'Exp-02':    'expanding_fold02_annual.csv',
    'Exp-04':    'expanding_fold04_annual.csv',
    'Exp-10':    'expanding_fold10_annual.csv',
    'Roll7y-06': 'rolling7y_fold06_annual.csv',
}
model_ranges = {
    'Exp-01':    (2013, 2025),
    'Exp-02':    (2014, 2025),
    'Exp-04':    (2016, 2025),
    'Exp-10':    (2022, 2025),
    'Roll7y-06': (2018, 2025),
}
models = list(files.keys())

# 데이터 로드
data = {}
for name, f in files.items():
    df = pd.read_csv(f)
    df['연도'] = df['연도'].astype(int)
    data[name] = df.set_index('연도')

# 전체 연도 목록 + KOSPI
all_years = sorted(set(y for df in data.values() for y in df.index))
kospi_ref = data['Exp-01']['KOSPI(%)'].to_dict()
for name, df in data.items():
    for y, row in df.iterrows():
        if y not in kospi_ref:
            kospi_ref[y] = row['KOSPI(%)']

SEP = '-' * 90

# ── 수익률 비교 ──────────────────────────────────────────────────────────────
print()
print('=' * 90)
print(' 5개 모델 연간 수익률 비교  (품질필터 적용)')
print('=' * 90)
hdr = '{:>4}  {:>7}  {:>9}  {:>9}  {:>9}  {:>9}  {:>9}'.format(
    '연도', 'KOSPI', 'Exp-01', 'Exp-02', 'Exp-04', 'Exp-10', 'Roll7y-06')
print(hdr)
print(SEP)
for y in all_years:
    k = kospi_ref.get(y, float('nan'))
    ks = '{:>+6.1f}%'.format(k) if not np.isnan(k) else '   N/A '
    vals = []
    for m in models:
        if y in data[m].index:
            v = data[m].loc[y, '전략(%)']
            vals.append('{:>+8.1f}%'.format(v))
        else:
            vals.append('{:>9}'.format('---'))
    print('{:>4}  {}  {}'.format(y, ks, '  '.join(vals)))

# ── 초과수익 비교 ─────────────────────────────────────────────────────────────
print()
print('=' * 90)
print(' 5개 모델 연간 KOSPI 초과수익 (%p)  (✅ 초과 / ❌ 미달)')
print('=' * 90)
print(hdr)
print(SEP)
beat_count = {m: 0 for m in models}
total_count = {m: 0 for m in models}
for y in all_years:
    k = kospi_ref.get(y, float('nan'))
    ks = '{:>+6.1f}%'.format(k) if not np.isnan(k) else '   N/A '
    vals = []
    for m in models:
        if y in data[m].index:
            exc = data[m].loc[y, '초과(%p)']
            total_count[m] += 1
            if exc > 0:
                beat_count[m] += 1
                vals.append('{:>+7.1f}p✅'.format(exc))
            else:
                vals.append('{:>+7.1f}p❌'.format(exc))
        else:
            vals.append('{:>9}'.format('---'))
    print('{:>4}  {}  {}'.format(y, ks, '  '.join(vals)))

# ── 요약 ─────────────────────────────────────────────────────────────────────
print()
print('=' * 90)
print(' 요약 통계')
print('=' * 90)
print('{:>10}  {:>11}  {:>4}  {:>8}  {:>9}  {:>8}  {:>7}  {:>7}  {:>8}  {:>9}'.format(
    '모델', '기간', '연수', '평균수익', 'KOSPI초과', '평균초과',
    '평균Sh', '평균MDD', '누적수익', '누적KOSPI'))
print(SEP)
for name in models:
    df = pd.read_csv(files[name])
    valid = df.dropna(subset=['KOSPI(%)'])
    n = len(df)
    n_beat = int((valid['초과(%p)'] > 0).sum())
    avg_ret = df['전략(%)'].mean()
    avg_exc = valid['초과(%p)'].mean()
    avg_sh  = df['Sharpe'].mean()
    avg_mdd = df['MDD(%)'].mean()
    cum_s   = ((1 + df['전략(%)'] / 100).prod() - 1) * 100
    cum_k   = ((1 + valid['KOSPI(%)'] / 100).prod() - 1) * 100
    fy, ty  = model_ranges[name]
    beat_str = '{}/{}'.format(n_beat, len(valid))
    print('{:>10}  {:>4}~{:>4}  {:>4}년  {:>+7.1f}%  {:>9}  {:>+7.1f}p  {:>7.2f}  {:>6.1f}%  {:>+7.1f}%  {:>+8.1f}%'.format(
        name, fy, ty, n, avg_ret, beat_str, avg_exc, avg_sh, avg_mdd, cum_s, cum_k))

# ── 연도별 KOSPI 초과 히트맵 ─────────────────────────────────────────────────
print()
print('=' * 90)
print(' KOSPI 초과/미달 히트맵  (✅초과  ❌미달  --없음)')
print('=' * 90)
print('{:>4}  {:>7}  {:>7}  {:>7}  {:>7}  {:>7}  {:>10}'.format(
    '연도', 'Exp-01', 'Exp-02', 'Exp-04', 'Exp-10', 'R7y-06', 'KOSPI'))
print(SEP)
for y in all_years:
    k = kospi_ref.get(y, float('nan'))
    ks = '{:>+6.1f}%'.format(k) if not np.isnan(k) else '   N/A'
    marks = []
    for m in models:
        if y in data[m].index:
            exc = data[m].loc[y, '초과(%p)']
            marks.append(' ✅' if exc > 0 else ' ❌')
        else:
            marks.append(' --')
    print('{:>4}  {}  {}'.format(
        y,
        '  '.join('{:>7}'.format(mk) for mk in marks),
        ks))