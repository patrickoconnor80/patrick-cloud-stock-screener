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
the state. Invoke with {"no_alerts": true} to refresh without emailing, or
{"sample_alert": true} to email a sample built from the latest cycle.json
(no rebuild, state untouched).
"""

import json
import os
import time

import boto3

import cycle
import screener
from gex_handler import Email, send

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


COLD, HOT = "#2563eb", "#dc2626"


def card(c, kicker, title, lead, setup, accent):
    """Alert email in the shared GEX card layout."""
    m = Email(kicker, title, accent)
    m.lead(lead)
    m.stats([("Score", f"{c['score']:+.0f}"), ("Reading", c["bucket"]), ("Phase", c["phase"]),
             ("Close", c["as_of"])])
    b = c["backtest"]
    row = next((r for r in b["buckets"] + b["phases"] + b["setups"] if r["name"] == setup), None)
    if row:
        m.heading("What happened next, " + b["from"][:4] + "-" + b["to"][:4])
        fmt = lambda v: "-" if v is None else f"{v:+.1f}%"
        base = b["baseline"]
        m.table([[f"{setup} ({row['days']} days)", fmt(row["spy20"]), fmt(row["spy60"]), fmt(row["sphb20"]), fmt(row["sphb60"])],
                 [("All days", "#6b7280"), fmt(base["spy20"]), fmt(base["spy60"]), fmt(base["sphb20"]), fmt(base["sphb60"])]],
                head=["", "SPY 1 mo", "SPY 3 mo", "SPHB 1 mo", "SPHB 3 mo"])
    m.heading("What is in the score")
    m.table([[x["label"], x["value"] or "-",
              (f"{x['temp']:+d}" if x["temp"] is not None else "-", COLD if (x["temp"] or 0) < 0 else HOT)]
             for x in c["components"]], head=["", "Value", "-100 cold / +100 hot"])
    mac = c["macro"]
    m.heading(f"High beta: {c['beta']['status']} - Macro backdrop: {mac['overall']}")
    sig = {"tailwind": "#16a34a", "headwind": "#dc2626", "neutral": "#6b7280"}
    m.table([[i["label"], i["value"], i["change"], (i["signal"], sig[i["signal"]])] for i in mac["items"]])
    m.para("Model readout from delayed Yahoo data; breadth uses today's index members. Not a recommendation to trade.", muted=True)
    return m


def alerts(c, state):
    """Returns the new state; sends what is due."""
    zone = zone_of(c["score"], state.get("zone"))
    hist = c["history"][-20:]
    lo20, hi20 = min(h["s"] for h in hist), max(h["s"] for h in hist)

    if zone and zone != state.get("zone"):
        if zone == "cold":
            send(f"Market cycle {c['score']:+.0f}: Washout",
                 card(c, "Market cycle", f"Washout: score {c['score']:+.0f}",
                      "Fear, breadth and momentum are washed out together. Readings like this have clustered near "
                      "short-term lows; the better entry has been the turn (a Bottoming alert follows when the score "
                      "starts rising) rather than the first dip.", "Washout", COLD))
        else:
            send(f"Market cycle {c['score']:+.0f}: Overheated",
                 card(c, "Market cycle", f"Overheated: score {c['score']:+.0f}",
                      "Momentum and breadth are stretched. The index has still tended to grind higher, but high-beta "
                      "stocks have historically gone nowhere over the next 3 months from here: tighten stops and trim "
                      "beta rather than short.", "Overheated", HOT))

    turn = state.get("phase") != c["phase"]
    if turn and c["phase"] == "Bottoming" and lo20 <= -ZONE_IN:
        send(f"Market cycle turning up after a washout ({c['score']:+.0f})",
             card(c, "Market cycle", "Turning up after a washout",
                  "The score is rising off a washout reading from the last 20 sessions: the turn that has marked "
                  "tradeable lows.", "Bottoming after a washout", COLD))
    if turn and c["phase"] == "Topping" and hi20 >= ZONE_IN:
        send(f"Market cycle rolling over after overheating ({c['score']:+.0f})",
             card(c, "Market cycle", "Rolling over after overheating",
                  "The score is falling from an overheated reading in the last 20 sessions. Tops are processes: "
                  "watch beta leadership (SPHB/SPLV) and Nasdaq breadth for confirmation.", "Topping after overheated", HOT))
    return {"zone": zone, "phase": c["phase"], "as_of": c["as_of"]}


def lambda_handler(event, context):
    event = event if isinstance(event, dict) else {}
    s3 = boto3.client("s3")
    prev = previous(s3)
    if event.get("sample_alert"):
        if not prev:
            return {"statusCode": 404, "error": "no cycle.json yet"}
        setup = "Bottoming after a washout" if prev["phase"] == "Bottoming" else prev["bucket"]
        send(f"[Sample] Market cycle {prev['score']:+.0f}: {prev['bucket']}, phase {prev['phase']}",
             card(prev, "Market cycle - sample", f"{prev['bucket']}: score {prev['score']:+.0f}, phase {prev['phase']}",
                  f"Sample built from the {prev['as_of']} close: real data, not a live trigger.",
                  setup, COLD if prev["score"] < 0 else HOT))
        return {"statusCode": 200, "sent": "sample"}
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
