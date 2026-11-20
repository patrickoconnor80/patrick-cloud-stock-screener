"""
Lambda entrypoint for the SPY/QQQ dealer-gamma snapshot.

Same container image as the stock screener, with the image command overridden
to gex_handler.lambda_handler (see tf/gex.tf). EventBridge fires it every 15
minutes on weekdays; the schedule is in UTC, so it is padded to cover market
hours in both EST and EDT, and this handler skips runs outside 9:30-16:15 ET.

Each run writes the snapshot to S3 for the website and emails (SES HTML, SNS text fallback):
  - a PIN alert the first time a ticker's pin setup turns ready for a given
    node and expiry (de-duplicated against the previous snapshot in S3), and
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
    return (f'<div style="overflow-x:auto;margin-top:4px;"><table cellspacing="0" cellpadding="0" style="border-collapse:collapse;background:#0b0b10;border-radius:6px;">'
            f'<tr><th style="{td}color:#9ca3af;">Strike</th>{head}</tr>{"".join(body)}</table></div>'
            f'<div style="font:11px/1.5 {FONT};color:#6b7280;margin-top:6px;">{cap}</div>')


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


FONT = "-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif"
ACCENT = {"pin": "#0d9488", "event": "#d97706", "test": "#6b7280"}
esc = html.escape


class Email:
    """One alert, built as an inline-styled HTML card (table layout, since email
    clients drop <style> blocks and flexbox) and a matching plain-text body."""

    def __init__(self, kicker, title, accent):
        self.kicker, self.title, self.accent = kicker, title, accent
        head = f"{kicker.upper()}: {title}"
        self.h, self.t = [], [head, "=" * len(head), ""]

    def lead(self, s):
        self.h.append(f'<p style="margin:0 0 18px;font:16px/1.5 {FONT};color:#111827;">{esc(s)}</p>')
        self.t += [s, ""]

    def para(self, s, muted=False):
        c, sz = ("#6b7280", 13) if muted else ("#374151", 14)
        self.h.append(f'<p style="margin:0 0 12px;font:{sz}px/1.55 {FONT};color:{c};">{esc(s)}</p>')
        self.t += [s, ""]

    def callout(self, s):
        self.h.append(f'<div style="margin:0 0 16px;padding:10px 14px;background:#fffbeb;border-left:4px solid #f59e0b;'
                      f'border-radius:4px;font:14px/1.5 {FONT};color:#78350f;">{esc(s)}</div>')
        self.t += [f"!! {s}", ""]

    def heading(self, s, text=True):
        self.h.append(f'<div style="margin:22px 0 10px;padding-top:16px;border-top:1px solid #e5e7eb;'
                      f'font:700 11px {FONT};letter-spacing:.08em;text-transform:uppercase;color:#6b7280;">{esc(s)}</div>')
        if text:
            self.t += [s.upper(), "-" * len(s)]

    def stats(self, pairs):
        """Row of label-over-value tiles."""
        cells = "".join(
            f'<td style="padding:10px 12px;background:#f9fafb;border:1px solid #e5e7eb;vertical-align:top;">'
            f'<div style="font:600 10px {FONT};letter-spacing:.06em;text-transform:uppercase;color:#6b7280;">{esc(k)}</div>'
            f'<div style="font:700 17px/1.3 {FONT};color:#111827;margin-top:3px;white-space:nowrap;">{esc(str(v))}</div></td>'
            for k, v in pairs)
        self.h.append(f'<table cellspacing="0" cellpadding="0" width="100%" style="border-collapse:collapse;margin:0 0 18px;">'
                      f'<tr>{cells}</tr></table>')
        w = max(len(k) for k, _ in pairs)
        self.t += [f"  {k:<{w}}  {v}" for k, v in pairs] + [""]

    def table(self, rows, head=None):
        """rows: lists of cells, first column left-aligned; a cell is text or (text, css color)."""
        n = len(head or rows[0])
        align = ["left"] + ["right"] * (n - 1)
        txt = lambda c: c[0] if isinstance(c, tuple) else str(c)
        out = []
        if head:
            out.append("<tr>" + "".join(
                f'<th style="padding:6px 8px;text-align:{a};font:600 11px {FONT};color:#6b7280;border-bottom:2px solid #e5e7eb;">{esc(h)}</th>'
                for h, a in zip(head, align)) + "</tr>")
        for r in rows:
            out.append("<tr>" + "".join(
                f'<td style="padding:7px 8px;text-align:{a};font:{"600 " if i == 0 else ""}14px {FONT};'
                f'color:{c[1] if isinstance(c, tuple) else "#111827"};border-bottom:1px solid #f3f4f6;">{esc(txt(c))}</td>'
                for i, (c, a) in enumerate(zip(r, align))) + "</tr>")
        self.h.append(f'<table cellspacing="0" cellpadding="0" width="100%" style="border-collapse:collapse;margin:0 0 8px;">{"".join(out)}</table>')
        grid = ([head] if head else []) + [[txt(c) for c in r] for r in rows]
        ws = [max(len(r[i]) for r in grid) for i in range(n)]
        for j, r in enumerate(grid):
            self.t.append("  " + "  ".join(c.ljust(w) if a == "left" else c.rjust(w) for c, w, a in zip(r, ws, align)).rstrip())
            if head and j == 0:
                self.t.append("  " + "  ".join("-" * w for w in ws))
        self.t.append("")

    def checks(self, items):
        rows = "".join(
            f'<tr><td style="padding:5px 10px 5px 0;width:22px;font:700 15px {FONT};color:{"#16a34a" if ok else "#9ca3af"};">'
            f'{"&#10003;" if ok else "&#10007;"}</td>'
            f'<td style="padding:5px 0;font:14px {FONT};color:{"#111827" if ok else "#6b7280"};">{esc(k)}</td></tr>'
            for k, ok in items.items())
        self.h.append(f'<table cellspacing="0" cellpadding="0" style="margin:0 0 8px;">{rows}</table>')
        self.t += [f"  [{'x' if ok else ' '}] {k}" for k, ok in items.items()] + [""]

    def raw(self, fragment):
        """HTML-only content (the heatmaps)."""
        if fragment:
            self.h.append(fragment)

    def html(self, footer=""):
        button = (f'<table cellspacing="0" cellpadding="0" style="margin:26px 0 4px;"><tr>'
                  f'<td style="background:{self.accent};border-radius:6px;">'
                  f'<a href="{SITE}" style="display:inline-block;padding:11px 20px;font:600 14px {FONT};color:#ffffff;text-decoration:none;">'
                  f'Open the live map &rarr;</a></td></tr></table>')
        foot = f'<p style="margin:0;font:12px/1.5 {FONT};color:#9ca3af;">{esc(footer)}</p>' if footer else ""
        return (f'<table cellspacing="0" cellpadding="0" width="100%" style="background:#f3f4f6;"><tr><td align="center" style="padding:24px 12px;">'
                f'<table cellspacing="0" cellpadding="0" width="100%" style="max-width:640px;background:#ffffff;border-radius:10px;overflow:hidden;border:1px solid #e5e7eb;">'
                f'<tr><td style="height:5px;background:{self.accent};font-size:0;line-height:0;">&nbsp;</td></tr>'
                f'<tr><td style="padding:24px 28px 8px;" align="left">'
                f'<div style="font:700 11px {FONT};letter-spacing:.1em;text-transform:uppercase;color:{self.accent};">{esc(self.kicker)}</div>'
                f'<div style="font:700 24px/1.25 {FONT};color:#111827;margin:6px 0 16px;">{esc(self.title)}</div>'
                + "".join(self.h) + button +
                f'</td></tr><tr><td align="left" style="padding:16px 28px 22px;border-top:1px solid #f3f4f6;">{foot}'
                f'<p style="margin:6px 0 0;font:12px {FONT};color:#9ca3af;">patrick-cloud.com &middot; GEX alerts</p>'
                f'</td></tr></table></td></tr></table>')

    def text(self, footer=""):
        return "\n".join(self.t + [f"Live map: {SITE}", ""] + ([footer] if footer else [])).strip() + "\n"


def send(subject, msg, footer=""):
    print(f"ALERT: {subject}")
    if not ALERT_TOPIC_ARN:
        return
    body = msg.text(footer)
    if SES_FROM:
        try:
            to = email_recipients()
            if to:
                boto3.client("ses").send_email(
                    Source=SES_FROM, Destination={"ToAddresses": to},
                    Message={"Subject": {"Data": subject[:200]},
                             "Body": {"Text": {"Data": body}, "Html": {"Data": msg.html(footer)}}})
                return
        except Exception as ex:   # fall back to the plain-text SNS email
            print(f"SES send failed ({type(ex).__name__}: {ex}); using SNS")
    boto3.client("sns").publish(TopicArn=ALERT_TOPIC_ARN, Subject=subject[:99], Message=body)


def heatmaps(m, tickers):
    for t in tickers:
        frag = heatmap_html(t)
        if frag:
            m.heading(f"{t['ticker']} gamma heatmap", text=False)
            m.raw(frag)


def _day(iso):
    d = date.fromisoformat(iso[:10])
    return f"{d:%a %b} {d.day}"


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
    today = payload["generated_at"][:10]
    events_today = [e["name"] for e in payload.get("events", []) if e["date"] == today]
    for t in payload["tickers"]:
        pin = t.get("pin") or {}
        if not pin.get("ready") or (t["ticker"], pin["node"], pin["expiry"]) in was_ready:
            continue
        node = f"{pin['node']:g}"
        m = Email("Pin alert", f"{t['ticker']} pin setup at {node}", ACCENT["pin"])
        m.lead(f"Dealers are net long gamma in {t['ticker']}, and the largest positive gamma node near spot is "
               f"{node}, expiring {_day(pin['expiry'])}. Every pin check passes.")
        m.stats([("Spot", f"${t['spot']:,.2f}"), ("Pin node", node), ("Node gamma", money(pin["node_gex"])),
                 ("Gamma flip", f"{t['flip']:g}" if t.get("flip") else "-")])
        if events_today:
            m.callout(f"Scheduled event today: {'; '.join(events_today)}. Pins break on news.")
        m.heading("Pin checks")
        m.checks(pin["checks"])
        m.heading("How to play it")
        m.para(f"Pin structures centered on {node}: a long call butterfly (debit) or a short iron butterfly (credit).")
        m.para("Not a short plain butterfly: that profits from movement away from the strike.", muted=True)
        heatmaps(m, [t])
        send(f"{t['ticker']} pin setup at {node} (exp {pin['expiry'][5:].replace('-', '/')})", m,
             footer=f"Model estimate from {payload.get('source') or 'delayed option chains'}. "
                    "Dealer positions are assumed, not observed.")


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
        implied = [x for x in e.get("implied", []) if x.get("move_pct")]
        live = [t for t in payload["tickers"] if not t.get("error")]
        m = Email("Event heads-up", e["name"], ACCENT["event"])
        m.lead(f"{e['name']} is scheduled for {d:%A, %B} {d.day} at {e.get('time', '?')} ET.")
        m.heading("Implied move")
        if implied:
            m.table([[x["ticker"], f"±{x['move_pct']:.2f}%", f"{x['strike']:g}", f"${x['straddle']:.2f}", _day(x["expiry"])]
                     for x in implied],
                    head=["", "Move", "ATM strike", "Straddle", "Through"])
        else:
            m.para("Not priced yet.", muted=True)
        m.heading("Dealer regime now")
        m.table([[t["ticker"],
                  (f"{t.get('regime', '?')} gamma",
                   {"long": "#0d9488", "short": "#c026d3"}.get(t.get("regime"), "#111827")),
                  f"${t['spot']:,.2f}"] for t in live],
                head=["", "Regime", "Spot"])
        m.heading("Playbook")
        m.para("IV-crush structures (short iron butterfly / iron condor) collect this premium and win if the actual "
               "move is smaller than implied.")
        m.para("In short gamma, a worse-than-priced print runs: keep defined-risk wings.", muted=True)
        heatmaps(m, live)
        send(f"Event {d:%a %b} {d.day}: {e['name']}", m, footer="Not a recommendation to trade.")


def lambda_handler(event, context):
    event = event if isinstance(event, dict) else {}
    if event.get("test_alert"):
        m = Email("Test", "Email alerts are working", ACCENT["test"])
        m.lead("This is a test message from the GEX Lambda. Pin and event alerts arrive in this format.")
        send("GEX alerts: test", m)
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
