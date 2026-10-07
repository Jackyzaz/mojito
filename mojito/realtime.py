"""Live mode: fetch the last weeks of every input and build the LSTM's input window.

The table goes through `minimal.build_table`, the same code that built the
training table, so live and training inputs cannot drift apart. Two inputs
need live stand-ins:

- ERA5 lags real time by about 5 days. The missing recent hours are filled
  from the ECMWF IFS analysis of the Open-Meteo forecast API, which has the
  same rain layer.
- Rain forecasts come from the latest GFS run instead of the archived runs
  used in the back-test, with the same bias multipliers.
"""

import datetime

import pandas

from mojito import config, features, minimal, sources

# 30-day LSTM window plus room for a few missing days
LOOKBACK_DAYS = 45
ERA5_STAND_IN_MODEL = "ecmwf_ifs025"


def _waterlevel(session, start, end):
    frames = []
    for code in minimal.STATIONS:
        station = config.WATERLEVEL_STATIONS[code]
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


def _forecast_rain(session):
    """Basin mean daily rain of the latest GFS run."""
    hourly = sources.open_meteo_forecast_hourly(session, config.ERA5_POINTS, minimal.GFS_MODEL, ["precipitation"])
    hourly["date"] = hourly["timestamp"].dt.tz_localize(None).dt.normalize()
    per_point = hourly.groupby(["lat", "lon", "date"])["precipitation"].sum()
    return per_point.groupby("date").mean()


def build_live_table(gfs_scale, today=None, session=None):
    """Daily table ending on the issue day, with forecast rain on the issue row.

    Returns (table, issue_day, info). The issue day is the latest day with
    enough X.44 readings; before noon that is usually yesterday."""
    session = session or sources.make_session(cache=False)
    today = today or datetime.date.today()
    start = today - datetime.timedelta(days=LOOKBACK_DAYS)

    waterlevel = _waterlevel(session, start, today)
    era5, last_era5 = _era5_with_recent_analysis(session, start, today)

    table = minimal.build_table(waterlevel, era5)
    issue_day = table["x44_max"].last_valid_index()
    table = table.loc[:issue_day].copy()

    forecast = _forecast_rain(session)
    for h in features.HORIZONS:
        days = pandas.date_range(issue_day + pandas.Timedelta(days=1), periods=h)
        table.loc[issue_day, f"rain_next{h}d"] = forecast.reindex(days).sum(min_count=h) * gfs_scale[h]

    x44 = waterlevel[waterlevel["station_code"] == "X.44"]
    info = {
        "fetched_at": datetime.datetime.now().isoformat(timespec="minutes"),
        "x44_last_reading": x44["timestamp"].max().isoformat(timespec="minutes"),
        "era5_until": last_era5.isoformat(timespec="minutes"),
        "forecast_rain_daily_mm": {
            day.date().isoformat(): round(float(value), 1)
            for day, value in forecast.loc[issue_day + pandas.Timedelta(days=1):].head(5).items()
        },
    }
    return table, issue_day, info
