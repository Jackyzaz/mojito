"""Forecasts served to the web page.

POC mode replays the daily feature table: picking a date issues the forecast
that would have been made at the end of that day. Live mode needs the
fetchers in `mojito.sources` to build today's feature row first.
"""

import json

import geopandas
import pandas

from mojito import config, features, models, spatial

DEFAULT_MODEL = "ridge_delta"
# Zones whose expected flooded share is below this are drawn transparent
MIN_SHARE_SHOWN = 0.02

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
    def __init__(self, model_name=DEFAULT_MODEL):
        self.model_name = model_name
        self.table = pandas.read_parquet(config.DATA_DIR / "processed" / "features_daily.parquet")
        self.forecasters = models.load(f"x44_{model_name}")
        self.uncertainty = json.loads((models.MODELS_DIR / "uncertainty.json").read_text())
        self.zones = geopandas.read_file(config.DATA_DIR / "processed" / "zones_h3.geojson")

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

        row = self.table.loc[[day]]
        horizons = []
        for model in self.forecasters:
            stage = float(model.predict(row).iloc[0])
            sigma = spatial.forecast_sigma(self.uncertainty, self.model_name, model.horizon, stage)
            share = spatial.zone_flood_probability(self.zones, stage, sigma)
            valid_day = day + pandas.Timedelta(days=model.horizon)
            observed = self.table["x44_max"].get(valid_day)
            horizons.append({
                "h": model.horizon,
                "date": valid_day.date().isoformat(),
                "stage": round(stage, 2),
                "sigma": sigma,
                "observed": None if observed is None or pandas.isna(observed) else round(float(observed), 2),
                "alert": alert_for(stage),
                "zone_share": [round(v, 3) if v >= MIN_SHARE_SHOWN else 0 for v in share],
                "zones_at_risk": int((share >= 0.5).sum()),
                "flooded_km2": round(float((share * self.zones["area_km2"]).sum()), 1),
            })

        history = self.table["x44_max"].loc[day - pandas.Timedelta(days=9):day]
        return {
            "issued": day.date().isoformat(),
            "model": self.model_name,
            "today": {
                "stage": round(float(self.table.at[day, "x44_max"]), 2),
                "alert": alert_for(float(self.table.at[day, "x44_max"])),
                "rain_3d_mm": _round(self.table.at[day, "gauge_rain_3d"]),
            },
            "history": [
                {"date": d.date().isoformat(), "stage": _round(v)} for d, v in history.items()
            ],
            "horizons": horizons,
            "alert_levels": {name: level for name, level in features.ALERT_LEVELS.items()},
        }


def _round(value, digits=2):
    return None if pandas.isna(value) else round(float(value), digits)
