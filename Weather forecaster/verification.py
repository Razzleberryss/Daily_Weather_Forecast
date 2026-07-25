"""Forecast logging and verification.

Every forecast run is logged per-source to data/forecast_log.csv. Observed
daily max/min values land in data/observed.csv. Joining the two gives each
source's real skill (MAE/bias) at this station, which analysis.py uses to
replace reputation priors with earned weights over time.
"""

from __future__ import annotations

import csv
import fcntl
import os
from contextlib import contextmanager
from datetime import date
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
FORECAST_LOG = DATA_DIR / "forecast_log.csv"
OBSERVED_LOG = DATA_DIR / "observed.csv"
LOCK_PATH = DATA_DIR / ".lock"

FORECAST_FIELDS = ["run_date", "made_at", "target_date", "lead_days", "kind", "source", "value_f"]
OBSERVED_FIELDS = ["target_date", "kind", "observed_f", "obs_source"]

# Lead-time buckets: exact-lead samples are too sparse early on, so skill is
# pooled within buckets of similar difficulty.
LEAD_BUCKETS = [(0, 1, "short"), (2, 3, "mid"), (4, 99, "long")]


def _bucket(lead_days: int) -> str:
    for lo, hi, name in LEAD_BUCKETS:
        if lo <= lead_days <= hi:
            return name
    return "long"


@contextmanager
def _locked():
    """Serialize read-modify-rewrite cycles across processes (a scheduled run
    overlapping a manual one must not lose either's rows)."""
    DATA_DIR.mkdir(exist_ok=True)
    with LOCK_PATH.open("w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    """Atomic replace: an interrupted write must never truncate the
    accumulated history the skill weights depend on."""
    DATA_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def log_forecasts(run_date: str, made_at: str, entries: list[dict]) -> int:
    """Log per-source forecast values. `entries` rows need target_date, kind,
    source, value_f. Re-running on the same day replaces that day's rows so
    the log keeps one (latest) forecast per run-day/target/source."""
    with _locked():
        existing = _read_csv(FORECAST_LOG)
        new_keys = {(run_date, e["target_date"], e["kind"], e["source"]) for e in entries}
        kept = [
            r for r in existing
            if (r["run_date"], r["target_date"], r["kind"], r["source"]) not in new_keys
        ]
        for e in entries:
            lead = (date.fromisoformat(e["target_date"]) - date.fromisoformat(run_date)).days
            kept.append({
                "run_date": run_date,
                "made_at": made_at,
                "target_date": e["target_date"],
                "lead_days": str(lead),
                "kind": e["kind"],
                "source": e["source"],
                "value_f": f"{float(e['value_f']):.1f}",
            })
        kept.sort(key=lambda r: (r["target_date"], r["run_date"], r["kind"], r["source"]))
        _write_csv(FORECAST_LOG, FORECAST_FIELDS, kept)
    return len(entries)


def record_observations(obs: list[dict]) -> int:
    """Store observed values: rows of {target_date, kind, observed_f, obs_source}.
    Existing rows for the same (target_date, kind) are replaced."""
    with _locked():
        existing = _read_csv(OBSERVED_LOG)
        new_keys = {(o["target_date"], o["kind"]) for o in obs}
        kept = [r for r in existing if (r["target_date"], r["kind"]) not in new_keys]
        for o in obs:
            kept.append({
                "target_date": o["target_date"],
                "kind": o["kind"],
                "observed_f": f"{float(o['observed_f']):.1f}",
                "obs_source": o.get("obs_source", ""),
            })
        kept.sort(key=lambda r: (r["target_date"], r["kind"]))
        _write_csv(OBSERVED_LOG, OBSERVED_FIELDS, kept)
    return len(obs)


def observed_lookup() -> dict:
    """{(target_date, kind): observed_f} — malformed rows (e.g. from a file
    damaged outside this tool) are skipped, never fatal."""
    out = {}
    for r in _read_csv(OBSERVED_LOG):
        value = _float(r.get("observed_f"))
        if value is not None and r.get("target_date") and r.get("kind"):
            out[(r["target_date"], r["kind"])] = value
    return out


def unverified_dates(before: date) -> list[str]:
    """Forecast target dates earlier than `before` that lack an observation."""
    obs = observed_lookup()
    missing = set()
    for r in _read_csv(FORECAST_LOG):
        try:
            target = date.fromisoformat(r.get("target_date") or "")
        except ValueError:
            continue
        if target < before and (r["target_date"], r["kind"]) not in obs:
            missing.add(r["target_date"])
    return sorted(missing)


def _joined_errors() -> list[dict]:
    """Forecast rows joined with observations; adds error_f and lead bucket.
    Malformed rows are skipped rather than crashing every forecast run."""
    obs = observed_lookup()
    out = []
    for r in _read_csv(FORECAST_LOG):
        key = (r.get("target_date"), r.get("kind"))
        if key not in obs:
            continue
        value = _float(r.get("value_f"))
        try:
            lead = int(r.get("lead_days") or "")
        except ValueError:
            continue
        if value is None:
            continue
        out.append({
            **r,
            "observed_f": obs[key],
            "error_f": value - obs[key],
            "bucket": _bucket(lead),
        })
    return out


def source_skill(lead_days: int) -> dict:
    """{(source, kind): {n, mae, bias}} for the matching lead bucket,
    excluding the synthetic BLEND rows."""
    bucket = _bucket(lead_days)
    groups: dict[tuple, list[float]] = {}
    for row in _joined_errors():
        if row["bucket"] != bucket or row["source"] == "BLEND":
            continue
        groups.setdefault((row["source"], row["kind"]), []).append(row["error_f"])
    return {
        key: {
            "n": len(errs),
            "mae": sum(abs(e) for e in errs) / len(errs),
            "bias": sum(errs) / len(errs),
        }
        for key, errs in groups.items()
    }


def blend_bias(kind: str, lead_days: int, min_samples: int = 8) -> float | None:
    """Mean signed error of past BLEND forecasts in this lead bucket, or None
    until there are enough samples to trust it."""
    bucket = _bucket(lead_days)
    errs = [
        row["error_f"] for row in _joined_errors()
        if row["source"] == "BLEND" and row["kind"] == kind and row["bucket"] == bucket
    ]
    if len(errs) < min_samples:
        return None
    return sum(errs) / len(errs)


def scoreboard() -> list[dict]:
    """Per-source verification summary across all leads, for display."""
    groups: dict[tuple, list[float]] = {}
    for row in _joined_errors():
        groups.setdefault((row["source"], row["kind"], row["bucket"]), []).append(row["error_f"])
    board = [
        {
            "source": src, "kind": kind, "lead_bucket": bucket,
            "n": len(errs),
            "mae_f": round(sum(abs(e) for e in errs) / len(errs), 2),
            "bias_f": round(sum(errs) / len(errs), 2),
        }
        for (src, kind, bucket), errs in groups.items()
    ]
    board.sort(key=lambda r: (r["kind"], r["lead_bucket"], r["mae_f"]))
    return board
