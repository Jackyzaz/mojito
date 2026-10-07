"""Daily table of the minimal pipeline: distance to bank + basin rain -> LSTM.

Each step is its own function so notebook 02 can walk through them one at a
time, and the web app (replay and live) builds exactly the same columns.
"""

import numpy
import pandas

from mojito import config, features

STATIONS = list(config.BANK_LEVEL_M)
HORIZONS = features.HORIZONS
TRAIN_END = "2024-12-31"
VALIDATION_END = "2025-09-30"
GFS_MODEL = "gfs_seamless"
# GFS bias is calibrated on the part of the training period with archived forecasts
CALIBRATION_START = "2024-03-01"


def prefix(station):
    return station.replace(".", "").lower()


FEATURES = [f"{prefix(station)}_bank" for station in STATIONS] + ["rain"]
RAIN_NEXT = [f"rain_next{h}d" for h in HORIZONS]


def bank_levels(hourly):
    """Daily max minus bank level per station, plus the X.44 level itself."""
    daily = features.daily_level_features(hourly)
    table = pandas.DataFrame(index=daily.index)
    for station, bank_level in config.BANK_LEVEL_M.items():
        table[f"{prefix(station)}_bank"] = daily[f"{prefix(station)}_max"] - bank_level
    table["x44_max"] = daily["x44_max"]
    return table.loc[table["x44_max"].first_valid_index():]


def add_rain(table, era5):
    table = table.copy()
    table["rain"] = features.basin_weather_daily(era5)["era5_rain"].reindex(table.index)
    return table


def add_targets(table):
    """X.44 level at t+h, its change from today, and the ERA5 rain that fell over t+1..t+h."""
    table = table.copy()
    for h in HORIZONS:
        table[f"target_h{h}"] = table["x44_max"].shift(-h)
        table[f"target_delta_h{h}"] = table[f"target_h{h}"] - table["x44_max"]
        table[f"era5_rain_next{h}d"] = table["rain"].rolling(h).sum().shift(-h)
    return table


def add_split(table):
    table = table.copy()
    table["split"] = numpy.select(
        [table.index <= TRAIN_END, table.index <= VALIDATION_END], ["train", "validation"], default="test"
    )
    return table


def add_gfs_rain(table, nwp_daily):
    """`gfs_raw_rain_next{h}d`: archived GFS basin rain over t+1..t+h, before bias correction."""
    with_gfs = features.add_forecast_rain(table, nwp_daily, GFS_MODEL)
    return with_gfs.rename(columns={f"nwp_{GFS_MODEL}_rain_next{h}d": f"gfs_raw_rain_next{h}d" for h in HORIZONS})


def gfs_bias_scale(table):
    """Multiplier per horizon: observed rain total / GFS forecast total over the calibration period."""
    calibration = table.loc[CALIBRATION_START:TRAIN_END]
    scale = {}
    for h in HORIZONS:
        actual, forecast = calibration[f"era5_rain_next{h}d"], calibration[f"gfs_raw_rain_next{h}d"]
        both = actual.notna() & forecast.notna()
        scale[h] = float(actual[both].sum() / forecast[both].sum())
    return scale


def add_rain_next(table, scale):
    """Model input `rain_next{h}d`: observed ERA5 rain in train, bias-corrected GFS elsewhere."""
    table = table.copy()
    is_train = table["split"] == "train"
    for h in HORIZONS:
        table[f"gfs_rain_next{h}d"] = table[f"gfs_raw_rain_next{h}d"] * scale[h]
        table[f"rain_next{h}d"] = table[f"era5_rain_next{h}d"].where(is_train, table[f"gfs_rain_next{h}d"])
    return table


def build_table(waterlevel_raw, era5):
    """Raw telemetry + ERA5 -> daily table with features, targets and split (no forecast rain)."""
    waterlevel_raw = waterlevel_raw[waterlevel_raw["station_code"].isin(STATIONS)]
    hourly, _ = features.clean_waterlevel(waterlevel_raw)
    return add_split(add_targets(add_rain(bank_levels(hourly), era5)))
