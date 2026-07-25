# KAUS Temperature Forecaster

Multi-source daily temperature forecaster for **Austin-Bergstrom International
Airport (KAUS)**. Pulls every reputable free source with an API, blends them
into a calibrated forecast with honest uncertainty, and — because it logs every
run and verifies against what actually happened — gets better the longer you
use it.

Zero dependencies: Python 3.11+ standard library only.

## Quick start

```bash
python3 forecaster.py forecast            # 7-day forecast, full detail
python3 forecaster.py forecast --brief    # just the numbers
python3 forecaster.py forecast --json     # machine-readable, incl. probabilities
python3 forecaster.py forecast --date 2026-07-22
python3 forecaster.py verify              # score past forecasts vs. reality
python3 forecaster.py dashboard           # re-render dashboard.html
```

**Dashboard:** `dashboard.html` (project root) is a self-contained page —
open it in any browser, no server needed. It shows today/tomorrow tiles with
probability ladders, the last 14 days observed against the next 7 forecast
(80% bands + climate normals), the full source board, the verified-skill
scoreboard, and the NWS forecasters' key messages. It regenerates
automatically after every `forecast` and `verify` run.

Or, in Claude Code, just run `/forecast` — the skill layers an analyst on
top: it reads the NWS forecaster discussion, checks live obs against the
models, interrogates disagreement between sources, and issues a reasoned
final call.

## Data sources

| Source | What it provides | Label(s) |
|---|---|---|
| NWS API (`api.weather.gov`) | Official forecaster-edited grid (max/min), public periods, PoP | `nws_official` |
| NBM via Open-Meteo + IEM | National Blend of Models: gridded + station guidance with its own std-dev | `om_nbm`, `mos_nbs`, `mos_nbe` |
| GFS MOS via IEM (`mesonet.agron.iastate.edu`) | Statistical station guidance (MAV/MEX) | `mos_mav`, `mos_mex` |
| Open-Meteo | ECMWF, GFS, ICON, UKMO, GEM, JMA, Météo-France, HRRR | `om_*` |
| Open-Meteo ensemble API | 31 GEFS + 51 ECMWF members (spread → uncertainty) | — |
| NWS / aviationweather.gov | Live ASOS obs, METAR 6-h max/min groups | context |
| IEM daily summaries | Observed daily max/min (forecast verification) | truth |
| NCEI | 1991–2020 climate normals (cached locally) | context |
| NWS AFD (EWX office) | Human forecaster reasoning | context |

Extended bulletins (NBE/MEX) only fill days their short-range siblings don't
cover, and `best_match` is excluded, so no model is counted twice.

## How the blend works

1. Every source's max/min for each date is collected into one table
   (`data/bundle_latest.json` keeps the full raw bundle, AFD text included).
2. Sources are combined by weighted mean. Weights start from reputation
   priors (NBM ≈ ECMWF > NWS official > GFS MOS > single global models) and
   automatically shift to **earned weights** (inverse MAE from the local
   verification log) once a source has ≥10 verified forecasts in that
   lead-time bucket.
3. Uncertainty (σ) is the widest of: inter-source disagreement, within-model
   ensemble member spread, the NBM's own published σ, and a lead-dependent
   floor. Output includes an 80% interval and a P(high ≥ T) ladder.
4. Once ≥8 verified runs exist per lead bucket, a shrunken station bias
   correction is applied to the blend.

## The learning loop

```bash
python3 forecaster.py forecast     # each run logs every source + the blend
python3 forecaster.py verify      # next day: fetch observed high/low, score
python3 forecaster.py scoreboard  # per-source MAE/bias by lead bucket
```

`data/forecast_log.csv` and `data/observed.csv` are plain CSVs — inspect or
analyze them freely. `log-agent --date … --high …` records a human/AI-adjusted
forecast under the `AGENT` label so its skill is tracked against the blend.

## Notes

- The climate day is midnight-to-midnight **America/Chicago**, matching how
  the airport's official high/low is recorded.
- All temperatures are °F.
- A dead source never kills a run — it's listed under `errors` in the output
  and the blend proceeds without it.
- Be polite: sources are free public APIs. A few runs a day is fine; don't
  hammer them in a loop.
