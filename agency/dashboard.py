"""Renders the analytics snapshot as one self-contained HTML page.

No external scripts, fonts or CDNs: charts are inline SVG generated here, so
the page works offline, from a file, or behind /admin on the server.
"""
from __future__ import annotations

import json
from html import escape

from .analytics import Snapshot


def money(cents: int | None) -> str:
    return "–" if cents is None else f"${cents / 100:,.0f}"


def pct(x: float | None) -> str:
    return "–" if x is None else f"{x:.1%}"


# ---------------------------------------------------------------------------
# charts
# ---------------------------------------------------------------------------
def _nice_max(v: float) -> float:
    if v <= 0:
        return 100.0
    for step in (1, 2, 2.5, 5):
        for mag in (10 ** k for k in range(0, 9)):
            if step * mag * 4 >= v:
                return step * mag * 4
    return v


def mrr_chart(history: list[dict]) -> str:
    """Stacked bars: base plan (series 1) + add-ons (series 2), in dollars."""
    W, H, L, R, T, B = 720, 260, 56, 12, 16, 28
    totals = [(h["base"] + h["addons"]) / 100 for h in history]
    top = _nice_max(max(totals + [0]))
    pw, ph = W - L - R, H - T - B
    slot = pw / max(len(history), 1)
    bw = min(28.0, slot * 0.55)
    y = lambda v: T + ph - (v / top) * ph  # noqa: E731
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Monthly recurring revenue, last 12 months" '
             f'class="chart">']
    for i in range(5):  # recessive grid + y labels
        v = top * i / 4
        parts.append(f'<line x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>'
                     f'<text x="{L - 8}" y="{y(v) + 4:.1f}" class="axis" text-anchor="end">${v:,.0f}</text>')
    for i, h in enumerate(history):
        cx = L + slot * i + slot / 2
        x = cx - bw / 2
        base, add = h["base"] / 100, h["addons"] / 100
        yb = y(base)
        tip = escape(json.dumps({"t": h["label"], "rows": [["Base plans", f"${base:,.0f}"],
                                                           ["Add-ons", f"${add:,.0f}"],
                                                           ["Total", f"${base + add:,.0f}"]]}))
        if base > 0:
            r = 4 if add <= 0 else 0
            parts.append(_bar(x, yb, bw, y(0) - yb, r, "s1"))
        if add > 0:
            ya = y(base + add)
            parts.append(_bar(x, ya, bw, max(yb - ya - 2, 1), 4, "s2"))  # 2px surface gap
        parts.append(f'<text x="{cx:.1f}" y="{H - 8}" class="axis" text-anchor="middle">{escape(h["label"])}</text>')
        parts.append(f'<rect x="{cx - slot / 2:.1f}" y="{T}" width="{slot:.1f}" height="{ph}" '
                     f'class="hit" data-tip="{tip}"/>')
    if history:  # selective direct label: the latest total only
        last = totals[-1]
        cx = L + slot * (len(history) - 1) + slot / 2
        parts.append(f'<text x="{cx:.1f}" y="{y(last) - 6:.1f}" class="label" text-anchor="middle">'
                     f'${last:,.0f}</text>')
    parts.append(f'<line x1="{L}" x2="{W - R}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="baseline"/></svg>')
    return "".join(parts)


def _bar(x: float, yt: float, w: float, h: float, r: float, cls: str) -> str:
    """Bar with rounded top (data end) and square bottom (anchored to baseline)."""
    r = min(r, w / 2, h)
    if r <= 0:
        return f'<rect x="{x:.1f}" y="{yt:.1f}" width="{w:.1f}" height="{h:.1f}" class="{cls}"/>'
    return (f'<path class="{cls}" d="M{x:.1f},{yt + h:.1f} V{yt + r:.1f} Q{x:.1f},{yt:.1f} {x + r:.1f},{yt:.1f} '
            f'H{x + w - r:.1f} Q{x + w:.1f},{yt:.1f} {x + w:.1f},{yt + r:.1f} V{yt + h:.1f} Z"/>')


def funnel_chart(funnel: list[dict]) -> str:
    first = max(funnel[0]["count"], 1) if funnel else 1
    rows = []
    for f in funnel:
        width = 100 * f["count"] / first
        conv = f"{f['rate_from_prev']:.0%} of previous" if f["rate_from_prev"] is not None else "&nbsp;"
        tip = escape(json.dumps({"t": f["stage"], "rows": [["Leads", f"{f['count']:,}"],
                                                           ["From previous step", pct(f["rate_from_prev"])]]}))
        rows.append(
            f'<div class="frow hit-row" data-tip="{tip}"><div class="fname">{escape(f["stage"])}</div>'
            f'<div class="ftrack"><div class="fbar" style="width:{max(width, 0.5):.1f}%"></div></div>'
            f'<div class="fval"><strong>{f["count"]:,}</strong><span>{conv}</span></div></div>')
    return '<div class="funnel">' + "".join(rows) + "</div>"


def segment_table(rows: list[dict], title: str) -> str:
    top = max([r["mrr_cents"] for r in rows] + [1])
    body = "".join(
        f'<tr><td>{escape(r["segment"])}</td><td class="n">{r["leads"]}</td><td class="n">{r["emailed"]}</td>'
        f'<td class="n">{pct(r["reply_rate"]) if r["emailed"] else "–"}</td>'
        f'<td class="n">{pct(r["close_rate"]) if r["emailed"] else "–"}{"" if r["enough_data"] else "<sup>*</sup>"}</td>'
        f'<td class="n">{r["clients"]}</td>'
        f'<td class="mrr"><span class="mini" style="width:{60 * r["mrr_cents"] / top:.0f}px"></span>{money(r["mrr_cents"])}</td></tr>'
        for r in rows) or '<tr><td colspan="7">No leads yet</td></tr>'
    return (f'<table><caption>{escape(title)}</caption><thead><tr><th>{escape(title.split()[-1].title())}</th>'
            '<th class="n">Leads</th><th class="n">Emailed</th><th class="n">Reply rate</th>'
            '<th class="n">Close rate</th><th class="n">Clients</th><th>MRR</th></tr></thead>'
            f'<tbody>{body}</tbody></table>')


def tile(label: str, value: str, sub: str = "") -> str:
    return (f'<div class="tile"><div class="tlabel">{escape(label)}</div><div class="tvalue">{value}</div>'
            f'<div class="tsub">{sub}</div></div>')


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------
CSS = """
:root{color-scheme:light;--surface:#fcfcfb;--card:#ffffff;--text-1:#0b0b0b;--text-2:#52514e;--muted:#7a7974;
--grid:#e7e6e2;--line:#c9c8c3;--s1:#2a78d6;--s2:#eb6834;--seq:#2a78d6;--good:#006300;--bad:#b42f2f}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--surface:#1a1a19;
--card:#222220;--text-1:#f0efec;--text-2:#c3c2b7;--muted:#8f8e86;--grid:#33322f;--line:#4a4945;--s1:#3987e5;
--s2:#d95926;--seq:#3987e5;--good:#0ca30c;--bad:#e66767}}
:root[data-theme="dark"]{color-scheme:dark;--surface:#1a1a19;--card:#222220;--text-1:#f0efec;--text-2:#c3c2b7;
--muted:#8f8e86;--grid:#33322f;--line:#4a4945;--s1:#3987e5;--s2:#d95926;--seq:#3987e5;--good:#0ca30c;--bad:#e66767}
*{box-sizing:border-box}body{margin:0;background:var(--surface);color:var(--text-1);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1080px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:1.5rem;margin:0}h2{font-size:1.05rem;margin:0 0 12px}.meta{color:var(--text-2);margin:4px 0 20px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.tile,.card{background:var(--card);border:1px solid var(--grid);border-radius:12px;padding:16px}
.tlabel{color:var(--text-2);font-size:.85rem}.tvalue{font-size:1.6rem;font-weight:700;font-variant-numeric:tabular-nums}
.tsub{color:var(--muted);font-size:.8rem;min-height:1.2em}.up{color:var(--good)}.down{color:var(--bad)}
.card{margin-bottom:16px;overflow-x:auto}.legend{display:flex;gap:16px;color:var(--text-2);font-size:.85rem;margin-bottom:6px}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.chart{width:100%;min-width:560px;height:auto;display:block}.grid{stroke:var(--grid);stroke-width:1}.baseline{stroke:var(--line);stroke-width:1}
.axis{fill:var(--muted);font-size:11px}.label{fill:var(--text-1);font-size:12px;font-weight:600}
.s1{fill:var(--s1)}.s2{fill:var(--s2)}.hit{fill:transparent}.hit:hover{fill:var(--text-1);fill-opacity:.04}
.funnel{display:grid;gap:10px}.frow{display:grid;grid-template-columns:130px 1fr 150px;gap:12px;align-items:center;
border-radius:6px}.frow:hover{background:color-mix(in srgb,var(--text-1) 4%,transparent)}
.ftrack{background:var(--grid);border-radius:4px;height:14px}.fbar{background:var(--seq);height:14px;border-radius:0 4px 4px 0}
.fval{font-variant-numeric:tabular-nums}.fval span{color:var(--muted);font-size:.8rem;margin-left:8px}
table{border-collapse:collapse;width:100%;font-size:.9rem}caption{text-align:left;font-weight:600;padding-bottom:8px}
th,td{padding:7px 8px;border-bottom:1px solid var(--grid);text-align:left;white-space:nowrap}th{color:var(--text-2);font-weight:600}
.n{text-align:right;font-variant-numeric:tabular-nums}.mrr{font-variant-numeric:tabular-nums}
.mini{display:inline-block;height:8px;background:var(--seq);border-radius:0 3px 3px 0;margin-right:8px;vertical-align:0}
ul.recs{margin:0;padding-left:20px}ul.recs li{margin:4px 0}details{margin-top:10px;color:var(--text-2)}
#tip{position:fixed;pointer-events:none;background:var(--card);color:var(--text-1);border:1px solid var(--line);
border-radius:8px;padding:8px 10px;font-size:.82rem;box-shadow:0 4px 16px rgba(0,0,0,.15);display:none;z-index:9}
#tip b{display:block;margin-bottom:4px}#tip td{border:0;padding:1px 6px 1px 0}.foot{color:var(--muted);font-size:.8rem}
@media (max-width:560px){.frow{grid-template-columns:96px 1fr;}.fval{grid-column:1/-1}}
"""

JS = """
const tip=document.getElementById('tip');
document.querySelectorAll('[data-tip]').forEach(el=>{
 el.addEventListener('mousemove',e=>{const d=JSON.parse(el.dataset.tip);
  tip.innerHTML='<b></b><table></table>';tip.querySelector('b').textContent=d.t;
  const t=tip.querySelector('table');d.rows.forEach(([k,v])=>{const r=t.insertRow();r.insertCell().textContent=k;
  const c=r.insertCell();c.textContent=v;c.style.textAlign='right';c.style.fontWeight='600'});
  tip.style.display='block';const x=Math.min(e.clientX+14,innerWidth-tip.offsetWidth-8);
  tip.style.left=x+'px';tip.style.top=(e.clientY+14)+'px'});
 el.addEventListener('mouseleave',()=>tip.style.display='none');});
"""


def render(snap: Snapshot, company: str) -> str:
    k = snap.kpis
    change = k["mrr_change_cents"]
    change_html = (f'<span class="{"up" if change > 0 else "down" if change < 0 else ""}">'
                   f'{"▲" if change > 0 else "▼" if change < 0 else ""} {money(abs(change))} vs 30 days ago</span>')
    churn_sub = "no churn data yet" if k["churn_rate_30d"] is None else "cancellations, last 30 days"
    ltv_sub = "needs churn data" if k["ltv_cents"] is None else "avg revenue ÷ churn"
    tiles = "".join([
        tile("Monthly recurring revenue", money(k["mrr_cents"]), change_html),
        tile("Clients", f'{k["clients"]:,}', f'base {money(k["base_mrr_cents"])} + add-ons {money(k["addon_mrr_cents"])}'),
        tile("Revenue per client", money(k["arpu_cents"]), "per month"),
        tile("Churn rate", pct(k["churn_rate_30d"]), churn_sub),
        tile("Lifetime value", money(k["ltv_cents"]), ltv_sub),
        tile("Collected (30 days)", money(k["revenue_30d_cents"]), f'{money(k["revenue_total_cents"])} all time'),
    ])
    mrr_rows = "".join(f'<tr><td>{escape(h["label"])}</td><td class="n">{money(h["base"])}</td>'
                       f'<td class="n">{money(h["addons"])}</td><td class="n">{money(h["base"] + h["addons"])}</td></tr>'
                       for h in snap.mrr_history)
    recs = "".join(f"<li>{escape(r).replace('**', '')}</li>" for r in snap.recommendations)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agency Performance</title><style>{CSS}</style></head>
<body><main>
<h1>{escape(company)}: Performance</h1>
<p class="meta">Updated {escape(snap.generated_at)}</p>
<section class="tiles">{tiles}</section>
<section class="card"><h2>What to do next</h2><ul class="recs">{recs}</ul></section>
<section class="card"><h2>Monthly recurring revenue</h2>
<div class="legend"><span><i class="sw" style="background:var(--s1)"></i>Base plans</span>
<span><i class="sw" style="background:var(--s2)"></i>Add-ons</span></div>
{mrr_chart(snap.mrr_history)}
<details><summary>Show as table</summary><table><thead><tr><th>Month</th><th class="n">Base</th>
<th class="n">Add-ons</th><th class="n">Total</th></tr></thead><tbody>{mrr_rows}</tbody></table></details></section>
<section class="card"><h2>Sales funnel (all time)</h2>{funnel_chart(snap.funnel)}</section>
<section class="card">{segment_table(snap.by_niche, "Performance by niche")}</section>
<section class="card">{segment_table(snap.by_city, "Performance by city")}</section>
<section class="card">{segment_table(snap.by_source, "Performance by source")}</section>
<section class="card">{segment_table(snap.by_theme, "Performance by design")}</section>
<p class="foot">* fewer than 20 emailed leads: too early to judge. Close rate = became client ÷ emailed.</p>
</main><div id="tip" role="tooltip"></div><script>{JS}</script></body></html>"""
