"""UK earnings revisions (Earnings button on the Database page).

Inputs
  Weekly Log Google Sheet     one row per week: "TICKER,fcEPS,2yfcEPS,fcRev,2yfcRev,target,close[,Q];..."
                              (the sheet behind File 4; read live, Mar 2026 onwards)
  data/earnings/seed_weekly.json.gz   earlier weeks (May 2025 - Feb 2026, this-year forecast only)
  data/earnings/meta.json     per company: name, index membership, sector, shares, close, last
                              full-year results date. Saved by the Database page when the
                              Friday files are loaded into the upload zone.

Output
  data/earnings/summary.json  small file the Earnings popup reads (weekly lines per group)

Method
  Companies only: investment vehicles, REITs and property investors, ETFs/VCTs/trusts/funds and overseas giants
  with a secondary London listing are left out. Groups use the latest index membership.
  Revision = week-to-week change in a company's forecast EPS, measured against the size of its
  EPS (floor: 2% of share price, so loss-makers and near-zero EPS work), capped at +/-15% and
  averaged across the group, then chain-linked from 100.
  Year-end roll-over: when a company reports its full year, "this year" moves on. If last
  week's next-year forecast is known, the new forecast is compared with that (same financial
  year); otherwise the results week is skipped.
  Breadth = % of companies with a net upgrade minus % with a net downgrade over 4 snapshots.
  Expected growth = median of next-year / this-year forecast EPS - 1 (profitable companies).
"""
import csv, datetime as dt, gzip, io, json, os, re, statistics as st, sys, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, 'data', 'earnings')
SHEET = ('https://docs.google.com/spreadsheets/d/1oov2nnwWUVcP4xTEi0wwkdqvr3uPqu8dmK4iJmy88sk/gviz/tq?tqx=out:csv'
         '&headers=1&range=A2:Z&sheet=' + urllib.parse.quote('\U0001F4C5 Weekly Log'))
GROUPS = [('FTSE 100', '^FTSE', 'FTSE 100'), ('FTSE 250', '^FTMC', 'FTSE 250'),
          ('Main market small', '^FTSC', 'FTSE SmallCap'), ('AIM', '^FTAI', 'AIM All-Share')]
EXCL_SECTORS = ('Open End and Miscellaneous Investment Vehicles', 'Real Estate Investment Trusts',
                'Closed End Investments')
# "Real Estate Investment and Services" mixes trading companies (Rightmove, Savills, Foxtons, LSL,
# Henry Boot, Winkworth ...), which are kept, with property investors whose earnings are driven by
# property valuations, which are left out by ticker:
PROPERTY_INVESTORS = {'CLI', 'HLCL', 'HWG', 'GR1T', 'HKLD', 'HKLJ', 'PLAZ', 'PNS', 'SPDI', 'WSP', 'CIC', 'CDFF', 'MFAI'}
FUND = re.compile(r'\b(ETF|ETP|ETC|UCITS|VCT|Trust|Fund)\b', re.I)
CAP, DROP = 0.15, 0.5


def num(v):
    m = re.match(r'^\s*[£$€]?\s*(-?[\d.]+)', str(v or '').replace(',', ''))
    return float(m.group(1)) if m else None


def pence(v):
    v = str(v or '').strip()
    n = num(v)
    if n is None:
        return None
    return n if v.endswith(('p', '¢')) else n * 100


def pdate(s):
    s = str(s).strip().split(' ')[0]
    for f in ('%d/%m/%Y', '%Y-%m-%d', '%d/%m/%y'):
        try:
            return dt.datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def read_sheet():
    txt = urllib.request.urlopen(SHEET, timeout=90).read().decode('utf-8', 'replace')
    out = {}
    for r in csv.reader(io.StringIO(txt)):
        if len(r) < 2 or ';' not in r[1]:
            continue
        d = pdate(r[0])
        if not d:
            continue
        snap = {}
        for item in r[1].split(';'):
            f = item.split(',')
            if len(f) < 3:
                continue
            fy1 = num(f[1])
            if fy1 is not None:
                snap[f[0].strip()] = [fy1, num(f[2]), pence(f[6]) if len(f) > 6 else None]
        if len(snap) > 200:
            out[d] = snap
    return out


def load_weeks():
    weeks = {}
    seed = os.path.join(D, 'seed_weekly.json.gz')
    if os.path.exists(seed):
        for k, v in json.load(gzip.open(seed, 'rt')).items():
            weeks[dt.date.fromisoformat(k)] = v
    live = read_sheet()
    first = min(live) if live else None
    weeks = {d: s for d, s in weeks.items() if not first or d < first - dt.timedelta(days=3)}
    weeks.update(live)
    # one snapshot per ISO week (the latest)
    by = {}
    for d in sorted(weeks):
        by[d.isocalendar()[:2]] = d
    return {d: weeks[d] for d in by.values()}, (max(live) if live else None)


def load_meta():
    m = json.load(open(os.path.join(D, 'meta.json')))
    out = {}
    for t, (name, idx, sector, shares, close, prelim) in m['c'].items():
        idx, sector = idx or '', sector or ''
        if 'FTSE 100' in idx:
            g = 'FTSE 100'
        elif 'FTSE 250' in idx:
            g = 'FTSE 250'
        elif 'AIM' in idx:
            g = 'AIM'
        else:
            g = 'Main market small'
        cap = (num(shares) or 0) * (pence(close) or 0) / 100          # GBP m
        ex = sector in EXCL_SECTORS or t in PROPERTY_INVESTORS or (not sector and FUND.search(name or '')) or \
            (g == 'Main market small' and cap > 1000 and not any(x in idx for x in ('Small Cap', 'Fledgling', 'techMARK')))
        p = pdate(prelim) if prelim else None
        out[t] = dict(group=g, excl=bool(ex), prelim=[p - dt.timedelta(days=365 * k) for k in range(3)] if p else [])
    return out, m.get('updated')


def rev(a, b, px):
    if a is None or b is None:
        return None
    base = max(abs(a), 0.02 * px) if px else abs(a)
    return (b - a) / base if base > 0 else None


def clip_mean(xs):
    return sum(max(-CAP, min(CAP, x)) for x in xs) / len(xs)


def index_prices(dates):
    try:
        import yfinance as yf
        start = (dates[0] - dt.timedelta(days=10)).isoformat()
        px = yf.download([g[1] for g in GROUPS], start=start, progress=False, auto_adjust=True)['Close'].ffill()
        out = {}
        for name, tick, _ in GROUPS:
            s = px[tick].dropna() if tick in px else []
            if not len(s):                      # Yahoo sometimes drops one index from a batch: ask again
                try:
                    s = yf.Ticker(tick).history(start=start, auto_adjust=True)['Close'].dropna()
                    s.index = s.index.tz_localize(None)
                except Exception as e:  # noqa: BLE001
                    print('retry failed', tick, e)
            if not len(s):
                continue
            vals = []
            for d in dates:
                sub = s[s.index.date <= d]
                vals.append(round(float(sub.iloc[-1]), 1) if len(sub) else None)
            out[name] = vals
        return out
    except Exception as e:  # noqa: BLE001
        print('index prices unavailable:', e)
        return {}


def carry(old, dates):
    """Index prices from the previous summary, when Yahoo gave none this run."""
    if not old or not old.get('px') or not any(v is not None for v in old['px']):
        return None
    m = dict(zip(old['dates'], old['px']))
    print('  index prices carried forward from the previous run')
    return [m.get(d.isoformat()) for d in dates]


def main():
    weeks, last_live = load_weeks()
    meta, meta_date = load_meta()
    dates = sorted(weeks)
    px_idx = index_prices(dates)
    prev = {}
    try:
        prev = json.load(open(os.path.join(D, 'summary.json')))['groups']
    except Exception:  # noqa: BLE001
        pass
    out = dict(updated=dt.date.today().isoformat(), latest=dates[-1].isoformat(), meta=meta_date, groups={})
    for g, _, pxname in GROUPS:
        mem = [t for t, m in meta.items() if m['group'] == g and not m['excl']]
        i1, i2, brd, hist, grw = [100.0], [None], [None], {}, []
        for i in range(1, len(dates)):
            A, B, d0, d1 = weeks[dates[i - 1]], weeks[dates[i]], dates[i - 1], dates[i]
            c1s, c2s = [], []
            for t in mem:
                if t not in A or t not in B:
                    continue
                a, b = A[t], B[t]
                px = b[2] or a[2]
                rw = any(d0 - dt.timedelta(days=14) < p <= d1 for p in meta[t]['prelim'])
                rolled = False
                if a[1] is not None:
                    if rw and abs(b[0] - a[1]) < abs(b[0] - a[0]):
                        rolled = True
                    if a[0] and a[1] and abs(a[1] / a[0] - 1) > 0.08 and abs(b[0] / a[1] - 1) < 0.04:
                        rolled = True
                if rolled:
                    c = rev(a[1], b[0], px)
                elif rw and a[1] is None:
                    continue
                else:
                    c = rev(a[0], b[0], px)
                if c is None or abs(c) > DROP:
                    continue
                c1s.append(c)
                hist.setdefault(t, {})[d1] = c
                if not rolled and a[1] is not None and b[1] is not None:
                    c2 = rev(a[1], b[1], px)
                    if c2 is not None and abs(c2) <= DROP:
                        c2s.append(c2)
            i1.append(i1[-1] * (1 + clip_mean(c1s)) if c1s else i1[-1])
            i2.append((i2[-1] or 100.0) * (1 + clip_mean(c2s)) if len(c2s) > 20 else i2[-1])
            win = dates[max(1, i - 3): i + 1]
            tot = {t: sum(h.get(d, 0) for d in win) for t, h in hist.items() if any(d in h for d in win)}
            up, dn = sum(v > 0.005 for v in tot.values()), sum(v < -0.005 for v in tot.values())
            brd.append(round((up - dn) / len(tot) * 100, 1) if tot else None)
        for d in dates:
            gr = [s[1] / s[0] - 1 for t in mem if (s := weeks[d].get(t)) and s[1] is not None and s[0] > 0 and s[1] > 0]
            grw.append(round(st.median(gr) * 100, 2) if len(gr) > 20 else None)
        last = weeks[dates[-1]]
        out['groups'][g] = dict(
            dates=[d.isoformat() for d in dates], fy1=[round(x, 2) for x in i1],
            fy2=[round(x, 2) if x else None for x in i2], breadth=brd, growth=grw,
            px=px_idx.get(g) or carry(prev.get(g), dates), pxname=pxname, companies=len(mem),
            with_fc=sum(t in last for t in mem), loss=sum(1 for t in mem if t in last and last[t][0] <= 0))
        f2 = [x for x in i2 if x]
        print(f"{g:18s} {len(mem):4d} companies | FY1 {i1[-1]-100:+.1f}% | FY2 {(f2[-1]-100 if f2 else 0):+.1f}% | breadth {brd[-1]} | growth {grw[-1]}")
    json.dump(out, open(os.path.join(D, 'summary.json'), 'w'), separators=(',', ':'))
    print('weeks', len(dates), dates[0], '->', dates[-1], '| meta', meta_date)


if __name__ == '__main__':
    sys.exit(main())
