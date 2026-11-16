"""Modelled CTA (trend-following fund) positioning for the S&P 500 and FTSE 250.

Built from index closes only. For each lookback (1, 3 and 6 months) the trend strength
(log return / (volatility * sqrt(lookback))) is squashed to +/-1 with tanh and averaged,
then scaled down when volatility is above a 20% annual target, as trend funds do.
The position moves a third of the way toward that target each day (about a 5-day lag).
Scale: -100 (fully short) to +100 (fully long).

Zones: "heavily long" = top 15% of readings since 2021, "heavily short" = bottom 5%.
Writes data/cta/cta.json (read by the Fear & Greed popup in Database_v2.html).
"""
import json
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import yfinance as yf

OUT = os.path.join(os.path.dirname(__file__), '..', 'data', 'cta', 'cta.json')
INDICES = {'sp500': ('^GSPC', 'S&P 500'), 'ftse250': ('^FTMC', 'FTSE 250')}
LOOKBACKS, K, VOL_SPAN, VOL_TARGET, FOLLOW = (20, 60, 120), 0.7, 60, 0.20, 1 / 3
START = '2021-01-01'


def closes(tk):
    for _ in range(3):
        px = yf.download(tk, start='2019-01-01', progress=False, auto_adjust=True)['Close']
        if isinstance(px, pd.DataFrame):
            px = px.iloc[:, 0]
        px = px.dropna()
        if len(px) > 500:
            return px
    raise SystemExit(f'no price data for {tk}')


def model(px):
    r = np.log(px).diff()
    vol = r.ewm(span=VOL_SPAN).std()
    sig = pd.concat([np.tanh(K * np.log(px / px.shift(L)) / (vol * np.sqrt(L))) for L in LOOKBACKS], axis=1).mean(axis=1)
    raw = (sig * (VOL_TARGET / (vol * np.sqrt(252))).clip(upper=1.0) * 100).dropna()
    pos, p = np.empty(len(raw)), raw.iloc[0]
    for i, t in enumerate(raw.values):
        p += FOLLOW * (t - p)
        pos[i] = p
    return raw.loc[START:], pd.Series(pos, index=raw.index).loc[START:]


def main():
    prev = {}
    if os.path.exists(OUT):
        prev = json.load(open(OUT)).get('idx', {})
    out = {}
    for key, (tk, name) in INDICES.items():
        try:
            px = closes(tk)
        except SystemExit as e:
            print(e)
            if key in prev:
                out[key] = prev[key]          # keep yesterday's data rather than drop the chart
            continue
        raw, pos = model(px)
        out[key] = {
            'name': name,
            'dates': [d.strftime('%Y-%m-%d') for d in pos.index],
            'px': px.reindex(pos.index).round(1).tolist(),
            'raw': raw.round(1).tolist(),
            'pos': pos.round(1).tolist(),
            'hi': round(float(np.percentile(pos, 85))),
            'lo': round(float(np.percentile(pos, 5))),
        }
        print(f"{name}: {out[key]['dates'][-1]} position {pos.iloc[-1]:+.0f} (zones {out[key]['lo']} / +{out[key]['hi']})")
    if not out:
        raise SystemExit('nothing computed')
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({'updated': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'), 'idx': out},
              open(OUT, 'w'), separators=(',', ':'))


if __name__ == '__main__':
    main()
