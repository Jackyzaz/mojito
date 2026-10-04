"""Flask POC: flood risk map for Hat Yai with a 5-day forecast slider."""

import pandas
from flask import Flask, abort, jsonify, render_template, request

from mojito import config
from mojito.web.forecast import ForecastService

# Opening date of the page: the end of the day before the Nov 2025 flood
# crossed the low-lying flood level, so the demo starts on the event
DEMO_DATE = "2025-11-21"


def create_app():
    app = Flask(__name__)
    service = ForecastService()
    stations = pandas.read_parquet(config.RAW_DIR / "stations.parquet")
    key_stations = stations[
        (stations["kind"] == "waterlevel")
        & stations["code"].isin(config.WATERLEVEL_STATIONS)
    ]

    @app.get("/")
    def index():
        return render_template("index.html", demo_date=DEMO_DATE)

    @app.get("/api/dates")
    def dates():
        return jsonify(service.dates())

    @app.get("/api/zones")
    def zones():
        return jsonify(service.zones_geojson())

    @app.get("/api/stations")
    def station_list():
        columns = ["code", "name", "lat", "lon", "min_bank"]
        return jsonify(key_stations[columns].to_dict(orient="records"))

    @app.get("/api/forecast")
    def forecast():
        day = request.args.get("date", DEMO_DATE)
        try:
            return jsonify(service.forecast(day))
        except (KeyError, ValueError) as error:
            abort(404, description=str(error))

    return app


def main():
    create_app().run(host="127.0.0.1", port=5050, debug=False)
