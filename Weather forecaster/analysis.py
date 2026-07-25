"""Ensemble analysis: blend per-source temperature forecasts into a single
calibrated forecast with uncertainty.

Inputs come from the normalized bundle produced by `sources.gather()`:
    bundle["per_date"][date_iso]["high"|"low"] = {source_label: value_f}
    bundle["ensemble_members"][date_iso]["high"|"low"] = [member values...]

Weighting starts from reputation-based priors and automatically shifts to
skill-based weights (inverse MAE) once the verification log has enough
history for a source at a given lead time.
"""

from __future__ import annotations

import math
from datetime import date

import verification

# Reputation priors: relative weight of each source before any local skill
# history exists. NBM and ECMWF lead; global models trail; `best_match` is
# excluded upstream because it duplicates models already counted.
PRIOR_WEIGHTS = {
    "mos_nbs": 3.0,         # NBM station guidance, short range
    "om_nbm": 3.0,          # NBM gridded (via Open-Meteo)
    "mos_nbe": 2.5,         # NBM station guidance, extended (fills past NBS)
    "nws_official": 2.5,    # NWS forecaster-adjusted public forecast
    "om_ecmwf": 2.5,        # ECMWF IFS via Open-Meteo
    "mos_mav": 2.0,         # GFS MOS short range
    "mos_mex": 1.5,         # GFS MOS extended (fills past MAV)
    "om_gfs": 1.5,
    "om_icon": 1.5,
    "om_ukmo": 1.5,
    "om_hrrr": 1.5,         # only covers ~2 days; missing beyond that
    "om_gem": 1.0,
    "om_jma": 1.0,
    "om_meteofrance": 1.0,
}
DEFAULT_PRIOR = 1.0

# Uncertainty floor (deg F, 1-sigma) by forecast lead in days. Even a perfect
# consensus can't beat typical station-level error at these leads.
SIGMA_FLOOR_BY_LEAD = [1.6, 1.9, 2.3, 2.7, 3.1, 3.6, 4.1, 4.6]

MIN_SKILL_SAMPLES = 10   # verified forecasts needed before skill weighting kicks in
MIN_BIAS_SAMPLES = 8     # verified blends needed before bias correction kicks in
BIAS_SHRINKAGE = 0.7     # apply only this fraction of measured bias


def _sigma_floor(lead_days: int) -> float:
    lead = max(0, lead_days)
    if lead < len(SIGMA_FLOOR_BY_LEAD):
        return SIGMA_FLOOR_BY_LEAD[lead]
    return SIGMA_FLOOR_BY_LEAD[-1] + 0.4 * (lead - len(SIGMA_FLOOR_BY_LEAD) + 1)


def _weight_for(source: str, kind: str, lead_days: int, skill: dict) -> float:
    """Blend the reputation prior with measured skill (inverse MAE)."""
    prior = PRIOR_WEIGHTS.get(source, DEFAULT_PRIOR)
    stats = skill.get((source, kind))
    if not stats or stats["n"] < MIN_SKILL_SAMPLES:
        return prior
    inv_mae = 1.0 / (stats["mae"] + 0.5)
    # Scale inverse-MAE into the same rough range as priors (median prior ~2)
    skill_weight = inv_mae * 4.0
    # Confidence in the skill estimate grows with sample count
    alpha = min(1.0, stats["n"] / 40.0)
    return (1 - alpha) * prior + alpha * skill_weight


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def analyze_date(bundle: dict, target: date, kind: str = "high") -> dict | None:
    """Produce the blended forecast for one date and one kind ('high'/'low')."""
    date_iso = target.isoformat()
    values = bundle.get("per_date", {}).get(date_iso, {}).get(kind, {})
    values = {s: v for s, v in values.items() if v is not None}
    if not values:
        return None

    run_date = date.fromisoformat(bundle["run_date"])
    lead_days = (target - run_date).days
    skill = verification.source_skill(lead_days)

    weighted = [(s, v, _weight_for(s, kind, lead_days, skill)) for s, v in values.items()]
    total_w = sum(w for _, _, w in weighted)
    blend = sum(v * w for _, v, w in weighted) / total_w

    # Bias correction from local verification history of past blends
    bias = verification.blend_bias(kind, lead_days)
    corrected = blend
    if bias is not None:
        corrected = blend - BIAS_SHRINKAGE * bias

    # Spread comes from three independent signals, take the widest:
    #  - disagreement between the point sources
    #  - within-model ensemble member spread (per model, then averaged, so a
    #    model's mean bias doesn't inflate it)
    #  - the NBM's own published std dev for this station/day
    vals = [v for _, v, _ in weighted]
    if len(vals) > 1:
        mean = sum(vals) / len(vals)
        inter_sigma = math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1))
    else:
        inter_sigma = 0.0
    member_models = bundle.get("ensemble_members", {}).get(date_iso, {}).get(kind) or {}
    model_sigmas, n_members = [], 0
    for members in member_models.values():
        n_members += len(members)
        if len(members) > 2:
            m_mean = sum(members) / len(members)
            model_sigmas.append(
                math.sqrt(sum((v - m_mean) ** 2 for v in members) / (len(members) - 1))
            )
    member_sigma = sum(model_sigmas) / len(model_sigmas) if model_sigmas else 0.0
    nbm_sigma = bundle.get("nbm_sd", {}).get(date_iso, {}).get(kind, 0.0)
    sigma = max(inter_sigma, member_sigma, nbm_sigma, _sigma_floor(lead_days))

    z = {"p10": -1.2816, "p25": -0.6745, "p50": 0.0, "p75": 0.6745, "p90": 1.2816}
    percentiles = {k: round(corrected + zz * sigma, 1) for k, zz in z.items()}

    # Exceedance probabilities for whole degrees around the point forecast
    lo, hi = int(math.floor(corrected - 2 * sigma)), int(math.ceil(corrected + 2 * sigma))
    prob_at_least = {
        t: round(1.0 - _normal_cdf(t - 0.5, corrected, sigma), 3)
        for t in range(lo, hi + 1)
    }

    sorted_vals = sorted(vals)
    n = len(sorted_vals)
    median = (sorted_vals[n // 2] if n % 2 else
              (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2)

    return {
        "date": date_iso,
        "kind": kind,
        "lead_days": lead_days,
        "point_f": round(corrected, 1),
        "raw_blend_f": round(blend, 1),
        "bias_correction_f": round(-BIAS_SHRINKAGE * bias, 2) if bias is not None else 0.0,
        "median_of_sources_f": round(median, 1),
        "sigma_f": round(sigma, 2),
        "percentiles_f": percentiles,
        "prob_at_least_f": prob_at_least,
        "n_sources": len(weighted),
        "n_ensemble_members": n_members,
        "sources": {
            s: {"value_f": round(v, 1), "weight": round(w / total_w, 3)}
            for s, v, w in sorted(weighted, key=lambda t: -t[2])
        },
        "spread": {
            "min_f": round(min(vals), 1),
            "max_f": round(max(vals), 1),
            "inter_source_sigma_f": round(inter_sigma, 2),
            "ensemble_member_sigma_f": round(member_sigma, 2),
            "nbm_published_sigma_f": round(nbm_sigma, 2),
        },
        "skill_weighted": any(
            (s, kind) in skill and skill[(s, kind)]["n"] >= MIN_SKILL_SAMPLES
            for s, _, _ in weighted
        ),
    }


def analyze(bundle: dict, targets: list[date]) -> dict:
    """Blend forecasts for every target date; returns {date_iso: {high:…, low:…}}."""
    out = {}
    for target in targets:
        row = {}
        for kind in ("high", "low"):
            result = analyze_date(bundle, target, kind)
            if result:
                row[kind] = result
        if row:
            out[target.isoformat()] = row
    return out
