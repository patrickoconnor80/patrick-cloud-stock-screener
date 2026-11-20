"""
Market cycle: where the broad market sits between a washout and a blow-off,
and which way it is moving.

  Score (-100..+100)   How stretched the market is right now: VIX against its
                       multi-year range and its weekly RSI, VIX term structure,
                       McClellan oscillators, breadth (% above 50- and 200-day),
                       SPY/QQQ RSI and extension. Fixed bands or 3-year
                       trailing percentiles, so the backtest has no look-ahead.
  Phase                Score level x direction (5-day change of the smoothed
                       score): Decline (cold, falling), Bottoming (cold,
                       rising), Advance (hot, rising), Topping (hot, falling).
  Breadth              % of Nasdaq-listed common stocks above their 200-day
                       average, weekly, against the Nasdaq Composite, with the
                       25% correction and 15% accumulation zones.
  Beta                 High-beta vs low-volatility leadership (SPHB/SPLV) and
                       SPHB momentum, since high-beta names amplify every swing.
  Macro backdrop       Rate path priced by 2-year yields vs T-bills, 10-year
                       yield, inflation expectations (TIP/IEF), credit (HYG/IEF)
                       and the dollar, scored as tailwind/headwind for beta.
                       Kept out of the score: macro moves over months and does
                       not time tops and bottoms.

Survivorship caveat: member lists are today's, so older breadth runs a little
strong (most for the Nasdaq-wide 200-day series, where delisted names are many).
"""

from __future__ import annotations

import io
import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests
import yfinance as yf

UA = {"User-Agent": "Mozilla/5.0 (market-cycle)"}
WINDOW = 756          # 3 trading years for percentile ranks
HORIZONS = (5, 20, 60)
BUCKETS = [(-101, -60, "Washout"), (-60, -25, "Cold"), (-25, 25, "Neutral"),
           (25, 60, "Warm"), (60, 101, "Overheated")]
PHASES = ["Decline", "Bottoming", "Advance", "Topping"]


# ---------------------------------------------------------------- universes
def nasdaq100() -> list[str]:
    html = requests.get("https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies", headers=UA, timeout=30).text
    for t in pd.read_html(io.StringIO(html)):
        col = next((c for c in t.columns if str(c).lower() in ("ticker", "symbol")), None)
        if col is not None and len(t) > 90:
            return [str(s).replace(".", "-") for s in t[col]]
    raise RuntimeError("Nasdaq-100 table not found")


def nasdaq_listed() -> list[str]:
    """Nasdaq-listed operating companies (no ETFs, warrants, units, preferreds)."""
    txt = requests.get("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", headers=UA, timeout=30).text
    d = pd.read_csv(io.StringIO(txt), sep="|", dtype=str)
    d = d[d.Symbol.notna() & ~d.Symbol.str.startswith("File Creation", na=False)]
    d = d[(d.ETF == "N") & (d["Test Issue"] == "N") & (d["Financial Status"] == "N")]
    name = d["Security Name"].fillna("")
    keep = name.str.contains(r"Common Stock|Ordinary Share|Class [ABC]", case=False) & \
        ~name.str.contains(r"Warrant|Right|Unit|Preferred|Depositary|Notes|Debenture", case=False)
    return sorted(d.Symbol[keep].str.replace(".", "-", regex=False).unique())


# ---------------------------------------------------------------- data
def closes(tickers, period="12y", chunk=250) -> pd.DataFrame:
    frames = []
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        try:
            d = yf.download(part, period=period, interval="1d", auto_adjust=True,
                            progress=False, threads=16)
        except Exception as ex:
            print(f"download chunk {i} failed: {ex}")
            continue
        if d.empty:
            continue
        c = d["Close"] if isinstance(d.columns, pd.MultiIndex) else d[["Close"]].rename(columns={"Close": part[0]})
        frames.append(c.astype("float32"))
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
    rana = (adv - dec) / (adv + dec).replace(0, np.nan) * 1000
    rana = rana[px.notna().sum(axis=1) > 0.5 * px.shape[1]]
    return (rana.ewm(span=19, adjust=False).mean() - rana.ewm(span=39, adjust=False).mean()).iloc[1:]


def pct_above_ma(px: pd.DataFrame, n) -> pd.Series:
    ma = px.rolling(n, min_periods=n).mean()
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


def rnd(x, n=1):
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) or math.isinf(x) else round(x, n)


def phase_of(level, slope):
    if level < 0:
        return "Decline" if slope < 0 else "Bottoming"
    return "Advance" if slope >= 0 else "Topping"


# ---------------------------------------------------------------- macro / beta
def macro_panel(px: dict) -> dict:
    """Each item: value, 3-month change, and whether it is a tailwind or a
    headwind for high-beta stocks."""
    def last(s):
        return s.dropna().iloc[-1]

    def chg(s, n=63):
        s = s.dropna()
        return s.iloc[-1] - s.iloc[-1 - n]

    def pchg(s, n=63):
        s = s.dropna()
        return (s.iloc[-1] / s.iloc[-1 - n] - 1) * 100

    def spark(s):
        w = s.dropna().iloc[-260:].resample("W-FRI").last().dropna()
        return [rnd(v, 3) for v in w]

    path = (px["2YY=F"] - px["^IRX"]).dropna() * 100           # bp
    tnx = px["^TNX"].dropna()
    infl = (px["TIP"] / px["IEF"]).dropna()
    credit = (px["HYG"] / px["IEF"]).dropna()
    dxy = px["DX-Y.NYB"].dropna()

    def sig(x, good_below, bad_above):
        return "tailwind" if x < good_below else "headwind" if x > bad_above else "neutral"

    items = [
        {"key": "path", "label": "Rate path priced", "value": f"{last(path):+.0f} bp",
         "detail": "2-year yield minus 3-month T-bill. Below zero = cuts priced, above = hikes priced.",
         "change": f"{chg(path):+.0f} bp in 3 mo", "signal": sig(last(path), -25, 25), "spark": spark(path)},
        {"key": "tnx", "label": "10-year Treasury yield", "value": f"{last(tnx):.2f}%",
         "detail": "Long-duration growth and beta names reprice hardest when long yields rise.",
         "change": f"{chg(tnx) * 100:+.0f} bp in 3 mo", "signal": sig(chg(tnx), -0.25, 0.25), "spark": spark(tnx)},
        {"key": "infl", "label": "Inflation expectations", "value": f"{pchg(infl):+.1f}%",
         "detail": "TIPS vs nominal Treasuries (TIP/IEF), 3-month change. Rising = market pricing hotter inflation.",
         "change": "TIP/IEF, 3 mo", "signal": sig(pchg(infl), -1, 1), "spark": spark(infl)},
        {"key": "credit", "label": "Credit appetite", "value": f"{pchg(credit):+.1f}%",
         "detail": "High-yield bonds vs Treasuries (HYG/IEF), 3-month change. Falling = spreads widening, risk-off.",
         "change": "HYG/IEF, 3 mo", "signal": "headwind" if pchg(credit) < -1.5 else "tailwind" if pchg(credit) > 1.5 else "neutral",
         "spark": spark(credit)},
        {"key": "dxy", "label": "US dollar", "value": f"{last(dxy):.1f}",
         "detail": "A rising dollar tightens global liquidity, which speculative names feel first.",
         "change": f"{pchg(dxy):+.1f}% in 3 mo", "signal": sig(pchg(dxy), -2.5, 2.5), "spark": spark(dxy)},
    ]
    tw = sum(i["signal"] == "tailwind" for i in items)
    hw = sum(i["signal"] == "headwind" for i in items)
    overall = "Tailwind" if tw - hw >= 2 else "Headwind" if hw - tw >= 2 else "Mixed"
    return {"overall": overall, "tailwinds": tw, "headwinds": hw, "items": items}


def beta_panel(sphb: pd.Series, splv: pd.Series, spy: pd.Series) -> dict:
    ratio = (sphb / splv).dropna()
    ma50 = ratio.rolling(50).mean()
    r20 = (ratio.iloc[-1] / ratio.iloc[-21] - 1) * 100
    above = ratio.iloc[-1] > ma50.iloc[-1]
    lead = ("Beta leading" if above and r20 > 0 else "Beta lagging" if not above and r20 < 0
            else "Beta turning up" if r20 > 0 else "Beta rolling over")
    sphb_rsi = rsi(sphb).iloc[-1]
    ext50 = (sphb.iloc[-1] / sphb.rolling(50).mean().iloc[-1] - 1) * 100
    ext200 = (sphb.iloc[-1] / sphb.rolling(200).mean().iloc[-1] - 1) * 100
    # realized beta of SPHB to SPY, 60 days
    r = pd.concat([sphb.pct_change(), spy.pct_change()], axis=1).dropna().iloc[-60:]
    beta = r.cov().iloc[0, 1] / r.iloc[:, 1].var()
    tail = ratio.iloc[-260:]
    return {
        "status": lead,
        "stats": [
            {"label": "SPHB / SPLV vs 50-day", "value": f"{(ratio.iloc[-1] / ma50.iloc[-1] - 1) * 100:+.1f}%"},
            {"label": "SPHB / SPLV, 20-day change", "value": f"{r20:+.1f}%"},
            {"label": "SPHB RSI(14)", "value": f"{sphb_rsi:.1f}"},
            {"label": "SPHB vs 50-day / 200-day", "value": f"{ext50:+.1f}% / {ext200:+.1f}%"},
            {"label": "SPHB realized beta to SPY (60d)", "value": f"{beta:.2f}x"},
        ],
        "ratio": [{"d": d.strftime("%Y-%m-%d"), "r": rnd(v, 4), "m": rnd(ma50[d], 4)} for d, v in tail.items()],
    }


# ---------------------------------------------------------------- build
def build(sp1500: list[str]) -> dict:
    singles = ["^VIX", "^VIX3M", "SPY", "QQQ", "SPHB", "SPLV", "^IXIC",
               "2YY=F", "^IRX", "^TNX", "TIP", "IEF", "HYG", "DX-Y.NYB"]
    px = {t: ohlc(t) for t in singles}
    spy, qqq = px["SPY"], px["QQQ"]
    idx = spy.index
    cl = {t: d["Close"] for t, d in px.items()}

    ndx_list, nas_list = nasdaq100(), nasdaq_listed()
    members = closes(sorted(set(sp1500) | set(ndx_list) | set(nas_list)))
    col = lambda lst: members[[t for t in dict.fromkeys(lst) if t in members.columns]]
    broad, ndx, nas = col(sp1500), col(ndx_list), col(nas_list)

    vix, vix3m = cl["^VIX"], cl["^VIX3M"]
    vix_wrsi = pd.Series(index=idx, dtype=float)
    for d in idx[-10:]:   # include the in-progress week
        vix_wrsi[d] = rsi(vix[:d].resample("W-FRI").last()).iloc[-1]
    vix_wrsi = vix_wrsi.combine_first(rsi(vix.resample("W-FRI").last()).reindex(idx, method="ffill"))

    tr = pd.concat([spy.High - spy.Low, (spy.High - spy.Close.shift()).abs(),
                    (spy.Low - spy.Close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    nas200 = pct_above_ma(nas, 200)

    raw = pd.DataFrame({
        "vix": vix.reindex(idx).ffill(),
        "vix_wrsi": vix_wrsi,
        "vix_term": (vix / vix3m).reindex(idx).ffill(),
        "mco_broad": mcclellan(broad).reindex(idx),
        "mco_ndx": mcclellan(ndx).reindex(idx),
        "above50": pct_above_ma(broad, 50).reindex(idx),
        "nas200": nas200.reindex(idx),
        "spy_rsi": rsi(spy.Close),
        "qqq_rsi": rsi(qqq.Close).reindex(idx),
        "spy_ext": (spy.Close - spy.Close.rolling(50).mean()) / atr,
    })

    temp = pd.DataFrame(index=idx)          # +100 hot (top risk), -100 cold (bottom zone)
    temp["vix"] = -band(raw.vix, 14.3, 28.3)
    temp["vix_wrsi"] = -band(raw.vix_wrsi, 35, 70)
    temp["vix_term"] = -band(raw.vix_term, 0.82, 1.02)
    for k in ("mco_broad", "mco_ndx", "above50", "spy_ext"):
        temp[k] = ((roll_pct(raw[k]) - 50) * 2).reindex(idx)
    temp["nas200"] = band(raw.nas200, 15, 75)
    temp["spy_rsi"] = band(raw.spy_rsi, 30, 70)
    temp["qqq_rsi"] = band(raw.qqq_rsi, 30, 70)
    score = temp.mean(axis=1, skipna=True).where(temp.notna().sum(axis=1) >= 7)

    smooth = score.ewm(span=5, adjust=False).mean().where(score.notna())
    slope = smooth - smooth.shift(5)
    phase = pd.Series([phase_of(l, s) if pd.notna(l) and pd.notna(s) else None
                       for l, s in zip(smooth, slope)], index=idx)

    # ---- backtest: SPY and SPHB forward returns by bucket and by phase
    sphb = cl["SPHB"].reindex(idx)
    fwd = {(n, h): s.shift(-h) / s - 1 for n, s in (("spy", spy.Close), ("sphb", sphb)) for h in HORIZONS}

    def stats(mask, name, **extra):
        row = {"name": name, "days": int(mask.sum()), **extra}
        for (n, h), f in fwd.items():
            r = f[mask].dropna()
            row[f"{n}{h}"] = rnd(r.mean() * 100, 2) if len(r) else None
            row[f"{n}win{h}"] = rnd((r > 0).mean() * 100, 0) if len(r) else None
        return row

    valid = score.notna()
    buckets = [stats(valid & (score > lo) & (score <= hi), n, lo=max(lo, -100), hi=min(hi, 100)) for lo, hi, n in BUCKETS]
    phases = [stats(phase == p, p) for p in PHASES]
    # the setups that matter most: turning up out of a washout, rolling over from overheated
    low20, high20 = score.rolling(20).min(), score.rolling(20).max()
    setups = [stats((phase == "Bottoming") & (low20 <= -60), "Bottoming after a washout"),
              stats((phase == "Topping") & (high20 >= 60), "Topping after overheated")]
    base = stats(valid, "All days")

    last = score.last_valid_index()
    meta = {
        "vix": ("VIX", "Multi-year range: 28+ = fear high, ~14 = complacent", "{:.2f}"),
        "vix_wrsi": ("VIX weekly RSI", ">70 = fear spike (stocks oversold)", "{:.1f}"),
        "vix_term": ("VIX / VIX3M", ">1.0 = backwardation, near-term panic", "{:.3f}"),
        "mco_broad": ("McClellan, S&P 1500", "NYMO stand-in; deep negative = washout", "{:.1f}"),
        "mco_ndx": ("McClellan, Nasdaq-100", "NAMO stand-in", "{:.1f}"),
        "above50": ("% above 50-day", "S&P 1500; <20% washout, >80% stretched", "{:.0f}%"),
        "nas200": ("Nasdaq % above 200-day", "<25% correction zone, <15% accumulate", "{:.0f}%"),
        "spy_rsi": ("SPY RSI(14)", "<30 oversold, >70 overbought", "{:.1f}"),
        "qqq_rsi": ("QQQ RSI(14)", "<30 oversold, >70 overbought", "{:.1f}"),
        "spy_ext": ("SPY vs 50-day", "Distance in ATRs; beyond ±3 is stretched", "{:+.2f} ATR"),
    }
    comps = [{"key": k, "label": lab, "note": note,
              "value": None if pd.isna(raw.at[last, k]) else fmt.format(raw.at[last, k]),
              "temp": None if pd.isna(temp.at[last, k]) else round(float(temp.at[last, k]))}
             for k, (lab, note, fmt) in meta.items()]

    hist = pd.DataFrame({"s": score, "m": smooth, "sl": slope, "spy": spy.Close}).dropna()
    hist = hist[hist.index >= "2019-01-01"]   # covers the 2020 crash and the 2022 bear market

    # weekly Nasdaq breadth vs the Composite, Perruna-style
    wk = pd.DataFrame({"b": nas200, "ix": cl["^IXIC"]}).resample("W-FRI").last().dropna()
    wk = wk[wk.index >= "2017-01-01"]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": last.strftime("%Y-%m-%d"),
        "score": rnd(score[last]),
        "smooth": rnd(smooth[last]),
        "slope": rnd(slope[last]),
        "bucket": next(n for lo, hi, n in BUCKETS if lo < score[last] <= hi),
        "phase": phase[last],
        "components": comps,
        "history": [{"d": d.strftime("%Y-%m-%d"), "s": rnd(r.s), "m": rnd(r.m), "sl": rnd(r.sl), "spy": rnd(r.spy, 2)}
                    for d, r in hist.iterrows()],
        "breadth200": {"now": rnd(nas200.dropna().iloc[-1]), "members": int(nas.shape[1]),
                       "weekly": [{"d": d.strftime("%Y-%m-%d"), "b": rnd(r.b), "ix": rnd(r.ix, 0)} for d, r in wk.iterrows()]},
        "beta": beta_panel(cl["SPHB"], cl["SPLV"], spy.Close),
        "macro": macro_panel(cl),
        "backtest": {"buckets": buckets, "phases": phases, "setups": setups, "baseline": base,
                     "from": score.first_valid_index().strftime("%Y-%m-%d"), "to": last.strftime("%Y-%m-%d")},
        "universe": {"broad": int(broad.shape[1]), "ndx": int(ndx.shape[1]), "nasdaq": int(nas.shape[1])},
    }
