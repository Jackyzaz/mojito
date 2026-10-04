"""Fetchers for each data source. Every function returns a pandas DataFrame
(or writes a raster) so the notebooks and, later, the real-time pipeline share
the same parsing code.

Endpoints and their quirks are documented in docs/data-sources.md
"""

import datetime
import io
import time

import numpy
import pandas
import requests
import requests_cache
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from mojito import config

THAIWATER_API = "https://api-v3.thaiwater.net/api/v1/thaiwater30/public"
RID_HYDRO8_API = "https://hyd-app-db.rid.go.th/webservice"
HYDRO8_MAXGH_PDF = "https://hydro-8.com/main/Submenu/4-RUNOFF/MaxGH/MaxGH_{code}.pdf"
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_FLOOD = "https://flood-api.open-meteo.com/v1/flood"
GHCND_CSV = "https://www.ncei.noaa.gov/data/global-historical-climatology-network-daily/access/{id}.csv"
ONI_TXT = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"
GFM_STAC = "https://stac.eodc.eu/api/v1"
COPERNICUS_DEM_TILE = (
    "https://copernicus-dem-30m.s3.amazonaws.com/"
    "Copernicus_DSM_COG_10_{tile}_DEM/Copernicus_DSM_COG_10_{tile}_DEM.tif"
)

# Pause between calls to the Thai government APIs so a long backfill does
# not hammer them. Cached responses skip the pause
POLITE_DELAY_S = 0.3


def make_session(cache=True):
    """HTTP session with retries. Historical responses never change, so they
    are cached on disk and a re-run of a notebook costs no network calls."""
    if cache:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        session = requests_cache.CachedSession(
            str(config.CACHE_DIR / "http_cache"),
            backend="sqlite",
            allowable_methods=("GET", "POST"),
            expire_after=requests_cache.NEVER_EXPIRE,
        )
    else:
        session = requests.Session()

    # 429 is left to _get_json: rate limits need a minute-long wait, not a
    # short backoff
    retry = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=None,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = "mojito-hatyai-flood-research/0.1"
    return session


RATE_LIMIT_WAIT_S = 65
RATE_LIMIT_ATTEMPTS = 30


def _get_json(session, url, **kwargs):
    # Open-Meteo weighs a multi-year, multi-point request as many calls, so a
    # long backfill hits its per-minute and per-hour limits. Wait them out
    for _ in range(RATE_LIMIT_ATTEMPTS):
        response = session.get(url, timeout=300, **kwargs)
        if response.status_code != 429:
            break
        print(f"rate limited by {url.split('/')[2]}, waiting {RATE_LIMIT_WAIT_S}s")
        time.sleep(RATE_LIMIT_WAIT_S)
    response.raise_for_status()
    if not getattr(response, "from_cache", False):
        time.sleep(POLITE_DELAY_S)
    return response.json()


def _localize(series):
    return pandas.to_datetime(series).dt.tz_localize(config.TIMEZONE)


# ---------------------------------------------------------------------------
# ThaiWater (HII)
# ---------------------------------------------------------------------------


def _in_bbox(lat, lon, bbox):
    min_lon, min_lat, max_lon, max_lat = bbox
    return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat


def thaiwater_stations(session, bbox=config.BASIN_BBOX):
    """Water level and rain stations inside the bbox, from the latest feeds."""
    rows = []

    waterlevel = _get_json(session, f"{THAIWATER_API}/waterlevel_load")
    for record in waterlevel["waterlevel_data"]["data"]:
        station = record["station"]
        lat, lon = station["tele_station_lat"], station["tele_station_long"]
        if lat is None or lon is None or not _in_bbox(lat, lon, bbox):
            continue
        rows.append(
            {
                "station_id": station["id"],
                "code": station["tele_station_oldcode"],
                "name": station["tele_station_name"].get("th"),
                "kind": "waterlevel",
                "agency": record["agency"]["agency_shortname"]["en"],
                "lat": lat,
                "lon": lon,
                "min_bank": station.get("min_bank"),
                "ground_level": station.get("ground_level"),
                "qmax": station.get("qmax"),
            }
        )

    rain = _get_json(session, f"{THAIWATER_API}/rain_24h")
    for record in rain["data"]:
        station = record["station"]
        lat, lon = station["tele_station_lat"], station["tele_station_long"]
        if lat is None or lon is None or not _in_bbox(lat, lon, bbox):
            continue
        rows.append(
            {
                "station_id": station["id"],
                "code": station["tele_station_oldcode"],
                "name": station["tele_station_name"].get("th"),
                "kind": "rain",
                "agency": record["agency"]["agency_shortname"]["en"],
                "lat": lat,
                "lon": lon,
            }
        )

    return pandas.DataFrame(rows)


def thaiwater_waterlevel_year(session, station_id, year):
    """One year of telemetry water level (m MSL) and discharge (m3/s).

    The interval follows the station: hourly for RID, 10 minutes for HII."""
    payload = _get_json(
        session,
        f"{THAIWATER_API}/waterlevel_yearly_graph",
        params={
            "station_type": "tele_waterlevel",
            "station_id": station_id,
            "year": year,
        },
    )
    records = [
        point
        for year_block in payload["data"]["graph_data"]
        for point in year_block["graph_data"]
    ]
    frame = pandas.DataFrame(records, columns=["datetime", "value", "discharge"])
    frame = frame.dropna(subset=["value", "discharge"], how="all")
    if frame.empty:
        return frame
    frame["timestamp"] = _localize(frame["datetime"])
    return frame.rename(columns={"value": "waterlevel_msl"})[
        ["timestamp", "waterlevel_msl", "discharge"]
    ]


def thaiwater_waterlevel(session, stations, years):
    frames = []
    for code, station in stations.items():
        for year in years:
            frame = thaiwater_waterlevel_year(session, station["id"], year)
            if frame.empty:
                continue
            frame.insert(0, "station_code", code)
            frames.append(frame)
    return pandas.concat(frames, ignore_index=True)


def thaiwater_rain_month(session, station_id, year, month):
    payload = _get_json(
        session,
        f"{THAIWATER_API}/rain_monthly_graph",
        params={"station_id": station_id, "year": year, "month": month},
    )
    frame = pandas.DataFrame(
        payload.get("data") or [], columns=["rainfall_datetime", "rainfall_value"]
    )
    frame = frame.dropna(subset=["rainfall_value"])
    frame["date"] = pandas.to_datetime(frame["rainfall_datetime"]).dt.date
    return frame.rename(columns={"rainfall_value": "rain_mm"})[["date", "rain_mm"]]


def thaiwater_rain_months_with_data(session, station_id, year):
    """Months that hold any rain record, from one yearly summary call."""
    payload = _get_json(
        session,
        f"{THAIWATER_API}/rain_yearly_graph",
        params={"station_id": station_id, "year": year},
    )
    # TMD gauges report day_count = 0 even when the month has rain, so the
    # monthly total is checked as well
    return [
        int(record["date_time"][5:7])
        for record in payload.get("data") or []
        if record.get("day_count") or record.get("rainfall") is not None
    ]


def thaiwater_rain_daily(session, stations, years):
    # Most gauges started recently, so the yearly summary is checked first
    # to skip the monthly calls for months that have no data
    frames = []
    for code, station in stations.items():
        for year in years:
            for month in thaiwater_rain_months_with_data(session, station["id"], year):
                frame = thaiwater_rain_month(session, station["id"], year, month)
                if frame.empty:
                    continue
                frame.insert(0, "station_code", code)
                frames.append(frame)
    return pandas.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# RID Hydrology Center 8 (southern region)
# ---------------------------------------------------------------------------


def _buddhist_date(date):
    return f"{date.day:02d}/{date.month:02d}/{date.year + 543}"


def _split_cell(cell):
    """RID packs "level|discharge|percent capacity" into one string."""
    parts = [part.strip() for part in (cell or "").split("|")]
    parts += [""] * (3 - len(parts))
    return [pandas.to_numeric(part, errors="coerce") for part in parts[:3]]


def rid_daily_week(session, end_date):
    """06:00 water level of the 7 days ending on `end_date`, all Hydro-8 stations.

    Columns Q1..Q7 hold end_date, end_date-1, ... end_date-6"""
    response = session.post(
        f"{RID_HYDRO8_API}/getDailyWaterLevelListReportMSL.ashx?option=2",
        data={
            "DW[UtokID]": "8",
            "DW[TimeCurrent]": _buddhist_date(end_date),
            "_search": "false",
            "nd": "1",
            "rows": "1000",
            "page": "1",
            "sidx": "indexcount",
            "sord": "asc",
        },
        timeout=120,
    )
    response.raise_for_status()
    if not getattr(response, "from_cache", False):
        time.sleep(POLITE_DELAY_S)

    rows = []
    for station in response.json().get("rows") or []:
        bank_level, qmax, _ = _split_cell(station.get("braelevel_qmax"))
        for offset in range(7):
            level, discharge, capacity = _split_cell(
                station.get(f"waterlevelvalueQ{offset + 1}")
            )
            if numpy.isnan(level) and numpy.isnan(discharge):
                continue
            rows.append(
                {
                    "date": end_date - datetime.timedelta(days=offset),
                    "station_code": station["stationcode"],
                    "basin": station.get("basinname"),
                    "amphoe": station.get("amphurname"),
                    "waterlevel_msl": level,
                    "discharge": discharge,
                    "capacity_pct": capacity,
                    "bank_level": bank_level,
                    "qmax": qmax,
                }
            )
    return pandas.DataFrame(rows)


def rid_daily(session, start_date, end_date):
    frames = []
    week_end = end_date
    while week_end >= start_date:
        frames.append(rid_daily_week(session, week_end))
        week_end -= datetime.timedelta(days=7)
    frame = pandas.concat(frames, ignore_index=True)
    frame = frame[frame["date"] >= start_date]
    return frame.drop_duplicates(["date", "station_code"]).sort_values(
        ["station_code", "date"], ignore_index=True
    )


def hydro8_annual_max(session, code="X44"):
    """Annual maximum stage from the Hydro-8 bar chart PDF.

    The PDF is a chart, not a table: years and bar labels are rotated text.
    Labels are read column by column and matched to the year below them."""
    import pdfplumber

    response = session.get(HYDRO8_MAXGH_PDF.format(code=code), timeout=120)
    response.raise_for_status()

    page = pdfplumber.open(io.BytesIO(response.content)).pages[0]
    rotated = [
        char
        for char in page.chars
        if char["matrix"][1] > 0.9 and char["text"] in "0123456789"
    ]

    columns = {}
    for char in rotated:
        key = (round(char["x0"] * 2) / 2, round(char["size"], 1))
        columns.setdefault(key, []).append(char)

    def read(chars):
        # Rotated 90 degrees counter-clockwise, so the text runs bottom to top
        return "".join(c["text"] for c in sorted(chars, key=lambda c: -c["top"]))

    years = {x: int(read(chars)) for (x, size), chars in columns.items() if size < 5.2}
    values = {x: read(chars) for (x, size), chars in columns.items() if size >= 5.2}

    rows = []
    for x, year_be in years.items():
        label = next((v for vx, v in values.items() if abs(vx - x) <= 1.0), None)
        # Labels are printed as "9.10" with the dot drawn as a separate glyph
        value = float(f"{label[0]}.{label[1:]}") if label else numpy.nan
        rows.append({"water_year": year_be - 543, "max_stage_msl": value})
    return pandas.DataFrame(rows).sort_values("water_year", ignore_index=True)


# ---------------------------------------------------------------------------
# Open-Meteo: ERA5 reanalysis and GloFAS river discharge
# ---------------------------------------------------------------------------

ERA5_HOURLY = [
    "precipitation",
    "soil_moisture_0_to_7cm",
    "soil_moisture_7_to_28cm",
    "soil_moisture_28_to_100cm",
]


def era5_hourly(session, points, start_year, end_date, chunk_years=5):
    """Hourly ERA5 for several grid points.

    `models=era5` is pinned: the default "best match" swaps to the IFS
    analysis for recent years, which would mix two rain climatologies"""
    lats = ",".join(str(lat) for lat, _ in points)
    lons = ",".join(str(lon) for _, lon in points)
    frames = []
    for year in range(start_year, end_date.year + 1, chunk_years):
        start = datetime.date(year, 1, 1)
        end = min(datetime.date(year + chunk_years - 1, 12, 31), end_date)
        payload = _get_json(
            session,
            OPEN_METEO_ARCHIVE,
            params={
                "latitude": lats,
                "longitude": lons,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "hourly": ",".join(ERA5_HOURLY),
                "models": "era5",
                "timezone": config.TIMEZONE,
            },
        )
        for (lat, lon), location in zip(points, payload):
            frame = pandas.DataFrame(location["hourly"])
            frame["timestamp"] = _localize(frame.pop("time"))
            frame.insert(0, "lat", lat)
            frame.insert(1, "lon", lon)
            frames.append(frame)
    frame = pandas.concat(frames, ignore_index=True)
    return frame.dropna(subset=ERA5_HOURLY, how="all")


def glofas_daily(session, points, start_date, end_date):
    frames = []
    for name, (lat, lon) in points.items():
        payload = _get_json(
            session,
            OPEN_METEO_FLOOD,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "river_discharge",
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
        )
        frame = pandas.DataFrame(payload["daily"])
        frame["date"] = pandas.to_datetime(frame.pop("time")).dt.date
        frame.insert(0, "point", name)
        frame["cell_lat"] = payload["latitude"]
        frame["cell_lon"] = payload["longitude"]
        frames.append(frame)
    return pandas.concat(frames, ignore_index=True).dropna(subset=["river_discharge"])


# ---------------------------------------------------------------------------
# NOAA: GHCN-Daily station rain and the ONI ENSO index
# ---------------------------------------------------------------------------


def ghcnd_daily(session, stations):
    frames = []
    for code, ghcnd_id in stations.items():
        response = session.get(GHCND_CSV.format(id=ghcnd_id), timeout=300)
        response.raise_for_status()
        frame = pandas.read_csv(
            io.StringIO(response.text),
            usecols=["DATE", "PRCP", "TMAX", "TMIN", "TAVG"],
            dtype={"PRCP": "float", "TMAX": "float", "TMIN": "float", "TAVG": "float"},
        )
        # GHCN-D stores rain in tenths of mm and temperature in tenths of degC
        frame = pandas.DataFrame(
            {
                "station_code": code,
                "date": pandas.to_datetime(frame["DATE"]).dt.date,
                "rain_mm": frame["PRCP"] / 10,
                "tmax_c": frame["TMAX"] / 10,
                "tmin_c": frame["TMIN"] / 10,
                "tavg_c": frame["TAVG"] / 10,
            }
        )
        frames.append(frame)
    return pandas.concat(frames, ignore_index=True)


ONI_SEASON_MONTH = {
    "DJF": 1, "JFM": 2, "FMA": 3, "MAM": 4, "AMJ": 5, "MJJ": 6,
    "JJA": 7, "JAS": 8, "ASO": 9, "SON": 10, "OND": 11, "NDJ": 12,
}


def oni(session):
    """Oceanic Nino Index, one value per 3-month season, dated at its middle month."""
    response = session.get(ONI_TXT, timeout=120)
    response.raise_for_status()
    frame = pandas.read_csv(io.StringIO(response.text), sep=r"\s+")
    frame["month"] = frame["SEAS"].map(ONI_SEASON_MONTH)
    frame["date"] = pandas.to_datetime(
        {"year": frame["YR"], "month": frame["month"], "day": 1}
    )
    return frame.rename(columns={"SEAS": "season", "ANOM": "oni"})[
        ["date", "season", "oni"]
    ]


# ---------------------------------------------------------------------------
# Rasters: Copernicus GFM flood extent and Copernicus DEM
# ---------------------------------------------------------------------------


def _quiet_gdal():
    import os

    # Stops GDAL from probing for sidecar files next to every remote COG
    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")


def gfm_flood_extent(out_dir, bbox, start, end):
    """Download GFM Sentinel-1 flood extent scenes clipped to the bbox.

    Writes one GeoTIFF per scene that has valid pixels in the bbox
    (0 = dry, 1 = flooded, 255 = no data) and returns a summary table."""
    import rioxarray
    from pystac_client import Client

    _quiet_gdal()
    out_dir.mkdir(parents=True, exist_ok=True)

    items = Client.open(GFM_STAC).search(
        collections=["GFM"], bbox=bbox, datetime=f"{start}/{end}"
    ).items()

    rows = []
    for item in items:
        scene = rioxarray.open_rasterio(item.assets["ensemble_flood_extent"].href)
        clipped = scene.rio.clip_box(*bbox, crs="EPSG:4326")
        values = clipped.values
        valid = values != 255
        if not valid.any():
            continue

        path = out_dir / f"{item.id}.tif"
        clipped.rio.to_raster(path, compress="deflate")
        pixel_km2 = abs(clipped.rio.resolution()[0] * clipped.rio.resolution()[1]) / 1e6
        rows.append(
            {
                "scene": item.id,
                "datetime": item.datetime,
                "path": str(path.relative_to(config.ROOT)),
                "valid_pct": round(100 * valid.mean(), 1),
                "flooded_km2": round(float((values == 1).sum()) * pixel_km2, 2),
            }
        )
    return pandas.DataFrame(rows)


SENTINEL_ASIA_EOSRS_2025 = (
    "https://sentinel-asia.org/EO/2025/article20251119TH/EOS-RS_20251123_FPM_S1/"
    "EOS-RS_20251123_FPM_S1_Thailand_Hat_Yai_Floods_v0.9_shp.zip"
)


def sentinel_asia_flood_proxy(session, bbox, url=SENTINEL_ASIA_EOSRS_2025):
    """Flood proxy polygons published through Sentinel Asia, clipped to the bbox.

    EOS-RS builds them from Sentinel-1 change detection, which also picks up
    flooding between buildings that the GFM water classifier leaves out"""
    import geopandas
    import shapely

    response = session.get(url, timeout=300)
    response.raise_for_status()
    polygons = geopandas.read_file(io.BytesIO(response.content))
    return polygons.clip(shapely.box(*bbox)).reset_index(drop=True)


def copernicus_dem(out_path, bbox, tiles=("N06_00_E100_00", "N07_00_E100_00")):
    """Copernicus GLO-30 DEM tiles merged and clipped to the bbox."""
    import rioxarray
    from rioxarray.merge import merge_arrays

    _quiet_gdal()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pieces = [
        rioxarray.open_rasterio(COPERNICUS_DEM_TILE.format(tile=tile)).rio.clip_box(
            *bbox
        )
        for tile in tiles
    ]
    dem = merge_arrays(pieces)
    dem.rio.to_raster(out_path, compress="deflate")
    return dem
