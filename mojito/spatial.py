"""Terrain features, flood susceptibility and per-zone flood probability.

The water level model forecasts one number, the daily maximum stage at X.44.
This module turns that number into a map:

1. Terrain features per 30 m DEM cell (elevation, HAND, drainage distance...)
2. A susceptibility score per cell, learned from the Nov 2025 flood extent
3. A "critical stage" per cell: the X.44 level at which that cell floods.
   Cells flood in order of susceptibility, from none at the onset level to
   the observed 2025 extent at the 2025 peak
4. Per-zone flood probability for a forecast stage and its uncertainty
"""

import math

import geopandas
import h3
import numpy
import pandas
import rioxarray
import shapely
from rasterio import features as rasterio_features
from scipy import ndimage
from scipy.stats import norm

# pysheds 0.5 still calls numpy.in1d, which numpy 2 removed; isin is the
# same function under its current name
numpy.in1d = numpy.isin
from pysheds.grid import Grid  # noqa: E402

DRAINAGE_MIN_CELLS = 5000  # ~4.5 km2 upstream area makes a channel
CELL_SIZE_M = 30

# X.44 stages that anchor the critical stage scale
FLOOD_ONSET_STAGE = 7.40  # low-lying areas start to flood (RID Hydro-8)
REFERENCE_PEAK_STAGE = 9.97  # Nov 2025 peak, when the label extent was mapped

TERRAIN_FEATURES = ["elevation", "hand", "log_accumulation", "drainage_distance", "slope"]


def terrain_features(dem_path):
    """Terrain rasters on the DEM grid, as a dict of 2-D arrays."""
    # Copernicus DEM has real 0 m cells along the lake, so 0 must not be nodata
    grid = Grid.from_raster(str(dem_path), nodata=-9999.0)
    dem = grid.read_raster(str(dem_path), nodata=-9999.0)
    conditioned = grid.resolve_flats(grid.fill_depressions(grid.fill_pits(dem)))
    flow_direction = grid.flowdir(conditioned)
    accumulation = grid.accumulation(flow_direction)
    channels = accumulation > DRAINAGE_MIN_CELLS

    hand = numpy.asarray(grid.compute_hand(flow_direction, dem, channels), dtype=float)
    hand[hand < -1000] = numpy.nan  # cells that drain off the grid edge

    elevation = numpy.asarray(dem, dtype=float)
    gradient_y, gradient_x = numpy.gradient(elevation, CELL_SIZE_M)
    return {
        "elevation": elevation,
        "hand": hand,
        "log_accumulation": numpy.log1p(numpy.asarray(accumulation, dtype=float)),
        "drainage_distance": ndimage.distance_transform_edt(~numpy.asarray(channels)) * CELL_SIZE_M,
        "slope": numpy.hypot(gradient_x, gradient_y),
        "channels": numpy.asarray(channels),
    }


def rasterize(geometries, like):
    """Burn geometries onto the grid of the `like` DataArray as a bool mask."""
    return rasterio_features.rasterize(
        geometries, out_shape=like.shape, transform=like.rio.transform(), fill=0, default_value=1
    ).astype(bool)


def stack(terrain, mask):
    return numpy.column_stack([terrain[name][mask] for name in TERRAIN_FEATURES])


def critical_stage(susceptibility, flooded_fraction_at_peak):
    """X.44 stage at which each cell floods.

    Cells are ranked by susceptibility. At the onset stage nothing floods; at
    the 2025 peak the top `flooded_fraction_at_peak` of cells flood, matching
    the observed extent. In between the flooded share grows linearly with
    stage. One mapped event cannot pin the shape of that curve, so linear is
    an assumption, not a fit."""
    rank = pandas.Series(susceptibility).rank(ascending=False, pct=True).to_numpy()
    span = REFERENCE_PEAK_STAGE - FLOOD_ONSET_STAGE
    return FLOOD_ONSET_STAGE + span * rank / flooded_fraction_at_peak


def zone_table(critical, lats, lons, resolution, observed=None, quantiles=numpy.linspace(0.05, 0.95, 10)):
    """Group cells into H3 zones; keep quantiles of the critical stage per zone.

    `observed` (optional, bool per cell) adds the share of the zone that
    flooded in the mapped event."""
    cells = [h3.latlng_to_cell(lat, lon, resolution) for lat, lon in zip(lats, lons)]
    frame = pandas.DataFrame({"h3": cells, "critical": critical})
    grouped = frame.groupby("h3")["critical"]
    table = grouped.quantile(quantiles).unstack()
    table.columns = [f"q{int(round(q * 100)):02d}" for q in quantiles]
    table["cells"] = grouped.size()
    if observed is not None:
        table["flooded_share_2025"] = frame.assign(observed=observed).groupby("h3")["observed"].mean()
    table = table[table["cells"] >= 20]  # drop slivers along the boundary
    geometry = [
        shapely.Polygon([(lon, lat) for lat, lon in h3.cell_to_boundary(cell)]) for cell in table.index
    ]
    return geopandas.GeoDataFrame(table.reset_index(), geometry=geometry, crs="EPSG:4326")


def zone_flood_probability(zones, stage, sigma):
    """Expected flooded share of each zone for a forecast stage.

    The forecast stage is treated as normal with standard deviation `sigma`
    (the model error at that horizon); each critical stage quantile floods
    with the probability that the true stage exceeds it."""
    quantile_columns = [c for c in zones.columns if c.startswith("q") and c[1:].isdigit()]
    critical = zones[quantile_columns].to_numpy()
    exceed = norm.sf(critical, loc=stage, scale=max(sigma, 1e-6))
    return pandas.Series(exceed.mean(axis=1), index=zones.index)


def forecast_sigma(uncertainty, model_name, horizon, stage):
    """Forecast error (m) for a horizon, by whether the forecast is high water."""
    entry = uncertainty[model_name][str(horizon)]
    if stage >= uncertainty["split_stage_m"] and entry["high"] is not None:
        return entry["high"]
    return entry["low"]


def open_dem(path):
    return rioxarray.open_rasterio(path).squeeze()


def cell_area_km2(dem, latitude=7.0):
    res_x, res_y = (abs(r) for r in dem.rio.resolution())
    return (res_x * 111.32 * math.cos(math.radians(latitude))) * (res_y * 110.57)
