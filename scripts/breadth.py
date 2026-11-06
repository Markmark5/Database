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
  <key>_index_spread.csv  Date,Close,Spread,RSI       index, rebuilt each run
  <key>_breadth.csv         Date,N,OB,OS,XOB,XOS,RSI50,A50,A100,A200,Source
                            % of members; history is FROZEN:
                            past rows are never recalculated, so each day keeps the
                            membership it had at the time. Source = backfill | live.
  <key>_latest.csv          TIDM,Name,Close,Spread,PctVs50,RSI   today's reading per stock

  RSI50 = % of members with RSI(14) above 50; A50/A100/A200 = % above that moving average.
  RSI is the standard 14-day Wilder RSI.

Price store
  Daily closes are kept between runs in $PRICE_STORE (default .price-store/), which the
  Action fetches from / saves back to the "price-data" GitHub release. Each run tops the
  store up with the last few weeks; a ticker whose stored prices no longer match Yahoo's
  (split, dividend adjustment, correction) is re-downloaded in full, and the whole store
  is re-downloaded once a week.
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
HISTORY_DAYS = 5 * 365 + 10     # 5 years: enough for 200-day averages and the 5Y view
STORE = os.environ.get("PRICE_STORE", os.path.join(ROOT, ".price-store"))
TOP_UP_DAYS = 15                # each run re-fetches this many recent days
FULL_REFRESH_DAYS = 7           # re-download everything at least this often
MATCH_TOL = 0.005               # stored vs fresh price gap that triggers a re-download


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
    # only needed when a recent finished session is missing prices that the ticker
    # normally has (or the newest session is missing altogether)
    recent = df.loc[df.index >= df.index.max() - pd.Timedelta(days=days), [t for t in tickers if t in df]]
    before = df[[t for t in tickers if t in df]].iloc[:-len(recent) or None].iloc[-40:]
    had = before.notna().mean() > 0.8          # tickers that normally have a price
    gaps = int((recent.isna() & had).sum().sum())
    now = pd.Timestamp.now(tz=tz)
    expect = now.date() if now.time() >= close else (now - pd.Timedelta(days=1)).date()
    while expect.weekday() >= 5:
        expect -= dt.timedelta(days=1)
    if gaps == 0 and len(df) and df.index.max().date() >= expect:
        return df
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


def rsi(df, n=14):
    """Wilder's RSI (the standard 14-day RSI used by ShareScope and most charting tools)."""
    d = df.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d).clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def _store_paths(name):
    return os.path.join(STORE, f"{name}.csv.gz"), os.path.join(STORE, "meta.json")


def load_prices(name, tickers, start):
    """Daily closes for `tickers` from the store, topped up from Yahoo.

    Full download when there is no store, it is over a week old, or FULL_REFRESH
    is set; otherwise only the last few weeks are fetched, and any ticker whose
    stored prices disagree with Yahoo's on the overlapping days is re-fetched in full."""
    import json
    path, meta_path = _store_paths(name)
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
    last_full = meta.get(name, {}).get("full")
    stale = not last_full or (dt.date.today() - dt.date.fromisoformat(last_full)).days >= FULL_REFRESH_DAYS
    if os.environ.get("FULL_REFRESH") or stale or not os.path.exists(path):
        print(f"  [{name}] full download from {start}")
        px = download(tickers, start)
        px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
        meta.setdefault(name, {})["full"] = dt.date.today().isoformat()
    else:
        store = pd.read_csv(path, index_col=0, parse_dates=True)
        recent = download(tickers, (dt.date.today() - dt.timedelta(days=TOP_UP_DAYS)).isoformat())
        recent.index = pd.to_datetime(recent.index).tz_localize(None).normalize()
        ov = recent.index.intersection(store.index)
        redo = [t for t in tickers if t not in store or store[t].count() == 0]
        for t in tickers:
            if t in store and t in recent and t not in redo:
                a, b = store.loc[ov, t], recent.loc[ov, t]
                m = a.notna() & b.notna() & (a != 0)
                if m.any() and ((b[m] / a[m] - 1).abs() > MATCH_TOL).any():
                    redo.append(t)
        if redo:
            print(f"  [{name}] re-downloading full history for {len(redo)}: {redo[:12]}{' …' if len(redo) > 12 else ''}")
            hist = download(redo, start)
            hist.index = pd.to_datetime(hist.index).tz_localize(None).normalize()
            store = pd.concat([store.drop(columns=[c for c in redo if c in store]), hist], axis=1)
        px = recent.combine_first(store.reindex(columns=tickers))
        print(f"  [{name}] topped up {len(recent.index)} recent days from store ({len(store.index)} days)")
    meta.setdefault(name, {})["updated"] = dt.date.today().isoformat()
    return px.sort_index().loc[start:].reindex(columns=tickers), meta


def save_prices(name, px, meta):
    import json
    os.makedirs(STORE, exist_ok=True)
    path, meta_path = _store_paths(name)
    px.round(4).to_csv(path, date_format="%Y-%m-%d", compression="gzip")
    json.dump(meta, open(meta_path, "w"), indent=1)


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
    allT = list(tick.values()) + [INDEX_TICKER]
    px, meta = load_prices(key, allT, start)
    market = cfg.get("market", "UK")
    px = drop_open_session(px, market)
    px = drop_open_session(hourly_fill(px, allT, market), market)
    save_prices(key, px, meta)          # finished sessions only

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
    out = pd.DataFrame({"Close": idx.round(2), "Spread": isp.iloc[:, 0].round(3),
                        "RSI": rsi(idx).round(2)}).dropna(subset=["Spread"])
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
    r = rsi(stocks)
    nr = r.notna().sum(axis=1)
    calc["RSI50"] = ((r > 50).sum(axis=1) / nr * 100).where(nr >= len(tick) * 0.5)
    for w in (50, 100, 200):
        ma = stocks.rolling(w, min_periods=w).mean()
        ok = ma.notna() & stocks.notna()
        nw = ok.sum(axis=1)
        calc[f"A{w}"] = (((stocks > ma) & ok).sum(axis=1) / nw * 100).where(nw >= len(tick) * 0.5)
    calc = calc[calc["N"] >= len(tick) * 0.5].round(2)   # skip warm-up / thin days
    calc.index = calc.index.strftime("%Y-%m-%d")

    # ---- freeze history: keep stored rows, recompute only from the last stored date on
    if os.path.exists(BREADTH):
        old = pd.read_csv(BREADTH, index_col="Date", dtype={"Source": str})
        last = old.index.max()
        keep = old[old.index < last]
        new = calc[calc.index >= last].copy()
        new["Source"] = "live"
        earlier = calc[calc.index < old.index.min()].copy()      # longer history: backfill
        earlier["Source"] = "backfill"
        merged = pd.concat([earlier, keep, new])
        # fill columns added later (RSI50, A50 ...) and any missing days from today's members
        merged = merged.combine_first(calc).sort_index()
        merged["Source"] = merged["Source"].fillna("backfill")
        merged = merged[list(calc.columns) + ["Source"]]
    else:
        merged = calc.copy()
        merged["Source"] = "backfill"
        merged.iloc[-1, merged.columns.get_loc("Source")] = "live"
    merged.index.name = "Date"
    merged.to_csv(BREADTH)

    # ---- today's per-stock table
    last_row = sp.iloc[-1]
    last_rsi = r.iloc[-1]
    rows = []
    for t, y in tick.items():
        if y in sp and pd.notna(last_row.get(y)):
            rows.append([t, members[t], round(float(stocks[y].iloc[-1]), 4),
                         round(float(last_row[y]), 3), round(float(pct[y].iloc[-1]), 2),
                         round(float(last_rsi[y]), 1) if pd.notna(last_rsi.get(y)) else ""])
    rows.sort(key=lambda r: -r[3])
    with open(LATEST, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["TIDM", "Name", "Close", "Spread", "PctVs50", "RSI"])
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
    own = rsi(px)                                  # RSI of each ETF's own price
    rows = []
    for t, name, grp in [("ABS:" + a, b, c) for a, b, c in ABSOLUTE] + RELATIVE:
        if t in pct and pd.notna(pct.at[last, t]):
            rows.append([last.strftime("%Y-%m-%d"), t.replace("ABS:", ""), name, grp,
                         round(float(pct.at[last, t]), 2), round(float(spr.at[last, t]), 2),
                         round(float(pct.at[wk, t]), 2) if pd.notna(pct.at[wk, t]) else "",
                         round(float(own.at[last, t.replace("ABS:", "")]), 1)])
    with open(os.path.join(D, "relative.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Ticker", "Name", "Group", "Pct", "Spread", "Pct1w", "RSI"])
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
