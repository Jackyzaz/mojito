"""Live mode: fetch the last weeks of every input and build today's feature row.

The row goes through `features.build_daily_table`, the same code that built
the training table, so live and training features cannot drift apart. Two
inputs need live stand-ins:

- ERA5 lags real time by about 5 days. The missing recent hours are filled
  from the ECMWF IFS analysis of the Open-Meteo forecast API, which has the
  same rain and soil moisture layers.
- Rain forecasts come from the latest model run instead of the archived runs
  used in the back-test (notebook 06).
"""

import datetime

import pandas

from mojito import config, features, sources

LOOKBACK_DAYS = 40  # covers the 30-day rain sum and the 3-day lags
ERA5_STAND_IN_MODEL = "ecmwf_ifs025"


def _waterlevel(session, start, end):
    frames = []
    for code, station in config.WATERLEVEL_STATIONS.items():
        frame = sources.thaiwater_waterlevel_range(session, station["id"], start, end)
        if not frame.empty:
            frames.append(frame.assign(station_code=code))
    return pandas.concat(frames, ignore_index=True)


def _era5_with_recent_analysis(session, start, end):
    era5 = sources.era5_hourly(session, config.ERA5_POINTS, start.year, end)
    era5 = era5[era5["timestamp"].dt.date >= start]
    recent = sources.open_meteo_forecast_hourly(
        session, config.ERA5_POINTS, ERA5_STAND_IN_MODEL, sources.ERA5_HOURLY,
        past_days=14, forecast_days=1,
    )
    last_era5 = era5["timestamp"].max()
    recent = recent[(recent["timestamp"] > last_era5) & (recent["timestamp"].dt.date <= end)]
    return pandas.concat([era5, recent], ignore_index=True), last_era5


def _rain_gauges(session, start, end):
    stations = pandas.read_parquet(config.RAW_DIR / "stations.parquet")
    gauges = stations[(stations["kind"] == "rain") & stations["code"].isin(features.CITY_RAIN_GAUGES)]
    ids = dict(zip(gauges["code"], gauges["station_id"]))
    ids.update({code: info["id"] for code, info in config.RAIN_STATIONS.items() if code in features.CITY_RAIN_GAUGES})
    months = pandas.period_range(start, end, freq="M")
    frames = [
        sources.thaiwater_rain_month(session, station_id, month.year, month.month).assign(station_code=code)
        for code, station_id in ids.items()
        for month in months
    ]
    # Daily totals close the next morning; today's value is the rolling 24 h
    latest = sources.thaiwater_rain_24h(session)
    latest = latest[latest["station_id"].isin(ids.values())]
    code_by_id = {station_id: code for code, station_id in ids.items()}
    frames.append(pandas.DataFrame({
        "station_code": latest["station_id"].map(code_by_id),
        "date": end,
        "rain_mm": latest["rain_mm"],
    }))
    gauges = pandas.concat(frames, ignore_index=True)
    return gauges.drop_duplicates(["station_code", "date"], keep="first")


def _forecast_rain(session, nwp_models):
    """Basin mean daily rain of the latest run, per model (columns) and day."""
    daily = {}
    for model in nwp_models:
        hourly = sources.open_meteo_forecast_hourly(session, config.ERA5_POINTS, model, ["precipitation"])
        hourly["date"] = hourly["timestamp"].dt.tz_localize(None).dt.normalize()
        per_point = hourly.groupby(["lat", "lon", "date"])["precipitation"].sum()
        daily[model] = per_point.groupby("date").mean()
    return pandas.DataFrame(daily)


def build_live_table(rain_setup, today=None, session=None):
    """Daily feature table ending today, with forecast rain on the issue row.

    Returns (table, issue_day, info). The issue day is the latest day with
    enough X.44 readings; before noon that is usually yesterday."""
    session = session or sources.make_session(cache=False)
    today = today or datetime.date.today()
    start = today - datetime.timedelta(days=LOOKBACK_DAYS)

    waterlevel = _waterlevel(session, start, today)
    era5, last_era5 = _era5_with_recent_analysis(session, start, today)
    rain_gauge = _rain_gauges(session, start, today)
    glofas = sources.glofas_daily(session, config.GLOFAS_POINTS, start, today)
    oni = sources.oni(session)

    table, _, _ = features.build_daily_table(waterlevel, era5, rain_gauge, glofas, oni)
    issue_day = table["x44_max"].last_valid_index()
    table = table.loc[:issue_day]

    forecast = _forecast_rain(session, rain_setup["nwp_models"])
    for model in rain_setup["nwp_models"]:
        for horizon in features.HORIZONS:
            days = pandas.date_range(issue_day + pandas.Timedelta(days=1), periods=horizon)
            table.loc[issue_day, f"nwp_{model}_rain_next{horizon}d"] = forecast[model].reindex(days).sum(min_count=horizon)
    table = features.combine_forecast_rain(table, rain_setup)

    x44 = waterlevel[waterlevel["station_code"] == "X.44"]
    info = {
        "fetched_at": datetime.datetime.now().isoformat(timespec="minutes"),
        "x44_last_reading": x44["timestamp"].max().isoformat(timespec="minutes"),
        "era5_until": last_era5.isoformat(timespec="minutes"),
        "forecast_rain_daily_mm": {
            day.date().isoformat(): round(float(value), 1)
            for day, value in forecast.mean(axis=1).loc[issue_day + pandas.Timedelta(days=1):].head(5).items()
        },
    }
    return table, issue_day, info
