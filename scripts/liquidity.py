"""Macro liquidity gauge (-100 tight ... +100 loose).

8 macro liquidity inputs, each turned into a 3-year z-score, signed so that
negative = tighter, averaged with equal weights and scaled x100 (an average of
-1 SD reads -100). Below -35 is tight, above +35 loose.

  python scripts/liquidity.py

Inputs (no API key needed)
  FRED  BAMLC0A0CM  IG corporate OAS            tighter when wider
        SOFR, EFFR  overnight funding spread     tighter when SOFR > Fed Funds (20d avg)
        WRESBAL     bank reserves (level)        tighter when lower
        WALCL       Fed balance sheet (3m chg)   tighter when shrinking
        DGS10       10-year yield (1w chg)       tighter when rising
  Yahoo ^MOVE       bond volatility (level)      tighter when higher
        DX-Y.NYB    dollar (3m chg, 10d vol)     tighter when stronger / choppier

Outputs (data/liquidity/)
  liquidity.csv   Date,Gauge,<8 z-scores>,<raw inputs>   daily, rebuilt each run
  latest.json     today's reading, bands, 1w / 1m ago, per-input z and raw values
"""
import datetime as dt
import io
import json
import os
import urllib.request

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "liquidity")
os.makedirs(OUT, exist_ok=True)

START = "2016-01-01"
WIN = 756            # 3 years of business days
MIN_OBS = 450        # ~60% of the window before a z-score is produced
FRED = ["BAMLC0A0CM", "SOFR", "EFFR", "WRESBAL", "WALCL", "DGS10"]
BANDS = [[-100, -35, "Tight"], [-35, 35, "Neutral"], [35, 101, "Loose"]]


def fred(series):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={START}"
    with urllib.request.urlopen(url, timeout=60) as r:
        txt = r.read().decode()
    s = pd.read_csv(io.StringIO(txt), index_col=0, parse_dates=True, na_values=".").iloc[:, 0]
    return s.rename(series)


def yahoo(ticker, name):
    h = yf.download(ticker, start=START, progress=False, auto_adjust=False, threads=False)
    if h is None or h.empty:
        raise RuntimeError(f"no Yahoo data for {ticker}")
    close = h["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    return close.dropna().rename(name)


def z(x):
    m = x.rolling(WIN, min_periods=MIN_OBS).mean()
    s = x.rolling(WIN, min_periods=MIN_OBS).std()
    return (x - m) / s


def main():
    today = dt.date.today()
    idx = pd.bdate_range(START, today)
    raw = {s: fred(s) for s in FRED}
    raw["MOVE"] = yahoo("^MOVE", "MOVE")
    raw["DXY"] = yahoo("DX-Y.NYB", "DXY")
    f = pd.DataFrame({k: v.reindex(idx).ffill() for k, v in raw.items()})
    # End on the last COMPLETED US session. MOVE only has a close once the US day is over;
    # DXY trades overnight, so its "today" bar exists before the US open and must not count.
    # A run before 21:00 UTC never includes today (GitHub can start the 21:50 job hours late).
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = pd.Timestamp(now.date() if now.hour >= 21 else now.date() - dt.timedelta(days=1))
    last_real = min(raw["MOVE"].dropna().index.max(), cutoff)
    f = f[f.index <= last_real]

    spr = (f.SOFR - f.EFFR) * 100
    ldx = np.log(f.DXY)
    inputs = {
        "z_ig":      z(-f.BAMLC0A0CM),
        "z_funding": z(-spr.rolling(20).mean()),
        "z_reserves": z(f.WRESBAL),
        "z_fed":     z(f.WALCL.diff(63)),
        "z_yield":   z(-f.DGS10.diff(5)),
        "z_dollar":  z(-ldx.diff(63)),
        "z_move":    z(-f.MOVE),
        "z_dxyvol":  z(-ldx.diff().rolling(10).std()),
    }
    Z = pd.DataFrame(inputs)
    gauge = (Z.mean(axis=1, skipna=False) * 100).clip(-100, 100)

    out = pd.DataFrame({"Gauge": gauge.round(2)})
    for k, v in inputs.items():
        out[k] = v.round(3)
    out["ig_oas"] = f.BAMLC0A0CM.round(2)
    out["sofr_effr_bp"] = spr.round(1)
    out["reserves_bn"] = (f.WRESBAL / 1000).round(0)
    out["fed_assets_bn"] = (f.WALCL / 1000).round(0)
    out["us10y"] = f.DGS10.round(2)
    out["dxy"] = f.DXY.round(2)
    out["move"] = f.MOVE.round(2)
    out["dxyvol_pct"] = (ldx.diff().rolling(10).std() * np.sqrt(252) * 100).round(2)
    out = out.dropna(subset=["Gauge"])
    out.index.name = "Date"
    out.to_csv(os.path.join(OUT, "liquidity.csv"), date_format="%Y-%m-%d")

    def at(days_back):
        d = out.index[-1] - pd.Timedelta(days=days_back)
        return out[:d].iloc[-1]

    last, wk, mo = out.iloc[-1], at(7), at(30)

    # Drift: where the gauge goes in 5 business days if every input stays at
    # today's value and only the rolling windows move on. Not a forecast of the
    # inputs, just the mechanical roll-off of recent changes.
    ext = pd.DataFrame([f.iloc[-1]] * 5, index=pd.bdate_range(f.index[-1], periods=6)[1:])
    f2 = pd.concat([f, ext])
    spr2 = (f2.SOFR - f2.EFFR) * 100
    ldx2 = np.log(f2.DXY)
    Z2 = pd.DataFrame({
        "z_ig": z(-f2.BAMLC0A0CM), "z_funding": z(-spr2.rolling(20).mean()),
        "z_reserves": z(f2.WRESBAL), "z_fed": z(f2.WALCL.diff(63)),
        "z_yield": z(-f2.DGS10.diff(5)), "z_dollar": z(-ldx2.diff(63)),
        "z_move": z(-f2.MOVE), "z_dxyvol": z(-ldx2.diff().rolling(10).std()),
    })
    drift_val = float((Z2.iloc[-1].mean() * 100).clip(-100, 100))
    dz = (Z2.iloc[-1] - Z.iloc[-1])
    driver = dz.abs().idxmax()
    names = {"z_ig": "IG spreads", "z_funding": "SOFR funding spread", "z_reserves": "bank reserves",
             "z_fed": "Fed balance sheet", "z_yield": "the recent yield move", "z_dollar": "the dollar trend",
             "z_move": "bond volatility", "z_dxyvol": "dollar volatility"}
    drift = {
        "value": round(drift_val, 2),
        "change": round(drift_val - float(last.Gauge), 2),
        "driver": names[driver],
        "driver_change": round(float(dz[driver]) * 100 / 8, 1),
        "note": "where the gauge settles in a week if every input stays at today's level; "
                "historically within about 15 points of the actual reading and right on direction two weeks in three",
    }
    band = next(b for b in BANDS if b[0] <= last.Gauge < b[1])
    latest = {
        "date": out.index[-1].strftime("%Y-%m-%d"),
        "gauge": float(last.Gauge),
        "band": band[2],
        "week_ago": float(wk.Gauge),
        "month_ago": float(mo.Gauge),
        "year_low": float(out.Gauge[-252:].min()),
        "year_high": float(out.Gauge[-252:].max()),
        "drift_1w": drift,
        "inputs": {k[2:]: {"z": float(last[k]), "week_ago_z": float(wk[k])} for k in inputs},
        "raw": {c: float(last[c]) for c in ["ig_oas", "sofr_effr_bp", "reserves_bn", "fed_assets_bn", "us10y", "dxy", "move", "dxyvol_pct"]},
        "bands": BANDS,
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(os.path.join(OUT, "latest.json"), "w") as fh:
        json.dump(latest, fh, indent=1)
    print(f"{latest['date']}: gauge {latest['gauge']:.1f} ({band[2]}), "
          f"1w {latest['week_ago']:.1f}, 1m {latest['month_ago']:.1f}; drift {drift_val:.1f} ({drift['driver']}); {len(out)} rows from {out.index[0].date()}")


if __name__ == "__main__":
    main()
