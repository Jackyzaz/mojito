# mojito — Hat Yai flood prediction (ML)

Course mini project for 240-318 AI/ML (PSU CoE): "พรุ่งนี้น้ำจะท่วมไหมนะ?". Predict the daily max water level at
station **X.44 (Hat Yai Nai, city centre)** 1–5 days ahead, then turn it into a per-zone flood risk heatmap
for Hat Yai district. Proposal: `~/Documents/coe691/Ai/Prepare Project.pdf`. Progress log: `docs/progress.md`
(read it first, update it when finishing work). Data source survey: `docs/data-sources.md`.

Sibling project `~/sindhu` is the user's production ETL (MageAI → MongoDB) + Flask/FastAPI web app. This repo
is the ML side plus a self-contained Flask POC for the course demo. Do not import sindhu code (it is tied to
MongoDB/beanie); copy small resources instead.

The user writes in Thai. Notebooks, markdown cells, plot labels and the web UI are Thai; code, identifiers and
code comments are English.

## Commands

```bash
uv sync                                   # Python 3.13 env
uv run jupyter lab                        # notebooks
uv run mojito-web                         # web POC on http://127.0.0.1:5050 (.claude/launch.json: mojito-web)
# run a notebook headless (GDAL var silences sidecar-file probing on remote COGs)
GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR uv run jupyter nbconvert --to notebook --execute --inplace \
  --ExecutePreprocessor.timeout=-1 notebook/<name>.ipynb
```

### Minimal pipeline (`notebook/`, the one presented to the instructor)

Small LSTM pipeline plus the zone map, meant to be explained end to end. Run 01 → 04:

| Notebook | Writes |
|---|---|
| 01_data | `data/raw/` stations, waterlevel_tele, era5_hourly, nwp_rain_previous_runs, eosrs_flood_2025, dem_glo30.tif (same files as the full pipeline; skips existing) |
| 02_features | `data/processed/minimal_daily.parquet` |
| 03_lstm | `data/models/x44_lstm.joblib`, `x44_lstm.json` (features, GFS bias multipliers), `uncertainty.json` key `lstm` |
| 04_flood_zones | `data/processed/zones_h3.geojson` (LightGBM terrain susceptibility → rank → critical stage → H3; demo uses the LSTM) |

- Inputs: daily max **distance to bank** (`{station}_bank` = level − `config.BANK_LEVEL_M`, ThaiWater `min_bank`)
  for X.44, X.90, X.173A, X.174, SLA007, SLA005 + ERA5 basin rain (7 per day, 30-day window), plus
  `rain_next{h}d` after the LSTM (ERA5 observed in train, GFS × 2024 bias multiplier in validation/test).
- Target: X.44 daily max (m MSL) at t+1..t+5, learned as the change from today (`lstm.LSTMForecaster`).
- Split (its own, not `features.TRAIN_END`): train ≤ 2024-12-31, validation 2025-01-01 → 2025-09-30,
  test ≥ 2025-10-01. Validation has no high-water day (max 3.26 m), so early stopping (patience 15) is driven by
  calm days.
- Table steps live in `mojito/minimal.py` (notebook 02 calls them one by one; web and live mode reuse them).
- The web app serves this LSTM (the same seed-0 model trained to 2024, not refit). Replay always feeds the
  archived bias-corrected GFS forecast (`gfs_rain_next{h}d`), so replay starts at 2024-02-29; test-period replay
  matches notebook 03 exactly. `uncertainty.json["lstm"]` is stage-split RMSE on test (validation has no high water).
- The web reads `data/processed/zones_h3.geojson` from notebook 04 (deterministic, `random_state=42`).

### Full pipeline (`notebooks/archive/`, kept for reference; its Ridge models are no longer served)

Notebooks must run top to bottom in order 01 → 07; each writes the inputs of the next (05_flood_zones moved to
`notebook/04_flood_zones`, so 06/07 need its `zones_h3.geojson` from there):

| Notebook | Writes |
|---|---|
| 01_data_collection | `data/raw/*` (first run ~30 min; skips files that exist; HTTP cache in `data/cache/`) |
| 02_data_exploration | nothing (EDA only) |
| 03_feature_engineering | `data/processed/features_daily.parquet` |
| 04_baseline_models | `data/models/x44_<model>_h{1..5}.joblib`, `<tag>.json`, `uncertainty.json` |
| 06_forecast_rain | `data/models/x44_ridge_delta_nwp*` (+ rain setup in its manifest), `uncertainty.json` key `ridge_delta_nwp` |
| 07_lstm | nothing (LSTM vs Ridge/LightGBM comparison; LSTM is not served) |

`data/` is gitignored. Notebooks are the source of truth (edit the .ipynb, then re-run it with nbconvert and
check outputs/plots for errors).

## Code map

- `mojito/config.py` — bbox, station ids (ThaiWater `station_id` ≠ public code), ERA5/GloFAS points, paths
- `mojito/sources.py` — one fetcher per source → DataFrame. `make_session()` = cached session with retries;
  `_get_json` waits out HTTP 429
- `mojito/features.py` — hourly cleaning (sentinels, spikes, gap fill) and the daily feature/target table
- `mojito/models.py` — `HorizonModel` (one model per horizon), save/load under `data/models/`; manifest
  `extra` carries prediction-time settings (rain forecast models and bias multipliers)
- `mojito/evaluation.py` — shared scores (MAE all / high-water days, 7.40 m alert hits), stage-split RMSE
- `mojito/minimal.py` — minimal pipeline table: distance to bank, basin rain, targets, split, GFS rain + bias
- `mojito/realtime.py` — live mode: last 45 days of the 6 stations + ERA5/IFS → `minimal.build_table` + latest GFS
- `mojito/lstm.py` — PyTorch LSTM (30-day window → 5 horizons, delta target), CPU-only torch via uv index
- `mojito/spatial.py` — terrain features (pysheds), critical stage per cell, H3 zones, zone flood probability
- `mojito/web/` — Flask app (`create_app`), `forecast.py` (ForecastService), Leaflet page in templates/static

## Domain facts and decisions (do not re-derive)

- Alert levels at X.44: bank 7.15 m, low-lying flooding 7.40 m MSL. Peak Nov 2025 = 9.97 m (25 Nov 10:00).
- Forecast setup: issue at end of day t with data up to t; targets `target_h{h}` = X.44 daily max at t+h.
  Split by time: train ≤ 2023, validation 2024, test ≥ 2025. Never shuffle.
- **Train has no flood** (max 5.16 m) while test peaks at 9.97 m. Tree models predicting the level cap at the
  train max, so models predict the change (`*_delta`) and add today's level back.
- Evaluate on high-water days (target ≥ 4 m) separately; all-day MAE is dominated by calm days.
- `oracle_rain_next{h}d` columns are future observed rain: only for upper-bound experiments, never features.
- Rain forecasts by lead time exist only from Mar 2024, so rain-aware models train on ERA5 future rain
  (`rain_next{h}d` = oracle, "perfect prognosis") and are tested with archived forecasts taken one lead
  older than needed (`FORECAST_LEAD_OFFSET`). GFS × 2024 bias multiplier was chosen on validation.
  Forecasts give only 20–45 % of heavy rain, which limits h=3–5 skill.
- `nwp_*` / `rain_next*` columns are excluded from `feature_columns()`; rain-aware models add them explicitly.
- Web serves the minimal-pipeline LSTM (`x44_lstm`). Live mode fills the ERA5 gap (~5 days) with ECMWF IFS
  analysis and uses the latest GFS run × the manifest's bias multipliers.
- Archive notebook 07 (46 features, old split) found LSTM did not beat Ridge overall; the minimal LSTM (bank
  distances, new split) beats persistence on high-water days and is the one presented and served.
- X.44 telemetry is continuous from 2020 only; RID 06:00 daily values (2017+) differ from the daily max by up
  to 1.5 m on rising days, so they are not mixed into the target.
- Spatial labels: EOS-RS flood proxy (Sentinel Asia, 23 Nov 2025). Copernicus GFM misses urban flooding
  (6 vs 72 km² in the city) — use it only outside the city.
- Critical stage per cell assumes the flooded share grows linearly from 7.40 m (none) to 9.97 m (2025
  extent); one mapped event cannot validate this. The EOS-RS image is from 23 Nov, when X.44 was only 7.3–7.9 m, yet its
  extent is pinned to the 9.97 m peak, so critical stages may be too high (listed as a limitation in the report).
- Forecast uncertainty (`uncertainty.json`) is split by forecast stage (< 4 m / ≥ 4 m); one global sigma
  produced ~10 % flood probability on every calm day.

## Source quirks

- ThaiWater: `999999` / `-999` = missing. `rain_yearly_graph` reports `day_count = 0` for TMD gauges that do
  have rain, so check `rainfall` too.
- RID Hydro-8 POST API takes Buddhist-era dates (`dd/mm/2568`) and returns 7 days per call as `"level|Q|%"`.
- Open-Meteo archive: always pass `models=era5` (default mixes in IFS analysis). Multi-year multi-point calls
  hit the hourly limit; `_get_json` sleeps and retries.
- GloFAS cell nearest the city is a tributary; the main U-Tapao channel is (7.025, 100.425). Data starts 1997.
- pysheds 0.5 needs `numpy.in1d = numpy.isin` (patched in `spatial.py`) and `nodata=-9999` so 0 m lake cells
  are kept.
- SLA005 (Songkhla Lake) has multi-hour spike runs; values > 4 m are dropped.
- Plots use font family `["Noto Sans Thai", "DejaVu Sans"]`.
