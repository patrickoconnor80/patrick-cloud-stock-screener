"""
Lambda entrypoint for the SPY/QQQ dealer-gamma snapshot.

Same container image as the stock screener, with the image command overridden
to gex_handler.lambda_handler (see tf/gex.tf). EventBridge fires it every 15
minutes on weekdays; the schedule is in UTC, so it is padded to cover market
hours in both EST and EDT, and this handler skips runs outside 9:30-16:15 ET.

Each run writes the snapshot to S3 for the website and emails (via SNS):
  - a PIN alert the first time a ticker's pin setup turns ready for a given
    node and expiry (de-duplicated against the previous snapshot in S3), and
  - a THERMOMETER alert when the daily market thermometer (thermo.py, stored in
    results.json by the screener Lambda) enters the washout (<= -60) or
    overheated (>= +60) zone. The zone is remembered in gex.json and only
    resets once the score is back inside +/-40, so it fires once per extreme.
  - an EVENT heads-up once, in the 3:30-3:44pm ET run on the trading day
    before a scheduled event (or that morning's 9:30 run for Monday events).

Environment variables (set by Terraform):
  RESULTS_BUCKET              S3 bucket to write gex.json to
  GEX_KEY                     S3 key, e.g. stock-screener/gex.json
  CLOUDFRONT_DISTRIBUTION_ID  distribution to invalidate after writing
  ALERT_TOPIC_ARN             SNS topic for email alerts (optional)
  SES_FROM                    verified SES sender; when set, alerts go out as HTML
                              (with the strike x expiration heatmap) to the topic's
                              confirmed email subscribers, falling back to SNS text

Invoke with {"force": true} to run outside market hours, and
{"test_alert": true} to send a test email.
"""

import html
import json
import os
import time as _time
from datetime import date, datetime, time, timedelta

import boto3

import gex

RESULTS_BUCKET = os.environ.get("RESULTS_BUCKET", "patrick-cloud.com")
GEX_KEY = os.environ.get("GEX_KEY", "stock-screener/gex.json")
RESULTS_KEY = os.environ.get("RESULTS_KEY", "stock-screener/results.json")   # holds the thermometer
CLOUDFRONT_DISTRIBUTION_ID = os.environ.get("CLOUDFRONT_DISTRIBUTION_ID")
ALERT_TOPIC_ARN = os.environ.get("ALERT_TOPIC_ARN")
TRADIER_TOKEN_PARAM = os.environ.get("TRADIER_TOKEN_PARAM")   # SSM SecureString name
SES_FROM = os.environ.get("SES_FROM")
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


def _cell(v, vmax):
    """Teal for positive gamma, magenta for negative, brighter with size."""
    if not v:
        return "#16161d", "#6b7280"
    a = min(abs(v) / vmax, 1) if vmax else 0
    lo, hi = ((15, 60, 58), (20, 184, 166)) if v > 0 else ((60, 20, 52), (217, 70, 239))
    r, g, b = (round(l + (h - l) * a ** 0.6) for l, h in zip(lo, hi))
    return f"rgb({r},{g},{b})", ("#0b0b10" if a > 0.55 else "#f3f4f6")


def _short(v):
    a = abs(v)
    s = f"{a / 1e9:.1f}B" if a >= 1e9 else f"{a / 1e6:.0f}M" if a >= 1e6 else f"{a / 1e3:.0f}K"
    return ("+" if v > 0 else "-") + s


def heatmap_html(t, rows=21):
    """Strike x expiration grid around spot as an inline-styled table (email clients
    drop <style> blocks). Largest absolute cell is yellow, like most GEX apps."""
    g = t.get("grid") or {}
    strikes, exps, vals = g.get("strikes") or [], g.get("expiries") or [], g.get("values") or []
    if not strikes or not exps:
        return ""
    spot, flip = t["spot"], t.get("flip")
    mid = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    lo = max(0, min(mid - rows // 2, len(strikes) - rows))
    idx = range(lo, min(len(strikes), lo + rows))
    cells = [v for i in idx for v in vals[i]]
    vmax = max((abs(v) for v in cells), default=0)
    td = "padding:4px 5px;text-align:right;font:11px Menlo,Consolas,monospace;white-space:nowrap;"
    head = "".join(f'<th style="{td}color:#9ca3af;font-weight:600;">{e[5:].replace("-", "/")}</th>' for e in exps)
    body = []
    for i in idx:
        k = strikes[i]
        is_spot = i == mid
        flip_here = flip is not None and i + 1 < len(strikes) and strikes[i + 1] < flip <= k
        lab = f"{k:g}" + (" &#9664;" if is_spot else "")
        row = [f'<td style="{td}color:{"#fbbf24" if is_spot else "#e5e7eb"};font-weight:{700 if is_spot else 400};">{lab}</td>']
        for v in vals[i]:
            bg, fg = ("#facc15", "#111") if vmax and abs(v) == vmax else _cell(v, vmax)
            row.append(f'<td style="{td}background:{bg};color:{fg};">{_short(v) if v else ""}</td>')
        border = "border-bottom:2px dashed #f59e0b;" if flip_here else ""
        body.append(f'<tr style="{border}">' + "".join(row) + "</tr>")
    regime = t.get("regime", "?")
    cap = (f"{t['ticker']} ${spot:,.2f} &middot; dealers net {html.escape(regime)} gamma"
           + (f" &middot; flip {flip:g} (dashed line)" if flip else "")
           + " &middot; &#9664; = spot &middot; $ gamma per 1% move")
    return (f'<div style="margin:18px 0 6px;font:600 14px -apple-system,Segoe UI,Arial;color:#111;">{t["ticker"]} gamma heatmap</div>'
            f'<div style="overflow-x:auto;"><table cellspacing="0" cellpadding="0" style="border-collapse:collapse;background:#0b0b10;border-radius:6px;">'
            f'<tr><th style="{td}color:#9ca3af;">Strike</th>{head}</tr>{"".join(body)}</table></div>'
            f'<div style="font:11px -apple-system,Segoe UI,Arial;color:#6b7280;margin-top:4px;max-width:420px;">{cap}</div>')


def email_recipients():
    """Confirmed email subscribers of the alert topic, so the address list lives in
    one place (the SNS subscription) and never in code or Terraform."""
    out, kw = [], {"TopicArn": ALERT_TOPIC_ARN}
    sns = boto3.client("sns")
    while True:
        r = sns.list_subscriptions_by_topic(**kw)
        out += [s["Endpoint"] for s in r["Subscriptions"]
                if s["Protocol"] == "email" and s["SubscriptionArn"].startswith("arn:")]
        if not r.get("NextToken"):
            return out
        kw["NextToken"] = r["NextToken"]


def send(subject, body, tickers=()):
    print(f"ALERT: {subject}")
    if not ALERT_TOPIC_ARN:
        return
    if SES_FROM:
        try:
            to = email_recipients()
            if to:
                text = html.escape(body).replace(html.escape(SITE), f'<a href="{SITE}">{SITE}</a>')
                page = ('<div style="max-width:640px;font:14px/1.5 -apple-system,Segoe UI,Arial;color:#111;">'
                        f'<div style="white-space:pre-wrap;">{text}</div>'
                        + "".join(heatmap_html(t) for t in tickers) + "</div>")
                boto3.client("ses").send_email(
                    Source=SES_FROM, Destination={"ToAddresses": to},
                    Message={"Subject": {"Data": subject[:200]},
                             "Body": {"Text": {"Data": body}, "Html": {"Data": page}}})
                return
        except Exception as ex:   # fall back to the plain-text SNS email
            print(f"SES send failed ({type(ex).__name__}: {ex}); using SNS")
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
            tickers=[t],
        )


THERMO_IN, THERMO_RESET = 60, 40


def thermo_zone(s3, prev):
    """(zone, thermometer): zone is "cold", "hot" or None, with hysteresis so a
    score hovering around the line doesn't re-alert."""
    last = prev.get("thermo_zone")
    try:
        t = json.loads(s3.get_object(Bucket=RESULTS_BUCKET, Key=RESULTS_KEY)["Body"].read()).get("thermometer")
    except Exception as ex:
        print(f"thermometer unavailable ({type(ex).__name__}); keeping zone {last}")
        return last, None
    if not t or t.get("score") is None:
        return last, None
    s = t["score"]
    if s <= -THERMO_IN:
        return "cold", t
    if s >= THERMO_IN:
        return "hot", t
    if abs(s) < THERMO_RESET:
        return None, t
    return last, t


def thermo_alert(t, zone, last):
    if not zone or zone == last or not t:
        return
    comps = "\n".join(f"  {c['label']}: {c['value'] or '-'}  ({'+' if (c['temp'] or 0) > 0 else ''}{c['temp']})"
                       for c in t["components"])
    row = next((b for b in t["backtest"]["buckets"] if b["bucket"] == t["bucket"]), None)
    base = t["backtest"]["baseline"]
    hist = (f"Past days in this zone ({row['days']}): SPY averaged {row['avg20']:+.2f}% over the next month and "
            f"{row['avg60']:+.2f}% over 3 months (all days: {base['avg20']:+.2f}% / {base['avg60']:+.2f}%).\n\n") if row else ""
    read = ("Fear, breadth and momentum are washed out together. Readings like this have clustered near "
            "short-term lows; look for the reversal day rather than catching the first dip."
            if zone == "cold" else
            "Momentum and breadth are stretched. Overheated readings have still averaged gains, just smaller ones; "
            "treat it as a reason to tighten stops and trim size, not as a short signal.")
    send(f"Market thermometer {t['score']:+.0f}: {t['bucket']}",
         f"The market thermometer closed {t['as_of']} at {t['score']:+.0f} ({t['bucket']}), "
         f"{'below -' if zone == 'cold' else 'above +'}{THERMO_IN}.\n\n{read}\n\n{hist}"
         f"Components (temperature -100 cold .. +100 hot):\n{comps}\n\n"
         f"Full readout: {SITE}\n\nNot a recommendation to trade.")


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
            tickers=[t for t in payload["tickers"] if not t.get("error")],
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
    zone, thermometer = thermo_zone(s3, prev)
    payload["thermo_zone"] = zone
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

    for fn, args in ((pin_alerts, (payload, prev)), (event_alerts, (payload, now)),
                     (thermo_alert, (thermometer, zone, prev.get("thermo_zone")))):
        try:
            fn(*args)
        except Exception as ex:   # alerting must never block the snapshot
            print(f"{fn.__name__} failed: {ex}")

    return {
        "statusCode": 200,
        "tickers": {t["ticker"]: t.get("regime") for t in payload["tickers"]},
        "pin_ready": [t["ticker"] for t in payload["tickers"] if t.get("pin", {}).get("ready")],
    }
