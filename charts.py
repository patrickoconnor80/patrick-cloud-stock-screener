"""
PNG charts for the alert emails. Email clients (Gmail included) do not render
inline SVG, so the market-cycle Lambda draws these once a day and uploads them
to the site bucket; emails reference them by URL.
"""

from __future__ import annotations

import io
import os
from datetime import datetime

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl")   # Lambda: only /tmp is writable

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

SIG = {"tailwind": "#16a34a", "headwind": "#dc2626", "neutral": "#9ca3af"}
INK, MUTED, RULE = "#111827", "#6b7280", "#e5e7eb"


def macro_png(macro: dict, years: float = 2.0) -> bytes:
    """Five small panels, one per macro item, last `years` years, line colored
    by the tailwind/headwind signal it gave at each point."""
    items = macro["items"]
    fig, axes = plt.subplots(3, 2, figsize=(9.6, 7.6), dpi=130)
    fig.patch.set_facecolor("white")
    for ax in axes.flat[len(items):]:
        ax.axis("off")
    cutoff = None
    for ax, it in zip(axes.flat, items):
        plot = it.get("plot") or {"key": "v", "lines": []}
        k = plot["key"]
        pts = [(datetime.fromisoformat(h["d"]), h[k], h["g"]) for h in it["hist"] if h.get(k) is not None]
        if cutoff is None and pts:
            last = max(p[0] for p in pts)
            cutoff = last.replace(year=last.year - int(years), month=last.month)
        pts = [p for p in pts if p[0] >= cutoff]
        for (d0, v0, _), (d1, v1, g1) in zip(pts, pts[1:]):
            ax.plot([d0, d1], [v0, v1], color=SIG.get(g1, SIG["neutral"]), lw=2.0, solid_capstyle="round")
        if pts:
            ax.scatter([pts[-1][0]], [pts[-1][1]], s=28, color=INK, zorder=3)
        for v in plot["lines"]:   # signal thresholds: below/above = tailwind/headwind
            ax.axhline(v, color=MUTED, lw=.8, ls="--")
        if plot["lines"]:
            ax.axhline(0, color=RULE, lw=.8)
        ax.set_title(f"{it['label']}  {it['value']}", loc="left", fontsize=11, fontweight="bold", color=INK, pad=20)
        ax.text(0, 1.03, f"{it['change']}  ·  {it['signal']}", transform=ax.transAxes, fontsize=8.5,
                color=SIG.get(it["signal"], MUTED), va="bottom", ha="left")
        ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=(1, 7)))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %y"))
        ax.tick_params(labelsize=8, colors=MUTED, length=0)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(RULE)
        ax.grid(axis="y", color=RULE, lw=.6)
    # legend in the spare cell
    leg = axes.flat[-1]
    for i, (name, c) in enumerate((("Tailwind for high beta", SIG["tailwind"]), ("Neutral", SIG["neutral"]),
                                   ("Headwind for high beta", SIG["headwind"]))):
        leg.plot([0.05, 0.2], [0.75 - i * .2] * 2, color=c, lw=3, transform=leg.transAxes)
        leg.text(0.25, 0.75 - i * .2, name, transform=leg.transAxes, va="center", fontsize=10, color=INK)
    leg.text(0.05, 0.08, f"Macro backdrop: {macro['overall']}", transform=leg.transAxes, fontsize=11,
             fontweight="bold", color=INK)
    fig.tight_layout(h_pad=2.2, w_pad=2.0)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    return buf.getvalue()
