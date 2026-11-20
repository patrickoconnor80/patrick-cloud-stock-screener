"""
Lambda entrypoint for the daily market-cycle readout (cycle.py).

Same container image as the screener, with the image command overridden to
cycle_handler.lambda_handler (see tf/cycle.tf). EventBridge runs it once each
weekday evening; it writes stock-screener/cycle.json for data.html and emails
the GEX alert subscribers (same SNS/SES path as gex_handler) when:

  - the score enters the washout zone (<= -60) or the overheated zone (>= +60);
    the zone resets only back inside +/-40, so it fires once per extreme;
  - the phase turns to Bottoming within 20 days of a washout reading, or to
    Topping within 20 days of an overheated one (once per turn).

Alert state lives in cycle.json itself. A run with no previous file only seeds
the state. Invoke with {"no_alerts": true} to refresh without emailing.
"""

import json
import os
import time

import boto3

import cycle
import screener
from gex_handler import send

RESULTS_BUCKET = os.environ.get("RESULTS_BUCKET", "patrick-cloud.com")
CYCLE_KEY = os.environ.get("CYCLE_KEY", "stock-screener/cycle.json")
CLOUDFRONT_DISTRIBUTION_ID = os.environ.get("CLOUDFRONT_DISTRIBUTION_ID")
SITE = "https://patrick-cloud.com/data.html"
ZONE_IN, ZONE_RESET = 60, 40


def previous(s3):
    try:
        return json.loads(s3.get_object(Bucket=RESULTS_BUCKET, Key=CYCLE_KEY)["Body"].read())
    except Exception:
        return None


def zone_of(score, last):
    if score <= -ZONE_IN:
        return "cold"
    if score >= ZONE_IN:
        return "hot"
    return None if abs(score) < ZONE_RESET else last


def summary(c):
    comps = "\n".join(f"  {x['label']}: {x['value'] or '-'}  ({'+' if (x['temp'] or 0) > 0 else ''}{x['temp']})"
                      for x in c["components"])
    m = c["macro"]
    macro = "\n".join(f"  {i['label']}: {i['value']} ({i['change']}) - {i['signal']}" for i in m["items"])
    return (f"Components (-100 cold .. +100 hot):\n{comps}\n\n"
            f"Beta: {c['beta']['status']}.\nMacro backdrop for beta: {m['overall']}\n{macro}\n\n"
            f"Full readout: {SITE}\n\nNot a recommendation to trade.")


def history_line(c, name):
    row = next((r for r in c["backtest"]["buckets"] + c["backtest"]["setups"] if r["name"] == name), None)
    b = c["backtest"]["baseline"]
    if not row:
        return ""
    return (f"Past days like this ({row['days']}): next 3 months SPY {row['spy60']:+.1f}%, high-beta SPHB "
            f"{row['sphb60']:+.1f}% (all days: SPY {b['spy60']:+.1f}%, SPHB {b['sphb60']:+.1f}%).\n\n")


def alerts(c, state):
    """Returns the new state; sends what is due."""
    zone = zone_of(c["score"], state.get("zone"))
    hist = c["history"][-20:]
    lo20, hi20 = min(h["s"] for h in hist), max(h["s"] for h in hist)
    head = f"Market cycle {c['score']:+.0f} ({c['bucket']}, phase {c['phase']}) at the {c['as_of']} close.\n\n"

    if zone and zone != state.get("zone"):
        if zone == "cold":
            send(f"Market cycle {c['score']:+.0f}: Washout",
                 head + "Fear, breadth and momentum are washed out together. Readings like this have clustered "
                 "near short-term lows; the better entry has been the turn (a Bottoming alert follows when the "
                 "score starts rising) rather than the first dip.\n\n" + history_line(c, "Washout") + summary(c))
        else:
            send(f"Market cycle {c['score']:+.0f}: Overheated",
                 head + "Momentum and breadth are stretched. The index has still tended to grind higher, but "
                 "high-beta stocks have historically gone nowhere over the following 3 months from here: "
                 "a time to tighten stops and trim beta, not to short.\n\n" + history_line(c, "Overheated") + summary(c))

    turn = state.get("phase") != c["phase"]
    if turn and c["phase"] == "Bottoming" and lo20 <= -ZONE_IN:
        send(f"Market cycle turning up after a washout ({c['score']:+.0f})",
             head + "The score is rising off a washout reading from the last 20 sessions: the turn that has marked "
                    "tradeable lows.\n\n" + history_line(c, "Bottoming after a washout") + summary(c))
    if turn and c["phase"] == "Topping" and hi20 >= ZONE_IN:
        send(f"Market cycle rolling over after overheating ({c['score']:+.0f})",
             head + "The score is falling from an overheated reading in the last 20 sessions. Tops are processes: "
                    "watch beta leadership (SPHB/SPLV) and Nasdaq breadth for confirmation.\n\n"
             + history_line(c, "Topping after overheated") + summary(c))
    return {"zone": zone, "phase": c["phase"], "as_of": c["as_of"]}


def lambda_handler(event, context):
    event = event if isinstance(event, dict) else {}
    s3 = boto3.client("s3")
    prev = previous(s3)
    c = cycle.build(screener.fetch_sp1500_universe())

    if prev is None:
        c["alert_state"] = {"zone": zone_of(c["score"], None), "phase": c["phase"], "as_of": c["as_of"]}
    elif event.get("no_alerts"):
        c["alert_state"] = prev.get("alert_state") or {}
    else:
        try:
            c["alert_state"] = alerts(c, prev.get("alert_state") or {})
        except Exception as ex:   # alerting must never block the page data
            print(f"cycle alerts failed: {type(ex).__name__}: {ex}")
            c["alert_state"] = prev.get("alert_state") or {}

    s3.put_object(Bucket=RESULTS_BUCKET, Key=CYCLE_KEY, Body=json.dumps(c),
                  ContentType="application/json", CacheControl="max-age=300")
    if CLOUDFRONT_DISTRIBUTION_ID:
        boto3.client("cloudfront").create_invalidation(
            DistributionId=CLOUDFRONT_DISTRIBUTION_ID,
            InvalidationBatch={"Paths": {"Quantity": 1, "Items": [f"/{CYCLE_KEY}"]},
                               "CallerReference": str(time.time())})
    print(f"cycle {c['as_of']}: {c['score']} {c['bucket']} / {c['phase']}")
    return {"statusCode": 200, "as_of": c["as_of"], "score": c["score"], "phase": c["phase"]}
