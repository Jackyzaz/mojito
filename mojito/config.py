"""Study area, station catalog and paths shared by the notebooks."""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
CACHE_DIR = DATA_DIR / "cache"
RESOURCES_DIR = ROOT / "resources"

TIMEZONE = "Asia/Bangkok"

# (min_lon, min_lat, max_lon, max_lat)
BASIN_BBOX = (100.2, 6.6, 100.7, 7.3)  # U-Tapao basin down to Songkhla Lake
CITY_BBOX = (100.35, 6.9, 100.55, 7.1)  # Hat Yai city

# Water level stations, keyed by their public code. `id` is the ThaiWater
# station id that the graph endpoints take as `station_id`
WATERLEVEL_STATIONS = {
    "X.44": {"id": 2591, "name": "หาดใหญ่ใน (ตัวเมือง)", "agency": "RID"},
    "X.90": {"id": 2589, "name": "บางศาลา (ต้นน้ำ)", "agency": "RID"},
    "X.173A": {"id": 2585, "name": "คลองอู่ตะเภา (ต้นน้ำ)", "agency": "RID"},
    "X.174": {"id": 2590, "name": "คลองวาด", "agency": "RID"},
    "SLA002": {"id": 860, "name": "คลองอู่ตะเภา", "agency": "HII"},
    "SLA007": {"id": 858, "name": "บางกล่ำ", "agency": "HII"},
    "SLA005": {"id": 859, "name": "ปากรอ (ทะเลสาบสงขลา)", "agency": "HII"},
    "ONE037": {"id": 1109526, "name": "สะพานคลองอู่ตะเภา", "agency": "HII"},
    "ONE038": {"id": 1109527, "name": "ปลายคลองอู่ตะเภา", "agency": "HII"},
}

# Lowest bank (m MSL) from ThaiWater `min_bank` (data/raw/stations.parquet)
BANK_LEVEL_M = {
    "X.44": 7.15,
    "X.90": 9.34,
    "X.173A": 16.13,
    "X.174": 8.88,
    "SLA007": 1.59,
    "SLA005": 0.97,
}

RAIN_STATIONS = {
    "48569": {"id": 3749, "name": "สนามบินหาดใหญ่", "agency": "TMD"},
    "48568": {"id": 3748, "name": "สงขลา", "agency": "TMD"},
    "SLA001": {"id": 856, "name": "หาดใหญ่", "agency": "HII"},
    "NPCT": {"id": 857, "name": "นาหม่อม", "agency": "HII"},
    "MOU421": {"id": 8268797, "name": "ต้นน้ำคลองอู่ตะเภา", "agency": "HII"},
}

# NOAA GHCN-Daily ids for the TMD synoptic stations
GHCND_STATIONS = {
    "48569": "TH000048569",  # Hat Yai airport, 1973-
    "48568": "TH000048568",  # Songkhla, 1943-
}

# ERA5 is on a 0.25 degree grid; these grid points cover the basin
ERA5_POINTS = [
    (lat, lon) for lat in (6.75, 7.0, 7.25) for lon in (100.25, 100.5)
]

# GloFAS 0.05 degree cells. The cell nearest the city is a small tributary,
# the main U-Tapao channel is one cell west
GLOFAS_POINTS = {
    "utapao_main": (7.025, 100.425),
    "utapao_upstream": (6.875, 100.425),  # main channel, upstream of X.90
}

# Major Hat Yai floods, used to highlight events in plots
FLOOD_EVENTS = {
    "2000-11": ("2000-11-15", "2000-12-05"),
    "2010-11": ("2010-10-25", "2010-11-15"),
    "2025-11": ("2025-11-15", "2025-12-05"),
}
