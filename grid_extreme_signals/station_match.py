"""Station-to-grid matching utilities (shared by Pipeline A and Pipeline B).

This module ports the **verified** station/grid matching logic from
``ref_code/calculate_wind_solar_out/station_output_calculator_0p1deg.py`` and
adapts it to the multi-source adapter layer of this project.

Responsibilities
----------------
- Longitude normalisation.  **Longitude convention is NOT uniform**: stations
  arrive as ``[-180, 180)``; the BCSD *weather* grid convention is **region-
  dependent** (e.g. Germany stored as ``5..15`` but Portugal as ``328.7..353.7``
  i.e. ``[0, 360)``).  Every grid longitude is therefore normalised to
  ``[-180, 180)`` on a per-file basis before matching.
- Nearest-cell lookup: separable 1D argmin (regular lat/lon, circular longitude)
  and 2D cKDTree (rotated-pole NAM-12).
- Country assignment via Natural Earth polygons (point-in-polygon), station
  deduplication with ``activation_year = min(year)``.
- Vectorised gather of a ``(time, *spatial)`` array to ``[(time, n_stations)]``.

Both pipelines import these primitives; the pipelines themselves never call
each other, keeping them decoupled.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from scipy.spatial import cKDTree
    _HAS_SCIPY = True
except ImportError:  # pragma: no cover
    _HAS_SCIPY = False

import shapefile  # pyshp
from shapely.geometry import Point, shape
from shapely.prepared import prep


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default nearest-neighbour distance tolerance (degrees).
#: BCSD ≈ 0 (stations and grid are both 0.1° integer-offset); China/NAM-12
#: ≈ 0.05–0.11.  0.15° covers both without rejecting valid in-grid stations.
MAX_DIST_DEG: float = 0.15

#: Station CSV filename → SSP scenario code.
SSP_MAP = {
    "SSP1-2.6": "ssp126",
    "SSP2-4.5": "ssp245",
    "SSP5-6.0": "ssp585",
    "SSP5-8.5": "ssp585",
}

#: BCSD region directory names that differ from the Natural Earth ``NAME`` field.
BCSD_REGION_TO_NAME = {
    "South-Africa": "South Africa",
    "South-Korea": "South Korea",
    "United-Kingdom": "United Kingdom",
    "México": "Mexico",
}


# ---------------------------------------------------------------------------
# Longitude normalisation
# ---------------------------------------------------------------------------

def lon_to_360(lon):
    """Longitude ``[-180, 180]`` → ``[0, 360)``."""
    return np.asarray(lon, dtype=np.float64) % 360.0


def lon_to_180(lon):
    """Longitude ``[0, 360]`` → ``[-180, 180)``."""
    return ((np.asarray(lon, dtype=np.float64) + 180.0) % 360.0) - 180.0


def is_lon_360(grid_lon) -> bool:
    """Return True if a grid longitude axis looks like ``[0, 360)``.

    Detection rule: any value > 180 ⇒ the convention is ``[0, 360)``.
    A region whose true longitudes are all positive (e.g. Germany 5..15) is
    ambiguous, but normalising it is a no-op there; a region with any negative
    true longitude (Portugal, Ireland, Chile) is stored > 180 in ``[0, 360)``
    and is correctly detected.
    """
    arr = np.asarray(grid_lon, dtype=np.float64)
    return bool(arr.size and float(np.nanmax(arr)) > 180.0)


def normalize_grid_lon(grid_lon) -> np.ndarray:
    """Normalise a grid longitude axis to ``[-180, 180)`` (per-file detection).

    No-op if the input is already ``[-180, 180)``; converts ``[0, 360)``
    (detected when any value > 180) to ``[-180, 180)``.
    """
    arr = np.asarray(grid_lon, dtype=np.float64)
    return lon_to_180(arr) if is_lon_360(arr) else arr


# ---------------------------------------------------------------------------
# Nearest-cell lookup
# ---------------------------------------------------------------------------

def nearest_index_regular(grid_lat, grid_lon_180, sta_lat, sta_lon_180):
    """Nearest cell on a regular 1D lat/lon grid.

    Longitude uses circular distance so the ±180° seam (e.g. Spain, Ireland,
    Alaska) is handled correctly.

    Parameters
    ----------
    grid_lat, grid_lon_180 : (n_lat,) (n_lon,)  grid coordinates; longitude
        already normalised to ``[-180, 180)``.
    sta_lat, sta_lon_180 : (n_sta,)  station coordinates (longitude in
        ``[-180, 180)``).

    Returns
    -------
    lat_idx, lon_idx : (n_sta,) int64  index into the 1D grid axes.
    dist : (n_sta,) float64  nearest-neighbour distance in degrees,
        ``max(|dlat|, |dlon_circular|)``.
    """
    grid_lat = np.asarray(grid_lat, dtype=np.float64)
    grid_lon = np.asarray(grid_lon_180, dtype=np.float64)
    sta_lat = np.asarray(sta_lat, dtype=np.float64)
    sta_lon = np.asarray(sta_lon_180, dtype=np.float64)
    n_sta = sta_lat.shape[0]

    lat_idx = np.empty(n_sta, dtype=np.int64)
    lon_idx = np.empty(n_sta, dtype=np.int64)
    dlat = np.empty(n_sta, dtype=np.float64)
    dlon = np.empty(n_sta, dtype=np.float64)

    for i in range(n_sta):
        d_lat = np.abs(grid_lat - sta_lat[i])
        j_lat = int(np.argmin(d_lat))
        lat_idx[i] = j_lat
        dlat[i] = d_lat[j_lat]

        d_lon = np.abs(((grid_lon - sta_lon[i] + 180.0) % 360.0) - 180.0)
        j_lon = int(np.argmin(d_lon))
        lon_idx[i] = j_lon
        dlon[i] = d_lon[j_lon]

    dist = np.maximum(dlat, dlon)
    return lat_idx, lon_idx, dist


def nearest_index_2d(lat2d, lon2d_180, sta_lat, sta_lon_180):
    """Nearest cell on a rotated-pole 2D grid (NAM-12).

    Flattens the 2D geographic ``lat``/``lon`` auxiliary coordinates and queries
    a cKDTree (falls back to brute-force argmin if scipy is unavailable).

    Returns
    -------
    idx0, idx1 : (n_sta,) int64  indices into the (rlat, rlon) grid.
    dist : (n_sta,) float64  planar distance in degrees.
    """
    lat2d = np.asarray(lat2d, dtype=np.float64)
    lon2d = np.asarray(lon2d_180, dtype=np.float64)
    n0, n1 = lat2d.shape
    lat_flat = lat2d.ravel()
    lon_flat = lon2d.ravel()
    pts = np.column_stack([lon_flat, lat_flat])
    queries = np.column_stack([np.asarray(sta_lon_180, dtype=np.float64),
                               np.asarray(sta_lat, dtype=np.float64)])

    if _HAS_SCIPY:
        tree = cKDTree(pts)
        dist, flat_idx = tree.query(queries, k=1)
        flat_idx = np.asarray(flat_idx, dtype=np.int64)
    else:  # pragma: no cover
        flat_idx = np.empty(len(queries), dtype=np.int64)
        dist = np.empty(len(queries), dtype=np.float64)
        for i, q in enumerate(queries):
            d = (lon_flat - q[0]) ** 2 + (lat_flat - q[1]) ** 2
            j = int(np.argmin(d))
            flat_idx[i] = j
            dist[i] = np.sqrt(d[j])

    return flat_idx // n1, flat_idx % n1, np.asarray(dist, dtype=np.float64)


# ---------------------------------------------------------------------------
# Country shapes & station filtering
# ---------------------------------------------------------------------------

def load_country_shapes(shp_path):
    """Read a Natural Earth admin-0 countries shapefile → ``{NAME: geometry}``.

    Uses pyshp (``shapefile``) + shapely, matching the verified reference.
    """
    sf = shapefile.Reader(shp_path)
    fields = [f[0] for f in sf.fields[1:]]
    name_idx = fields.index("NAME")
    countries = {}
    for i, rec in enumerate(sf.records()):
        name = rec[name_idx]
        countries[name] = shape(sf.shape(i).__geo_interface__)
    return countries


def bcsd_region_to_ne_name(region_dir: str) -> str:
    """Map a BCSD region directory name to its Natural Earth ``NAME``."""
    return BCSD_REGION_TO_NAME.get(region_dir, region_dir)


def infer_scenario_from_csv(csv_path: str) -> str:
    """Infer the SSP scenario code (``ssp126``/…) from a station CSV filename."""
    basename = os.path.basename(csv_path)
    for ssp_name, ssp_code in SSP_MAP.items():
        if ssp_name in basename:
            return ssp_code
    raise ValueError(
        f"无法从文件名 '{basename}' 推断 SSP 情景，支持: {list(SSP_MAP.keys())}"
    )


def load_stations(csv_path) -> pd.DataFrame:
    """Load a station siting CSV.

    Expected columns: ``year, type, lon, lat, capacity_gw``.
    Returns a DataFrame with ``lon`` normalised to ``[-180, 180)`` and numeric
    dtypes.  Rows with missing lon/lat are dropped.
    """
    df = pd.read_csv(csv_path)
    df["lon"] = lon_to_180(pd.to_numeric(df["lon"], errors="coerce"))
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")
    df["capacity_gw"] = pd.to_numeric(df["capacity_gw"], errors="coerce")
    df = df.dropna(subset=["lon", "lat", "year"]).reset_index(drop=True)
    return df


def filter_stations_for_country(stations_df: pd.DataFrame, country_geom,
                                stype: str) -> pd.DataFrame:
    """Select stations of ``stype`` inside ``country_geom`` and deduplicate.

    Dedup keeps ``(lon, lat)`` unique with ``activation_year = min(year)`` over
    the 2030/2040/2050 nesting (a station activated earlier also exists later).

    Returns columns: ``lon, lat, type, activation_year, capacity_gw``.
    """
    if stations_df.empty:
        return stations_df.assign(activation_year=pd.Series(dtype="int64"))
    df_typed = stations_df[stations_df["type"] == stype].copy()
    if df_typed.empty:
        return df_typed.assign(activation_year=pd.Series(dtype="int64"))

    prepared = prep(country_geom)
    keep = np.fromiter(
        (prepared.contains(Point(float(row.lon), float(row.lat)))
         for row in df_typed.itertuples()),
        dtype=bool, count=len(df_typed),
    )
    result = df_typed[keep].copy()
    if result.empty:
        return result.assign(activation_year=pd.Series(dtype="int64"))

    result = (
        result.sort_values("year")
        .groupby(["lon", "lat"], as_index=False)
        .agg({"year": "min", "type": "first", "capacity_gw": "first"})
        .rename(columns={"year": "activation_year"})
        .reset_index(drop=True)
    )
    return result


# ---------------------------------------------------------------------------
# Match result + gather
# ---------------------------------------------------------------------------

@dataclass
class StationMatch:
    """Result of matching a set of stations to one grid.

    Attributes
    ----------
    stations : DataFrame  filtered stations (lon/lat in [-180,180), type,
        activation_year, capacity_gw).
    idx0 : (n_sta,) int64  lat index (regular) or rlat index (rotated).
    idx1 : (n_sta,) int64  lon index (regular) or rlon index (rotated).
    dist_deg : (n_sta,) float64  nearest-cell distance.
    valid : (n_sta,) bool  ``dist_deg <= max_dist``.
    grid_kind : str  ``"regular_latlon"`` or ``"rotated_pole"``.
    """

    stations: pd.DataFrame
    idx0: np.ndarray
    idx1: np.ndarray
    dist_deg: np.ndarray
    valid: np.ndarray
    grid_kind: str

    def __len__(self) -> int:
        return len(self.stations)


def match_regular(grid_lat, grid_lon, stations_df: pd.DataFrame,
                  max_dist: float = MAX_DIST_DEG) -> StationMatch:
    """Match stations to a regular lat/lon grid (per-file lon normalisation)."""
    grid_lon_180 = normalize_grid_lon(grid_lon)
    lat_idx, lon_idx, dist = nearest_index_regular(
        grid_lat, grid_lon_180,
        stations_df["lat"].to_numpy(np.float64),
        stations_df["lon"].to_numpy(np.float64),
    )
    return StationMatch(
        stations=stations_df.reset_index(drop=True),
        idx0=lat_idx, idx1=lon_idx, dist_deg=dist,
        valid=dist <= max_dist, grid_kind="regular_latlon",
    )


def match_2d(lat2d, lon2d, stations_df: pd.DataFrame,
             max_dist: float = MAX_DIST_DEG) -> StationMatch:
    """Match stations to a rotated-pole 2D grid (NAM-12)."""
    lon2d_180 = normalize_grid_lon(np.asarray(lon2d))
    idx0, idx1, dist = nearest_index_2d(
        lat2d, lon2d_180,
        stations_df["lat"].to_numpy(np.float64),
        stations_df["lon"].to_numpy(np.float64),
    )
    return StationMatch(
        stations=stations_df.reset_index(drop=True),
        idx0=idx0, idx1=idx1, dist_deg=dist,
        valid=dist <= max_dist, grid_kind="rotated_pole",
    )


def gather_to_stations(arr3d, match: StationMatch) -> np.ndarray:
    """Gather a ``(time, idx0, idx1)`` array to ``(time, n_sta)``.

    Fancy-indexes the matched cells.  Stations flagged ``valid=False``
    (``dist > max_dist``) are still gathered but should be masked downstream.
    """
    arr3d = np.asarray(arr3d)
    return arr3d[:, match.idx0, match.idx1]


# ---------------------------------------------------------------------------
# Station-level signal writer (shared by Pipeline A and Pipeline B)
# ---------------------------------------------------------------------------

def write_station_signals(
    out_path,
    masks: dict,
    times: np.ndarray,
    match: "StationMatch",
    tech: str,
    *,
    source: str,
    model: str,
    region: str,
    scenario: str,
    source_csv: str,
    pipeline: str,
    supported,
    skipped,
    skipped_reasons: dict,
    max_dist: float,
    activation_mask_on: bool,
    compress_level: int = 4,
) -> None:
    """Write a station-level extreme-signal NetCDF.

    Layout: dims ``(time, station)``; one ``signal_<event>(time, station)``
    ``int8`` per event plus per-station metadata (lon/lat/type/capacity_gw/
    activation_year/match_dist_deg).  Both pipelines write identical files so
    their outputs are directly comparable.
    """
    import xarray as xr  # local import: xarray only needed for writing

    p = Path(out_path) if not isinstance(out_path, Path) else out_path
    p.parent.mkdir(parents=True, exist_ok=True)
    n_sta = len(match)
    sta = match.stations

    data_vars = {}
    for name, arr in masks.items():
        ev = name[len("signal_"):] if name.startswith("signal_") else name
        data_vars[name] = xr.DataArray(
            np.asarray(arr, dtype=np.int8), dims=("time", "station"),
            attrs={"flag_values": "0, 1", "flag_meanings": "false true",
                   "long_name": f"Extreme weather signal: {ev}"})
    data_vars["station_lon"] = xr.DataArray(sta["lon"].to_numpy(np.float32), dims=("station",))
    data_vars["station_lat"] = xr.DataArray(sta["lat"].to_numpy(np.float32), dims=("station",))
    data_vars["station_type"] = xr.DataArray(
        np.full(n_sta, 1 if tech == "wind" else 0, dtype=np.int8), dims=("station",),
        attrs={"flag_values": "0, 1", "flag_meanings": "solar wind"})
    data_vars["capacity_gw"] = xr.DataArray(sta["capacity_gw"].to_numpy(np.float32), dims=("station",))
    data_vars["activation_year"] = xr.DataArray(sta["activation_year"].to_numpy(np.int16), dims=("station",))
    data_vars["match_dist_deg"] = xr.DataArray(match.dist_deg.astype(np.float32), dims=("station",))

    ds = xr.Dataset(data_vars, coords={
        "time": times, "station": np.arange(n_sta, dtype=np.int32)})
    ds.attrs.update({
        "source": source, "model": model, "region": region, "scenario": scenario,
        "source_csv": source_csv, "pipeline": pipeline,
        "grid_resolution": "0.1deg", "match_method": "nearest",
        "max_match_dist_deg": str(max_dist),
        "activation_mask": "on" if activation_mask_on else "off",
        "supported_events": ",".join(supported),
        "skipped_events": ",".join(skipped),
        "skipped_event_reasons": "; ".join(f"{k}: {v}" for k, v in skipped_reasons.items()),
        "n_stations": str(n_sta),
        "threshold_source": "extreme_event_definitions/events",
    })
    encoding = {name: {"zlib": True, "complevel": compress_level, "dtype": "int8"}
                for name in masks}
    ds.to_netcdf(str(p), encoding=encoding)
    ds.close()
