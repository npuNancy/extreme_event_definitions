"""Global regular-grid to station matching used by the patchify workflow."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

MAX_DIST_DEG = 0.15
STATION_ID_SCHEME = "sha1-20:scenario|tech|lon4|lat4"
STATION_ID_COORDINATE_DECIMALS = 4
STATION_ID_PATTERN = re.compile(r"^[0-9a-f]{20}$")
VALID_SCENARIOS = frozenset({"ssp126", "ssp245", "ssp585"})
VALID_TECHS = frozenset({"wind", "solar"})

def lon_to_360(lon):
    return np.asarray(lon, dtype=np.float64) % 360.0

def lon_to_180(lon):
    return ((np.asarray(lon, dtype=np.float64) + 180.0) % 360.0) - 180.0

def normalize_grid_lon(lon):
    values = np.asarray(lon, dtype=np.float64)
    return lon_to_180(values) if values.size and np.nanmax(values) > 180.0 else values

def _validate_context(scenario: str, tech: str) -> None:
    if scenario not in VALID_SCENARIOS: raise ValueError(f"不支持的情景 {scenario!r}")
    if tech not in VALID_TECHS: raise ValueError(f"不支持的技术类型 {tech!r}")

def station_id(scenario: str, tech: str, lon: float, lat: float) -> str:
    _validate_context(scenario, tech)
    lon, lat = float(lon_to_180(lon)), float(lat)
    if not np.isfinite(lon) or not np.isfinite(lat) or not -90 <= lat <= 90:
        raise ValueError("场站经纬度必须为有限值且纬度位于 [-90, 90]")
    return hashlib.sha1(f"{scenario}|{tech}|{lon:.4f}|{lat:.4f}".encode()).hexdigest()[:20]

def station_ids(scenario: str, tech: str, lons, lats) -> np.ndarray:
    _validate_context(scenario, tech)
    lons, lats = np.asarray(lons, float), np.asarray(lats, float)
    if lons.ndim != 1 or lats.ndim != 1 or lons.shape != lats.shape:
        raise ValueError("场站经纬度必须为形状相同的一维数组")
    ids = np.asarray([station_id(scenario, tech, x, y) for x, y in zip(lons, lats)], dtype=str)
    if len(set(ids.tolist())) != len(ids): raise ValueError("文件内 station_id 重复或发生哈希碰撞")
    return ids

def validate_station_ids(ids, scenario: str, tech: str, lons, lats) -> np.ndarray:
    values = np.asarray(ids).astype(str)
    if values.ndim != 1 or any(STATION_ID_PATTERN.fullmatch(v) is None for v in values): raise ValueError("station_id 格式错误")
    expected = station_ids(scenario, tech, lons, lats)
    if values.shape != expected.shape or not np.array_equal(values, expected): raise ValueError("station_id 与场站坐标重算结果不一致")
    return values

def load_stations(csv_path: str | Path) -> pd.DataFrame:
    """读取全局 SSP 场站表，统一经度到 [-180, 180)。"""
    df = pd.read_csv(csv_path)
    required = {"year", "type", "lon", "lat", "capacity_gw"}
    missing = required - set(df.columns)
    if missing: raise ValueError(f"场站 CSV 缺少列: {sorted(missing)}")
    df = df.copy()
    df["lon"] = lon_to_180(pd.to_numeric(df["lon"], errors="coerce"))
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df["capacity_gw"] = pd.to_numeric(df["capacity_gw"], errors="coerce").fillna(0.0)
    return df.dropna(subset=["lon", "lat", "year"]).reset_index(drop=True)

@dataclass
class StationSpatialWeights:
    stations: pd.DataFrame
    idx0: np.ndarray
    idx1: np.ndarray
    weight: np.ndarray
    dist_deg: np.ndarray
    valid: np.ndarray
    grid_kind: str = "regular_latlon"
    method: str = "nearest"
    def __len__(self): return len(self.stations)

def _nearest(grid_lat, grid_lon, sta_lat, sta_lon):
    lat, lon = np.asarray(grid_lat, float), normalize_grid_lon(grid_lon)
    sta_lat, sta_lon = np.asarray(sta_lat, float), lon_to_180(sta_lon)
    i0 = np.abs(lat[:, None] - sta_lat[None, :]).argmin(axis=0)
    dlon = np.abs(((lon[:, None] - sta_lon[None, :] + 180) % 360) - 180)
    i1 = dlon.argmin(axis=0)
    dist = np.maximum(np.abs(lat[i0] - sta_lat), dlon[i1, np.arange(len(sta_lat))])
    return i0, i1, dist

def _bilinear_indices(grid_lat, grid_lon, sta_lat, sta_lon):
    lat, lon = np.asarray(grid_lat, float), lon_to_360(grid_lon)
    order_y, order_x = np.argsort(lat), np.argsort(lon); ys, xs = lat[order_y], lon[order_x]
    out0 = np.empty((len(sta_lat), 4), int); out1 = np.empty_like(out0); weights = np.empty_like(out0, float)
    for k, (y, x) in enumerate(zip(np.asarray(sta_lat, float), lon_to_360(sta_lon))):
        py = max(1, min(int(np.searchsorted(ys, y, side="right")), len(ys)-1)); px = int(np.searchsorted(xs, x, side="right")) % len(xs)
        a, b, c, d = py-1, py, (px-1) % len(xs), px % len(xs)
        wy = 0.0 if ys[b] == ys[a] else float(np.clip((y-ys[a])/(ys[b]-ys[a]), 0, 1)); dx = (xs[d]-xs[c]) % 360.0
        wx = 0.0 if dx == 0 else float(np.clip(((x-xs[c]) % 360.0)/dx, 0, 1))
        out0[k] = [order_y[a], order_y[a], order_y[b], order_y[b]]; out1[k] = [order_x[c], order_x[d], order_x[c], order_x[d]]
        weights[k] = [(1-wy)*(1-wx), (1-wy)*wx, wy*(1-wx), wy*wx]
    return out0, out1, weights.astype(np.float32)

def match_regular_weighted(grid_lat, grid_lon, stations_df: pd.DataFrame, method="nearest", max_dist=MAX_DIST_DEG):
    method = method.lower()
    if method not in {"nearest", "bilinear"}: raise ValueError("空间插值方法必须是 nearest 或 bilinear")
    n0, n1, dist = _nearest(grid_lat, grid_lon, stations_df.lat, stations_df.lon)
    if method == "nearest": idx0, idx1, weight = n0[:, None], n1[:, None], np.ones((len(stations_df), 1), np.float32)
    else: idx0, idx1, weight = _bilinear_indices(grid_lat, grid_lon, stations_df.lat, stations_df.lon)
    return StationSpatialWeights(stations_df.reset_index(drop=True), idx0, idx1, weight, dist, dist <= max_dist, method=method)

def gather_to_stations_weighted(arr3d, match: StationSpatialWeights) -> np.ndarray:
    values = np.asarray(arr3d)[:, match.idx0, match.idx1].astype(np.float32); weights = match.weight[None, :, :]
    finite = np.isfinite(values); denom = np.where(finite, weights, 0).sum(axis=2); out = np.full(values.shape[:2], np.nan, np.float32)
    np.divide(np.where(finite, values * weights, 0).sum(axis=2), denom, out=out, where=denom > 0)
    return out

def write_station_signals(out_path, masks, times, match, tech, *, source, model, patch_id, scenario,
                          source_csv, pipeline, supported, skipped, skipped_reasons, max_dist,
                          activation_mask_on, compress_level=4, attrs_extra=None):
    import xarray as xr
    path = Path(out_path); path.parent.mkdir(parents=True, exist_ok=True); sta = match.stations; n = len(sta)
    ids = validate_station_ids(station_ids(scenario, tech, sta.lon, sta.lat), scenario, tech, sta.lon, sta.lat)
    data = {name: (("time", "station"), np.asarray(values, np.int8), {"flag_values": "0, 1", "flag_meanings": "false true"}) for name, values in masks.items()}
    ds = xr.Dataset(data, coords={"time": times, "station": np.arange(n, dtype=np.int32), "station_id": ("station", ids),
        "lon": ("station", sta.lon.to_numpy(np.float32)), "lat": ("station", sta.lat.to_numpy(np.float32)),
        "capacity_gw": ("station", sta.capacity_gw.to_numpy(np.float32)), "activation_year": ("station", sta.activation_year.to_numpy(np.int16)),
        "match_dist_deg": ("station", match.dist_deg.astype(np.float32))})
    ds.attrs.update(source=source, model=model, patch_id=patch_id, scenario=scenario, tech=tech, source_csv=source_csv,
                    pipeline=pipeline, grid_resolution="0.1deg", match_method=match.method, max_match_dist_deg=str(max_dist),
                    activation_mask="on" if activation_mask_on else "off", supported_events=",".join(supported), skipped_events=",".join(skipped),
                    skipped_event_reasons="; ".join(f"{k}: {v}" for k, v in skipped_reasons.items()), n_stations=str(n),
                    station_id_scheme=STATION_ID_SCHEME, station_id_coordinate_decimals=STATION_ID_COORDINATE_DECIMALS,
                    threshold_source="extreme_event_definitions/events")
    if attrs_extra: ds.attrs.update(attrs_extra)
    ds.to_netcdf(path, encoding={name: {"zlib": True, "complevel": compress_level, "dtype": "i1"} for name in masks}); ds.close()
