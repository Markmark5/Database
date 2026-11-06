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
    "sp500":    {"name": "S&P 500",       "yahoo": "^GSPC", "suffix": "", "market": "US"},
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


# exchange time zone and the time after which that day's closes are final
MARKETS = {"UK": ("Europe/London", dt.time(16, 40)), "US": ("America/New_York", dt.time(16, 10))}


def drop_open_session(df, market):
    """Remove today's row while that market is still trading (a late-starting run
    would otherwise count part of a session as a close)."""
    tz, close = MARKETS[market]
    now = pd.Timestamp.now(tz=tz)
    if len(df) and df.index[-1].date() == now.date() and now.time() < close:
        print(f"  ignoring {now.date()}: {market} market not closed yet")
        df = df.iloc[:-1]
    return df


def hourly_fill(df, tickers, market, days=7):
    """Fill missing recent daily closes from Yahoo's hourly prices.

    Yahoo's daily feed sometimes skips a day for many stocks (seen for every
    FTSE stock on 5 Nov 2026) while its hourly feed has it; the last hourly
    price of a finished session equals that day's close."""
    tz, close = MARKETS[market]
    try:
        h = yf.download(tickers, period=f"{days}d", interval="1h", progress=False,
                        auto_adjust=False, threads=True)["Close"]
    except Exception as e:  # noqa: BLE001
        print("  hourly fetch failed", e)
        return df
    if isinstance(h, pd.Series):
        h = h.to_frame(tickers[0])
    if not len(h):
        return df
    h.index = h.index.tz_convert(tz)
    now = pd.Timestamp.now(tz=tz)
    finished = [d for d in sorted(set(h.index.date)) if d < now.date() or now.time() >= close]
    rows = {pd.Timestamp(d): h[h.index.date == d].ffill().iloc[-1] for d in finished}
    if not rows:
        return df
    hd = pd.DataFrame(rows).T.reindex(columns=df.columns)
    before = int(df.reindex(hd.index)[tickers].notna().sum().sum())
    out = df.combine_first(hd).sort_index()
    after = int(out.reindex(hd.index)[tickers].notna().sum().sum())
    if after > before:
        print(f"  filled {after - before} missing closes from hourly prices")
    return out


def complete_days(df, need=0.8):
    """Drop trailing days that too few tickers have reported yet.

    Yahoo publishes closes in batches; a run soon after the close can see the
    newest day for only some tickers. Those days are left for a later run."""
    rep = df.notna().sum(axis=1)
    full = rep.max()
    while len(df) and rep.iloc[-1] < need * full:
        print(f"  skipping {df.index[-1].date()}: only {int(rep.iloc[-1])}/{int(full)} tickers reported")
        df, rep = df.iloc[:-1], rep.iloc[:-1]
    return df


def fill_gaps(df):
    """Forward-fill short gaps inside the history (holidays, suspensions) but never
    the newest day: a ticker with no price for the latest day is left out of it
    instead of being counted at yesterday's price."""
    out = df.ffill(limit=3)
    out.iloc[-1] = df.iloc[-1]
    return out


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
    market = cfg.get("market", "UK")
    px = drop_open_session(px.sort_index(), market)
    px = drop_open_session(hourly_fill(px, list(tick.values()) + [INDEX_TICKER], market), market)

    # ---- index panel (no membership issue: rebuild every run)
    idx = px[INDEX_TICKER].dropna()
    # Yahoo sometimes publishes UK index levels hours after the constituents' closes.
    # If the index is behind the stocks, ask for it again on its own.
    last_stock = px.drop(columns=[INDEX_TICKER]).dropna(how="all").index.max()
    if idx.index.max() < last_stock:
        try:
            h = yf.Ticker(INDEX_TICKER).history(start=start, auto_adjust=True)["Close"]
            h.index = pd.to_datetime(h.index).tz_localize(None).normalize()
            idx = idx.combine_first(h.dropna()).sort_index()
        except Exception as e:  # noqa: BLE001
            print("index retry failed", INDEX_TICKER, e)
        if idx.index.max() < last_stock:
            print(f"[{cfg['name']}] index level only to {idx.index.max().date()}, stocks to {last_stock.date()}")
    isp, _ = spread(idx.to_frame())
    out = pd.DataFrame({"Close": idx.round(2), "Spread": isp.iloc[:, 0].round(3)}).dropna()
    out.index.name = "Date"
    out.to_csv(INDEX, date_format="%Y-%m-%d")

    # ---- constituents
    stocks = px[[v for v in tick.values() if v in px]].copy()
    stocks = fill_gaps(complete_days(stocks.apply(fix_units)))
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
# Indices are shown against their OWN 50-day average (vs SPY would make SPY always 0)
ABSOLUTE = [("SPY", "S&P 500", "Index"), ("QQQ", "Nasdaq 100", "Index"), ("IWM", "Russell 2000", "Index")]


def run_relative():
    start = (dt.date.today() - dt.timedelta(days=200)).isoformat()
    tick = [t for t, _, _ in RELATIVE]
    absT = [t for t, _, _ in ABSOLUTE]
    px = download(list(dict.fromkeys(tick + absT + [RELATIVE_BENCH])), start)
    px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
    px = px.sort_index()
    allT = list(dict.fromkeys(tick + absT + [RELATIVE_BENCH]))
    px = drop_open_session(hourly_fill(drop_open_session(px, "US"), allT, "US"), "US")
    px = px[px[RELATIVE_BENCH].notna()]          # every ratio needs that day's SPY close
    px = fill_gaps(complete_days(px))
    rel = px[[t for t in tick if t in px]].div(px[RELATIVE_BENCH], axis=0)
    rel = pd.concat([px[[t for t in absT if t in px]].add_prefix("ABS:"), rel], axis=1)
    ma = rel.rolling(WIN, min_periods=WIN).mean()
    sd = rel.rolling(WIN, min_periods=WIN).std()
    pct = (rel / ma - 1) * 100
    spr = (rel - ma) / sd.replace(0, np.nan)
    last = pct.index[-1]
    wk = pct.index[max(0, len(pct.index) - 6)]
    rows = []
    for t, name, grp in [("ABS:" + a, b, c) for a, b, c in ABSOLUTE] + RELATIVE:
        if t in pct and pd.notna(pct.at[last, t]):
            rows.append([last.strftime("%Y-%m-%d"), t.replace("ABS:", ""), name, grp,
                         round(float(pct.at[last, t]), 2), round(float(spr.at[last, t]), 2),
                         round(float(pct.at[wk, t]), 2) if pd.notna(pct.at[wk, t]) else ""])
    with open(os.path.join(D, "relative.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Ticker", "Name", "Group", "Pct", "Spread", "Pct1w"])
        w.writerows(rows)
    print(f"[relative] {len(rows)}/{len(RELATIVE) + len(ABSOLUTE)} vs {RELATIVE_BENCH} as of {last.date()}")


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
