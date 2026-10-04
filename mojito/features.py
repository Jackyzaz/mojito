"""Cleaning and daily feature table for the X.44 water level forecast.

Setup: a forecast is issued at the end of day t using only data up to day t,
and predicts the daily maximum water level at X.44 for days t+1 .. t+5.
"""

import numpy
import pandas

from mojito import config

HORIZONS = (1, 2, 3, 4, 5)
TARGET_STATION = "X.44"

# Below this many hourly readings a daily aggregate is not trusted
MIN_HOURS_PER_DAY = 12
# A reading this far from its 7-hour centred median is a sensor spike; real
# rises on these channels stay well under 0.5 m/h
SPIKE_THRESHOLD_M = 1.0
SPIKE_WINDOW_H = 7
# Physical ceilings. Songkhla Lake peaked at 2.6 m in the 2025 flood, so lake
# readings above 4 m are runs of spikes the median filter cannot catch
PLAUSIBLE_MAX_M = {"SLA005": 4.0}
# Short telemetry dropouts are bridged by linear interpolation
MAX_GAP_FILL_H = 6

# Stations whose daily aggregates become features. SLA002 shares the X.90
# site and SLA005 is the lake end of the channel
LEVEL_FEATURE_STATIONS = ["X.44", "X.90", "X.173A", "X.174", "SLA007", "SLA005"]

# City rain gauges averaged into one gauge rain series
CITY_RAIN_GAUGES = ["48569", "SLA001", "SLA002", "SLA007", "STN0580", "STN0710", "NPCT"]

TRAIN_END = "2023-12-31"
VALIDATION_END = "2024-12-31"

ALERT_LEVELS = {"ล้นตลิ่ง": 7.15, "ท่วมพื้นที่ลุ่มต่ำ": 7.40}


def clean_waterlevel(raw):
    """Telemetry rows -> hourly water level, one column per station.

    Returns the cleaned frame and a per-station report of what was removed."""
    frame = raw.copy()
    sentinel = frame["waterlevel_msl"].abs() >= 999
    frame.loc[sentinel, "waterlevel_msl"] = numpy.nan

    hourly = (
        frame.set_index("timestamp")
        .groupby("station_code")["waterlevel_msl"]
        .resample("h")
        .mean()
        .unstack("station_code")
    )
    hourly.index = hourly.index.tz_localize(None)

    median = hourly.rolling(SPIKE_WINDOW_H, center=True, min_periods=3).median()
    spikes = (hourly - median).abs() > SPIKE_THRESHOLD_M
    for station, ceiling in PLAUSIBLE_MAX_M.items():
        if station in hourly:
            spikes[station] |= hourly[station] > ceiling
    cleaned = hourly.mask(spikes)

    before_fill = cleaned.notna().sum()
    cleaned = cleaned.interpolate(limit=MAX_GAP_FILL_H, limit_area="inside")

    report = pandas.DataFrame(
        {
            "sentinel_removed": frame[sentinel].groupby("station_code").size(),
            "spikes_removed": spikes.sum(),
            "hours_gap_filled": cleaned.notna().sum() - before_fill,
            "hours_valid": cleaned.notna().sum(),
        }
    ).fillna(0).astype(int)
    return cleaned, report


def daily_level_features(hourly):
    """Daily max / mean / end-of-day level per station, NaN on thin days."""
    enough = hourly.notna().resample("D").sum() >= MIN_HOURS_PER_DAY
    columns = {}
    for station in LEVEL_FEATURE_STATIONS:
        if station not in hourly:
            continue
        series = hourly[station]
        daily = series.resample("D")
        prefix = station.replace(".", "").lower()
        columns[f"{prefix}_max"] = daily.max().where(enough[station])
        columns[f"{prefix}_mean"] = daily.mean().where(enough[station])
        columns[f"{prefix}_last"] = daily.last().where(enough[station])
    return pandas.DataFrame(columns)


def basin_weather_daily(era5):
    """ERA5 basin mean: daily rain sum and soil moisture means."""
    basin = era5.groupby("timestamp")[
        ["precipitation", "soil_moisture_0_to_7cm", "soil_moisture_7_to_28cm",
         "soil_moisture_28_to_100cm"]
    ].mean()
    basin.index = basin.index.tz_localize(None)
    daily = basin.resample("D").agg(
        {
            "precipitation": "sum",
            "soil_moisture_0_to_7cm": "mean",
            "soil_moisture_7_to_28cm": "mean",
            "soil_moisture_28_to_100cm": "mean",
        }
    )
    return daily.rename(
        columns={
            "precipitation": "era5_rain",
            "soil_moisture_0_to_7cm": "soil_0_7",
            "soil_moisture_7_to_28cm": "soil_7_28",
            "soil_moisture_28_to_100cm": "soil_28_100",
        }
    )


def gauge_rain_daily(rain_gauge):
    city = rain_gauge[rain_gauge["station_code"].isin(CITY_RAIN_GAUGES)]
    wide = city.pivot_table(index="date", columns="station_code", values="rain_mm")
    wide.index = pandas.to_datetime(wide.index)
    return pandas.DataFrame(
        {"gauge_rain": wide.mean(axis=1), "gauge_count": wide.notna().sum(axis=1)}
    )


def build_daily_table(waterlevel_raw, era5, rain_gauge, glofas, oni):
    hourly, report = clean_waterlevel(waterlevel_raw)
    table = daily_level_features(hourly)
    # HII stations reach back to 2012 but the target station starts in 2020
    table = table.loc[table["x44_max"].first_valid_index():]

    # Trends of the target and the upstream stations
    for prefix in ("x44", "x90", "x173a"):
        table[f"{prefix}_change_1d"] = table[f"{prefix}_max"].diff(1)
        table[f"{prefix}_change_3d"] = table[f"{prefix}_max"].diff(3)
    for lag in (1, 2, 3):
        table[f"x44_max_lag{lag}"] = table["x44_max"].shift(lag)

    weather = basin_weather_daily(era5)
    for days in (3, 7, 14, 30):
        weather[f"era5_rain_{days}d"] = weather["era5_rain"].rolling(days).sum()
    table = table.join(weather, how="left")

    gauges = gauge_rain_daily(rain_gauge)
    for days in (3, 7):
        gauges[f"gauge_rain_{days}d"] = gauges["gauge_rain"].rolling(days, min_periods=days - 1).sum()
    table = table.join(gauges, how="left")

    discharge = glofas.pivot_table(index="date", columns="point", values="river_discharge")
    discharge.index = pandas.to_datetime(discharge.index)
    discharge = discharge.rename(columns=lambda name: f"glofas_{name}")
    discharge["glofas_utapao_main_change_1d"] = discharge["glofas_utapao_main"].diff()
    table = table.join(discharge, how="left")

    # ONI for a season is published about a month after it ends, so the value
    # known on day t is the one centred two months earlier
    oni_known = oni[["date", "oni"]].assign(date=lambda f: f["date"] + pandas.DateOffset(months=2))
    table = pandas.merge_asof(
        table.rename_axis("date").reset_index(), oni_known.sort_values("date"), on="date"
    ).set_index("date")

    day_of_year = table.index.dayofyear
    table["month"] = table.index.month
    table["doy_sin"] = numpy.sin(2 * numpy.pi * day_of_year / 365.25)
    table["doy_cos"] = numpy.cos(2 * numpy.pi * day_of_year / 365.25)

    for horizon in HORIZONS:
        table[f"target_h{horizon}"] = table["x44_max"].shift(-horizon)
        # Change from today. Trees cannot predict above the highest level they
        # were trained on; predicting the rise and adding it back can
        table[f"target_delta_h{horizon}"] = table[f"target_h{horizon}"] - table["x44_max"]
        # Rain that actually fell over the next days. Not known at forecast
        # time: only for "perfect rain forecast" upper-bound experiments
        table[f"oracle_rain_next{horizon}d"] = (
            table["era5_rain"].rolling(horizon).sum().shift(-horizon)
        )

    table["split"] = numpy.select(
        [table.index <= TRAIN_END, table.index <= VALIDATION_END],
        ["train", "validation"],
        default="test",
    )
    return table, hourly, report


def feature_columns(table):
    """Columns a model may use: everything known at the end of day t.

    Forecast rain (`nwp_*`) is known at day t too, but only from 2024, so it
    is opted into explicitly by the models that use it."""
    excluded = ("target_", "oracle_", "split", "nwp_", "rain_next")
    return [c for c in table.columns if not c.startswith(excluded)]


# Rain forecasts are taken from the run one day older than the lead needs:
# the run issued during day t-1 is surely published by the end of day t
FORECAST_LEAD_OFFSET = 1


def nwp_daily_rain(nwp_rain):
    """Hourly previous-run forecasts -> basin mean daily rain per (model, lead)."""
    frame = nwp_rain.assign(date=nwp_rain["timestamp"].dt.tz_localize(None).dt.normalize())
    per_point = frame.groupby(["model", "lead", "lat", "lon", "date"])["precipitation"].sum()
    daily = per_point.groupby(["model", "lead", "date"]).mean().unstack(["model", "lead"])
    return daily.sort_index(axis=1)


def add_forecast_rain(table, nwp_daily, model, scale=None):
    """Columns `nwp_{model}_rain_next{h}d`: forecast basin rain over t+1..t+h.

    `scale` maps horizon -> multiplier that corrects the model's rain bias."""
    table = table.copy()
    days = []
    for k in HORIZONS:
        lead = k + FORECAST_LEAD_OFFSET
        valid_day = nwp_daily[(model, lead)].reindex(table.index + pandas.Timedelta(days=k))
        days.append(valid_day.to_numpy())
    days = numpy.column_stack(days)
    for horizon in HORIZONS:
        total = days[:, :horizon].sum(axis=1)
        total[numpy.isnan(days[:, :horizon]).any(axis=1)] = numpy.nan
        if scale is not None:
            total = total * scale[horizon]
        table[f"nwp_{model}_rain_next{horizon}d"] = total
    return table


def combine_forecast_rain(table, setup):
    """`rain_next{h}d` model input from the per-model `nwp_*` columns.

    `setup` is the "rain" block of a model manifest: the forecast models to
    average and whether to apply their bias multipliers."""
    table = table.copy()
    for horizon in HORIZONS:
        values = []
        for model in setup["nwp_models"]:
            column = table[f"nwp_{model}_rain_next{horizon}d"]
            if setup["scaled"]:
                column = column * setup["scale"][model][str(horizon)]
            values.append(column)
        table[f"rain_next{horizon}d"] = sum(values) / len(values)
    return table
