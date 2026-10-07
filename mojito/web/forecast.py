"""Forecasts served to the web page.

Two modes share one prediction path:

- replay: any past date of the minimal daily table, as if issued at the end
  of that day. The archived, bias-corrected GFS forecast of that day is the
  rain input, so only days from March 2024 (archived forecasts) can be shown.
- live: `mojito.realtime` fetches the last weeks of data and today's rain
  forecast. The result is cached for a while so page loads do not refetch.

Both run the LSTM saved by notebook/03_lstm.ipynb.
"""

import datetime
import json
import threading

import geopandas
import joblib
import pandas

from mojito import config, features, minimal, realtime, spatial

MODEL_NAME = "lstm"
MODEL_LABEL = "LSTM (ระยะจากตลิ่ง 6 สถานี + ฝน) + พยากรณ์ฝน GFS"
MODELS_DIR = config.DATA_DIR / "models"
LIVE_CACHE = datetime.timedelta(minutes=30)
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
    def __init__(self):
        self.model = joblib.load(MODELS_DIR / "x44_lstm.joblib")
        manifest = json.loads((MODELS_DIR / "x44_lstm.json").read_text())
        self.gfs_scale = {int(h): value for h, value in manifest["gfs_scale"].items()}
        self.uncertainty = json.loads((MODELS_DIR / "uncertainty.json").read_text())
        self.zones = geopandas.read_file(config.DATA_DIR / "processed" / "zones_h3.geojson")

        # Replay always uses the archived forecast, never the observed future rain of the training rows
        table = pandas.read_parquet(config.DATA_DIR / "processed" / "minimal_daily.parquet")
        for h in features.HORIZONS:
            table[f"rain_next{h}d"] = table[f"gfs_rain_next{h}d"]
        self.table = table

        self._live = None
        self._live_lock = threading.Lock()

    def _can_forecast(self, table):
        return table["x44_max"].notna() & table[minimal.RAIN_NEXT].notna().all(axis=1)

    def dates(self):
        days = self.table.index[self._can_forecast(self.table)]
        return {"first": days.min().date().isoformat(), "last": days.max().date().isoformat()}

    def zones_geojson(self):
        columns = ["h3", "start_stage", "flooded_share_2025", "geometry"]
        return json.loads(self.zones[columns].round(3).to_json())

    def forecast(self, day):
        day = pandas.Timestamp(day)
        if day not in self.table.index or pandas.isna(self.table.at[day, "x44_max"]):
            raise KeyError(f"ไม่มีข้อมูลระดับน้ำ X.44 ของวันที่ {day.date()}")
        if not self._can_forecast(self.table).loc[day]:
            raise KeyError(f"ไม่มีพยากรณ์ฝนย้อนหลังของวันที่ {day.date()}")
        result = self._predict(self.table, day)
        result["mode"] = "replay"
        return result

    def forecast_live(self):
        with self._live_lock:
            if self._live and datetime.datetime.now() - self._live[0] < LIVE_CACHE:
                return self._live[1]
            table, issue_day, info = realtime.build_live_table(self.gfs_scale)
            result = self._predict(table, issue_day)
            result.update(mode="live", live=info)
            self._live = (datetime.datetime.now(), result)
            return result

    def _predict(self, table, day):
        # The LSTM reads the 30 days ending on `day` from the table itself
        stages = self.model.predict(table, [day]).iloc[0]
        horizons = []
        for h in features.HORIZONS:
            stage = float(stages[f"h{h}"])
            sigma = spatial.forecast_sigma(self.uncertainty, MODEL_NAME, h, stage)
            share = spatial.zone_flood_probability(self.zones, stage, sigma)
            valid_day = day + pandas.Timedelta(days=h)
            observed = self.table["x44_max"].get(valid_day)
            rain_total = table.at[day, f"rain_next{h}d"]
            horizons.append({
                "h": h,
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
            "model": MODEL_NAME,
            "model_label": MODEL_LABEL,
            "today": {
                "stage": round(today_stage, 2),
                "alert": alert_for(today_stage),
            },
            "history": [{"date": d.date().isoformat(), "stage": _round(v)} for d, v in history.items()],
            "horizons": horizons,
            "alert_levels": dict(features.ALERT_LEVELS),
        }


def _round(value, digits=2):
    return None if value is None or pandas.isna(value) else round(float(value), digits)
