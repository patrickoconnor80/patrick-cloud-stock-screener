"""Dealer gamma (GEX) snapshot for the index option markets.

Two data sources, chosen by build_snapshot():
  - Tradier (SPX, NDX): needs an API token. Index options are where most of the
    index gamma sits; Yahoo carries no open interest for SPX, so it can't do them.
  - Yahoo via yfinance (SPY, QQQ): free, ~15 min delayed. Fallback when no
    Tradier token is configured.

Either way, open interest is as of the prior close. Gamma is Black-Scholes from
each contract's implied vol, and the GEX profile uses the standard convention:
dealers long calls (+), short puts (-). That is an estimate of dealer
positioning, not an observation.

build_snapshot() returns a JSON-serializable dict consumed by gex_handler.py
(which writes it to S3 for the website). Run directly for a local check:

    python gex.py                         # Yahoo SPY/QQQ
    TRADIER_TOKEN=... python gex.py       # Tradier SPX/NDX
"""
from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import requests
import yfinance as yf

# Lambda's filesystem is read-only outside /tmp; point yfinance's cache there.
try:
    yf.set_tz_cache_location("/tmp/yf-cache")
except Exception:
    pass

ET = ZoneInfo("America/New_York")
EVENTS = Path(__file__).resolve().parent / "events.json"

RATE = 0.04            # risk-free rate for Black-Scholes
DAYS_AHEAD = 10        # expirations included in the "whole book" profile and grid
BAND = 0.03            # strikes within +/-3% of spot go in the published profile
GRID_ROWS = 36         # strikes nearest spot shown in the heatmap grid
PIN_MAX_DIST = 0.006   # pin node must be within 0.6% of spot
PIN_WINDOW = (time(10, 30), time(14, 30))   # ET, expiration day
EVE_WINDOW = (time(13, 0), time(16, 15))    # ET, day before expiration


# ---------------------------------------------------------------- math
def bs_gamma(spot, strike, t_years, iv):
    """Black-Scholes gamma (same for calls and puts). Vectorized."""
    spot = np.asarray(spot, dtype=float)
    sqrt_t = np.sqrt(t_years)
    d1 = (np.log(spot / strike) + (RATE + 0.5 * iv**2) * t_years) / (iv * sqrt_t)
    return np.exp(-0.5 * d1**2) / (math.sqrt(2 * math.pi) * spot * iv * sqrt_t)


def years_to_expiry(exp: date, now: datetime) -> float:
    close = datetime.combine(exp, time(16, 0), ET)
    return max((close - now).total_seconds(), 15 * 60) / (365 * 24 * 3600)


# ---------------------------------------------------------------- data sources
# Each source returns contracts as dicts: strike, type ("call"/"put"), oi, iv, bid, ask, last.

class YahooSource:
    name = "Yahoo Finance option chains (~15 min delayed; open interest as of prior close)"
    tickers = ("SPY", "QQQ")

    def __init__(self):
        self._tk, self._chains = {}, {}

    def _t(self, ticker):
        return self._tk.setdefault(ticker, yf.Ticker(ticker))

    def spot(self, ticker):
        return float(self._t(ticker).fast_info["lastPrice"])

    def expirations(self, ticker):
        return [date.fromisoformat(s) for s in self._t(ticker).options]

    def chain(self, ticker, exp: date):
        key = (ticker, exp)
        if key not in self._chains:
            ch = self._t(ticker).option_chain(exp.isoformat())
            out = []
            for df, typ in ((ch.calls, "call"), (ch.puts, "put")):
                for r in df.itertuples():
                    out.append({"strike": float(r.strike), "type": typ,
                                "oi": float(r.openInterest or 0) if r.openInterest == r.openInterest else 0.0,
                                "iv": float(r.impliedVolatility or 0), "bid": float(r.bid or 0),
                                "ask": float(r.ask or 0), "last": float(r.lastPrice or 0)})
            self._chains[key] = out
        return self._chains[key]


class TradierSource:
    """Tradier market data API. Greeks/IV come from ORATS (refreshed about hourly)."""
    name = "Tradier option chains with ORATS IV (open interest as of prior close)"
    tickers = ("SPX", "NDX")

    def __init__(self, token, base=None):
        self.base = (base or os.environ.get("TRADIER_BASE") or "https://api.tradier.com/v1").rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
        self._chains = {}

    def _get(self, path, **params):
        r = self.s.get(f"{self.base}/{path}", params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _list(x):
        return [] if x is None else x if isinstance(x, list) else [x]

    def spot(self, ticker):
        q = self._list((self._get("markets/quotes", symbols=ticker).get("quotes") or {}).get("quote"))[0]
        return float(q.get("last") or q.get("prevclose"))

    def expirations(self, ticker):
        d = self._get("markets/options/expirations", symbol=ticker, includeAllRoots="true")
        return sorted(date.fromisoformat(x) for x in self._list((d.get("expirations") or {}).get("date")))

    def chain(self, ticker, exp: date):
        key = (ticker, exp)
        if key not in self._chains:
            d = self._get("markets/options/chains", symbol=ticker, expiration=exp.isoformat(), greeks="true")
            out = []
            for o in self._list((d.get("options") or {}).get("option")):
                g = o.get("greeks") or {}
                out.append({"strike": float(o["strike"]), "type": o["option_type"],
                            "oi": float(o.get("open_interest") or 0),
                            "iv": float(g.get("mid_iv") or g.get("smv_vol") or 0),
                            "bid": float(o.get("bid") or 0), "ask": float(o.get("ask") or 0),
                            "last": float(o.get("last") or 0)})
            self._chains[key] = out
        return self._chains[key]


def pick_source():
    """Tradier (SPX/NDX) when a token is configured, else Yahoo (SPY/QQQ).

    Tradier issues separate tokens for live accounts (api.tradier.com) and developer
    sandbox accounts (sandbox.tradier.com). If TRADIER_BASE isn't set, try live first
    and fall back to sandbox on 401. Any failure falls back to Yahoo so the site keeps
    updating."""
    token = os.environ.get("TRADIER_TOKEN")
    if not token:
        return YahooSource()
    bases = [os.environ["TRADIER_BASE"]] if os.environ.get("TRADIER_BASE") else \
        ["https://api.tradier.com/v1", "https://sandbox.tradier.com/v1"]
    for base in bases:
        src = TradierSource(token, base)
        try:
            src.spot("SPX")
            return src
        except requests.HTTPError as ex:
            print(f"Tradier {base}: {ex}")
        except Exception as ex:
            print(f"Tradier {base}: {ex}")
    print("Tradier unavailable; falling back to Yahoo SPY/QQQ")
    return YahooSource()


def load_chain(src, ticker, spot: float, now: datetime):
    rows, expiries = [], []   # rows: (expiry, strike, sign, oi, iv, t)
    for exp in src.expirations(ticker):
        if exp < now.date() or (exp == now.date() and now.time() >= time(16, 0)):
            continue
        if (exp - now.date()).days > DAYS_AHEAD:
            break
        expiries.append(exp)
        t = years_to_expiry(exp, now)
        for c in src.chain(ticker, exp):
            if c["oi"] > 0 and 0.03 <= c["iv"] <= 3.0 and spot * 0.85 < c["strike"] < spot * 1.15:
                rows.append((exp, c["strike"], 1 if c["type"] == "call" else -1, c["oi"], c["iv"], t))
    return expiries, rows


def gex_by_strike(rows, spot, only_exp=None):
    """Dollar GEX per strike per 1% move at the current spot, plus call/put OI."""
    gex, calls, puts = {}, {}, {}
    for exp, k, sign, oi, iv, t in rows:
        if only_exp and exp != only_exp:
            continue
        gex[k] = gex.get(k, 0.0) + sign * float(bs_gamma(spot, k, t, iv)) * oi * 100 * spot**2 * 0.01
        book = calls if sign > 0 else puts
        book[k] = book.get(k, 0.0) + oi
    return gex, calls, puts


def gamma_flip(rows, spot):
    """Spot level where total GEX crosses zero, nearest the current spot."""
    if not rows:
        return None
    k, sgn, oi, iv, t = (np.array([r[i] for r in rows]) for i in (1, 2, 3, 4, 5))
    grid = np.linspace(spot * 0.95, spot * 1.05, 201)
    totals = np.array([float(np.sum(sgn * bs_gamma(s, k, t, iv) * oi * 100 * s**2 * 0.01)) for s in grid])
    crossings = [
        grid[i] - totals[i] * (grid[i + 1] - grid[i]) / (totals[i + 1] - totals[i])
        for i in range(len(grid) - 1) if np.sign(totals[i]) != np.sign(totals[i + 1])
    ]
    return float(min(crossings, key=lambda x: abs(x - spot))) if crossings else None


def atm_straddle(src, ticker, spot: float, exp: date):
    ch = src.chain(ticker, exp)
    both = {c["strike"] for c in ch if c["type"] == "call"} & {c["strike"] for c in ch if c["type"] == "put"}
    k = min(both, key=lambda x: abs(x - spot))
    def mid(typ):
        c = next((c for c in ch if c["strike"] == k and c["type"] == typ), None)
        if c is None:
            raise ValueError(f"no {typ} at the {k:g} strike")
        return (c["bid"] + c["ask"]) / 2 if c["bid"] > 0 and c["ask"] > 0 else c["last"]
    return k, mid("call") + mid("put")


# ---------------------------------------------------------------- levels and outlook
INDEX_FOR = {"SPY": ("SPX", "^SPX"), "QQQ": ("NDX", "^NDX")}


def key_levels(book: dict, spot: float, band=0.03) -> dict:
    """Largest positive/negative nodes above and below spot within the band."""
    near = {k: v for k, v in book.items() if abs(k - spot) / spot <= band}
    def pick(keys, sign):
        ks = [k for k in keys if (near[k] > 0) == (sign > 0) and near[k] != 0]
        k = max(ks, key=lambda x: abs(near[x])) if ks else None
        return {"strike": k, "gex": round(near[k])} if k is not None else None
    above = [k for k in near if k > spot]
    below = [k for k in near if k < spot]
    return {"resistance": pick(above, 1), "support": pick(below, 1),
            "accel_up": pick(above, -1), "accel_down": pick(below, -1)}


def third_friday(d: date) -> date:
    first = d.replace(day=1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def monthly_snapshot(src, ticker: str, spot: float, now: datetime):
    """Largest nodes on the next standard monthly expiry (third Friday) after the nearest week."""
    tf = third_friday(now.date())
    if tf <= now.date() + timedelta(days=2):          # this month's is (nearly) done: use next month
        nxt = (now.date().replace(day=1) + timedelta(days=32)).replace(day=1)
        tf = third_friday(nxt)
    if tf not in src.expirations(ticker):
        return None
    t = years_to_expiry(tf, now)
    gex = {}
    for c in src.chain(ticker, tf):
        if c["oi"] > 0 and 0.03 <= c["iv"] <= 3.0 and abs(c["strike"] - spot) / spot <= 0.05:
            sign = 1 if c["type"] == "call" else -1
            k = c["strike"]
            gex[k] = gex.get(k, 0.0) + sign * float(bs_gamma(spot, k, t, c["iv"])) * c["oi"] * 100 * spot**2 * 0.01
    top = sorted(gex, key=lambda k: abs(gex[k]), reverse=True)[:3]
    quad = tf.month in (3, 6, 9, 12)
    return {"expiry": tf.isoformat(), "quad_witching": quad,
            "net": round(sum(gex.values())),
            "nodes": [{"strike": k, "gex": round(gex[k])} for k in sorted(top, reverse=True)]}


def index_info(ticker: str, spot: float):
    """Equivalent cash-index level, so levels can be quoted in SPX / NDX points."""
    if ticker not in INDEX_FOR:
        return None
    name, sym = INDEX_FOR[ticker]
    try:
        px = float(yf.Ticker(sym).fast_info["lastPrice"])
        return {"name": name, "spot": round(px, 2), "ratio": round(px / spot, 4)}
    except Exception:
        return None


def _lvl(x, idx):
    if x is None:
        return None
    s = f"{x:g}"
    return f"{s} (~{x * idx['ratio']:,.0f} {idx['name']})" if idx else s


def build_outlook(t: dict, events: list, now: datetime) -> list:
    """Rule-based read for the nearest session, the next few sessions, and the monthly opex.
    These are mechanical setups that follow from the gamma map, not recommendations."""
    if t.get("error"):
        return []
    idx, spot, flip, lv = t.get("index"), t["spot"], t["flip"], t["levels"]
    L = lambda k: _lvl((lv.get(k) or {}).get("strike"), idx)
    nearest = date.fromisoformat(t["nearest_expiry"])
    out = []

    # ---- nearest session
    pin = t.get("pin") or {}
    if pin.get("ready"):
        today = (f"Pin conditions are met at {_lvl(pin['node'], idx)}: dealers are long gamma and that node "
                 f"dominates nearby gamma into the {nearest:%b %d} expiration.")
        trade = f"Long call butterfly or short iron butterfly centered on {_lvl(pin['node'], idx)}."
    elif t["regime"] == "long":
        today = (f"Dealers are net long gamma (spot above the flip at {_lvl(flip, idx)}), so moves should be "
                 f"absorbed. Expect a range between support near {L('support') or 'n/a'} and resistance near "
                 f"{L('resistance') or 'n/a'}.")
        trade = (f"Range: sell premium outside the range (iron condor with short strikes beyond "
                 f"{L('support') or 'support'} and {L('resistance') or 'resistance'}). A break below "
                 f"{_lvl(flip, idx)} ends the setup.") if lv.get("support") and lv.get("resistance") else \
                "Range-bound conditions favor premium selling around the largest nearby positive node."
    elif flip and abs(spot - flip) / spot <= 0.004:
        today = (f"Spot is sitting on the gamma flip ({_lvl(flip, idx)}). Above it, dealer hedging calms the "
                 f"tape; below it, hedging adds to moves.")
        trade = (f"Wait for a 15-minute close away from {_lvl(flip, idx)}. Above: chop and dip-buying. Below: "
                 f"momentum toward {L('accel_down') or 'the next negative node'}.")
    else:
        today = (f"Dealers are net short gamma (spot below the flip at {_lvl(flip, idx) or 'n/a'}), so moves "
                 f"in either direction tend to extend. The largest acceleration zone below is "
                 f"{L('accel_down') or 'n/a'}; above, {L('accel_up') or 'none nearby'}.")
        trade = (f"Avoid selling premium. Momentum setups only: a hold below {_lvl(spot, idx)} targets "
                 f"{L('accel_down') or 'lower nodes'}; a reclaim of {_lvl(flip, idx) or 'the flip'} flips the tape "
                 f"back to range-bound.")
    ev_near = [e for e in events if date.fromisoformat(e["date"]) <= nearest]
    if ev_near:
        trade += " Scheduled event before this expiration (" + "; ".join(e["name"] for e in ev_near) + "): pins and ranges break on news."
    out.append({"horizon": f"Nearest session (exp {nearest:%b %d})", "read": today, "setup": trade})

    # ---- next few sessions: what the book looks like once the nearest expiry rolls off
    an = t.get("after_nearest") or {}
    if an.get("flip"):
        moved = an["flip"] - (flip or an["flip"])
        read = (f"Once the {nearest:%b %d} contracts expire, the flip moves to {_lvl(an['flip'], idx)} "
                f"({'up' if moved > 0 else 'down'} {abs(moved):.1f}), leaving dealers net "
                f"{an['regime']} gamma at today's price.")
        setup = ("Expect the dampening to persist: range and pin setups stay in play." if an["regime"] == "long" else
                 "The cushion thins after expiration: expect larger swings until new positioning builds.")
    else:
        read, setup = "No gamma flip within 5% once the nearest expiration rolls off.", "Positioning is one-sided; size for larger moves."
    soon = [e for e in events if nearest < date.fromisoformat(e["date"]) <= nearest + timedelta(days=7)]
    if soon:
        e = soon[0]
        mv = next((x for x in e.get("implied", []) if x.get("ticker") == t["ticker"] and x.get("move_pct")), None)
        setup += (f" Next catalyst: {e['name']} ({date.fromisoformat(e['date']):%b %d}"
                  + (f", options price ±{mv['move_pct']:.1f}% through {mv['expiry'][5:]}" if mv else "")
                  + "). Selling that implied move with defined-risk wings works only if the print is milder than priced.")
    out.append({"horizon": "Next few sessions", "read": read, "setup": setup})

    # ---- weeks out: the monthly expiration
    m = t.get("monthly")
    if m:
        exp = date.fromisoformat(m["expiry"])
        nodes = ", ".join(f"{_lvl(n['strike'], idx)} {'+' if n['gex'] > 0 else '-'}${abs(n['gex'])/1e6:,.0f}M" for n in m["nodes"])
        read = (f"{'Quad witching' if m['quad_witching'] else 'Monthly expiration'} on {exp:%b %d}. "
                f"Largest nodes: {nodes}. Net gamma on that expiry: {'positive' if m['net'] > 0 else 'negative'}.")
        setup = ("Big nodes on the monthly tend to pull price into the date and release it after; expect a larger "
                 "hedge unwind and more volatility the week after." if m["quad_witching"] else
                 "Watch whether price gravitates toward the largest node into the date; the week after usually loosens.")
        out.append({"horizon": f"Into {exp:%b %d} opex", "read": read, "setup": setup})
    return out


# ---------------------------------------------------------------- snapshot
def ticker_snapshot(src, ticker: str, now: datetime) -> dict:
    spot = src.spot(ticker)
    expiries, rows = load_chain(src, ticker, spot, now)
    if not expiries:
        return {"ticker": ticker, "spot": round(spot, 2), "error": "no expirations in range"}
    nearest = expiries[0]
    flip = gamma_flip(rows, spot)
    book, calls_all, puts_all = gex_by_strike(rows, spot)
    near, calls, puts = gex_by_strike(rows, spot, only_exp=nearest)
    net_2pct = sum(v for k, v in book.items() if abs(k - spot) / spot < 0.02)
    long_gamma = (spot > flip) if flip else net_2pct > 0

    band_keys = sorted((k for k in book if abs(k - spot) / spot <= BAND), reverse=True)
    profile = [{"strike": k, "near": round(near.get(k, 0)), "book": round(book[k]),
                "call_oi": int(calls_all.get(k, 0)), "put_oi": int(puts_all.get(k, 0))}
               for k in band_keys]
    king = max(band_keys, key=lambda k: abs(book[k])) if band_keys else None

    # ---- heatmap grid: strikes (rows) x expirations (columns), like a vendor GEX map
    per_exp = {e: gex_by_strike(rows, spot, only_exp=e)[0] for e in expiries}
    grid_strikes = sorted(sorted(book, key=lambda k: abs(k - spot))[:GRID_ROWS], reverse=True)
    grid = {
        "expiries": [e.isoformat() for e in expiries],
        "strikes": grid_strikes,
        "values": [[round(per_exp[e].get(k, 0)) for e in expiries] for k in grid_strikes],
    }

    # ---- pin setup on the nearest expiry
    band2 = {k: v for k, v in near.items() if abs(k - spot) / spot <= 0.02}
    pos = {k: v for k, v in band2.items() if v > 0}
    pin = {"node": None, "checks": {}, "ready": False}
    if pos:
        node = max(pos, key=pos.get)
        c, p = calls.get(node, 0), puts.get(node, 0)
        days_to_exp = (nearest - now.date()).days
        in_window = (days_to_exp == 0 and PIN_WINDOW[0] <= now.time() <= PIN_WINDOW[1]) or \
                    (days_to_exp == 1 and EVE_WINDOW[0] <= now.time() <= EVE_WINDOW[1])
        checks = {
            "Net long gamma at spot": bool(long_gamma),
            "Node within 0.6% of spot": abs(node - spot) / spot <= PIN_MAX_DIST,
            "Node is 15%+ of all gamma within 2%": pos[node] >= 0.15 * sum(abs(v) for v in band2.values()),
            "Node is call-heavy (sign credible)": (c / (c + p) if c + p else 0) >= 0.55,
            "Expiration-day window": in_window,
        }
        pin = {"node": node, "node_gex": round(pos[node]), "expiry": nearest.isoformat(),
               "checks": checks, "ready": all(checks.values())}

    # ---- levels, what remains after the nearest expiry, and the monthly opex
    levels = key_levels(book, spot)
    rest = [r for r in rows if r[0] != nearest]
    flip_after = gamma_flip(rest, spot) if rest else None
    monthly = monthly_snapshot(src, ticker, spot, now)

    return {
        "ticker": ticker,
        "spot": round(spot, 2),
        "levels": levels,
        "after_nearest": {"flip": round(flip_after, 2) if flip_after else None,
                          "regime": ("long" if spot > flip_after else "short") if flip_after else None},
        "monthly": monthly,
        "expiries": [e.isoformat() for e in expiries],
        "nearest_expiry": nearest.isoformat(),
        "flip": round(flip, 2) if flip else None,
        "regime": "long" if long_gamma else "short",
        "net_gex_2pct": round(net_2pct),
        "king": {"strike": king, "gex": round(book[king])} if king else None,
        "pin": pin,
        "profile": profile,
        "grid": grid,
    }


def events_snapshot(src, tickers, now: datetime, max_priced=3) -> list:
    events = json.loads(EVENTS.read_text()) if EVENTS.exists() else []
    today = now.date()
    upcoming = [e for e in events if today <= date.fromisoformat(e["date"]) <= today + timedelta(days=21)]
    out = []
    for i, e in enumerate(upcoming):
        d = date.fromisoformat(e["date"])
        item = dict(e, implied=[])
        if i < max_priced:   # straddle lookups cost a request each; price the nearest few
            first = d if e.get("time", "00:00") < "16:00" else d + timedelta(days=1)
            for t in tickers:
                try:
                    exp = next((x for x in src.expirations(t) if x >= first), None)
                    if not exp:
                        continue
                    spot = src.spot(t)
                    k, straddle = atm_straddle(src, t, spot, exp)
                    item["implied"].append({"ticker": t, "expiry": exp.isoformat(), "strike": k,
                                            "straddle": round(straddle, 2),
                                            "move_pct": round(straddle / spot * 100, 2)})
                except Exception as ex:   # one bad lookup shouldn't drop the calendar
                    item["implied"].append({"ticker": t, "error": str(ex)})
        out.append(item)
    return out


def build_snapshot(src=None) -> dict:
    src = src or pick_source()
    now = datetime.now(ET)
    tickers = src.tickers
    snaps = [ticker_snapshot(src, t, now) for t in tickers]
    events = events_snapshot(src, tickers, now)
    for s in snaps:
        if s.get("error"):
            continue
        s["index"] = index_info(s["ticker"], s["spot"])   # None for SPX/NDX themselves
        try:
            s["outlook"] = build_outlook(s, events, now)
        except Exception as ex:   # the outlook is commentary; never drop the snapshot over it
            print(f"outlook {s['ticker']}: {ex}")
            s["outlook"] = []
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "source": src.name,
        "convention": "dealers long calls (+), short puts (-); GEX in $ per 1% move",
        "tickers": snaps,
        "events": events,
    }


if __name__ == "__main__":
    print(json.dumps(build_snapshot(), indent=1, default=str))
