"""Printable batch / shift report (HTML; the browser saves it as PDF)."""
from __future__ import annotations

from datetime import datetime
from html import escape

from .recipes import RECIPE_FIELDS

UNITS = {"camera_delay_ms": "ms", "reject_delay_ms": "ms", "exposure_us": "µs", "gain_db": "",
         "vision_sensitivity": "", "vision_min_area": "px", "pulses": "", "pulse_on_ms": "ms",
         "pulse_off_ms": "ms"}


def _dt(s):
    return datetime.fromisoformat(s) if s else None


def _dur(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s"


def _chart(b: dict) -> str:
    buckets = sorted((int(k), v) for k, v in b.get("buckets", {}).items())
    if not buckets:
        return '<p class="muted">No cans inspected.</p>'
    W, H, pl, pr, pt, pb = 900, 220, 44, 44, 10, 28
    cw, ch = W - pl - pr, H - pt - pb
    t0 = buckets[0][0]
    n_slots = (buckets[-1][0] - t0) // 300 + 1
    peak = max(v[0] for _, v in buckets)
    step = next((s for s in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000) if s * 4 >= peak),
                max(1, -(-peak // 4)))
    top = step * 4
    bw = cw / max(1, n_slots)
    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Cans and yield per 5 minutes">']
    for k in range(5):
        y = pt + ch - ch * k / 4
        out.append(f'<line x1="{pl}" x2="{W - pr}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{pl - 8}" y="{y + 4:.1f}" text-anchor="end">{step * k}</text>')
        out.append(f'<text x="{W - pr + 8}" y="{y + 4:.1f}">{k * 25}%</text>')
    pts = []
    for t, (n, bad) in buckets:
        i = (t - t0) // 300
        x = pl + i * bw + bw * 0.15
        w = bw * 0.7
        h = ch * n / top
        hb = ch * bad / top
        out.append(f'<rect x="{x:.1f}" y="{pt + ch - h:.1f}" width="{w:.1f}" height="{h:.1f}" class="bar"/>')
        if bad:
            out.append(f'<rect x="{x:.1f}" y="{pt + ch - hb:.1f}" width="{w:.1f}" height="{hb:.1f}" class="bad"/>')
        pts.append((pl + (i + 0.5) * bw, pt + ch - ch * (n - bad) / n))
    if len(pts) > 1:
        out.append('<polyline class="line" points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + '"/>')
    for x, y in pts:
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" class="dot"/>')
    labels = {0: buckets[0][0], n_slots - 1: buckets[-1][0]}
    if n_slots > 2:
        labels[(n_slots - 1) // 2] = t0 + (n_slots - 1) // 2 * 300
    for i, t in labels.items():
        x = pl + (i + 0.5) * bw
        out.append(f'<text x="{x:.1f}" y="{H - 8}" text-anchor="middle">{datetime.fromtimestamp(t):%H:%M}</text>')
    out.append("</svg>")
    return "".join(out)


def render_report(b: dict, review: dict | None, station: str = "Yards Brewing · empty can line",
                  embed: bool = False) -> str:
    e = escape
    n, passed, failed = b["n"], b["passed"], b["failed"]
    start, end = _dt(b["start"]), _dt(b.get("end"))
    duration = ((end or datetime.now()) - start).total_seconds()
    run_s = b.get("run_s", 0)
    yld = f"{passed / n * 100:.1f}%" if n else "—"
    rate = f"{n / (run_s / 60):.0f}" if run_s >= 60 else "—"
    avg_ms = f"{b['ms_total'] / n:.0f} ms" if n else "—"
    status = "Completed" if end else "In progress (live)"

    kpis = [("Inspected", f"{n:,}", ""), ("Passed", f"{passed:,}", ""), ("Rejected", f"{failed:,}", ""),
            ("Yield", yld, ""), ("Not rejected", f"{b['escaped']:,}", "bad" if b["escaped"] else ""),
            ("Cans / min (running)", rate, ""), ("Avg inspection", avg_ms, "")]
    kpi_html = "".join(f'<div class="kpi {c}"><div class="k">{k}</div><div class="v">{v}</div></div>'
                       for k, v, c in kpis)

    defects = sorted(b.get("defects", {}).items(), key=lambda x: -x[1])
    def_rows = "".join(
        f"<tr><td>{e(name)}</td><td class='num'>{c:,}</td><td class='num'>{c / n * 100:.2f}%</td>"
        f"<td class='num'>{c / failed * 100:.0f}%</td></tr>" for name, c in defects) if defects else \
        "<tr><td colspan='4' class='muted'>No defects recorded.</td></tr>"

    if review and review["reviewed"]:
        fr = review["false_reject_rate"]
        rev_html = (f"<p>{review['reviewed']:,} of {b.get('images', 0):,} saved images reviewed. "
                    f"<b>{review['false_rejects']}</b> false rejects out of {review['fails_reviewed']} reviewed "
                    f"rejects (false-reject rate <b>{fr:.1f}%</b>)" if fr is not None else
                    f"<p>{review['reviewed']:,} images reviewed")
        if review["passes_reviewed"]:
            rev_html += f"; {review['missed']} missed defects among {review['passes_reviewed']} reviewed passes"
        rev_html += ".</p>"
    else:
        rev_html = ("<p class='muted'>No images from this batch have been reviewed yet "
                    "(Review page in the dashboard).</p>")

    events = b.get("events", [])
    ev_rows = "".join(
        f"<tr class='{e(x['level'].lower())}'><td>{e(x['t'][11:19])}</td><td>{e(x['level'])}</td>"
        f"<td>{e(x['msg'])}</td></tr>" for x in events[:300]) or \
        "<tr><td colspan='3' class='muted'>No warnings or faults during this batch.</td></tr>"
    if len(events) > 300:
        ev_rows += f"<tr><td colspan='3' class='muted'>… {len(events) - 300} more in the event log</td></tr>"

    settings = b.get("settings", {})
    set_rows = "".join(f"<tr><td>{label}</td><td class='num'>{e(str(settings.get(f, '—')))} {UNITS.get(f, '')}</td></tr>"
                       for f, label in RECIPE_FIELDS.items())
    for f, label in (("inspector", "Inspector"), ("camera_backend", "Camera source"),
                     ("reject_enabled", "Reject enabled")):
        if f in settings:
            set_rows += f"<tr><td>{label}</td><td class='num'>{e(str(settings[f]))}</td></tr>"

    def t(v):
        return f"{v:.0f} °C" if v is not None else "—"

    health = (f"Raspberry Pi {t(b.get('cpu_min'))} – {t(b.get('cpu_max'))} · "
              f"camera {t(b.get('cam_min'))} – {t(b.get('cam_max'))}")
    availability = f"{run_s / duration * 100:.0f}%" if duration > 0 else "—"

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Batch report {e(b['lot'])}</title>
<style>
@font-face {{ font-family: InterLocal; src: url(/static/fonts/Inter-Regular.woff) format("woff"); font-weight: 400; }}
@font-face {{ font-family: InterLocal; src: url(/static/fonts/Inter-SemiBold.woff) format("woff"); font-weight: 600; }}
@font-face {{ font-family: InterLocal; src: url(/static/fonts/Inter-Bold.woff) format("woff"); font-weight: 700; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #eef1f5; color: #111827; font: 13px/1.5 InterLocal, system-ui, sans-serif; font-feature-settings: "tnum"; }}
.page {{ max-width: 960px; margin: 24px auto; background: #fff; padding: 40px 44px; border-radius: 10px; box-shadow: 0 4px 24px rgba(0,0,0,.08); }}
header {{ display: flex; justify-content: space-between; align-items: flex-start; border-bottom: 2px solid #111827; padding-bottom: 16px; margin-bottom: 20px; }}
h1 {{ font-size: 22px; margin: 0 0 2px; }} h2 {{ font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: #2563eb; margin: 26px 0 10px; }}
.sub {{ color: #6b7280; }} .status {{ font-weight: 700; padding: 4px 10px; border-radius: 6px; background: #ecfdf5; color: #047857; }}
.status.live {{ background: #eff6ff; color: #1d4ed8; }}
.meta {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px 20px; }}
.meta div b {{ display: block; font-size: 11px; color: #6b7280; font-weight: 600; text-transform: uppercase; letter-spacing: .05em; }}
.kpis {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 8px; }}
.kpi {{ border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px 12px; }}
.kpi .k {{ font-size: 10.5px; color: #6b7280; text-transform: uppercase; letter-spacing: .04em; font-weight: 600; }}
.kpi .v {{ font-size: 20px; font-weight: 700; }}
.kpi.bad {{ border-color: #fdba74; background: #fff7ed; }} .kpi.bad .v {{ color: #c2410c; }}
table {{ width: 100%; border-collapse: collapse; }} th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #e5e7eb; }}
th {{ font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: #6b7280; }}
.num {{ text-align: right; }} .muted {{ color: #9ca3af; }}
tr.error td, tr.alarm td {{ color: #b91c1c; }} tr.warn td {{ color: #b45309; }}
.two {{ display: grid; grid-template-columns: 1fr 1fr; gap: 28px; }}
.chart {{ width: 100%; height: auto; }} .chart text {{ font-size: 11px; fill: #6b7280; }}
.chart .grid {{ stroke: #e5e7eb; }} .chart .bar {{ fill: #93c5fd; }} .chart .bad {{ fill: #ef4444; }}
.chart .line {{ fill: none; stroke: #16a34a; stroke-width: 2; }} .chart .dot {{ fill: #16a34a; }}
.legend {{ color: #6b7280; font-size: 11.5px; }} .legend i {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin: 0 4px 0 12px; vertical-align: -1px; }}
.sign {{ display: grid; grid-template-columns: 1fr 1fr; gap: 40px; margin-top: 40px; }}
.sign div {{ border-top: 1px solid #111827; padding-top: 6px; color: #6b7280; font-size: 12px; }}
.toolbar {{ max-width: 960px; margin: 16px auto 0; display: flex; gap: 8px; justify-content: flex-end; }}
.toolbar a, .toolbar button {{ font: 600 13px InterLocal, sans-serif; padding: 8px 14px; border-radius: 8px; border: 1px solid #d1d5db; background: #fff; color: #111827; cursor: pointer; text-decoration: none; }}
.toolbar button {{ background: #2563eb; border-color: #2563eb; color: #fff; }}
footer {{ margin-top: 28px; color: #9ca3af; font-size: 11px; }}
body.embed .page {{ margin: 16px auto; }}
@media print {{ body {{ background: #fff; }} .page {{ margin: 0; box-shadow: none; padding: 0; }} .toolbar {{ display: none; }}
  h2 {{ break-after: avoid; }} table, .chart {{ break-inside: avoid; }} @page {{ margin: 14mm; }} }}
</style></head><body class="{'embed' if embed else ''}">
{"" if embed else f'<div class="toolbar"><a href="/api/batch/{e(b["id"])}.csv">Download CSV</a><button onclick="print()">Print / save as PDF</button></div>'}
<div class="page">
<header><div><h1>Batch report — Lot {e(b['lot'])}</h1><div class="sub">{e(station)} · Can inspection station</div></div>
<span class="status {'live' if not end else ''}">{status}</span></header>

<div class="meta">
<div><b>Lot / batch</b>{e(b['lot'])}</div><div><b>Recipe</b>{e(b['recipe'])}</div>
<div><b>Operator</b>{e(b['operator'])}</div><div><b>Batch ID</b>{e(b['id'])}</div>
<div><b>Start</b>{start:%Y-%m-%d %H:%M}</div><div><b>End</b>{f"{end:%Y-%m-%d %H:%M}" if end else "—"}</div>
<div><b>Duration</b>{_dur(duration)}</div><div><b>Line running</b>{_dur(run_s)} ({availability})</div>
</div>
{f"<p><b>Note:</b> {e(b['note'])}</p>" if b.get('note') else ""}

<h2>Results</h2><div class="kpis">{kpi_html}</div>

<h2>Throughput &amp; yield (per 5 minutes)</h2>
<div class="legend"><i style="background:#93c5fd"></i>cans<i style="background:#ef4444"></i>defects<i style="background:#16a34a"></i>yield %</div>
{_chart(b)}

<div class="two">
<div><h2>Defects by type</h2><table><tr><th>Type</th><th class="num">Cans</th><th class="num">% of cans</th><th class="num">% of defects</th></tr>{def_rows}</table></div>
<div><h2>Review (human check)</h2>{rev_html}
<h2>Station health</h2><p>{health}</p></div>
</div>

<h2>Warnings &amp; faults</h2><table><tr><th>Time</th><th>Level</th><th>Message</th></tr>{ev_rows}</table>

<h2>Settings used</h2><table>{set_rows}</table>

<div class="sign"><div>Operator signature / date</div><div>Quality check signature / date</div></div>
<footer>Generated {datetime.now():%Y-%m-%d %H:%M} by the can inspection station. Every can is listed in the CSV.</footer>
</div></body></html>"""
