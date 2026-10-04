"""Forecasts served to the web page.

Two modes share one prediction path:

- replay: any past date in the feature table, as if issued at the end of
  that day. From March 2024 the archived rain forecasts of that day feed the
  rain-aware model; earlier dates fall back to the model without rain.
- live: `mojito.realtime` fetches the last weeks of data and today's rain
  forecast. The result is cached for a while so page loads do not refetch.
"""

import datetime
import json
import threading

import geopandas
import pandas

from mojito import config, features, models, realtime, spatial

PLAIN_MODEL = "ridge_delta"
RAIN_MODEL = "ridge_delta_nwp"
LIVE_CACHE = datetime.timedelta(minutes=30)
# Zones whose expected flooded share is below this are drawn transparent
MIN_SHARE_SHOWN = 0.02

MODEL_LABELS = {
    PLAIN_MODEL: "Ridge (ระดับน้ำ + ฝนที่ตกแล้ว)",
    RAIN_MODEL: "Ridge + พยากรณ์ฝน GFS",
}

ALERT_STEPS = [
    (0.0, "normal", "ปกติ"),
    (features.ALERT_LEVELS["ล้นตลิ่ง"], "bank", "น้ำล้นตลิ่ง"),
    (features.ALERT_LEVELS["ท่วมพื้นที่ลุ่มต่ำ"], "flood", "ท่วมพื้นที่ลุ่มต่ำ"),
]


def alert_for(stage):
    level = ALERT_STEPS[0]
    for step in ALERT_STEPS:
        if stage >= step[0]:
            level = step
    return {"code": level[1], "label": level[2]}


class ForecastService:
    def __init__(self):
        self.forecasters = {
            PLAIN_MODEL: models.load(f"x44_{PLAIN_MODEL}"),
            RAIN_MODEL: models.load(f"x44_{RAIN_MODEL}"),
        }
        self.rain_setup = models.load_manifest(f"x44_{RAIN_MODEL}")["extra"]["rain"]
        self.uncertainty = json.loads((models.MODELS_DIR / "uncertainty.json").read_text())
        self.zones = geopandas.read_file(config.DATA_DIR / "processed" / "zones_h3.geojson")

        table = pandas.read_parquet(config.DATA_DIR / "processed" / "features_daily.parquet")
        nwp_daily = features.nwp_daily_rain(pandas.read_parquet(config.RAW_DIR / "nwp_rain_previous_runs.parquet"))
        for model in self.rain_setup["nwp_models"]:
            table = features.add_forecast_rain(table, nwp_daily, model)
        self.table = features.combine_forecast_rain(table, self.rain_setup)

        self._live = None
        self._live_lock = threading.Lock()

    def dates(self):
        days = self.table.index[self.table["x44_max"].notna()]
        return {"first": days.min().date().isoformat(), "last": days.max().date().isoformat()}

    def zones_geojson(self):
        columns = ["h3", "start_stage", "flooded_share_2025", "geometry"]
        return json.loads(self.zones[columns].round(3).to_json())

    def forecast(self, day):
        day = pandas.Timestamp(day)
        if day not in self.table.index or pandas.isna(self.table.at[day, "x44_max"]):
            raise KeyError(f"ไม่มีข้อมูลระดับน้ำ X.44 ของวันที่ {day.date()}")
        result = self._predict(self.table, day)
        result["mode"] = "replay"
        return result

    def forecast_live(self):
        with self._live_lock:
            if self._live and datetime.datetime.now() - self._live[0] < LIVE_CACHE:
                return self._live[1]
            table, issue_day, info = realtime.build_live_table(self.rain_setup)
            result = self._predict(table, issue_day)
            result.update(mode="live", live=info)
            self._live = (datetime.datetime.now(), result)
            return result

    def _predict(self, table, day):
        row = table.loc[[day]]
        rain_columns = [f"rain_next{h}d" for h in features.HORIZONS]
        uses_rain = bool(row[rain_columns].notna().all(axis=None))
        model_name = RAIN_MODEL if uses_rain else PLAIN_MODEL

        horizons = []
        for model in self.forecasters[model_name]:
            stage = float(model.predict(row).iloc[0])
            sigma = spatial.forecast_sigma(self.uncertainty, model_name, model.horizon, stage)
            share = spatial.zone_flood_probability(self.zones, stage, sigma)
            valid_day = day + pandas.Timedelta(days=model.horizon)
            observed = self.table["x44_max"].get(valid_day)
            rain_total = row[f"rain_next{model.horizon}d"].iloc[0] if uses_rain else None
            horizons.append({
                "h": model.horizon,
                "date": valid_day.date().isoformat(),
                "stage": round(stage, 2),
                "sigma": sigma,
                "observed": _round(observed),
                "alert": alert_for(stage),
                "rain_forecast_total_mm": _round(rain_total, 1),
                "zone_share": [round(v, 3) if v >= MIN_SHARE_SHOWN else 0 for v in share],
                "zones_at_risk": int((share >= 0.5).sum()),
                "flooded_km2": round(float((share * self.zones["area_km2"]).sum()), 1),
            })

        today_stage = float(table.at[day, "x44_max"])
        history = table["x44_max"].loc[day - pandas.Timedelta(days=9):day]
        return {
            "issued": day.date().isoformat(),
            "model": model_name,
            "model_label": MODEL_LABELS[model_name],
            "today": {
                "stage": round(today_stage, 2),
                "alert": alert_for(today_stage),
                "rain_3d_mm": _round(table.at[day, "gauge_rain_3d"], 1),
            },
            "history": [{"date": d.date().isoformat(), "stage": _round(v)} for d, v in history.items()],
            "horizons": horizons,
            "alert_levels": dict(features.ALERT_LEVELS),
        }


def _round(value, digits=2):
    return None if value is None or pandas.isna(value) else round(float(value), digits)
