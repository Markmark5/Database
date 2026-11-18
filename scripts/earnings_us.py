"""US earnings revisions: S&P 500 and Nasdaq 100 (Earnings button on the Database page).

Same charts as the UK version (scripts/earnings.py), built from Yahoo Finance consensus estimates instead of
ShareScope, because US EPS is reported on GAAP and adjusted bases and Yahoo keeps each company on one basis
(its "methodology": normalized or gaap) for both years and all history points.

Inputs
  data/breadth/sp500_members.csv     S&P 500 members (kept up to date by the breadth job)
  Nasdaq 100 members                 Wikipedia "List of NASDAQ-100 companies"; cached in data/earnings/ndx_members.csv
  Yahoo Finance (yfinance)           per company: this-year (0y) and next-year (+1y) consensus EPS now and
                                     7/30/60/90 days ago, last year's actual EPS, latest price

State
  data/earnings/us_weekly.json       one snapshot per run date: {ticker: [fy1, fy2, price, last-year actual]}
                                     First run is back-filled from Yahoo's 90/60/30/7-days-ago estimates.

Output
  data/earnings/summary_us.json      same shape as summary.json; the popup shows these groups next to the UK ones

Method (as the UK script)
  Revision = change in a company's forecast EPS between snapshots, measured against the size of its EPS
  (floor: 2% of share price), capped at +/-15%, averaged across the group and chain-linked from 100.
  Year-end roll-over: when last year's actual EPS changes (the company has reported), "this year" has moved on,
  so the new this-year forecast is compared with the old next-year forecast.
  Breadth = % of companies whose this-year forecast rose over ~4 weeks minus % whose forecast fell.
  Expected growth = median of next-year / this-year forecast EPS - 1 (profitable companies).
"""
import csv, datetime as dt, io, json, os, statistics as st, sys, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, 'data', 'earnings')
SNAP = os.path.join(D, 'us_weekly.json')
OUT = os.path.join(D, 'summary_us.json')
NDX_CACHE = os.path.join(D, 'ndx_members.csv')
GROUPS = [('S&P 500', '^GSPC', 'S&P 500'), ('Nasdaq 100', '^NDX', 'Nasdaq 100')]
CAP, DROP = 0.15, 0.6          # per-company weekly change: capped at 15% in the mean, dropped above 60% (data glitch)
NOTE = 'Yahoo Finance consensus EPS · each company on one basis (adjusted or GAAP) throughout'


def yahoo(t):
    return t.strip().upper().replace('.', '-')


def sp500():
    with open(os.path.join(ROOT, 'data', 'breadth', 'sp500_members.csv')) as f:
        return [yahoo(r['TIDM']) for r in csv.DictReader(f) if r.get('TIDM')]


def ndx100():
    try:
        import pandas as pd
        req = urllib.request.Request('https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies',
                                     headers={'User-Agent': 'Mozilla/5.0 (earnings revisions script)'})
        html = urllib.request.urlopen(req, timeout=30).read().decode()
        for t in pd.read_html(io.StringIO(html)):
            if 'Ticker' in t.columns and 90 <= len(t) <= 110:
                rows = [(str(a).strip(), str(b).strip()) for a, b in zip(t['Ticker'], t['Company'])]
                with open(NDX_CACHE, 'w', newline='') as f:
                    w = csv.writer(f)
                    w.writerow(['TIDM', 'Name'])
                    w.writerows(rows)
                return [yahoo(a) for a, _ in rows]
    except Exception as e:  # noqa: BLE001
        print('Nasdaq 100 list from Wikipedia failed, using the cache:', e)
    with open(NDX_CACHE) as f:
        return [yahoo(r['TIDM']) for r in csv.DictReader(f) if r.get('TIDM')]


def fetch(tickers):
    """{ticker: {'now': [fy1, fy2], 'd7': .., 'd30': .., 'd60': .., 'd90': .., 'ya': last-year actual}}"""
    import yfinance as yf
    out, bad = {}, []
    for i, t in enumerate(tickers):
        for attempt in range(3):
            try:
                tk = yf.Ticker(t)
                tr, es = tk.eps_trend, tk.earnings_estimate
                if tr is None or '0y' not in tr.index:
                    break
                g = lambda col, p: (float(tr.loc[p, col]) if p in tr.index and tr.loc[p, col] == tr.loc[p, col] else None)
                rec = {k: [g(c, '0y'), g(c, '+1y')] for k, c in
                       (('now', 'current'), ('d7', '7daysAgo'), ('d30', '30daysAgo'), ('d60', '60daysAgo'), ('d90', '90daysAgo'))}
                ya = es.loc['0y', 'yearAgoEps'] if es is not None and '0y' in es.index and 'yearAgoEps' in es.columns else None
                rec['ya'] = float(ya) if ya == ya and ya is not None else None
                if rec['now'][0] is not None:
                    out[t] = rec
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    bad.append(t)
                    print('  failed', t, e)
                time.sleep(2 + 3 * attempt)
        if i % 50 == 49:
            print(f'  {i + 1}/{len(tickers)} read')
    print(f'estimates for {len(out)} of {len(tickers)} companies ({len(bad)} failed)')
    return out


def prices(tickers, dates):
    """Closing price per ticker on or before each date (one batch download)."""
    import yfinance as yf
    start = (min(dates) - dt.timedelta(days=10)).isoformat()
    px = yf.download(tickers, start=start, progress=False, auto_adjust=True, threads=True)['Close'].ffill()
    out = {}
    for d in dates:
        sub = px[px.index.date <= d]
        if len(sub):
            row = sub.iloc[-1]
            out[d] = {t: round(float(row[t]), 2) for t in tickers if t in row and row[t] == row[t]}
    return out


def index_prices(dates):
    try:
        import yfinance as yf
        start = (min(dates) - dt.timedelta(days=10)).isoformat()
        px = yf.download([g[1] for g in GROUPS], start=start, progress=False, auto_adjust=True)['Close'].ffill()
        out = {}
        for name, tick, _ in GROUPS:
            if tick not in px:
                continue
            s = px[tick].dropna()
            out[name] = [round(float(s[s.index.date <= d].iloc[-1]), 1) if len(s[s.index.date <= d]) else None for d in dates]
        return out
    except Exception as e:  # noqa: BLE001
        print('index prices unavailable:', e)
        return {}


def rev(a, b, px):
    if a is None or b is None:
        return None
    base = max(abs(a), 0.02 * px) if px else abs(a)
    return (b - a) / base if base > 0 else None


def clip_mean(xs):
    return sum(max(-CAP, min(CAP, x)) for x in xs) / len(xs)


def main():
    today = dt.date.today()
    sp, nd = sp500(), ndx100()
    members = {'S&P 500': sp, 'Nasdaq 100': nd}
    tickers = sorted(set(sp) | set(nd))
    print(f'S&P 500 {len(sp)} · Nasdaq 100 {len(nd)} · {len(tickers)} unique')
    est = fetch(tickers)
    if len(est) < 0.6 * len(tickers):
        print('too few estimates this run: keeping the previous summary')
        return 1

    snaps = {}
    try:
        snaps = {dt.date.fromisoformat(k): v for k, v in json.load(open(SNAP)).items()}
    except Exception:  # noqa: BLE001
        pass
    new_dates = [today]
    if not snaps:                                   # first run: back-fill from Yahoo's days-ago estimates
        new_dates = [today - dt.timedelta(days=n) for n in (90, 60, 30, 7)] + [today]
    px = prices(sorted(est), new_dates)
    for d in new_dates:
        key = {90: 'd90', 60: 'd60', 30: 'd30', 7: 'd7', 0: 'now'}[(today - d).days]
        p = px.get(d, {})
        snaps[d] = {t: [r[key][0], r[key][1], p.get(t), r['ya']] for t, r in est.items() if r[key][0] is not None}
    # one snapshot per week: a re-run in the same week replaces that week's snapshot
    by = {}
    for d in sorted(snaps):
        by[d.isocalendar()[:2]] = d
    snaps = {d: snaps[d] for d in by.values()}
    json.dump({d.isoformat(): v for d, v in sorted(snaps.items())}, open(SNAP, 'w'), separators=(',', ':'))

    dates = sorted(snaps)
    px_idx = index_prices(dates)
    out = dict(updated=today.isoformat(), latest=dates[-1].isoformat(), source='yahoo', groups={})
    for g, _, pxname in GROUPS:
        mem = [t for t in members[g]]
        i1, i2, brd, grw = [100.0], [None], [None], []
        for i in range(1, len(dates)):
            A, B = snaps[dates[i - 1]], snaps[dates[i]]
            c1s, c2s = [], []
            for t in mem:
                if t not in A or t not in B:
                    continue
                a, b = A[t], B[t]
                p = b[2] or a[2]
                rolled = a[3] is not None and b[3] is not None and a[3] != 0 and abs(b[3] / a[3] - 1) > 0.005
                c = rev(a[1], b[0], p) if rolled else rev(a[0], b[0], p)
                if c is None or abs(c) > DROP:
                    continue
                c1s.append(c)
                if not rolled and a[1] is not None and b[1] is not None:
                    c2 = rev(a[1], b[1], p)
                    if c2 is not None and abs(c2) <= DROP:
                        c2s.append(c2)
            i1.append(i1[-1] * (1 + clip_mean(c1s)) if c1s else i1[-1])
            i2.append((i2[-1] or 100.0) * (1 + clip_mean(c2s)) if len(c2s) > 20 else i2[-1])
            # breadth: change in this-year forecast against the snapshot ~4 weeks earlier
            j = max((k for k in range(i) if (dates[i] - dates[k]).days >= 21), default=None)
            if j is None:
                brd.append(None)
            else:
                P, up, dn, n = snaps[dates[j]], 0, 0, 0
                for t in mem:
                    if t in P and t in B and P[t][3] == B[t][3]:          # same financial year
                        c = rev(P[t][0], B[t][0], B[t][2] or P[t][2])
                        if c is None or abs(c) > DROP:
                            continue
                        n += 1
                        up += c > 0.005
                        dn += c < -0.005
                brd.append(round((up - dn) / n * 100, 1) if n else None)
        for d in dates:
            gr = [s[1] / s[0] - 1 for t in mem if (s := snaps[d].get(t)) and s[1] is not None and s[0] and s[0] > 0 and s[1] > 0]
            grw.append(round(st.median(gr) * 100, 2) if len(gr) > 20 else None)
        last = snaps[dates[-1]]
        out['groups'][g] = dict(
            dates=[d.isoformat() for d in dates], fy1=[round(x, 2) for x in i1],
            fy2=[round(x, 2) if x else None for x in i2], breadth=brd, growth=grw,
            px=px_idx.get(g), pxname=pxname, companies=len(mem), with_fc=sum(t in last for t in mem),
            loss=sum(1 for t in mem if t in last and last[t][0] <= 0), note=NOTE)
        f2 = [x for x in i2 if x]
        print(f"{g:11s} {len(mem):4d} companies | FY1 {i1[-1] - 100:+.1f}% | FY2 {(f2[-1] - 100 if f2 else 0):+.1f}% | breadth {brd[-1]} | growth {grw[-1]}")
    json.dump(out, open(OUT, 'w'), separators=(',', ':'))
    print('snapshots', len(dates), dates[0], '->', dates[-1])
    return 0


if __name__ == '__main__':
    sys.exit(main())
