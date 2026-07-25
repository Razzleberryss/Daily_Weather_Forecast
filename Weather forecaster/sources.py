"""Data fetchers for every source feeding the KAUS forecast.

Each fetcher returns normalized data; gather() runs them all, tolerating
individual failures, and produces one bundle dict:

    per_date[date_iso]["high"|"low"][source_label] = value_f
    ensemble_members[date_iso]["high"|"low"][model] = [member values...]
    context = obs / climatology / recent observed days / AFD text / errors

Endpoint contracts below were validated live on 2026-07-20.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from wxutil import (
    LOCAL_TZ, SourceError, build_url, c_to_f, daterange, http_get_json,
    now_local, parse_iso, parse_iso_interval, to_local, today_local,
)

STATION = "KAUS"
IEM_STATION = "AUS"          # IEM drops the K prefix
LAT, LON = 30.1831, -97.6799
NWS_GRID = "EWX/158,87"
NWS_OFFICE = "EWX"
GHCND_ID = "USW00013904"     # NCEI id for Austin-Bergstrom

DATA_DIR = Path(__file__).resolve().parent / "data"

# Open-Meteo model ids -> our source labels. best_match is excluded: it
# resolves to GFS at this location and would double-count.
OPEN_METEO_MODELS = {
    "ecmwf_ifs025": "om_ecmwf",
    "gfs_seamless": "om_gfs",
    "icon_seamless": "om_icon",
    "gem_seamless": "om_gem",
    "meteofrance_seamless": "om_meteofrance",
    "ukmo_seamless": "om_ukmo",
    "jma_seamless": "om_jma",
    "ncep_nbm_conus": "om_nbm",
    "ncep_hrrr_conus": "om_hrrr",
}


def _set(per_date: dict, date_iso: str, kind: str, label: str, value: float | None):
    if value is None:
        return
    per_date.setdefault(date_iso, {"high": {}, "low": {}})[kind][label] = round(float(value), 1)


# ---------------------------------------------------------------- NWS ------

def fetch_nws_gridpoint(per_date: dict) -> None:
    """Official NWS forecast grid (forecaster-edited, NBM-seeded). Values are
    °C; validTime is 'ISO-start/ISO-duration'. Interval midpoint's local date
    identifies the calendar day for both max (daytime) and min (overnight)."""
    data = http_get_json(f"https://api.weather.gov/gridpoints/{NWS_GRID}")
    props = data["properties"]
    for field, kind in (("maxTemperature", "high"), ("minTemperature", "low")):
        for entry in props.get(field, {}).get("values", []):
            if entry.get("value") is None:
                continue
            start, end = parse_iso_interval(entry["validTime"])
            midpoint = start + (end - start) / 2
            local_day = to_local(midpoint).date()
            _set(per_date, local_day.isoformat(), kind, "nws_official", c_to_f(entry["value"]))


def fetch_nws_periods() -> list[dict]:
    """Public forecast periods — used as context (shortForecast, PoP) and as
    a fallback check on the gridpoint numbers. Temps already °F."""
    data = http_get_json(f"https://api.weather.gov/gridpoints/{NWS_GRID}/forecast")
    periods = []
    for p in data["properties"]["periods"]:
        periods.append({
            "name": p["name"],
            "start": p["startTime"],
            "is_daytime": p["isDaytime"],
            "temperature_f": p["temperature"],
            "pop_percent": (p.get("probabilityOfPrecipitation") or {}).get("value"),
            "short_forecast": p.get("shortForecast", ""),
        })
    return periods


def apply_nws_periods_fallback(per_date: dict, periods: list[dict]) -> None:
    """Fill any nws_official gaps from the period forecast. Temperatures can
    come back null (or as a QuantitativeValue dict) during NWS grid outages —
    skip those periods rather than poisoning the run."""
    for p in periods:
        if not isinstance(p.get("temperature_f"), (int, float)):
            continue
        local_start = to_local(parse_iso(p["start"]))
        if p["is_daytime"]:
            day, kind = local_start.date(), "high"
        else:
            # Night period belongs to the day its morning ends on
            day, kind = (local_start + timedelta(hours=12)).date(), "low"
        cell = per_date.setdefault(day.isoformat(), {"high": {}, "low": {}})[kind]
        cell.setdefault("nws_official", float(p["temperature_f"]))


# --------------------------------------------------------- Open-Meteo ------

def fetch_open_meteo(per_date: dict) -> list[str]:
    """Daily max/min from 9 independent models in one call. With multiple
    models the keys are suffixed: daily.temperature_2m_max_<model_id>."""
    url = build_url("https://api.open-meteo.com/v1/forecast", {
        "latitude": LAT, "longitude": LON,
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit",
        "timezone": "America/Chicago",
        "forecast_days": 8,
        "models": ",".join(OPEN_METEO_MODELS),
    })
    daily = http_get_json(url)["daily"]
    dates = daily["time"]
    found = []
    for model_id, label in OPEN_METEO_MODELS.items():
        highs = daily.get(f"temperature_2m_max_{model_id}")
        lows = daily.get(f"temperature_2m_min_{model_id}")
        if not highs:
            continue
        found.append(label)
        for i, d in enumerate(dates):
            _set(per_date, d, "high", label, highs[i])
            if lows:
                _set(per_date, d, "low", label, lows[i])
    return found


def fetch_open_meteo_ensemble(ensemble_members: dict) -> dict:
    """GEFS (31 members) + ECMWF ENS (51 members) daily max/min. Used only
    for uncertainty spread — the coarse ensemble grid (30.25,-97.75) runs
    warm vs the station, so member means are not blended as point values.
    Response renames models: gfs025 -> ncep_gefs025, ecmwf_ifs025 ->
    ecmwf_ifs025_ensemble; members are *_memberNN_<model> keys."""
    url = build_url("https://ensemble-api.open-meteo.com/v1/ensemble", {
        "latitude": LAT, "longitude": LON,
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit",
        "timezone": "America/Chicago",
        "forecast_days": 7,
        "models": "gfs025,ecmwf_ifs025",
    })
    daily = http_get_json(url)["daily"]
    dates = daily["time"]
    key_re = re.compile(r"temperature_2m_(max|min)(?:_member(\d+))?_(.+)")
    summary: dict = {}
    for key, series in daily.items():
        m = key_re.fullmatch(key)
        if not m or series is None:
            continue
        kind = "high" if m.group(1) == "max" else "low"
        model = {"ncep_gefs025": "gefs", "ecmwf_ifs025_ensemble": "ecmwf_ens"}.get(m.group(3), m.group(3))
        for i, d in enumerate(dates):
            if series[i] is None:
                continue
            bucket = (ensemble_members.setdefault(d, {"high": {}, "low": {}})[kind]
                      .setdefault(model, []))
            bucket.append(round(float(series[i]), 1))
    for d, kinds in ensemble_members.items():
        summary[d] = {k: {mdl: len(v) for mdl, v in models.items()}
                      for k, models in kinds.items()}
    return summary


# ------------------------------------------------------- Observations ------

def fetch_observations() -> dict:
    """Live station picture: latest temp, rolling 24-hour max/min, the
    running max/min of the current climate day (midnight-to-now local,
    labeled with its date so a midnight crossing can't misattribute it),
    and the synoptic 6-h max groups from METARs."""
    out: dict = {}
    latest = http_get_json(f"https://api.weather.gov/stations/{STATION}/observations/latest")
    lp = latest["properties"]
    out["latest"] = {
        "time_local": to_local(parse_iso(lp["timestamp"])).isoformat(),
        "temp_f": round(c_to_f(lp["temperature"]["value"]), 1)
        if lp["temperature"]["value"] is not None else None,
    }

    window_end = now_local()
    window_start = window_end - timedelta(hours=24)
    url = build_url(f"https://api.weather.gov/stations/{STATION}/observations", {
        "start": window_start.isoformat(),
        "end": window_end.isoformat(),
        "limit": 500,
    })
    series = http_get_json(url)
    readings = [
        (to_local(parse_iso(f["properties"]["timestamp"])),
         c_to_f(f["properties"]["temperature"]["value"]))
        for f in series.get("features", [])
        if f["properties"]["temperature"]["value"] is not None
    ]
    if readings:
        temps = [t for _, t in readings]
        out["last_24h"] = {
            "from_local": window_start.isoformat(),
            "max_f": round(max(temps), 1),
            "min_f": round(min(temps), 1),
            "n_obs": len(temps),
        }
        today = window_end.date()
        today_temps = [t for ts, t in readings if ts.date() == today]
        if today_temps:
            out["today_so_far"] = {
                "date": today.isoformat(),
                "max_f": round(max(today_temps), 1),
                "min_f": round(min(today_temps), 1),
                "n_obs": len(today_temps),
            }

    # METARs are a separate service — its failure must not discard the NWS
    # obs already gathered above. It has also been seen returning error dicts
    # with HTTP 200, so the shape is checked, not assumed.
    try:
        metars = http_get_json(
            f"https://aviationweather.gov/api/data/metar?ids={STATION}&format=json&hours=24"
        )
        if not isinstance(metars, list):
            raise SourceError(f"unexpected METAR payload type: {type(metars).__name__}")
        metars = [m for m in metars if isinstance(m, dict)]
        synoptic = []
        for m in metars:
            if m.get("maxT") is not None or m.get("minT") is not None:
                synoptic.append({
                    "report_time": m.get("reportTime"),
                    "max_6h_f": round(c_to_f(m["maxT"]), 1) if m.get("maxT") is not None else None,
                    "min_6h_f": round(c_to_f(m["minT"]), 1) if m.get("minT") is not None else None,
                })
        out["metar_6h_extremes"] = synoptic
        out["recent_metars"] = [
            {"time": m.get("reportTime"), "temp_f": round(c_to_f(m["temp"]), 1)}
            for m in metars[:8] if m.get("temp") is not None
        ]
    except Exception as e:  # noqa: BLE001 — partial obs beat no obs
        out["metar_error"] = str(e)
    return out


# ---------------------------------------------- Verification / climate -----

def fetch_iem_daily(year: int, month: int) -> list[dict]:
    """Observed daily max/min from the ASOS daily summaries. Today's row is
    provisional; rows for future days have null temps (filtered out)."""
    url = build_url("https://mesonet.agron.iastate.edu/api/1/daily.json", {
        "station": IEM_STATION, "network": "TX_ASOS", "year": year, "month": month,
    })
    rows = http_get_json(url)["data"]
    return [
        {"date": r["date"], "max_f": r["max_tmpf"], "min_f": r["min_tmpf"],
         "estimated": bool(r.get("tmpf_est"))}
        for r in rows if r.get("max_tmpf") is not None
    ]


def fetch_observed_range(start: date, end: date) -> dict:
    """{date_iso: {'max_f':…, 'min_f':…}} for start..end inclusive, spanning
    months as needed. Excludes today (provisional until the day closes)."""
    out = {}
    months = set()
    d = start
    while d <= end:
        months.add((d.year, d.month))
        d += timedelta(days=1)
    for year, month in sorted(months):
        for row in fetch_iem_daily(year, month):
            row_date = date.fromisoformat(row["date"])
            if start <= row_date <= end and row_date < today_local():
                out[row["date"]] = {"max_f": row["max_f"], "min_f": row["min_f"]}
    return out


def fetch_normals(targets: list[date]) -> dict:
    """1991-2020 NCEI daily normals; static, so cached to disk after the
    first fetch. NCEI's dataset convention uses placeholder year 2010."""
    cache_path = DATA_DIR / "normals_cache.json"
    cache = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())
    missing = [t for t in targets if t.strftime("%m-%d") not in cache]
    if missing:
        url = build_url("https://www.ncei.noaa.gov/access/services/data/v1", {
            "dataset": "normals-daily",
            "stations": GHCND_ID,
            "dataTypes": "DLY-TMAX-NORMAL,DLY-TMIN-NORMAL",
            "startDate": "2010-01-01",
            "endDate": "2010-12-31",
            "format": "json",
            "units": "standard",
        })
        for row in http_get_json(url):
            cache[row["DATE"]] = {
                "normal_high_f": float(row["DLY-TMAX-NORMAL"]),
                "normal_low_f": float(row["DLY-TMIN-NORMAL"]),
            }
        DATA_DIR.mkdir(exist_ok=True)
        cache_path.write_text(json.dumps(cache))
    return {t.isoformat(): cache.get(t.strftime("%m-%d"), {}) for t in targets}


# ----------------------------------------------------------------- AFD -----

def fetch_afd() -> dict:
    """Latest Area Forecast Discussion from the Austin/San Antonio office —
    the human forecasters' reasoning about the synoptic setup."""
    data = http_get_json(
        f"https://api.weather.gov/products/types/AFD/locations/{NWS_OFFICE}/latest"
    )
    return {
        "issued_utc": data["issuanceTime"],
        "text": data["productText"],
    }


# -------------------------------------------------------------- gather -----

def gather(days: int = 7) -> dict:
    """Fetch everything concurrently; individual source failures land in
    errors instead of killing the run. Fetchers write disjoint source labels,
    so sharing per_date across threads is safe; the NWS periods fallback runs
    after the gridpoint fetch inside the same thread because it must not win
    over gridpoint values."""
    run_dt = now_local()
    targets = daterange(today_local(), days + 1)
    per_date: dict = {}
    ensemble_members: dict = {}
    nbm_sd: dict = {}
    context: dict = {}
    errors: dict = {}

    def attempt(name, fn, *args):
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001 — a dead source must not kill the run
            errors[name] = str(e)
            return None

    def nws_chain():
        attempt("nws_gridpoint", fetch_nws_gridpoint, per_date)
        periods = attempt("nws_periods", fetch_nws_periods)
        if periods:
            attempt("nws_periods_fallback", apply_nws_periods_fallback, per_date, periods)
            context["nws_periods"] = periods

    jobs = {
        "nws": nws_chain,
        "open_meteo": lambda: attempt("open_meteo", fetch_open_meteo, per_date),
        "ensemble": lambda: attempt("open_meteo_ensemble",
                                    fetch_open_meteo_ensemble, ensemble_members),
        "mos": lambda: attempt("mos", fetch_mos, per_date, nbm_sd),
        "obs": lambda: attempt("observations", fetch_observations),
        "normals": lambda: attempt("normals", fetch_normals, targets),
        "recent": lambda: attempt("recent_observed", fetch_observed_range,
                                  today_local() - timedelta(days=14), today_local()),
        "afd": lambda: attempt("afd", fetch_afd),
    }
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {name: pool.submit(fn) for name, fn in jobs.items()}
        results = {name: f.result() for name, f in futures.items()}

    if results["ensemble"]:
        context["ensemble_member_counts"] = results["ensemble"]
    if results["mos"]:
        context["mos_runs"] = results["mos"]
    if results["obs"]:
        context["obs"] = results["obs"]
    if results["normals"]:
        context["climatology_normals"] = results["normals"]
    if results["recent"]:
        context["recent_observed_days"] = results["recent"]
    if results["afd"]:
        context["afd"] = results["afd"]

    target_isos = {t.isoformat() for t in targets}
    per_date = {d: v for d, v in per_date.items() if d in target_isos}
    ensemble_members = {d: v for d, v in ensemble_members.items() if d in target_isos}
    nbm_sd = {d: v for d, v in nbm_sd.items() if d in target_isos}

    return {
        "generated_at": run_dt.isoformat(),
        "run_date": run_dt.date().isoformat(),
        "station": STATION,
        "latitude": LAT,
        "longitude": LON,
        "target_dates": [t.isoformat() for t in targets],
        "per_date": per_date,
        "ensemble_members": ensemble_members,
        "nbm_sd": nbm_sd,
        "context": context,
        "errors": errors,
    }


# ----------------------------------------------------------------- MOS -----

# (model id, label, value field). GFS/MEX carry max/min in n_x; the NBM
# bulletins leave n_x null and use txn instead (with xnd = its std dev).
MOS_MODELS = [
    ("NBS", "mos_nbs", "txn"),   # NBM short-range station guidance
    ("NBE", "mos_nbe", "txn"),   # NBM extended — fills days beyond NBS only
    ("GFS", "mos_mav", "n_x"),   # GFS MOS short-range (MAV)
    ("MEX", "mos_mex", "n_x"),   # GFS MOS extended — fills beyond MAV only
]


def fetch_mos(per_date: dict, nbm_sd: dict) -> dict:
    """Station statistical guidance via the IEM MOS API (latest run of each
    bulletin). X/N convention: a value stamped at 00Z is the daytime MAX and
    at 12Z the morning MIN — both belong to the ftime's *local* calendar
    date (00Z = 6-7pm local the same evening). NBS/NBE and MAV/MEX pairs
    overlap in range, so the extended bulletin only fills dates its
    short-range sibling doesn't cover (avoids double-counting one model).
    Any bulletin can 404 in the gap after a new cycle — skipped quietly."""
    runs = {}
    for model, label, field in MOS_MODELS:
        try:
            data = http_get_json(
                f"https://mesonet.agron.iastate.edu/api/1/mos.json?station={STATION}&model={model}"
            )
        except SourceError:
            continue
        rows = data.get("data", [])
        if not rows:
            continue
        runs[label] = rows[0].get("runtime_utc")
        short_label = {"mos_nbe": "mos_nbs", "mos_mex": "mos_mav"}.get(label)
        for row in rows:
            value = row.get(field)
            if value is None:
                continue
            ftime = parse_iso(row["ftime_utc"] + "+00:00"
                              if not row["ftime_utc"].endswith(("Z", "+00:00"))
                              else row["ftime_utc"])
            local_dt = to_local(ftime)
            if ftime.hour == 0:
                kind = "high"
            elif ftime.hour == 12:
                kind = "low"
            else:
                continue
            date_iso = local_dt.date().isoformat()
            cell = per_date.setdefault(date_iso, {"high": {}, "low": {}})[kind]
            # Extended bulletins defer to their short-range sibling
            if short_label and short_label in cell:
                continue
            cell[label] = round(float(value), 1)
            sd = row.get("xnd")
            if sd is not None and field == "txn":
                slot = nbm_sd.setdefault(date_iso, {})
                slot[kind] = max(float(sd), slot.get(kind, 0.0))
    return runs
