"""
Market thermometer: one score from -100 (washed out, bottom zone) to +100
(overheated, top risk), built from the indicators traders use to call
short-term extremes:

  VIX weekly close         Fixed bands from the classic VIX range read: ~28+ is
                           where stocks have been bought, ~14 is complacency.
  VIX weekly RSI(14)       VIX overbought = equities oversold.
  VIX / VIX3M              Above 1 (backwardation) = near-term panic.
  McClellan, broad         Ratio-adjusted McClellan oscillator on S&P 1500
                           constituents (stand-in for $NYMO).
  McClellan, Nasdaq-100    Same on Nasdaq-100 constituents (stand-in for $NAMO).
  % above 50-day           Share of S&P 1500 names over their 50-day average.
  SPY / QQQ RSI(14)        Daily momentum.
  SPY vs 50-day            Extension, in ATRs.

Each indicator is turned into a "temperature" by its percentile rank over the
trailing 3 years (only past data, so the backtest has no look-ahead), flipped
where high readings mean fear. The composite is the average. The backtest then
groups every past day by composite bucket and reports SPY's forward returns.

Caveat: breadth uses today's index members back through history (survivorship
bias), so old breadth readings run a little strong.
"""

from __future__ import annotations

import io
import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests
import yfinance as yf

UA = {"User-Agent": "Mozilla/5.0 (market-thermometer)"}
WINDOW = 756          # 3 trading years for percentile ranks
HORIZONS = (5, 20, 60)
BUCKETS = [(-101, -60, "Washout"), (-60, -25, "Cold"), (-25, 25, "Neutral"),
           (25, 60, "Warm"), (60, 101, "Overheated")]


# ---------------------------------------------------------------- data
def nasdaq100() -> list[str]:
    html = requests.get("https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies", headers=UA, timeout=30).text
    for t in pd.read_html(io.StringIO(html)):
        col = next((c for c in t.columns if str(c).lower() in ("ticker", "symbol")), None)
        if col is not None and len(t) > 90:
            return [str(s).replace(".", "-") for s in t[col]]
    raise RuntimeError("Nasdaq-100 table not found")


def closes(tickers, period="12y", chunk=200) -> pd.DataFrame:
    frames = []
    for i in range(0, len(tickers), chunk):
        d = yf.download(tickers[i:i + chunk], period=period, interval="1d",
                        auto_adjust=True, progress=False, threads=True)
        if d.empty:
            continue
        c = d["Close"] if isinstance(d.columns, pd.MultiIndex) else d[["Close"]].rename(columns={"Close": tickers[i]})
        frames.append(c)
    out = pd.concat(frames, axis=1)
    out.index = pd.to_datetime(out.index).tz_localize(None)
    out = out.loc[:, ~out.columns.duplicated()].sort_index()
    return out[out.notna().mean(axis=1) > 0.5]   # Yahoo sometimes adds an empty row for today


def ohlc(ticker, period="12y") -> pd.DataFrame:
    d = yf.Ticker(ticker).history(period=period, auto_adjust=True)
    d.index = pd.to_datetime(d.index).tz_localize(None).normalize()
    return d.dropna(subset=["Close"])


# ---------------------------------------------------------------- indicators
def rsi(s: pd.Series, n=14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def mcclellan(px: pd.DataFrame) -> pd.Series:
    """Ratio-adjusted McClellan oscillator from member closes."""
    ch = px.diff()
    adv, dec = (ch > 0).sum(axis=1), (ch < 0).sum(axis=1)
    tot = (adv + dec).replace(0, np.nan)
    rana = (adv - dec) / tot * 1000
    rana = rana[px.notna().sum(axis=1) > 0.5 * px.shape[1]]   # skip thin early rows
    return (rana.ewm(span=19, adjust=False).mean() - rana.ewm(span=39, adjust=False).mean()).iloc[1:]


def pct_above_ma(px: pd.DataFrame, n=50) -> pd.Series:
    ma = px.rolling(n).mean()
    valid = ma.notna() & px.notna()
    return ((px > ma) & valid).sum(axis=1) / valid.sum(axis=1).replace(0, np.nan) * 100


def roll_pct(s: pd.Series, n=WINDOW) -> pd.Series:
    """Percentile (0-100) of each value within its trailing n observations."""
    s = s.dropna()
    return s.rolling(n, min_periods=n // 2).apply(lambda w: (w[:-1] < w[-1]).mean() * 100
                                                  + (w[:-1] == w[-1]).mean() * 50, raw=True)


def band(s: pd.Series, cold: float, hot: float) -> pd.Series:
    """Linear map: value at `cold` -> -100, at `hot` -> +100 (clipped)."""
    return ((s - cold) / (hot - cold) * 200 - 100).clip(-100, 100)


# ---------------------------------------------------------------- build
def build(sp1500: list[str]) -> dict:
    vix, vix3m = ohlc("^VIX")["Close"], ohlc("^VIX3M")["Close"]
    spy, qqq = ohlc("SPY"), ohlc("QQQ")
    broad = closes(sp1500)
    ndx = closes(nasdaq100())
    idx = spy.index

    vix_w = vix.resample("W-FRI").last()
    # Weekly readings carried to the daily grid; the live week uses the latest close.
    def weekly(series_w):
        return series_w.reindex(idx, method="ffill")
    vix_d = vix.reindex(idx).ffill()
    vix_wrsi = rsi(vix_w)
    # include the in-progress week so today's value is live
    vix_wrsi_d = pd.Series(index=idx, dtype=float)
    for d in idx[-10:]:
        w = vix[:d].resample("W-FRI").last()
        vix_wrsi_d[d] = rsi(w).iloc[-1]
    vix_wrsi_d = vix_wrsi_d.combine_first(weekly(vix_wrsi))

    tr = pd.concat([spy.High - spy.Low, (spy.High - spy.Close.shift()).abs(),
                    (spy.Low - spy.Close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()

    raw = pd.DataFrame({
        "vix": vix_d,
        "vix_wrsi": vix_wrsi_d,
        "vix_term": (vix / vix3m).reindex(idx).ffill(),
        "mco_broad": mcclellan(broad).reindex(idx),
        "mco_ndx": mcclellan(ndx).reindex(idx),
        "above50": pct_above_ma(broad).reindex(idx),
        "spy_rsi": rsi(spy.Close),
        "qqq_rsi": rsi(qqq.Close).reindex(idx),
        "spy_ext": (spy.Close - spy.Close.rolling(50).mean()) / atr,
    })

    # Temperatures: +100 hot (top risk), -100 cold (bottom zone)
    temp = pd.DataFrame(index=idx)
    temp["vix"] = -band(raw.vix, 14.3, 28.3)                       # fixed range read
    temp["vix_wrsi"] = -band(raw.vix_wrsi, 35, 70)
    temp["vix_term"] = -band(raw.vix_term, 0.82, 1.02)
    for k in ("mco_broad", "mco_ndx", "above50", "spy_ext"):
        temp[k] = ((roll_pct(raw[k]) - 50) * 2).reindex(idx)
    temp["spy_rsi"] = band(raw.spy_rsi, 30, 70)
    temp["qqq_rsi"] = band(raw.qqq_rsi, 30, 70)
    score = temp.mean(axis=1, skipna=True).where(temp.notna().sum(axis=1) >= 6)

    # ---- backtest: SPY forward returns by bucket
    fwd = {h: spy.Close.shift(-h) / spy.Close - 1 for h in HORIZONS}
    bt = []
    for lo, hi, name in BUCKETS:
        m = (score > lo) & (score <= hi)
        row = {"bucket": name, "lo": lo, "hi": hi, "days": int(m.sum())}
        for h in HORIZONS:
            r = fwd[h][m].dropna()
            row[f"avg{h}"] = round(float(r.mean()) * 100, 2) if len(r) else None
            row[f"win{h}"] = round(float((r > 0).mean()) * 100, 1) if len(r) else None
        bt.append(row)
    base = {f"avg{h}": round(float(fwd[h].dropna().mean()) * 100, 2) for h in HORIZONS}
    base.update({f"win{h}": round(float((fwd[h].dropna() > 0).mean()) * 100, 1) for h in HORIZONS})

    last = score.last_valid_index()
    meta = {
        "vix": ("VIX", "Multi-year range: 28+ = fear high, ~14 = complacent", "{:.2f}"),
        "vix_wrsi": ("VIX weekly RSI", ">70 = fear spike (stocks oversold)", "{:.1f}"),
        "vix_term": ("VIX / VIX3M", ">1.0 = backwardation, near-term panic", "{:.3f}"),
        "mco_broad": ("McClellan, S&P 1500", "NYMO stand-in; deep negative = washout", "{:.1f}"),
        "mco_ndx": ("McClellan, Nasdaq-100", "NAMO stand-in", "{:.1f}"),
        "above50": ("% above 50-day", "S&P 1500 breadth; <20% washout, >80% stretched", "{:.0f}%"),
        "spy_rsi": ("SPY RSI(14)", "<30 oversold, >70 overbought", "{:.1f}"),
        "qqq_rsi": ("QQQ RSI(14)", "<30 oversold, >70 overbought", "{:.1f}"),
        "spy_ext": ("SPY vs 50-day", "Distance in ATRs; beyond ±3 is stretched", "{:+.2f} ATR"),
    }
    comps = []
    for k, (label, note, fmt) in meta.items():
        v, t = raw.at[last, k], temp.at[last, k]
        comps.append({"key": k, "label": label, "note": note,
                      "value": None if pd.isna(v) else fmt.format(v),
                      "temp": None if pd.isna(t) else round(float(t))})

    hist = score.dropna().iloc[-260:]
    def r(x):
        return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 1)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": last.strftime("%Y-%m-%d"),
        "score": r(score[last]),
        "bucket": next(n for lo, hi, n in BUCKETS if lo < score[last] <= hi),
        "components": comps,
        "history": [{"d": d.strftime("%Y-%m-%d"), "s": r(v), "spy": r(spy.Close[d])} for d, v in hist.items()],
        "backtest": {"buckets": bt, "baseline": base,
                     "from": score.first_valid_index().strftime("%Y-%m-%d"), "to": last.strftime("%Y-%m-%d")},
        "universe": {"broad": int(broad.shape[1]), "ndx": int(ndx.shape[1])},
    }
