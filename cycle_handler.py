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
from gex_handler import Email, macro_block, send

RESULTS_BUCKET = os.environ.get("RESULTS_BUCKET", "patrick-cloud.com")
CYCLE_KEY = os.environ.get("CYCLE_KEY", "stock-screener/cycle.json")
CHARTS_PREFIX = os.environ.get("CHARTS_PREFIX", "stock-screener/charts/")
SITE_ROOT = "https://patrick-cloud.com/"
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
    m.para(f"High beta: {c['beta']['status']}.")
    macro_block(m, c)
    m.para("Model readout from delayed Yahoo data; breadth uses today's index members. Not a recommendation to trade.", muted=True)
    return m


def upload_charts(c, s3):
    """Macro chart PNG for the emails. Dated key, so an old email keeps the
    chart of its own day; nothing to invalidate."""
    import charts   # matplotlib: only loaded on full runs
    key = f"{CHARTS_PREFIX}macro-{c['as_of']}.png"
    s3.put_object(Bucket=RESULTS_BUCKET, Key=key, Body=charts.macro_png(c["macro"]),
                  ContentType="image/png", CacheControl="max-age=31536000")
    return {"macro": SITE_ROOT + key}


BREAK = "#ea580c"


def break_card(c, r, kicker="High beta"):
    """Trailing-stop email: SPHB broke its 20-day trend just off a 52-week high."""
    m = Email(kicker, f"High-beta trend break: SPHB {r['off_peak']:+.1f}% from its high", BREAK)
    m.lead(f"SPHB closed at {r['sphb']:.2f}, {abs(r['below_ema']):.1f}% under its 20-day EMA and {abs(r['off_peak']):.1f}% "
           f"below the {r['peak']:.2f} high of {r['peak_d']}. This is the trailing-stop cue for high-beta positions: "
           "the leading trend has broken. It is not a top call: it fires after the peak, and high beta has often "
           "recovered from here.")
    m.stats([("SPHB", f"{r['sphb']:.2f}"), ("Off high", f"{r['off_peak']:+.1f}%"), ("Cycle score", f"{c['score']:+.0f}"),
             ("Beta", c["beta"]["status"].replace("Beta ", ""))])
    past = [s for s in c.get("signals", []) if s["kind"] == "trend_break" and s.get("sphb60") is not None]
    if past:
        avg = lambda k: sum(s[k] for s in past) / len(past)
        fmt = lambda v: f"{(0.0 if abs(v) < 0.05 else v):+.1f}%"
        b = c["backtest"]["baseline"]
        m.heading(f"After the past {len(past)} breaks since {past[0]['d'][:4]}")
        m.table([["Trend break", fmt(avg("sphb20")), fmt(avg("sphb60")), fmt(avg("sphb_low60"))],
                 [("All days", "#6b7280"), fmt(b["sphb20"]), fmt(b["sphb60"]), "-"]],
                head=["", "SPHB 1 mo", "SPHB 3 mo", "Worst dip in 3 mo"])
        worst = sorted(past, key=lambda s: s["sphb_low60"])[:3]
        m.para("Biggest drops that followed: " + "; ".join(f"{s['d']} ({s['sphb_low60']:+.0f}%)" for s in worst)
               + f". SPHB was higher 3 months later after {round(100 * sum(s['sphb60'] > 0 for s in past) / len(past))}% "
               "of breaks: the value is in sidestepping the few large declines, at the cost of some whipsaws.")
    macro_block(m, c)
    m.para("Rule: SPHB closes more than 2% below its 20-day EMA for the first time while its 15-day high is within 2% "
           "of its 52-week high. One alert per peak; it re-arms at a new 52-week high. Not a recommendation to trade.", muted=True)
    return m


WATCH = "#9333ea"


def watch_card(c, r, kicker="Top watch"):
    """Top-watch email: VIX waking up near the highs while high beta lags."""
    why = (f"VIX is at {r['vix']:.1f}, up {(r['vix'] / r['vix_low20'] - 1) * 100:.0f}% from its 20-day low of {r['vix_low20']:.1f}"
           if r["why"] == "VIX spike" else
           f"VIX/VIX3M is back at {r['term']:.2f} after sitting under 0.82: near-term protection is getting bid")
    m = Email(kicker, "Fear is waking up near the highs", WATCH)
    m.lead(f"{why}, with SPY only {abs(r['spy_off_high']):.1f}% below its 52-week high and high beta already lagging "
           f"(SPHB/SPLV {r['ratio_vs_50d']:+.1f}% vs its 50-day). This combination has come before several high-beta "
           "declines. It is a lean-defensive cue, not a sell signal: it often fires weeks early, and high beta sometimes "
           "rallies first.")
    m.stats([("VIX", f"{r['vix']:.1f}"), ("SPY off high", f"{r['spy_off_high']:+.1f}%"),
             ("SPHB/SPLV vs 50d", f"{r['ratio_vs_50d']:+.1f}%"), ("Cycle score", f"{c['score']:+.0f}")])
    past = [s for s in c.get("signals", []) if s["kind"] == "top_watch" and s.get("sphb60") is not None]
    if past:
        avg = lambda k: sum(s[k] for s in past) / len(past)
        fmt = lambda v: f"{(0.0 if abs(v) < 0.05 else v):+.1f}%"
        b = c["backtest"]["baseline"]
        m.heading(f"After the past {len(past)} top watches since {past[0]['d'][:4]}")
        m.table([["Top watch", fmt(avg("sphb20")), fmt(avg("sphb60")), fmt(avg("sphb_low60"))],
                 [("All days", "#6b7280"), fmt(b["sphb20"]), fmt(b["sphb60"]), "-"]],
                head=["", "SPHB 1 mo", "SPHB 3 mo", "Worst dip in 3 mo"])
        worst = sorted(past, key=lambda s: s["sphb_low60"])[:3]
        m.para("Biggest drops that followed: " + "; ".join(f"{s['d']} ({s['sphb_low60']:+.0f}%)" for s in worst)
               + f". SPHB was still higher 3 months later after {round(100 * sum(s['sphb60'] > 0 for s in past) / len(past))}% "
               "of them: use it to tighten stops and trim size, then let the trend-break alert decide.")
    macro_block(m, c)
    m.para("Rule: VIX 25% or more above its 20-day low (and under 22), or VIX/VIX3M back above 0.90 after being "
           "under 0.82 in the last 20 sessions; SPY within 3% of its 52-week high; SPHB/SPLV below its 50-day average. "
           "First day of each episode. Not a recommendation to trade.", muted=True)
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
    brk = next((s for s in reversed(c.get("signals", [])) if s["kind"] == "trend_break"), None)
    sent_break = state.get("break")
    if brk and brk["d"] == c["as_of"] and sent_break != brk["d"]:
        send(f"High-beta trend break: SPHB {brk['off_peak']:+.1f}% from its high", break_card(c, brk))
        sent_break = brk["d"]
    tw = next((s for s in reversed(c.get("signals", [])) if s["kind"] == "top_watch"), None)
    sent_watch = state.get("top_watch")
    if tw and tw["d"] == c["as_of"] and sent_watch != tw["d"]:
        send(f"Top watch: VIX waking up near the highs ({tw['vix']:.1f})", watch_card(c, tw))
        sent_watch = tw["d"]
    return {"zone": zone, "phase": c["phase"], "as_of": c["as_of"], "break": sent_break, "top_watch": sent_watch}


def lambda_handler(event, context):
    event = event if isinstance(event, dict) else {}
    s3 = boto3.client("s3")
    prev = previous(s3)
    if event.get("sample_break"):
        brk = next((s for s in reversed((prev or {}).get("signals", [])) if s["kind"] == "trend_break"), None)
        if not brk:
            return {"statusCode": 404, "error": "no trend break in cycle.json"}
        send(f"[Sample] High-beta trend break: SPHB {brk['off_peak']:+.1f}% from its high ({brk['d']})",
             break_card(prev, brk, kicker="High beta - sample"))
        return {"statusCode": 200, "sent": "sample_break"}
    if event.get("sample_watch"):
        tw = next((s for s in reversed((prev or {}).get("signals", [])) if s["kind"] == "top_watch"), None)
        if not tw:
            return {"statusCode": 404, "error": "no top watch in cycle.json"}
        send(f"[Sample] Top watch: VIX waking up near the highs ({tw['d']})", watch_card(prev, tw, kicker="Top watch - sample"))
        return {"statusCode": 200, "sent": "sample_watch"}
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
    try:
        c["charts"] = upload_charts(c, s3)
    except Exception as ex:   # emails fall back to the table alone
        print(f"cycle charts failed: {type(ex).__name__}: {ex}")

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
