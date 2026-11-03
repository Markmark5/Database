"""Daily save of the actual broker forecast figures (not ShareScope's % columns) for Broker Changes.

Reads the Group A live sheets listed in data/fc_urls.json (Ignition's Turnover/EPS feeds), matching
columns by name, and writes data/fc_daily/YYYY-MM-DD.json = {TICKER: [fc EPS, 2y fc EPS, fc Rev, 2y fc Rev]}
plus data/fc_daily/index.json (dates held, newest last). Keeps the last 45 days.
"""
import csv, io, json, os, time, urllib.request
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'data', 'fc_daily')
COLS = ['fc EPS', '2y fc EPS', 'fc Turnover (m)', '2y fc Turnover (m)']
KEEP_DAYS = 45


def num(v):
    v = str(v or '').replace(',', '').replace('£', '').strip()
    try:
        return float(v)
    except ValueError:
        return None


def uk_today():
    # UK date: BST (UTC+1) from the last Sunday of March to the last Sunday of October
    now = datetime.now(timezone.utc)
    def last_sunday(y, m):
        d = datetime(y, m + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
        return d - timedelta(days=(d.weekday() + 1) % 7)
    bst = last_sunday(now.year, 3).replace(hour=1) <= now < last_sunday(now.year, 10).replace(hour=1)
    uk = now + timedelta(hours=1 if bst else 0)
    # GitHub often starts scheduled jobs hours late. A run before 9am UK is still the previous
    # trading day's close (ShareScope updates ~10am), so label it with that day, skipping weekends.
    if uk.hour < 9:
        uk -= timedelta(days=1)
        while uk.weekday() >= 5:
            uk -= timedelta(days=1)
    return uk.strftime('%Y-%m-%d')


def main():
    urls = json.load(open(os.path.join(ROOT, 'data', 'fc_urls.json')))
    out = {}
    for u in urls:
        try:
            txt = urllib.request.urlopen(u + ('&' if '?' in u else '?') + 't=%d' % time.time(), timeout=60).read().decode('utf-8', 'replace')
        except Exception as e:
            print('skip (fetch failed):', u[:70], e)
            continue
        rows = list(csv.reader(io.StringIO(txt)))
        if not rows:
            continue
        head = [h.strip() for h in rows[0]]
        if 'TIDM' not in head or any(c not in head for c in COLS):
            print('skip (no forecast columns):', u[:70])
            continue
        ti, ix = head.index('TIDM'), [head.index(c) for c in COLS]
        for r in rows[1:]:
            tk = (r[ti] if ti < len(r) else '').strip().upper()
            if not tk or '#' in tk or tk in out:
                continue
            vals = [num(r[i]) if i < len(r) else None for i in ix]
            if any(v is not None for v in vals):
                out[tk] = vals
    if len(out) < 100:
        raise SystemExit('only %d tickers read — not saving' % len(out))
    os.makedirs(OUT, exist_ok=True)
    day = uk_today()
    with open(os.path.join(OUT, day + '.json'), 'w') as f:
        json.dump(out, f, separators=(',', ':'))
    dates = sorted(n[:-5] for n in os.listdir(OUT) if len(n) == 15 and n.endswith('.json') and n[:4].isdigit())
    for d in dates[:-KEEP_DAYS]:
        os.remove(os.path.join(OUT, d + '.json'))
    dates = dates[-KEEP_DAYS:]
    with open(os.path.join(OUT, 'index.json'), 'w') as f:
        json.dump({'dates': dates, 'cols': COLS}, f)
    print('saved', day, len(out), 'tickers;', len(dates), 'days held')


if __name__ == '__main__':
    main()
