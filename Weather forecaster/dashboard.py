"""Render the KAUS forecast dashboard: one self-contained HTML file built
from local data (bundle_latest.json, forecast_log.csv, observed.csv,
normals_cache.json). No external requests, works offline, light/dark aware.

Chart palette follows the validated reference dataviz palette (slots 1-2:
blue = low, orange = high; adjacent-pair CVD-safe in both modes).
"""

from __future__ import annotations

import html
import json
import re
from datetime import date, timedelta
from pathlib import Path

import verification
from wxutil import today_local

DATA_DIR = Path(__file__).resolve().parent / "data"
OUT_PATH = Path(__file__).resolve().parent / "dashboard.html"

PAST_DAYS = 14  # observed history shown left of "today"


# ------------------------------------------------------------ data prep ----

def _normals_for(d: date) -> dict:
    cache_path = DATA_DIR / "normals_cache.json"
    if not hasattr(_normals_for, "_cache"):
        try:
            _normals_for._cache = json.loads(cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            _normals_for._cache = {}
    return _normals_for._cache.get(d.strftime("%m-%d"), {})


def _observed_series() -> dict:
    """{date_iso: {'high': f, 'low': f}} from observed.csv."""
    out: dict = {}
    for (date_iso, kind), value in verification.observed_lookup().items():
        out.setdefault(date_iso, {})[kind] = value
    return out


def _yesterday_check(observed: dict) -> dict | None:
    """Shortest-lead BLEND forecast vs observed for the most recent verified day."""
    for back in range(1, 4):
        d = (today_local() - timedelta(days=back)).isoformat()
        obs = observed.get(d, {})
        if "high" not in obs:
            continue
        rows = [r for r in verification._read_csv(verification.FORECAST_LOG)
                if r["target_date"] == d and r["source"] == "BLEND" and r["kind"] == "high"]
        if not rows:
            continue
        best = min(rows, key=lambda r: int(r["lead_days"]))
        try:
            fc = float(best["value_f"])
        except ValueError:
            continue
        return {"date": d, "forecast": fc, "observed": obs["high"],
                "lead": int(best["lead_days"])}
    return None


def _afd_key_messages(afd_text: str) -> list[str]:
    """Pull the bullet list out of the .KEY MESSAGES section, if present."""
    m = re.search(r"\.KEY MESSAGES\.{3}(.*?)(?:\n&&|\n\.[A-Z])", afd_text, re.S)
    if not m:
        return []
    bullets = re.split(r"\n\s*-\s+", m.group(1))
    out = []
    for b in bullets[1:]:
        text = " ".join(b.split())
        if text:
            out.append(text)
    return out[:4]


def build_view(bundle: dict, results: dict) -> dict:
    """Assemble everything the template needs."""
    today = today_local()
    observed = _observed_series()

    # Merge provisional today/recent from the bundle so the chart shows the
    # freshest picture even before `verify` runs.
    for d_iso, row in (bundle.get("context", {}).get("recent_observed_days") or {}).items():
        cell = observed.setdefault(d_iso, {})
        if row.get("max_f") is not None:
            cell.setdefault("high", row["max_f"])
        if row.get("min_f") is not None:
            cell.setdefault("low", row["min_f"])

    days = []
    start = today - timedelta(days=PAST_DAYS)
    fc_dates = sorted(results)
    end = date.fromisoformat(fc_dates[-1]) if fc_dates else today
    d = start
    while d <= end:
        iso = d.isoformat()
        r = results.get(iso, {})
        hi, lo = r.get("high"), r.get("low")
        norm = _normals_for(d)
        days.append({
            "date": iso,
            "dow": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d.weekday()],
            "obs_high": observed.get(iso, {}).get("high"),
            "obs_low": observed.get(iso, {}).get("low"),
            "fc_high": hi["point_f"] if hi else None,
            "hi_p10": hi["percentiles_f"]["p10"] if hi else None,
            "hi_p90": hi["percentiles_f"]["p90"] if hi else None,
            "fc_low": lo["point_f"] if lo else None,
            "lo_p10": lo["percentiles_f"]["p10"] if lo else None,
            "lo_p90": lo["percentiles_f"]["p90"] if lo else None,
            "n_sources": hi["n_sources"] if hi else None,
            "normal_high": norm.get("normal_high_f"),
            "normal_low": norm.get("normal_low_f"),
        })
        d += timedelta(days=1)

    obs = bundle.get("context", {}).get("obs", {})
    afd = bundle.get("context", {}).get("afd", {})
    return {
        "generated_at": bundle.get("generated_at", ""),
        "today": today.isoformat(),
        "days": days,
        "results": results,
        "fc_dates": fc_dates,
        "now": obs.get("latest", {}),
        "today_so_far": obs.get("today_so_far"),
        "last_24h": obs.get("last_24h"),
        "yesterday": _yesterday_check(observed),
        "scoreboard": verification.scoreboard(),
        "afd_issued": afd.get("issued_utc", ""),
        "afd_bullets": _afd_key_messages(afd.get("text", "")),
        "errors": bundle.get("errors", {}),
    }


# ------------------------------------------------------------ SVG chart ----

CHART_W, CHART_H = 900, 330
MARGIN = {"l": 44, "r": 118, "t": 16, "b": 30}


def _scales(days: list[dict]):
    vals = []
    for d in days:
        for k in ("obs_high", "obs_low", "fc_high", "fc_low",
                  "hi_p10", "hi_p90", "lo_p10", "lo_p90", "normal_high", "normal_low"):
            if d.get(k) is not None:
                vals.append(d[k])
    lo = (min(vals) // 5) * 5 - 5 if vals else 60
    hi = (max(vals) // 5) * 5 + 5 if vals else 110
    plot_w = CHART_W - MARGIN["l"] - MARGIN["r"]
    plot_h = CHART_H - MARGIN["t"] - MARGIN["b"]
    n = max(len(days) - 1, 1)

    def x(i):
        return MARGIN["l"] + plot_w * i / n

    def y(v):
        return MARGIN["t"] + plot_h * (1 - (v - lo) / (hi - lo))

    return x, y, lo, hi


def _path(points: list[tuple[float, float] | None]) -> str:
    """SVG path over (x,y) points, breaking the line at gaps."""
    cmds, pen = [], False
    for pt in points:
        if pt is None:
            pen = False
            continue
        cmds.append(f"{'L' if pen else 'M'}{pt[0]:.1f},{pt[1]:.1f}")
        pen = True
    return " ".join(cmds)


def _series_pts(days, x, y, key):
    return [(x(i), y(d[key])) if d.get(key) is not None else None
            for i, d in enumerate(days)]


def _band(days, x, y, lo_key, hi_key) -> str:
    top = [(x(i), y(d[hi_key])) for i, d in enumerate(days) if d.get(hi_key) is not None]
    bot = [(x(i), y(d[lo_key])) for i, d in enumerate(days) if d.get(lo_key) is not None]
    if len(top) < 2 or len(bot) < 2:
        return ""
    pts = top + bot[::-1]
    return "M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in pts) + " Z"


def render_chart(view: dict) -> str:
    days = view["days"]
    x, y, lo, hi = _scales(days)
    today_idx = next((i for i, d in enumerate(days) if d["date"] == view["today"]), None)

    parts = [f'<svg viewBox="0 0 {CHART_W} {CHART_H}" role="img" '
             f'aria-label="Observed and forecast temperatures at KAUS" '
             f'style="width:100%;height:auto;display:block">']

    # Gridlines + y ticks (hairline, recessive)
    step = 10 if hi - lo > 40 else 5
    t = lo - (lo % step) + step
    while t < hi:
        yy = y(t)
        parts.append(f'<line x1="{MARGIN["l"]}" y1="{yy:.1f}" x2="{CHART_W - MARGIN["r"]}" '
                     f'y2="{yy:.1f}" stroke="var(--grid)" stroke-width="1"/>')
        parts.append(f'<text x="{MARGIN["l"] - 8}" y="{yy + 4:.1f}" text-anchor="end" '
                     f'class="tick">{int(t)}°</text>')
        t += step

    # Today divider
    if today_idx is not None:
        tx = x(today_idx)
        parts.append(f'<line x1="{tx:.1f}" y1="{MARGIN["t"]}" x2="{tx:.1f}" '
                     f'y2="{CHART_H - MARGIN["b"]}" stroke="var(--axis)" stroke-width="1"/>')
        parts.append(f'<text x="{tx:.1f}" y="{MARGIN["t"] + 2}" text-anchor="middle" '
                     f'class="tick" dy="-4">today</text>')

    # Normals (muted gray reference lines)
    for key in ("normal_high", "normal_low"):
        parts.append(f'<path d="{_path(_series_pts(days, x, y, key))}" fill="none" '
                     f'stroke="var(--muted)" stroke-width="1.5" opacity="0.55"/>')

    # Forecast uncertainty bands (10% washes)
    hi_band = _band(days, x, y, "hi_p10", "hi_p90")
    lo_band = _band(days, x, y, "lo_p10", "lo_p90")
    if hi_band:
        parts.append(f'<path d="{hi_band}" fill="var(--series-high)" opacity="0.12"/>')
    if lo_band:
        parts.append(f'<path d="{lo_band}" fill="var(--series-low)" opacity="0.12"/>')

    # Observed + forecast lines (2px, round)
    line_specs = [
        ("obs_high", "var(--series-high)", ""),
        ("obs_low", "var(--series-low)", ""),
        ("fc_high", "var(--series-high)", 'stroke-dasharray="none"'),
        ("fc_low", "var(--series-low)", 'stroke-dasharray="none"'),
    ]
    # Bridge observed -> forecast: prepend today's obs point to forecast line
    for key, color, extra in line_specs:
        parts.append(f'<path d="{_path(_series_pts(days, x, y, key))}" fill="none" '
                     f'stroke="{color}" stroke-width="2" stroke-linecap="round" '
                     f'stroke-linejoin="round" {extra}/>')

    # Markers with surface ring (observed: filled; forecast: ringed)
    for key, color in (("obs_high", "var(--series-high)"), ("obs_low", "var(--series-low)"),
                       ("fc_high", "var(--series-high)"), ("fc_low", "var(--series-low)")):
        for i, d in enumerate(days):
            if d.get(key) is None:
                continue
            parts.append(f'<circle cx="{x(i):.1f}" cy="{y(d[key]):.1f}" r="4" '
                         f'fill="{color}" stroke="var(--surface)" stroke-width="2"/>')

    # Direct end labels (forecast line ends + normals reference)
    last = days[-1]
    for key, label in (("fc_high", "High"), ("fc_low", "Low")):
        if last.get(key) is not None:
            parts.append(f'<text x="{x(len(days) - 1) + 10:.1f}" y="{y(last[key]) + 4:.1f}" '
                         f'class="endlabel">{label} {last[key]:.0f}°</text>')
    if last.get("normal_high") is not None:
        parts.append(f'<text x="{x(len(days) - 1) + 10:.1f}" '
                     f'y="{y(last["normal_high"]) + 4:.1f}" class="tick">'
                     f'normal {last["normal_high"]:.0f}°</text>')

    # X labels (every other day)
    for i, d in enumerate(days):
        if i % 2 == 0:
            parts.append(f'<text x="{x(i):.1f}" y="{CHART_H - 8}" text-anchor="middle" '
                         f'class="tick">{d["dow"]} {d["date"][8:]}</text>')

    # Hover hit columns
    n = len(days)
    plot_w = CHART_W - MARGIN["l"] - MARGIN["r"]
    col_w = plot_w / max(n - 1, 1)
    for i in range(n):
        cx = x(i)
        parts.append(f'<rect x="{cx - col_w / 2:.1f}" y="{MARGIN["t"]}" width="{col_w:.1f}" '
                     f'height="{CHART_H - MARGIN["t"] - MARGIN["b"]}" fill="transparent" '
                     f'data-idx="{i}" class="hit"/>')

    parts.append("</svg>")
    return "".join(parts)


# ------------------------------------------------------------ sections -----

def _fmt(v, suffix="°"):
    return f"{v:.0f}{suffix}" if v is not None else "—"


def render_tiles(view: dict) -> str:
    tiles = []
    results = view["results"]
    today_iso = view["today"]
    tomorrow_iso = (date.fromisoformat(today_iso) + timedelta(days=1)).isoformat()

    def tile(label, value, sub="", delta_html=""):
        return (f'<div class="tile"><div class="tile-label">{label}</div>'
                f'<div class="tile-value">{value}</div>'
                f'{delta_html}<div class="tile-sub">{sub}</div></div>')

    def day_tile(iso, label):
        r = results.get(iso)
        if not r or "high" not in r:
            return ""
        h = r["high"]
        p = h["percentiles_f"]
        low = r.get("low")
        norm = _normals_for(date.fromisoformat(iso)).get("normal_high_f")
        delta_html = ""
        if norm is not None:
            diff = h["point_f"] - norm
            delta_html = (f'<div class="tile-delta">{diff:+.0f}° vs normal '
                          f'{norm:.0f}°</div>')
        sub = (f'80% CI {p["p10"]:.0f}–{p["p90"]:.0f}° · '
               f'low {_fmt(low["point_f"] if low else None)} · '
               f'{h["n_sources"]} sources')
        return tile(label, f'{h["point_f"]:.0f}°', sub, delta_html)

    tiles.append(day_tile(today_iso, f"Today’s high · {today_iso}"))
    tiles.append(day_tile(tomorrow_iso, f"Tomorrow’s high · {tomorrow_iso}"))

    now = view["now"] or {}
    tsf = view["today_so_far"] or {}
    if now.get("temp_f") is not None:
        when = (now.get("time_local") or "")[11:16]
        sub = ""
        if tsf.get("max_f") is not None:
            sub = f'today so far {tsf["max_f"]:.0f}° / {tsf["min_f"]:.0f}°'
        tiles.append(tile(f"At the airport now · {when}", f'{now["temp_f"]:.0f}°', sub))

    yd = view["yesterday"]
    if yd:
        err = yd["forecast"] - yd["observed"]
        cls = "good" if abs(err) <= 2 else "bad"
        verdict = "hit" if abs(err) <= 2 else "miss"
        tiles.append(tile(
            f'Yesterday’s call · {yd["date"]}',
            f'{yd["forecast"]:.0f}° → {yd["observed"]:.0f}°',
            f'lead {yd["lead"]}d blend vs observed high',
            f'<div class="tile-delta {cls}">{err:+.1f}° ({verdict})</div>'))
    return "\n".join(t for t in tiles if t)


def render_ladder(view: dict) -> str:
    """P(high >= T) bars for the first two forecast days."""
    blocks = []
    for iso in view["fc_dates"][:2]:
        h = view["results"].get(iso, {}).get("high")
        if not h:
            continue
        probs = h["prob_at_least_f"]
        center = int(round(h["point_f"]))
        rows = []
        for t in range(center + 3, center - 4, -1):
            p = probs.get(str(t), probs.get(t))
            if p is None:
                continue
            pct = p * 100
            rows.append(
                f'<div class="lrow"><span class="lt">≥ {t}°</span>'
                f'<span class="lbar"><span class="lfill" style="width:{pct:.0f}%"></span></span>'
                f'<span class="lv">{pct:.0f}%</span></div>')
        blocks.append(f'<div class="card"><h3>P(high ≥ T) · {iso}</h3>{"".join(rows)}</div>')
    return "\n".join(blocks)


SOURCE_NAMES = {
    "nws_official": "NWS official", "om_nbm": "NBM (grid)", "mos_nbs": "NBM station",
    "mos_nbe": "NBM extended", "mos_mav": "GFS MOS", "mos_mex": "GFS MOS ext",
    "om_ecmwf": "ECMWF", "om_gfs": "GFS", "om_icon": "ICON", "om_ukmo": "UKMO",
    "om_hrrr": "HRRR", "om_gem": "GEM", "om_jma": "JMA", "om_meteofrance": "Météo-France",
    "BLEND": "Blend", "AGENT": "Agent",
}


def render_source_board(view: dict) -> str:
    dates = view["fc_dates"][:4]
    if not dates:
        return ""
    order, seen = [], set()
    for iso in dates:
        h = view["results"].get(iso, {}).get("high")
        if h:
            for s in h["sources"]:
                if s not in seen:
                    seen.add(s)
                    order.append(s)
    head = "".join(f"<th>{iso[5:]}</th>" for iso in dates)
    rows = []
    for s in order:
        cells = []
        for iso in dates:
            h = view["results"].get(iso, {}).get("high", {})
            info = (h.get("sources") or {}).get(s)
            cells.append(f"<td>{info['value_f']:.0f}°</td>" if info else "<td>—</td>")
        w = next((h["sources"][s]["weight"]
                  for iso in dates
                  if (h := view["results"].get(iso, {}).get("high", {})) and s in h.get("sources", {})), 0)
        rows.append(f"<tr><td class='sname'>{SOURCE_NAMES.get(s, s)}</td>{''.join(cells)}"
                    f"<td class='w'>{w:.2f}</td></tr>")
    blend = []
    for iso in dates:
        h = view["results"].get(iso, {}).get("high")
        blend.append(f"<td>{h['point_f']:.0f}°</td>" if h else "<td>—</td>")
    rows.append(f"<tr class='blend'><td class='sname'>Blend</td>{''.join(blend)}<td class='w'></td></tr>")
    return (f'<div class="card"><h3>Source board — forecast highs</h3>'
            f'<table class="board"><thead><tr><th>source</th>{head}<th>wt</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def render_scoreboard(view: dict) -> str:
    board = [r for r in view["scoreboard"] if r["kind"] == "high"]
    if not board:
        return ('<div class="card"><h3>Verified skill</h3><p class="empty">No verified '
                'forecasts yet — scores appear after the first <code>verify</code> the '
                'day after a forecast run.</p></div>')
    max_mae = max(r["mae_f"] for r in board) or 1
    rows = []
    for r in board:
        pct = 100 * r["mae_f"] / max_mae
        rows.append(
            f'<div class="lrow"><span class="lt wide">{SOURCE_NAMES.get(r["source"], r["source"])}'
            f' <em>{r["lead_bucket"]}</em></span>'
            f'<span class="lbar"><span class="lfill" style="width:{pct:.0f}%"></span></span>'
            f'<span class="lv">{r["mae_f"]:.1f}° <em>n={r["n"]}</em></span></div>')
    return (f'<div class="card"><h3>Verified skill — MAE on highs (lower is better)</h3>'
            f'{"".join(rows)}</div>')


def render_afd(view: dict) -> str:
    if not view["afd_bullets"]:
        return ""
    items = "".join(f"<li>{html.escape(b)}</li>" for b in view["afd_bullets"])
    return (f'<div class="card"><h3>NWS forecaster key messages '
            f'<span class="when">{view["afd_issued"][:16]}Z</span></h3>'
            f'<ul class="afd">{items}</ul></div>')


# ------------------------------------------------------------ template -----

TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KAUS forecast dashboard</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb;
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --series-high: #eb6834; --series-low: #2a78d6;
  --good: #006300; --bad: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19;
    --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --series-high: #d95926; --series-low: #3987e5;
    --good: #0ca30c; --bad: #e66767;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19;
  --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  --series-high: #d95926; --series-low: #3987e5;
  --good: #0ca30c; --bad: #e66767;
}
* { box-sizing: border-box; margin: 0; }
body { background: var(--page); color: var(--ink);
  font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
  padding: 24px; }
.wrap { max-width: 1060px; margin: 0 auto; display: grid; gap: 16px; }
header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 12px; }
header h1 { font-size: 20px; font-weight: 650; }
header .when { color: var(--ink-2); font-size: 13px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(215px, 1fr)); gap: 12px; }
.tile, .card { background: var(--surface); border: 1px solid var(--ring);
  border-radius: 10px; padding: 14px 16px; }
.tile-label { font-size: 12.5px; color: var(--ink-2); }
.tile-value { font-size: 34px; font-weight: 600; margin: 2px 0; }
.tile-delta { font-size: 13px; font-weight: 550; color: var(--ink-2); }
.tile-delta.good { color: var(--good); } .tile-delta.bad { color: var(--bad); }
.tile-sub { font-size: 12.5px; color: var(--muted); margin-top: 2px; }
.card h3 { font-size: 13.5px; font-weight: 600; color: var(--ink-2); margin-bottom: 10px; }
.card h3 .when { font-weight: 400; color: var(--muted); float: right; }
.legend { display: flex; gap: 16px; font-size: 12.5px; color: var(--ink-2); margin-bottom: 6px; }
.legend .key { display: inline-flex; align-items: center; gap: 6px; }
.legend .swatch { width: 14px; height: 3px; border-radius: 2px; display: inline-block; }
.tick, .ticklabel { font-size: 11px; fill: var(--muted); }
.endlabel { font-size: 12px; font-weight: 600; fill: var(--ink-2); }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 760px) { .grid2 { grid-template-columns: 1fr; } }
.lrow { display: grid; grid-template-columns: 64px 1fr 78px; gap: 10px;
  align-items: center; margin: 5px 0; font-size: 13px; }
.lrow .lt { color: var(--ink-2); font-variant-numeric: tabular-nums; }
.lrow .lt.wide { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.lrow:has(.wide) { grid-template-columns: 150px 1fr 86px; }
.lrow .lt em, .lrow .lv em { color: var(--muted); font-style: normal; font-size: 11.5px; }
.lbar { background: var(--grid); border-radius: 4px; height: 14px; overflow: hidden; }
.lfill { display: block; height: 100%; background: var(--series-low);
  border-radius: 0 4px 4px 0; }
.lv { text-align: right; font-variant-numeric: tabular-nums; color: var(--ink-2); }
table.board { width: 100%; border-collapse: collapse; font-size: 13px; }
table.board th { text-align: right; font-weight: 550; color: var(--muted);
  padding: 4px 6px; border-bottom: 1px solid var(--grid); }
table.board th:first-child { text-align: left; }
table.board td { text-align: right; padding: 4px 6px;
  font-variant-numeric: tabular-nums; border-bottom: 1px solid var(--grid); }
table.board td.sname { text-align: left; color: var(--ink-2); }
table.board td.w { color: var(--muted); }
table.board tr.blend td { font-weight: 650; border-top: 2px solid var(--axis); border-bottom: none; }
ul.afd { padding-left: 18px; display: grid; gap: 6px; font-size: 13.5px; color: var(--ink-2); }
.empty { color: var(--muted); font-size: 13px; }
footer { color: var(--muted); font-size: 12px; }
#tip { position: fixed; pointer-events: none; background: var(--surface);
  border: 1px solid var(--ring); border-radius: 8px; padding: 8px 10px;
  font-size: 12.5px; display: none; box-shadow: 0 4px 14px rgba(0,0,0,0.12); z-index: 5; }
#tip b { font-weight: 650; }
#tip .trow { display: flex; justify-content: space-between; gap: 14px; color: var(--ink-2); }
.hit:hover { fill: var(--ring); }
</style></head><body>
<div class="wrap">
<header>
  <h1>KAUS · Austin-Bergstrom forecast</h1>
  <span class="when">generated __GENERATED__ · refresh with <code>python3 forecaster.py dashboard</code></span>
</header>
<div class="tiles">__TILES__</div>
<div class="card">
  <h3>Last __PAST__ days observed → next __FUTURE__ days forecast (band = 80% interval, gray = 1991–2020 normals)</h3>
  <div class="legend">
    <span class="key"><span class="swatch" style="background:var(--series-high)"></span>High</span>
    <span class="key"><span class="swatch" style="background:var(--series-low)"></span>Low</span>
    <span class="key"><span class="swatch" style="background:var(--muted)"></span>Normal</span>
  </div>
  __CHART__
</div>
<div class="grid2">__LADDERS__</div>
<div class="grid2">__BOARD__ __SCOREBOARD__</div>
__AFD__
<footer>Sources: NWS/NOAA · NBM · MOS (IEM) · Open-Meteo (ECMWF, GFS, ICON, UKMO, GEM, JMA, Météo-France, HRRR) · GEFS+ECMWF ensembles · NCEI normals__ERRORS__</footer>
</div>
<div id="tip"></div>
<script>
const DAYS = __DAYS_JSON__;
const tip = document.getElementById('tip');
const fmt = v => v == null ? '—' : Math.round(v) + '°';
document.querySelectorAll('.hit').forEach(r => {
  r.addEventListener('mousemove', e => {
    const d = DAYS[+r.dataset.idx];
    let rows = '';
    const add = (k, v) => { if (v) rows += `<div class="trow"><span>${k}</span><b>${v}</b></div>`; };
    add('Observed', d.obs_high != null ? fmt(d.obs_high) + ' / ' + fmt(d.obs_low) : '');
    add('Forecast', d.fc_high != null ? fmt(d.fc_high) + ' / ' + fmt(d.fc_low) : '');
    add('80% band', d.hi_p10 != null ? fmt(d.hi_p10) + '–' + fmt(d.hi_p90) : '');
    add('Normal', d.normal_high != null ? fmt(d.normal_high) + ' / ' + fmt(d.normal_low) : '');
    add('Sources', d.n_sources || '');
    tip.innerHTML = `<b>${d.dow} ${d.date}</b>${rows}`;
    tip.style.display = 'block';
    const w = tip.offsetWidth, x = e.clientX + 14 + w > innerWidth ? e.clientX - w - 14 : e.clientX + 14;
    tip.style.left = x + 'px'; tip.style.top = (e.clientY + 14) + 'px';
  });
  r.addEventListener('mouseleave', () => tip.style.display = 'none');
});
</script>
</body></html>
"""


def render(bundle: dict, results: dict) -> str:
    view = build_view(bundle, results)
    n_future = len(view["fc_dates"])
    out = (TEMPLATE
           .replace("__GENERATED__", html.escape(view["generated_at"][:16].replace("T", " ")))
           .replace("__TILES__", render_tiles(view))
           .replace("__PAST__", str(PAST_DAYS))
           .replace("__FUTURE__", str(n_future))
           .replace("__CHART__", render_chart(view))
           .replace("__LADDERS__", render_ladder(view))
           .replace("__BOARD__", render_source_board(view))
           .replace("__SCOREBOARD__", render_scoreboard(view))
           .replace("__AFD__", render_afd(view))
           .replace("__ERRORS__",
                    f' · <b>failed this run: {", ".join(view["errors"])}</b>'
                    if view["errors"] else "")
           .replace("__DAYS_JSON__", json.dumps(view["days"])))
    return out


def write_dashboard(bundle: dict, results: dict) -> Path:
    OUT_PATH.write_text(render(bundle, results))
    return OUT_PATH
