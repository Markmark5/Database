"""Overbought/oversold breadth for UK indices (Bespoke-style 50-day spread).

Run for every index in INDICES whose members file exists:
  python scripts/breadth.py            all indices
  python scripts/breadth.py smallcap   just one

Spread = (Close - 50DMA) / SD of last 50 closes.
  >= +1  overbought (OB)      >= +2  extreme OB
  <= -1  oversold   (OS)      <= -2  extreme OS

Files use the index key as prefix, e.g. ftse250_*, smallcap_*.

Inputs
  data/breadth/<key>_members.csv   TIDM,Name  (a raw ShareScope list export works as-is;
                                     only the first two columns are read)

Outputs (data/breadth/)
  <key>_index_spread.csv  Date,Close,Spread           index, rebuilt each run
  <key>_breadth.csv         Date,N,OB,OS,XOB,XOS,Source  % of members; history is FROZEN:
                            past rows are never recalculated, so each day keeps the
                            membership it had at the time. Source = backfill | live.
  <key>_latest.csv          TIDM,Name,Close,Spread,PctVs50   today's reading per stock
  <key>_members_snapshot.csv    last membership seen (used to detect changes)
  <key>_membership_log.csv    Date,Change,TIDM,Name    joiners / leavers each review
  <key>_failed.txt          tickers Yahoo returned no usable data for
"""
import csv
import datetime as dt
import os
import sys

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, "data", "breadth")
INDICES = {
    "ftse250":  {"name": "FTSE 250",      "yahoo": "^FTMC"},
    "smallcap": {"name": "FTSE SmallCap", "yahoo": "^FTSC"},
    "sp500":    {"name": "S&P 500",       "yahoo": "^GSPC", "suffix": ""},
}

WIN = 50
HISTORY_DAYS = 480          # ~12 months to plot + 50-day warm-up


def read_members(path):
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            if row and row[0].strip():
                out[row[0].strip()] = (row[1].strip() if len(row) > 1 else "")
    return out


def yahoo(tidm, suffix=".L"):
    # LSE: "AO." -> "AO.L", "BT.A" -> "BT-A.L"   US (suffix ""): "BRK.B" -> "BRK-B"
    t = tidm.rstrip(".").replace(".", "-")
    return t + suffix


def log_membership(members, today, SNAP, LOG):
    old = read_members(SNAP) if os.path.exists(SNAP) else None
    if old is not None:
        joined = sorted(set(members) - set(old))
        left = sorted(set(old) - set(members))
        if joined or left:
            new_file = not os.path.exists(LOG)
            with open(LOG, "a", newline="") as f:
                w = csv.writer(f)
                if new_file:
                    w.writerow(["Date", "Change", "TIDM", "Name"])
                for t in joined:
                    w.writerow([today, "joined", t, members[t]])
                for t in left:
                    w.writerow([today, "left", t, old[t]])
            print(f"Membership change: +{len(joined)} -{len(left)}")
    with open(SNAP, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["TIDM", "Name"])
        for t, n in members.items():
            w.writerow([t, n])


def fix_units(s):
    """Yahoo sometimes flips a day between pence and pounds (x100 jumps). Undo those."""
    s = s.copy()
    r = s / s.shift(1)
    for i in np.where((r > 50) | (r < 0.02))[0]:
        prev, cur = s.iloc[i - 1], s.iloc[i]
        s.iloc[i] = cur / 100 if cur > prev else cur * 100
    return s


def download(tickers, start):
    df = yf.download(tickers, start=start, auto_adjust=True, progress=False,
                     threads=True, group_by="column")["Close"]
    if isinstance(df, pd.Series):
        df = df.to_frame(tickers[0])
    missing = [t for t in tickers if t not in df or df[t].count() == 0]
    for t in missing:                       # retry one at a time (cache-lock / throttle blips)
        try:
            s = yf.download(t, start=start, auto_adjust=True, progress=False)["Close"]
            if isinstance(s, pd.DataFrame):
                s = s.iloc[:, 0]
            if s.count():
                df[t] = s
        except Exception as e:              # noqa: BLE001
            print("retry failed", t, e)
    return df


def spread(df):
    ma = df.rolling(WIN, min_periods=WIN).mean()
    sd = df.rolling(WIN, min_periods=WIN).std()
    return (df - ma) / sd.replace(0, np.nan), (df / ma - 1) * 100


def run(key):
    cfg = INDICES[key]
    P = lambda n: os.path.join(D, f"{key}_{n}")
    MEMBERS, SNAP, LOG = P("members.csv"), P("members_snapshot.csv"), P("membership_log.csv")
    BREADTH, INDEX = P("breadth.csv"), P("index_spread.csv")
    LATEST, FAILED = P("latest.csv"), P("failed.txt")
    INDEX_TICKER = cfg["yahoo"]
    today = dt.date.today().isoformat()
    members = read_members(MEMBERS)
    log_membership(members, today, SNAP, LOG)

    start = (dt.date.today() - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    tick = {t: yahoo(t, cfg.get("suffix", ".L")) for t in members}
    px = download(list(tick.values()) + [INDEX_TICKER], start)
    px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
    px = px.sort_index()

    # ---- index panel (no membership issue: rebuild every run)
    idx = px[INDEX_TICKER].dropna()
    isp, _ = spread(idx.to_frame())
    out = pd.DataFrame({"Close": idx.round(2), "Spread": isp.iloc[:, 0].round(3)}).dropna()
    out.index.name = "Date"
    out.to_csv(INDEX, date_format="%Y-%m-%d")

    # ---- constituents
    stocks = px[[v for v in tick.values() if v in px]].copy()
    # forward-fill only short gaps (holidays/suspensions); stale names drop out naturally
    stocks = stocks.apply(fix_units).ffill(limit=3)
    stocks = stocks.loc[idx.index.min():]
    sp, pct = spread(stocks)

    failed = sorted(t for t, y in tick.items() if y not in stocks or stocks[y].count() < WIN)
    with open(FAILED, "w") as f:
        f.write("\n".join(f"{t}\t{tick[t]}" for t in failed) + ("\n" if failed else ""))

    valid = sp.notna()
    n = valid.sum(axis=1)
    calc = pd.DataFrame({
        "N": n,
        "OB": (sp >= 1).sum(axis=1) / n * 100,
        "OS": (sp <= -1).sum(axis=1) / n * 100,
        "XOB": (sp >= 2).sum(axis=1) / n * 100,
        "XOS": (sp <= -2).sum(axis=1) / n * 100,
    })
    calc = calc[calc["N"] >= len(tick) * 0.5].round(2)   # skip warm-up / thin days
    calc.index = calc.index.strftime("%Y-%m-%d")

    # ---- freeze history: keep stored rows, recompute only from the last stored date on
    if os.path.exists(BREADTH):
        old = pd.read_csv(BREADTH, index_col="Date", dtype={"Source": str})
        last = old.index.max()
        keep = old[old.index < last]
        new = calc[calc.index >= last].copy()
        new["Source"] = "live"
        merged = pd.concat([keep, new])
    else:
        merged = calc.copy()
        merged["Source"] = "backfill"
        merged.iloc[-1, merged.columns.get_loc("Source")] = "live"
    merged.index.name = "Date"
    merged.to_csv(BREADTH)

    # ---- today's per-stock table
    last_row = sp.iloc[-1]
    rows = []
    for t, y in tick.items():
        if y in sp and pd.notna(last_row.get(y)):
            rows.append([t, members[t], round(float(stocks[y].iloc[-1]), 4),
                         round(float(last_row[y]), 3), round(float(pct[y].iloc[-1]), 2)])
    rows.sort(key=lambda r: -r[3])
    with open(LATEST, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["TIDM", "Name", "Close", "Spread", "PctVs50"])
        w.writerows(rows)

    print(f"[{cfg['name']}] {len(members)} members, {len(rows)} with data, {len(failed)} failed: {failed}")
    print("Latest breadth:", merged.iloc[-1].to_dict())
    print("Latest index spread:", out.iloc[-1].to_dict())


# ---- sectors, bonds & commodities relative to the S&P 500 -------------------------
# Ratio = ETF close / SPY close.  Pct = how far today's ratio is above/below its
# own 50-day average.  Spread = same move in SDs (same yardstick as the breadth charts).
RELATIVE = [
    ("XLE", "Energy", "Sector"), ("XLV", "Health Care", "Sector"), ("XLP", "Staples", "Sector"),
    ("XLU", "Utilities", "Sector"), ("XLB", "Materials", "Sector"), ("XLY", "Discretionary", "Sector"),
    ("XLRE", "Real Estate", "Sector"), ("XLF", "Financials", "Sector"), ("XLI", "Industrials", "Sector"),
    ("XLK", "Technology", "Sector"), ("XLC", "Communication", "Sector"),
    ("TLT", "20+ yr Treasuries", "Other"), ("GLD", "Gold", "Other"), ("SLV", "Silver", "Other"),
    ("BNO", "Brent oil", "Other"), ("USO", "WTI oil", "Other"), ("UNG", "Natural gas", "Other"),
    ("CPER", "Copper", "Other"),
]
RELATIVE_BENCH = "SPY"


def run_relative():
    start = (dt.date.today() - dt.timedelta(days=200)).isoformat()
    tick = [t for t, _, _ in RELATIVE]
    px = download(tick + [RELATIVE_BENCH], start)
    px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
    px = px.sort_index().ffill(limit=3)
    rel = px[[t for t in tick if t in px]].div(px[RELATIVE_BENCH], axis=0)
    ma = rel.rolling(WIN, min_periods=WIN).mean()
    sd = rel.rolling(WIN, min_periods=WIN).std()
    pct = (rel / ma - 1) * 100
    spr = (rel - ma) / sd.replace(0, np.nan)
    last = pct.index[-1]
    wk = pct.index[max(0, len(pct.index) - 6)]
    rows = []
    for t, name, grp in RELATIVE:
        if t in pct and pd.notna(pct.at[last, t]):
            rows.append([last.strftime("%Y-%m-%d"), t, name, grp,
                         round(float(pct.at[last, t]), 2), round(float(spr.at[last, t]), 2),
                         round(float(pct.at[wk, t]), 2) if pd.notna(pct.at[wk, t]) else ""])
    with open(os.path.join(D, "relative.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Ticker", "Name", "Group", "Pct", "Spread", "Pct1w"])
        w.writerows(rows)
    print(f"[relative] {len(rows)}/{len(RELATIVE)} vs {RELATIVE_BENCH} as of {last.date()}")


def main():
    keys = sys.argv[1:] or list(INDICES) + ["relative"]
    for key in keys:
        if key == "relative":
            run_relative()
            continue
        if not os.path.exists(os.path.join(D, f"{key}_members.csv")):
            print(f"[{key}] no members file yet, skipping")
            continue
        run(key)


if __name__ == "__main__":
    sys.exit(main())
