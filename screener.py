#!/usr/bin/env python3
"""
Daily stock screener.

Universe: S&P 1500 (S&P 500 + 400 + 600) by default -- fast, and every
constituent is already comfortably above the $1B market-cap filter. Pass
--universe full to instead scan the entire SEC-registered ticker list
(~10,000 tickers, much slower and noisier -- lots of delisted symbols).

Filters:
  - Last close > 200-day SMA
  - Last close > 8-day EMA
  - Market cap > $1B
Output: top N by EPS (trailing twelve months), descending.

Requires: pandas, requests, yfinance, lxml
    pip install pandas requests yfinance lxml

Usage:
    python screener.py
    python screener.py --top 10 --min-marketcap 5e9 --output results.csv
    python screener.py --universe full
"""

import argparse
import concurrent.futures as cf
import io
import sys
import time
from datetime import datetime

import pandas as pd
import requests
import yfinance as yf

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
# SEC requires a descriptive User-Agent identifying the requester (see sec.gov/os/webmaster-faq#developers).
SEC_HEADERS = {"User-Agent": "personal-stock-screener patrickoconnor8014@gmail.com"}

WIKIPEDIA_HEADERS = {"User-Agent": "Mozilla/5.0"}
SP_INDEX_URLS = {
    "S&P 500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "S&P 400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "S&P 600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}

CHUNK_SIZE = 150          # tickers per bulk price-history request
MAX_WORKERS = 8           # threads for fundamentals lookups
MAX_RETRIES = 3           # retries for transient network errors (DNS hiccups, etc.)
HISTORY_PERIOD = "1y"     # enough bars for a 200-day SMA


def fetch_sp1500_universe(indexes=("S&P 500", "S&P 400", "S&P 600")) -> list[str]:
    """Pull S&P index constituents from Wikipedia -- small, curated, all >$1B market cap."""
    print(f"Fetching ticker universe: {', '.join(indexes)}...", file=sys.stderr)
    symbols = set()
    for name in indexes:
        resp = requests.get(SP_INDEX_URLS[name], headers=WIKIPEDIA_HEADERS, timeout=30)
        resp.raise_for_status()
        table = pd.read_html(io.StringIO(resp.text))[0]
        symbols.update(table["Symbol"].astype(str).str.strip().str.replace(".", "-", regex=False))

    cleaned = sorted(symbols)
    print(f"Universe size: {len(cleaned)} tickers", file=sys.stderr)
    return cleaned


def fetch_full_universe() -> list[str]:
    """Pull the full list of SEC-registered US tickers (NASDAQ + NYSE + AMEX, etc.)."""
    print("Fetching ticker universe from SEC EDGAR...", file=sys.stderr)

    resp = requests.get(SEC_TICKERS_URL, headers=SEC_HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # Rows for warrants/units/rights/preferreds and other non-common-stock issues
    # don't carry a market cap or EPS in yfinance, so they're dropped naturally
    # downstream by fundamental_filter() -- no need for exact upstream classification.
    # Note: this list also includes many delisted/defunct tickers, since SEC does
    # not prune historical registrants -- expect a higher failure/skip rate.
    cleaned = []
    for row in data.values():
        symbol = str(row.get("ticker", "")).strip()
        if not symbol or any(ch in symbol for ch in ".$+ "):
            continue
        cleaned.append(symbol)

    cleaned = sorted(set(cleaned))
    print(f"Universe size: {len(cleaned)} tickers", file=sys.stderr)
    return cleaned


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def technical_filter(tickers: list[str], sma_period: int, ema_period: int) -> list[str]:
    """Bulk-download price history and keep tickers with close > SMA200 and close > EMA8."""
    survivors = []
    total_chunks = (len(tickers) + CHUNK_SIZE - 1) // CHUNK_SIZE

    for idx, batch in enumerate(chunked(tickers, CHUNK_SIZE), start=1):
        print(f"Price history batch {idx}/{total_chunks} ({len(batch)} tickers)...", file=sys.stderr)
        data = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                data = yf.download(
                    batch,
                    period=HISTORY_PERIOD,
                    interval="1d",
                    group_by="ticker",
                    threads=True,
                    progress=False,
                    auto_adjust=True,
                )
                break
            except Exception as e:
                print(f"  batch failed (attempt {attempt}/{MAX_RETRIES}): {e}", file=sys.stderr)
                if attempt < MAX_RETRIES:
                    time.sleep(2 * attempt)
        if data is None:
            continue

        for ticker in batch:
            try:
                closes = data[ticker]["Close"].dropna() if len(batch) > 1 else data["Close"].dropna()
            except (KeyError, TypeError):
                continue

            if len(closes) < sma_period:
                continue

            sma200 = closes.rolling(sma_period).mean().iloc[-1]
            ema8 = closes.ewm(span=ema_period, adjust=False).mean().iloc[-1]
            last_close = closes.iloc[-1]

            if pd.isna(sma200) or pd.isna(ema8):
                continue

            if last_close > sma200 and last_close > ema8:
                survivors.append(ticker)

    print(f"Passed technical filter: {len(survivors)} tickers", file=sys.stderr)
    return survivors


def fetch_fundamentals(ticker: str) -> dict | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            info = yf.Ticker(ticker).get_info()
            market_cap = info.get("marketCap")
            eps = info.get("trailingEps")
            if market_cap is None or eps is None:
                return None
            return {
                "ticker": ticker,
                "name": info.get("shortName"),
                "market_cap": market_cap,
                "eps": eps,
                "price": info.get("currentPrice") or info.get("regularMarketPrice"),
            }
        except Exception:
            if attempt < MAX_RETRIES:
                time.sleep(1 * attempt)
    return None


def fundamental_filter(tickers: list[str], min_marketcap: float, max_workers: int = MAX_WORKERS) -> pd.DataFrame:
    print(f"Fetching fundamentals for {len(tickers)} candidates...", file=sys.stderr)
    rows = []
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for i, result in enumerate(ex.map(fetch_fundamentals, tickers), start=1):
            if result and result["market_cap"] and result["market_cap"] > min_marketcap:
                rows.append(result)
            if i % 50 == 0:
                print(f"  ...{i}/{len(tickers)} checked", file=sys.stderr)

    print(f"Passed market-cap filter (> ${min_marketcap:,.0f}): {len(rows)} tickers", file=sys.stderr)
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description="Daily stock screener: 200 SMA + 8 EMA + market cap, ranked by EPS.")
    parser.add_argument("--top", type=int, default=5, help="number of results to return (default: 5)")
    parser.add_argument("--min-marketcap", type=float, default=1e9, help="minimum market cap in dollars (default: 1e9)")
    parser.add_argument("--sma-period", type=int, default=200, help="SMA lookback period (default: 200)")
    parser.add_argument("--ema-period", type=int, default=8, help="EMA lookback period (default: 8)")
    parser.add_argument(
        "--universe",
        choices=["sp1500", "sp500", "full"],
        default="sp1500",
        help="ticker universe: sp1500 (S&P 500+400+600, fast, default), sp500 (S&P 500 only, fastest), "
             "full (all SEC-registered tickers, slow, ~10k tickers)",
    )
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help=f"parallel threads for fundamentals lookups (default: {MAX_WORKERS})")
    parser.add_argument("--output", type=str, default=None, help="optional CSV path to save full result set")
    args = parser.parse_args()

    if args.universe == "sp500":
        universe = fetch_sp1500_universe(indexes=("S&P 500",))
    elif args.universe == "full":
        universe = fetch_full_universe()
    else:
        universe = fetch_sp1500_universe()

    technical_survivors = technical_filter(universe, args.sma_period, args.ema_period)

    if not technical_survivors:
        print("No tickers passed the technical filter today.")
        return

    df = fundamental_filter(technical_survivors, args.min_marketcap, max_workers=args.workers)

    if df.empty:
        print("No tickers passed the market-cap filter today.")
        return

    df = df.sort_values("eps", ascending=False).reset_index(drop=True)
    top = df.head(args.top)

    print(f"\n=== Top {args.top} by EPS ({datetime.now():%Y-%m-%d}) ===")
    print(f"Filters: close > {args.sma_period}d SMA, close > {args.ema_period}d EMA, market cap > ${args.min_marketcap:,.0f}\n")
    print(top.to_string(index=False, formatters={
        "market_cap": lambda x: f"${x:,.0f}",
        "eps": lambda x: f"{x:.2f}",
        "price": lambda x: f"${x:,.2f}" if pd.notna(x) else "n/a",
    }))

    if args.output:
        df.to_csv(args.output, index=False)
        print(f"\nFull result set ({len(df)} rows) saved to {args.output}")


if __name__ == "__main__":
    main()
