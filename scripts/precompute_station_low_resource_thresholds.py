#!/usr/bin/env python3
"""预计算 SSP 场站稀疏 ERA5Land 低资源阈值。"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

os.environ.setdefault("HDF5_USE_FILE_LOCKING", "FALSE")

import netCDF4
import numpy as np
import pandas as pd

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from grid_extreme_signals import cf_low_resource  # noqa: E402
from grid_extreme_signals import station_match as sm  # noqa: E402
from scripts import precompute_low_resource_thresholds as full_precompute  # noqa: E402
from tools.logging_utils import setup_logging  # noqa: E402

logger = logging.getLogger("precompute_station_low_resource_thresholds")

DEFAULT_OUTPUT_DIR = "outputs/low_resource_thresholds/sparse_station_ERA5Land_2015-2025"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="基于 ERA5Land CF 预计算 SSP 场站稀疏低资源 clim/threshold。",
    )
    p.add_argument("--cf_root", default="data/cfs")
    p.add_argument("--stations_csv", required=True)
    p.add_argument("--scenario", default=None,
                   help="ssp126/ssp245/ssp585；省略时从 stations_csv 文件名推断。")
    p.add_argument("--output_dir", default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--baseline_years", default="2015-2025")
    p.add_argument("--tech", choices=["wind", "solar", "both"], default="both")
    p.add_argument(
        "--threshold_interp",
        choices=["nearest_valid", "bilinear"],
        default="nearest_valid",
        help="ERA5Land CF 抽取到场站的方式：nearest_valid=最近有效格点（默认），bilinear=四点双线性。",
    )
    p.add_argument("--station_chunk", type=int, default=128)
    p.add_argument("--compress_level", type=int, default=4)
    p.add_argument("--allow_incomplete", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--dry_run", action="store_true")
    return p


def _station_type_code(tech: str) -> int:
    return 1 if tech == "wind" else 0


def _prepare_stations(stations_csv: str | Path, tech: str) -> pd.DataFrame:
    df = sm.load_stations(stations_csv)
    df = df[df["type"] == tech].copy()
    if df.empty:
        return pd.DataFrame(columns=["lon", "lat", "type", "activation_year", "capacity_gw"])
    return (
        df.sort_values("year")
        .groupby(["lon", "lat", "type"], as_index=False)
        .agg({"year": "min", "capacity_gw": "first"})
        .rename(columns={"year": "activation_year"})
        .reset_index(drop=True)
    )


def _regular_land_mask(lat: np.ndarray, lon: np.ndarray, chunk_rows: int = 128) -> np.ndarray | None:
    """用可选陆地掩膜识别 ERA5Land 陆地点；缺少依赖时返回 None。"""
    try:
        from global_land_mask import globe
    except ImportError:
        return None

    lat = np.asarray(lat, dtype=np.float64)
    lon_180 = sm.lon_to_180(lon)
    mask = np.zeros((lat.size, lon_180.size), dtype=bool)
    lon_row = lon_180[None, :]
    for r0 in range(0, lat.size, chunk_rows):
        r1 = min(r0 + chunk_rows, lat.size)
        lat_block = np.repeat(lat[r0:r1, None], lon_180.size, axis=1)
        lon_block = np.repeat(lon_row, r1 - r0, axis=0)
        mask[r0:r1, :] = globe.is_land(lat_block, lon_block)
    return mask


def _read_valid_cf_mask(
    path: Path,
    tech: str,
    lat: np.ndarray,
    lon: np.ndarray,
    probe_steps: int = 24,
) -> np.ndarray:
    """读取 ERA5Land CF 有效格点掩膜，用于避开海上缺测或填 0 格点。"""
    var_name = full_precompute.era5land_cf_var(tech)
    with cf_low_resource.open_h5(path, "r") as f:
        d = f[var_name]
        n_probe = min(int(probe_steps), int(d.shape[0]))
        valid = np.zeros(tuple(int(x) for x in d.shape[1:]), dtype=bool)
        positive = np.zeros_like(valid)
        for t in range(n_probe):
            arr = d[t, :, :]
            finite = np.isfinite(arr)
            valid |= finite
            positive |= finite & (arr > 0.0)
    land = _regular_land_mask(lat, lon)
    if land is not None:
        valid &= (land | positive)
    else:
        logger.warning(
            "未找到 global_land_mask，使用前 %d 个时次内出现正 CF 的格点估计 ERA5Land 有效格点",
            n_probe,
        )
        valid = positive
    if not np.any(valid):
        raise ValueError(f"{path}: {var_name} 前 {n_probe} 个时次均无有效格点")
    return valid


def _read_month_station_cf(
    path: Path,
    tech: str,
    lat_idx: np.ndarray,
    lon_idx: np.ndarray,
    weights: np.ndarray,
) -> tuple[pd.DatetimeIndex, np.ndarray]:
    """读取一个 ERA5Land 月文件的加权场站 CF。"""
    var_name = full_precompute.era5land_cf_var(tech)
    n_station = lat_idx.shape[0]
    with cf_low_resource.open_h5(path, "r") as f:
        times = cf_low_resource.read_time(f)
        d = f[var_name]
        out = np.zeros((d.shape[0], n_station), dtype=np.float32)
        for corner in range(4):
            corner_weight = weights[:, corner]
            if not np.any(corner_weight != 0.0):
                continue
            corner_values = np.empty((d.shape[0], n_station), dtype=np.float32)
            for lat_i in np.unique(lat_idx[:, corner]):
                pos = np.where(lat_idx[:, corner] == lat_i)[0]
                unique_lon, inv = np.unique(lon_idx[pos, corner], return_inverse=True)
                slab = d[:, int(lat_i), unique_lon].astype(np.float32)
                corner_values[:, pos] = slab[:, inv]
            out += corner_values * corner_weight[None, :]
    return times, out


def load_station_block(
    files: list[Path],
    tech: str,
    match: cf_low_resource.FourPointMatch,
    station_slice: slice,
) -> tuple[pd.DatetimeIndex, np.ndarray]:
    """读取一组场站在所有 ERA5Land 月文件上的加权 CF。"""
    times: list[pd.DatetimeIndex] = []
    blocks: list[np.ndarray] = []
    lat_idx = match.lat_idx[station_slice]
    lon_idx = match.lon_idx[station_slice]
    weights = match.weights[station_slice].astype(np.float32)
    for path in files:
        t, arr = _read_month_station_cf(path, tech, lat_idx, lon_idx, weights)
        times.append(t)
        blocks.append(arr)
    time = pd.DatetimeIndex(np.concatenate([t.values for t in times]))
    data = np.concatenate(blocks, axis=0)
    return time, data


def create_sparse_output(
    path: Path,
    *,
    scenario: str,
    tech: str,
    stations: pd.DataFrame,
    match: cf_low_resource.FourPointMatch,
    files: list[Path],
    baseline_years: str,
    baseline_effective: str,
    interpolation_method: str,
    corner_order: str,
    compress_level: int,
) -> netCDF4.Dataset:
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = netCDF4.Dataset(path, "w", format="NETCDF4")
    n_station = len(stations)
    ds.createDimension("station", n_station)
    ds.createDimension("corner", 4)
    ds.createDimension("month", 12)
    ds.createDimension("hour", 24)

    ds.createVariable("station", "i4", ("station",))[:] = np.arange(n_station, dtype=np.int32)
    ds.createVariable("corner", "i1", ("corner",))[:] = np.arange(4, dtype=np.int8)
    ds.createVariable("month", "i2", ("month",))[:] = np.arange(1, 13, dtype=np.int16)
    ds.createVariable("hour", "i2", ("hour",))[:] = np.arange(24, dtype=np.int16)

    lon = ds.createVariable("station_lon", "f4", ("station",), zlib=True,
                            complevel=compress_level)
    lat = ds.createVariable("station_lat", "f4", ("station",), zlib=True,
                            complevel=compress_level)
    stype = ds.createVariable("station_type", "i1", ("station",), zlib=True,
                              complevel=compress_level)
    capacity = ds.createVariable("capacity_gw", "f4", ("station",), zlib=True,
                                 complevel=compress_level)
    activation = ds.createVariable("activation_year", "i2", ("station",), zlib=True,
                                   complevel=compress_level)
    era5_lat_idx = ds.createVariable("era5_lat_idx", "i4", ("station", "corner"),
                                     zlib=True, complevel=compress_level)
    era5_lon_idx = ds.createVariable("era5_lon_idx", "i4", ("station", "corner"),
                                     zlib=True, complevel=compress_level)
    era5_lat = ds.createVariable("era5_lat", "f4", ("station", "corner"),
                                 zlib=True, complevel=compress_level)
    era5_lon = ds.createVariable("era5_lon", "f4", ("station", "corner"),
                                 zlib=True, complevel=compress_level)
    weight = ds.createVariable("weight", "f4", ("station", "corner"),
                               zlib=True, complevel=compress_level)

    lon[:] = stations["lon"].to_numpy(np.float32)
    lat[:] = stations["lat"].to_numpy(np.float32)
    stype[:] = np.full(n_station, _station_type_code(tech), dtype=np.int8)
    capacity[:] = stations["capacity_gw"].to_numpy(np.float32)
    activation[:] = stations["activation_year"].to_numpy(np.int16)
    era5_lat_idx[:, :] = match.lat_idx.astype(np.int32)
    era5_lon_idx[:, :] = match.lon_idx.astype(np.int32)
    era5_lat[:, :] = match.corner_lat.astype(np.float32)
    era5_lon[:, :] = match.corner_lon.astype(np.float32)
    weight[:, :] = match.weights.astype(np.float32)

    fill = np.float32(np.nan)
    clim = ds.createVariable(
        "clim", "f4", ("month", "hour", "station"),
        zlib=True, complevel=compress_level, fill_value=fill,
        chunksizes=(1, 24, min(256, max(1, n_station))),
    )
    threshold = ds.createVariable(
        "threshold", "f4", ("station",),
        zlib=True, complevel=compress_level, fill_value=fill,
        chunksizes=(min(1024, max(1, n_station)),),
    )
    valid_count = ds.createVariable(
        "valid_count", "i4", ("station",),
        zlib=True, complevel=compress_level,
        chunksizes=(min(1024, max(1, n_station)),),
    )
    clim.long_name = "ERA5Land station-level month-hour capacity factor rolling-mean climatology"
    threshold.long_name = "ERA5Land station-level low-resource anomaly percentile threshold"
    valid_count.long_name = "Finite anomaly count used for threshold"

    ds.threshold_kind = "sparse_station"
    ds.threshold_source = "ERA5Land"
    ds.threshold_source_files = ",".join(str(p) for p in files)
    ds.scenario = scenario
    ds.tech = tech
    ds.baseline_years_requested = baseline_years
    ds.baseline_years_effective = baseline_effective
    ds.resource_variable = full_precompute.era5land_cf_var(tech)
    ds.resource_units = "1"
    ds.interpolation_method = interpolation_method
    ds.corner_order = corner_order
    ds.window_hours = "24"
    ds.window_steps = "24"
    ds.timestep_hours = "1"
    ds.percentile = "5"
    ds.climatology = "month_hour"
    ds.created_by = "scripts/precompute_station_low_resource_thresholds.py"
    return ds


def process_tech(args, scenario: str, tech: str) -> Path | None:
    stations = _prepare_stations(args.stations_csv, tech)
    if stations.empty:
        logger.info("[%s/%s] 无场站，跳过", scenario, tech)
        return None

    files, missing = full_precompute.find_monthly_files(
        args.cf_root, tech, args.baseline_years
    )
    if missing:
        msg = "缺少 ERA5Land CF 月文件：" + ", ".join(f"{y}-{m:02d}" for y, m in missing)
        if not args.allow_incomplete:
            raise FileNotFoundError(f"{tech}: {msg}")
        logger.warning("[%s/%s] %s；按 --allow_incomplete 继续", scenario, tech, msg)
    if not files:
        raise FileNotFoundError(f"{tech}: 未找到 ERA5Land CF 文件")

    lat, lon, shape = full_precompute.read_static_grid(files[0])
    out_path = cf_low_resource.sparse_threshold_file_for_scenario_tech(
        args.output_dir, scenario, tech, args.baseline_years
    )
    logger.info(
        "[%s/%s] 场站=%d 月文件=%d ERA5Land网格=%dx%d 输出=%s",
        scenario, tech, len(stations), len(files), lat.size, lon.size, out_path,
    )
    if args.dry_run:
        return out_path
    if out_path.exists() and not args.overwrite:
        logger.info("[%s/%s] 输出已存在，跳过：%s", scenario, tech, out_path)
        return out_path
    if shape[1] != lat.size or shape[2] != lon.size:
        raise ValueError(f"{files[0]}: CF 变量形状与 lat/lon 不一致：{shape}")

    station_lats = stations["lat"].to_numpy(np.float64)
    station_lons = stations["lon"].to_numpy(np.float64)
    if args.threshold_interp == "bilinear":
        match = cf_low_resource.bilinear_four_point_regular(
            lat,
            lon,
            station_lats,
            station_lons,
        )
        interpolation_method = "bilinear_4point"
        corner_order = "southwest,southeast,northwest,northeast"
    else:
        valid_mask = _read_valid_cf_mask(files[0], tech, lat, lon)
        nearest_lat, nearest_lon, _ = sm.nearest_index_regular(
            lat,
            sm.normalize_grid_lon(lon),
            station_lats,
            sm.lon_to_180(station_lons),
        )
        match = cf_low_resource.nearest_valid_point_regular(
            lat,
            lon,
            valid_mask,
            station_lats,
            station_lons,
        )
        replaced = np.count_nonzero(
            (match.lat_idx[:, 0] != nearest_lat) | (match.lon_idx[:, 0] != nearest_lon)
        )
        logger.info(
            "[%s/%s] 最近有效格点匹配：有效网格=%d/%d，替换最近邻无效格点=%d",
            scenario, tech, int(valid_mask.sum()), int(valid_mask.size), int(replaced),
        )
        interpolation_method = "nearest_valid"
        corner_order = "nearest_valid,unused,unused,unused"
    weight_sum = match.weights.sum(axis=1)
    if not np.allclose(weight_sum, 1.0, atol=1e-5):
        raise ValueError("场站匹配权重和不等于 1")

    ds = create_sparse_output(
        out_path,
        scenario=scenario,
        tech=tech,
        stations=stations,
        match=match,
        files=files,
        baseline_years=args.baseline_years,
        baseline_effective=full_precompute.effective_years(files),
        interpolation_method=interpolation_method,
        corner_order=corner_order,
        compress_level=args.compress_level,
    )
    try:
        first_time: pd.DatetimeIndex | None = None
        n_station = len(stations)
        for s0 in range(0, n_station, args.station_chunk):
            s1 = min(s0 + args.station_chunk, n_station)
            times, block = load_station_block(files, tech, match, slice(s0, s1))
            if first_time is None:
                full_precompute.validate_time_axis(times, args.allow_incomplete)
                first_time = times
            clim, threshold, valid_count = full_precompute.compute_threshold_block(block, times)
            ds["clim"][:, :, s0:s1] = clim
            ds["threshold"][s0:s1] = threshold
            ds["valid_count"][s0:s1] = valid_count
            logger.info("[%s/%s] 写入 station %d:%d", scenario, tech, s0, s1)
    finally:
        ds.close()
    return out_path


def main() -> None:
    args = build_parser().parse_args()
    setup_logging("precompute_station_low_resource_thresholds")
    if args.station_chunk < 1:
        raise ValueError("--station_chunk 必须为正整数")
    scenario = args.scenario or sm.infer_scenario_from_csv(args.stations_csv)
    techs = ["wind", "solar"] if args.tech == "both" else [args.tech]
    for tech in techs:
        process_tech(args, scenario, tech)


if __name__ == "__main__":
    main()
