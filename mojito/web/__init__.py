"""Flask POC: flood risk map for Hat Yai with a 5-day forecast slider."""

import queue
import threading

import pandas
from flask import Flask, Response, abort, jsonify, render_template, request

from mojito import config
from mojito.web.forecast import LIVE_STEPS, ForecastService

# Opening date of the page: the end of the day before the Nov 2025 flood
# crossed the low-lying flood level, so the demo starts on the event
DEMO_DATE = "2025-11-21"


def create_app():
    app = Flask(__name__)
    # Re-read index.html when it changes so it never runs against a newer app.js
    app.config["TEMPLATES_AUTO_RELOAD"] = True
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

    @app.get("/api/forecast/live")
    def forecast_live():
        try:
            return jsonify(service.forecast_live())
        except Exception as error:  # an upstream API being down must not crash the page
            app.logger.exception("live forecast failed")
            abort(503, description=f"ดึงข้อมูลสดไม่สำเร็จ: {error}")

    @app.get("/api/forecast/live/stream")
    def forecast_live_stream():
        """Server-sent events for the live button: `start` (the step list), one `progress`
        per step as it starts, then `result` with the forecast or `failed` with a message."""
        events = queue.Queue()
        started = []

        def progress(label):
            started.append(label)
            events.put(("progress", {"step": len(started) - 1, "label": label}))

        def work():
            try:
                events.put(("result", service.forecast_live(progress)))
            except Exception as error:  # an upstream API being down must not crash the page
                app.logger.exception("live forecast failed")
                events.put(("failed", {"message": f"ดึงข้อมูลสดไม่สำเร็จ: {error}"}))

        # The forecast runs in its own thread, so it finishes (and fills the cache) even if
        # the page stops listening
        threading.Thread(target=work, daemon=True).start()

        def stream():
            steps = [{"group": group, "label": label} for group, label in LIVE_STEPS]
            yield f"event: start\ndata: {app.json.dumps({'steps': steps})}\n\n"
            while True:
                kind, data = events.get()
                yield f"event: {kind}\ndata: {app.json.dumps(data)}\n\n"
                if kind != "progress":
                    return

        return Response(stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})

    return app


def main():
    create_app().run(host="127.0.0.1", port=5050, debug=False)
