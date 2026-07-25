#!/usr/bin/env python3
"""KAUS daily temperature forecaster.

Gathers forecasts and observations from every reputable free source with an
API — NWS official forecast, NBM (gridded + station guidance), GFS MOS,
ECMWF/GFS/ICON/UKMO/GEM/JMA/Météo-France/HRRR via Open-Meteo, GEFS + ECMWF
ensemble members, live ASOS/METAR observations, NCEI climate normals, and the
NWS forecast discussion — then blends them into a calibrated forecast with
uncertainty, logs every run, and scores each source against what actually
happened so the weighting improves with use.

Commands:
    forecast    gather everything, analyze, print forecast (logs the run)
    gather      fetch all sources, save data/bundle_latest.json
    verify      score past forecasts against observed highs/lows
    scoreboard  per-source verified skill (MAE/bias)
    obs         current conditions + today so far at the airport
    afd         latest NWS Austin/San Antonio forecast discussion
    log-agent   record an analyst-adjusted forecast for skill tracking
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta

import analysis
import sources
import verification
from wxutil import now_local, today_local

BUNDLE_PATH = sources.DATA_DIR / "bundle_latest.json"

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _save_bundle(bundle: dict) -> None:
    sources.DATA_DIR.mkdir(exist_ok=True)
    tmp = BUNDLE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(bundle, indent=1))
    os.replace(tmp, BUNDLE_PATH)


def _load_or_gather(args) -> dict:
    if getattr(args, "cached", False) and BUNDLE_PATH.exists():
        try:
            bundle = json.loads(BUNDLE_PATH.read_text())
            if bundle.get("run_date") == today_local().isoformat():
                return bundle
            print("cached bundle is stale; refetching", file=sys.stderr)
        except (json.JSONDecodeError, OSError) as e:
            print(f"cached bundle unreadable ({e}); refetching", file=sys.stderr)
    bundle = sources.gather(days=args.days)
    _save_bundle(bundle)
    return bundle


def _pick_targets(args, bundle: dict) -> list[date]:
    if args.date:
        try:
            return [date.fromisoformat(args.date)]
        except ValueError:
            print(f"--date must be YYYY-MM-DD, got {args.date!r}", file=sys.stderr)
            sys.exit(2)
    start = today_local()
    # After 6pm local the day's high is in the books — start at tomorrow
    if now_local().hour >= 18:
        start = start + timedelta(days=1)
    available = {date.fromisoformat(d) for d in bundle["per_date"]}
    return sorted(d for d in available if d >= start)[: args.days]


def _fmt_day(d: date) -> str:
    return f"{DAY_NAMES[d.weekday()]} {d.isoformat()}"


def _print_forecast(bundle: dict, results: dict, verbose: bool) -> None:
    climo = bundle["context"].get("climatology_normals", {})
    obs = bundle["context"].get("obs", {})
    print(f"\nKAUS — Austin-Bergstrom Intl Airport")
    print(f"run: {bundle['generated_at']}   "
          f"sources failed: {', '.join(bundle['errors']) or 'none'}")
    if obs.get("latest", {}).get("temp_f") is not None:
        line = f"now: {obs['latest']['temp_f']}°F ({obs['latest']['time_local'][11:16]} local)"
        h24 = obs.get("last_24h")
        if h24:
            line += f"   last 24h: {h24['max_f']}/{h24['min_f']}°F"
        tsf = obs.get("today_so_far")
        if tsf:
            line += f"   {tsf['date']} so far: {tsf['max_f']}/{tsf['min_f']}°F"
        print(line)

    for date_iso in sorted(results):
        row = results[date_iso]
        d = date.fromisoformat(date_iso)
        normals = climo.get(date_iso, {})
        print(f"\n{_fmt_day(d)}  (lead {row.get('high', row.get('low'))['lead_days']}d)")
        for kind in ("high", "low"):
            r = row.get(kind)
            if not r:
                continue
            p = r["percentiles_f"]
            anomaly = ""
            normal = normals.get(f"normal_{kind}_f")
            if normal is not None:
                diff = r["point_f"] - normal
                anomaly = f"  ({diff:+.1f} vs normal {normal:.0f})"
            print(f"  {kind.upper():4} {r['point_f']:6.1f}°F   "
                  f"80% CI [{p['p10']:.1f}, {p['p90']:.1f}]   "
                  f"σ {r['sigma_f']:.1f}   "
                  f"{r['n_sources']} sources"
                  + (f", {r['n_ensemble_members']} ens members" if r["n_ensemble_members"] else "")
                  + anomaly)
            if verbose:
                parts = [f"{s} {info['value_f']:.0f} (w{info['weight']:.2f})"
                         for s, info in r["sources"].items()]
                print(f"       {'; '.join(parts)}")
                if kind == "high":
                    probs = r["prob_at_least_f"]
                    center = int(round(r["point_f"]))
                    ladder = [f"≥{t}: {probs[t]*100:.0f}%"
                              for t in range(center - 2, center + 3) if t in probs]
                    print(f"       P(high {'  '.join(ladder)})")
        if row.get("high", {}).get("spread", {}).get("max_f") is not None and verbose:
            s = row["high"]["spread"]
            print(f"       high spread: {s['min_f']:.0f}–{s['max_f']:.0f}°F "
                  f"(inter-source σ {s['inter_source_sigma_f']}, "
                  f"ens σ {s['ensemble_member_sigma_f']}, "
                  f"NBM σ {s['nbm_published_sigma_f']})")
    print()


def _log_run(bundle: dict, results: dict) -> None:
    entries = []
    for date_iso, row in results.items():
        for kind, r in row.items():
            for source, info in r["sources"].items():
                entries.append({"target_date": date_iso, "kind": kind,
                                "source": source, "value_f": info["value_f"]})
            entries.append({"target_date": date_iso, "kind": kind,
                            "source": "BLEND", "value_f": r["point_f"]})
    if entries:
        verification.log_forecasts(bundle["run_date"], bundle["generated_at"], entries)


def _refresh_dashboard(bundle: dict) -> None:
    """Render dashboard.html over the bundle's FULL forecast horizon, so the
    page looks the same no matter which command triggered the refresh."""
    try:
        import dashboard
        targets = sorted(date.fromisoformat(d) for d in bundle["per_date"])
        results = analysis.analyze(bundle, targets)
        if results:
            dashboard.write_dashboard(bundle, results)
    except Exception as e:  # noqa: BLE001 — dashboard is a bonus, never fatal
        print(f"dashboard refresh failed: {e}", file=sys.stderr)


def cmd_forecast(args) -> None:
    bundle = _load_or_gather(args)
    targets = _pick_targets(args, bundle)
    results = analysis.analyze(bundle, targets)
    if not results:
        print("No forecast data available for the requested dates.", file=sys.stderr)
        sys.exit(1)
    if not args.no_log:
        _log_run(bundle, results)
    _refresh_dashboard(bundle)
    if args.json:
        print(json.dumps({
            "generated_at": bundle["generated_at"],
            "station": bundle["station"],
            "forecasts": results,
            "errors": bundle["errors"],
        }, indent=1))
    else:
        _print_forecast(bundle, results, verbose=not args.brief)


def cmd_gather(args) -> None:
    bundle = sources.gather(days=args.days)
    _save_bundle(bundle)
    n_vals = sum(len(k["high"]) + len(k["low"]) for k in bundle["per_date"].values())
    print(f"bundle saved to {BUNDLE_PATH}")
    print(f"{len(bundle['per_date'])} dates, {n_vals} forecast values, "
          f"errors: {bundle['errors'] or 'none'}")


def cmd_verify(args) -> None:
    missing = verification.unverified_dates(before=today_local())
    if missing:
        lo = date.fromisoformat(missing[0])
        hi = date.fromisoformat(missing[-1])
        observed = sources.fetch_observed_range(lo, hi)
        rows = []
        for date_iso in missing:
            day = observed.get(date_iso)
            if not day:
                continue
            if day.get("max_f") is not None:
                rows.append({"target_date": date_iso, "kind": "high",
                             "observed_f": day["max_f"], "obs_source": "IEM_ASOS"})
            if day.get("min_f") is not None:
                rows.append({"target_date": date_iso, "kind": "low",
                             "observed_f": day["min_f"], "obs_source": "IEM_ASOS"})
        if rows:
            verification.record_observations(rows)
            print(f"recorded observations for: "
                  f"{', '.join(sorted({r['target_date'] for r in rows}))}")
        still = [d for d in missing if d not in observed]
        if still:
            print(f"not yet available: {', '.join(still)}")
    else:
        print("all past forecasts already verified")
    _print_scoreboard()
    # Keep the dashboard's scoreboard current when today's bundle is on disk
    if BUNDLE_PATH.exists():
        try:
            bundle = json.loads(BUNDLE_PATH.read_text())
            if bundle.get("run_date") == today_local().isoformat():
                _refresh_dashboard(bundle)
        except (json.JSONDecodeError, OSError):
            pass


def _print_scoreboard() -> None:
    board = verification.scoreboard()
    if not board:
        print("no verified forecasts yet — run `forecast` today and `verify` tomorrow")
        return
    print(f"\n{'source':<14} {'kind':<5} {'lead':<6} {'n':>3} {'MAE':>6} {'bias':>6}")
    for r in board:
        print(f"{r['source']:<14} {r['kind']:<5} {r['lead_bucket']:<6} "
              f"{r['n']:>3} {r['mae_f']:>6.2f} {r['bias_f']:>+6.2f}")


def cmd_scoreboard(args) -> None:
    _print_scoreboard()


def cmd_obs(args) -> None:
    print(json.dumps(sources.fetch_observations(), indent=1))


def cmd_afd(args) -> None:
    afd = sources.fetch_afd()
    print(f"issued: {afd['issued_utc']}\n")
    print(afd["text"])


def cmd_dashboard(args) -> None:
    bundle = _load_or_gather(args)
    import dashboard
    _refresh_dashboard(bundle)
    print(f"dashboard written to {dashboard.OUT_PATH}")


def cmd_log_agent(args) -> None:
    try:
        target = date.fromisoformat(args.date)
    except ValueError:
        print(f"--date must be YYYY-MM-DD, got {args.date!r}", file=sys.stderr)
        sys.exit(2)
    if target < today_local():
        print(f"{args.date} is in the past — refusing to log a hindsight "
              f"forecast (it would score as perfect skill)", file=sys.stderr)
        sys.exit(2)
    entries = []
    if args.high is not None:
        entries.append({"target_date": args.date, "kind": "high",
                        "source": "AGENT", "value_f": args.high})
    if args.low is not None:
        entries.append({"target_date": args.date, "kind": "low",
                        "source": "AGENT", "value_f": args.low})
    if not entries:
        print("provide --high and/or --low", file=sys.stderr)
        sys.exit(1)
    verification.log_forecasts(today_local().isoformat(), now_local().isoformat(), entries)
    print(f"logged AGENT forecast for {args.date}: "
          + ", ".join(f"{e['kind']}={e['value_f']}" for e in entries))


def _positive_days(value: str) -> int:
    days = int(value)
    if days < 1:
        raise argparse.ArgumentTypeError("--days must be >= 1")
    return days


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("forecast", help="gather, analyze, and print the forecast")
    p.add_argument("--days", type=_positive_days, default=7,
                   help="days ahead to forecast (default 7)")
    p.add_argument("--date", help="forecast a single date (YYYY-MM-DD)")
    p.add_argument("--json", action="store_true", help="full JSON output")
    p.add_argument("--brief", action="store_true", help="hide per-source detail")
    p.add_argument("--cached", action="store_true", help="reuse today's saved bundle")
    p.add_argument("--no-log", action="store_true", help="don't log this run")
    p.set_defaults(fn=cmd_forecast)

    p = sub.add_parser("gather", help="fetch all sources, save the bundle")
    p.add_argument("--days", type=_positive_days, default=7)
    p.set_defaults(fn=cmd_gather)

    p = sub.add_parser("verify", help="score past forecasts against observations")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("dashboard", help="render dashboard.html from the latest data")
    p.add_argument("--days", type=_positive_days, default=7)
    p.add_argument("--date", help=argparse.SUPPRESS)
    p.add_argument("--cached", action="store_true", default=True,
                   help="reuse today's saved bundle (default)")
    p.add_argument("--fresh", dest="cached", action="store_false",
                   help="force a full refetch")
    p.set_defaults(fn=cmd_dashboard, date=None)

    p = sub.add_parser("scoreboard", help="per-source verified skill")
    p.set_defaults(fn=cmd_scoreboard)

    p = sub.add_parser("obs", help="current airport observations")
    p.set_defaults(fn=cmd_obs)

    p = sub.add_parser("afd", help="latest NWS forecast discussion")
    p.set_defaults(fn=cmd_afd)

    p = sub.add_parser("log-agent", help="log an analyst-adjusted forecast")
    p.add_argument("--date", required=True, help="target date YYYY-MM-DD")
    p.add_argument("--high", type=float)
    p.add_argument("--low", type=float)
    p.set_defaults(fn=cmd_log_agent)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
