"""
Lambda entrypoint for the SPY/QQQ dealer-gamma snapshot.

Same container image as the stock screener, with the image command overridden
to gex_handler.lambda_handler (see tf/gex.tf). EventBridge fires it every 15
minutes on weekdays; the schedule is in UTC, so it is padded to cover market
hours in both EST and EDT, and this handler skips runs outside 9:30-16:15 ET.

Each run writes the snapshot to S3 for the website and emails (via SNS):
  - a PIN alert the first time a ticker's pin setup turns ready for a given
    node and expiry (de-duplicated against the previous snapshot in S3), and
  - an EVENT heads-up once, in the 3:30-3:44pm ET run on the trading day
    before a scheduled event (or that morning's 9:30 run for Monday events).

Environment variables (set by Terraform):
  RESULTS_BUCKET              S3 bucket to write gex.json to
  GEX_KEY                     S3 key, e.g. stock-screener/gex.json
  CLOUDFRONT_DISTRIBUTION_ID  distribution to invalidate after writing
  ALERT_TOPIC_ARN             SNS topic for email alerts (optional)

Invoke with {"force": true} to run outside market hours, and
{"test_alert": true} to send a test email.
"""

import json
import os
import time as _time
from datetime import date, datetime, time, timedelta

import boto3

import gex

RESULTS_BUCKET = os.environ.get("RESULTS_BUCKET", "patrick-cloud.com")
GEX_KEY = os.environ.get("GEX_KEY", "stock-screener/gex.json")
CLOUDFRONT_DISTRIBUTION_ID = os.environ.get("CLOUDFRONT_DISTRIBUTION_ID")
ALERT_TOPIC_ARN = os.environ.get("ALERT_TOPIC_ARN")
TRADIER_TOKEN_PARAM = os.environ.get("TRADIER_TOKEN_PARAM")   # SSM SecureString name
SITE = "https://patrick-cloud.com/data.html"


def load_tradier_token():
    """Put the Tradier token from SSM into the env for gex.pick_source(). Missing
    parameter = no token = Yahoo SPY/QQQ fallback."""
    if os.environ.get("TRADIER_TOKEN") or not TRADIER_TOKEN_PARAM:
        return
    try:
        p = boto3.client("ssm").get_parameter(Name=TRADIER_TOKEN_PARAM, WithDecryption=True)
        os.environ["TRADIER_TOKEN"] = p["Parameter"]["Value"].strip()
    except Exception as ex:
        print(f"No Tradier token ({type(ex).__name__}); using Yahoo SPY/QQQ")


def money(n):
    a = abs(n)
    s = f"{a / 1e9:.2f}B" if a >= 1e9 else f"{a / 1e6:.0f}M"
    return ("-$" if n < 0 else "+$") + s


def send(subject, body):
    print(f"ALERT: {subject}")
    if ALERT_TOPIC_ARN:
        boto3.client("sns").publish(TopicArn=ALERT_TOPIC_ARN, Subject=subject[:99], Message=body)


def previous_snapshot(s3):
    try:
        return json.loads(s3.get_object(Bucket=RESULTS_BUCKET, Key=GEX_KEY)["Body"].read())
    except Exception:
        return {}


def pin_alerts(payload, prev):
    was_ready = {
        (t["ticker"], t["pin"].get("node"), t["pin"].get("expiry"))
        for t in prev.get("tickers", []) if t.get("pin", {}).get("ready")
    }
    for t in payload["tickers"]:
        pin = t.get("pin") or {}
        if not pin.get("ready") or (t["ticker"], pin["node"], pin["expiry"]) in was_ready:
            continue
        today = payload["generated_at"][:10]
        events_today = [e["name"] for e in payload.get("events", []) if e["date"] == today]
        checks = "\n".join(f"  [{'x' if ok else ' '}] {k}" for k, ok in pin["checks"].items())
        warn = f"\nWARNING: scheduled event today ({'; '.join(events_today)}). Pins break on news.\n" if events_today else ""
        send(
            f"{t['ticker']} pin setup at {pin['node']:g} (exp {pin['expiry'][5:]})",
            f"{t['ticker']} ${t['spot']:.2f}: dealers net long gamma (flip {t['flip']}).\n"
            f"Largest positive node near spot: {pin['node']:g} ({money(pin['node_gex'])}), expiring {pin['expiry']}.\n\n"
            f"Checks:\n{checks}\n{warn}\n"
            f"Pin structures centered on {pin['node']:g}: long call butterfly (debit) or short iron butterfly (credit).\n"
            f"Not a short plain butterfly: that profits from movement away from the strike.\n\n"
            f"Live map: {SITE}\n\nModel estimate from delayed Yahoo option chains; dealer positions are assumed, not observed.",
        )


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def event_alerts(payload, now):
    in_afternoon_slot = time(15, 30) <= now.time() < time(15, 45)
    in_morning_slot = time(9, 30) <= now.time() < time(9, 45)
    for e in payload.get("events", []):
        d = date.fromisoformat(e["date"])
        due = (in_afternoon_slot and d == next_trading_day(now.date())) or \
              (in_morning_slot and d == now.date() and now.weekday() == 0)  # Monday events: weekend gap
        if not due:
            continue
        moves = "\n".join(
            f"  {x['ticker']}: +/-{x['move_pct']:.2f}% (ATM {x['strike']:g} straddle ${x['straddle']:.2f}, through {x['expiry']})"
            for x in e.get("implied", []) if x.get("move_pct")
        ) or "  (not priced yet)"
        regimes = ", ".join(f"{t['ticker']} {t.get('regime', '?')} gamma" for t in payload["tickers"])
        send(
            f"Event {d:%a %b %d}: {e['name']}",
            f"{e['name']}: {d:%A %b %d} at {e.get('time', '?')} ET.\n\n"
            f"Implied move:\n{moves}\n\nDealer regime now: {regimes}.\n\n"
            f"IV-crush structures (short iron butterfly / iron condor) collect this premium and win if the actual move is "
            f"smaller than implied. In short gamma, a worse-than-priced print runs: keep defined-risk wings.\n\n"
            f"Calendar and live map: {SITE}",
        )


def lambda_handler(event, context):
    event = event if isinstance(event, dict) else {}
    if event.get("test_alert"):
        send("GEX alerts: test", f"Email alerts from the GEX Lambda are working.\n\n{SITE}")
        return {"statusCode": 200, "sent": "test"}

    now = datetime.now(gex.ET)
    if not event.get("force") and (now.weekday() >= 5 or not time(9, 30) <= now.time() <= time(16, 15)):
        return {"statusCode": 204, "skipped": "outside market hours"}

    s3 = boto3.client("s3")
    load_tradier_token()
    prev = previous_snapshot(s3)
    payload = gex.build_snapshot()
    s3.put_object(
        Bucket=RESULTS_BUCKET,
        Key=GEX_KEY,
        Body=json.dumps(payload, default=str),
        ContentType="application/json",
        CacheControl="max-age=60",
    )
    if CLOUDFRONT_DISTRIBUTION_ID:
        boto3.client("cloudfront").create_invalidation(
            DistributionId=CLOUDFRONT_DISTRIBUTION_ID,
            InvalidationBatch={
                "Paths": {"Quantity": 1, "Items": [f"/{GEX_KEY}"]},
                "CallerReference": str(_time.time()),
            },
        )

    for fn, args in ((pin_alerts, (payload, prev)), (event_alerts, (payload, now))):
        try:
            fn(*args)
        except Exception as ex:   # alerting must never block the snapshot
            print(f"{fn.__name__} failed: {ex}")

    return {
        "statusCode": 200,
        "tickers": {t["ticker"]: t.get("regime") for t in payload["tickers"]},
        "pin_ready": [t["ticker"] for t in payload["tickers"] if t.get("pin", {}).get("ready")],
    }
