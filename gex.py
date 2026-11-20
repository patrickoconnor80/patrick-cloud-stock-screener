"""Dealer gamma (GEX) snapshot for SPY / QQQ.

Pulls option chains from Yahoo (yfinance, ~15 min delayed; open interest is as
of the prior close), computes Black-Scholes gamma per contract, and builds a
GEX profile under the standard convention: dealers long calls (+), short puts
(-). That convention is an estimate of dealer positioning, not an observation.

build_snapshot() returns a JSON-serializable dict consumed by gex_handler.py
(which writes it to S3 for the website). Run directly for a local check:

    python gex.py            # prints the JSON
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
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
GRID_BAND = 0.025      # strikes within +/-2.5% of spot go in the heatmap grid
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


# ---------------------------------------------------------------- data
def load_chain(tk: yf.Ticker, spot: float, now: datetime):
    rows, expiries = [], []   # rows: (expiry, strike, sign, oi, iv, t)
    for exp_s in tk.options:
        exp = date.fromisoformat(exp_s)
        if exp < now.date() or (exp == now.date() and now.time() >= time(16, 0)):
            continue
        if (exp - now.date()).days > DAYS_AHEAD:
            break
        expiries.append(exp)
        ch = tk.option_chain(exp_s)
        t = years_to_expiry(exp, now)
        for df, sign in ((ch.calls, 1), (ch.puts, -1)):
            df = df[(df.openInterest > 0) & (df.impliedVolatility.between(0.03, 3.0))]
            df = df[(df.strike > spot * 0.85) & (df.strike < spot * 1.15)]
            for k, oi, iv in zip(df.strike, df.openInterest, df.impliedVolatility):
                rows.append((exp, float(k), sign, float(oi), float(iv), t))
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


def atm_straddle(tk: yf.Ticker, spot: float, exp: date):
    ch = tk.option_chain(exp.isoformat())
    k = float(min(ch.calls.strike, key=lambda x: abs(x - spot)))
    def mid(df):
        r = df[df.strike == k].iloc[0]
        return float((r.bid + r.ask) / 2 if r.bid > 0 and r.ask > 0 else r.lastPrice)
    return k, mid(ch.calls) + mid(ch.puts)


# ---------------------------------------------------------------- snapshot
def ticker_snapshot(ticker: str, now: datetime) -> dict:
    tk = yf.Ticker(ticker)
    spot = float(tk.fast_info["lastPrice"])
    expiries, rows = load_chain(tk, spot, now)
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
    grid_strikes = [k for k in band_keys if abs(k - spot) / spot <= GRID_BAND]
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

    return {
        "ticker": ticker,
        "spot": round(spot, 2),
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


def events_snapshot(tickers, now: datetime, max_priced=3) -> list:
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
                    tk = yf.Ticker(t)
                    exp = next((date.fromisoformat(s) for s in tk.options if date.fromisoformat(s) >= first), None)
                    if not exp:
                        continue
                    spot = float(tk.fast_info["lastPrice"])
                    k, straddle = atm_straddle(tk, spot, exp)
                    item["implied"].append({"ticker": t, "expiry": exp.isoformat(), "strike": k,
                                            "straddle": round(straddle, 2),
                                            "move_pct": round(straddle / spot * 100, 2)})
                except Exception as ex:   # one bad lookup shouldn't drop the calendar
                    item["implied"].append({"ticker": t, "error": str(ex)})
        out.append(item)
    return out


def build_snapshot(tickers=("SPY", "QQQ")) -> dict:
    now = datetime.now(ET)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "source": "Yahoo Finance option chains (~15 min delayed; open interest as of prior close)",
        "convention": "dealers long calls (+), short puts (-); GEX in $ per 1% move",
        "tickers": [ticker_snapshot(t, now) for t in tickers],
        "events": events_snapshot(tickers, now),
    }


if __name__ == "__main__":
    print(json.dumps(build_snapshot(), indent=1, default=str))
