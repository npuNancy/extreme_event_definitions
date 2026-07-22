#!/usr/bin/env python3
"""预计算 ERA5Land 风电/光伏低资源阈值。"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import warnings
from pathlib import Path

os.environ.setdefault("HDF5_USE_FILE_LOCKING", "FALSE")

import h5py
import netCDF4
import numpy as np
import pandas as pd

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from grid_extreme_signals import cf_low_resource  # noqa: E402
from tools.logging_utils import setup_logging  # noqa: E402

logger = logging.getLogger("precompute_low_resource_thresholds")

DEFAULT_OUTPUT_DIR = "outputs/low_resource_thresholds/ERA5Land_2015-2024"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="基于 ERA5Land 2015-2024 CF 预计算低资源 clim/threshold。",
    )
    p.add_argument("--cf_root", default="data/cfs")
    p.add_argument("--output_dir", default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--baseline_years", default="2015-2024")
    p.add_argument("--tech", choices=["wind", "solar", "both"], default="both")
    p.add_argument("--lat_chunk", type=int, default=1)
    p.add_argument("--lon_chunk", type=int, default=360)
    p.add_argument("--compress_level", type=int, default=4)
    p.add_argument("--allow_incomplete", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--dry_run", action="store_true")
    return p


def expected_months(years: str) -> list[tuple[int, int]]:
    y0, y1 = cf_low_resource.parse_years(years)
    return [(y, m) for y in range(y0, y1 + 1) for m in range(1, 13)]


def era5land_cf_dir(cf_root: str | Path, tech: str) -> Path:
    subdir = "CFs_of_solar_ERA5Land" if tech == "solar" else "CFs_of_wind_ERA5Land"
    return Path(cf_root) / subdir


def era5land_cf_var(tech: str) -> str:
    return "solar_cf" if tech == "solar" else "wind_cf"


def threshold_filename(tech: str, baseline_years: str) -> str:
    return f"low_resource_threshold_{tech}_ERA5Land_{baseline_years}.nc"


def find_monthly_files(
    cf_root: str | Path,
    tech: str,
    baseline_years: str,
) -> tuple[list[Path], list[tuple[int, int]]]:
    root = era5land_cf_dir(cf_root, tech)
    files_by_month: dict[tuple[int, int], Path] = {}
    prefix = "solar_cf" if tech == "solar" else "wind_cf"
    for path in root.glob(f"{prefix}_????_??.nc"):
        parts = path.stem.split("_")
        try:
            y, m = int(parts[-2]), int(parts[-1])
        except (ValueError, IndexError):
            continue
        files_by_month[(y, m)] = path

    wanted = expected_months(baseline_years)
    missing = [ym for ym in wanted if ym not in files_by_month]
    files = [files_by_month[ym] for ym in wanted if ym in files_by_month]
    return files, missing


def read_month_time(path: Path) -> pd.DatetimeIndex:
    with cf_low_resource.open_h5(path, "r") as f:
        return cf_low_resource.read_time(f)


def read_static_grid(path: Path) -> tuple[np.ndarray, np.ndarray, tuple[int, int, int]]:
    with cf_low_resource.open_h5(path, "r") as f:
        lat = f["lat"][:].astype(np.float64)
        lon = f["lon"][:].astype(np.float64)
        var_name = "solar_cf" if "solar_cf" in f else "wind_cf"
        shape = tuple(int(x) for x in f[var_name].shape)
    return lat, lon, shape


def load_block(
    files: list[Path],
    tech: str,
    lat_slice: slice,
    lon_slice: slice,
) -> tuple[pd.DatetimeIndex, np.ndarray]:
    times: list[pd.DatetimeIndex] = []
    blocks: list[np.ndarray] = []
    var_name = era5land_cf_var(tech)
    for path in files:
        with cf_low_resource.open_h5(path, "r") as f:
            times.append(cf_low_resource.read_time(f))
            blocks.append(f[var_name][:, lat_slice, lon_slice].astype(np.float32))
    time = pd.DatetimeIndex(np.concatenate([t.values for t in times]))
    data = np.concatenate(blocks, axis=0)
    n_time = data.shape[0]
    return time, data.reshape(n_time, -1)


def validate_time_axis(times: pd.DatetimeIndex, allow_incomplete: bool) -> None:
    if len(times) < 2:
        raise ValueError("ERA5Land 时间轴长度不足")
    if not times.is_monotonic_increasing:
        raise ValueError("ERA5Land 时间轴不是递增顺序")
    ns = times.to_numpy(dtype="datetime64[ns]").astype("int64")
    diffs = np.diff(ns).astype(np.float64) / 3.6e12
    bad = ~np.isclose(diffs, 1.0, atol=1e-9)
    if np.any(bad):
        msg = f"ERA5Land 时间轴存在非 1h 间隔，例如 {times[np.where(bad)[0][0]]}"
        if allow_incomplete:
            logger.warning(msg)
        else:
            raise ValueError(msg)


def compute_threshold_block(
    cf_block: np.ndarray,
    times: pd.DatetimeIndex,
    *,
    pct: float = 5.0,
    window_steps: int = 24,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    roll = (
        pd.DataFrame(cf_block)
        .rolling(window_steps, center=True, min_periods=window_steps)
        .mean()
        .to_numpy(dtype=np.float32)
    )
    months = times.month.to_numpy() - 1
    hours = times.hour.to_numpy()
    n_cell = cf_block.shape[1]
    clim = np.full((12, 24, n_cell), np.nan, dtype=np.float32)
    for m in range(12):
        for h in range(24):
            sel = (months == m) & (hours == h)
            if np.any(sel):
                with np.errstate(all="ignore"), warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    clim[m, h] = np.nanmean(roll[sel], axis=0)

    anom = roll - clim[months, hours]
    valid_count = np.isfinite(anom).sum(axis=0).astype(np.int32)
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        threshold = np.nanpercentile(
            np.where(np.isfinite(anom), anom, np.nan),
            pct,
            axis=0,
        ).astype(np.float32)
    return clim, threshold, valid_count


def create_output(
    path: Path,
    *,
    tech: str,
    lat: np.ndarray,
    lon: np.ndarray,
    files: list[Path],
    baseline_years: str,
    baseline_effective: str,
    compress_level: int,
) -> netCDF4.Dataset:
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = netCDF4.Dataset(path, "w", format="NETCDF4")
    ds.createDimension("month", 12)
    ds.createDimension("hour", 24)
    ds.createDimension("lat", lat.size)
    ds.createDimension("lon", lon.size)

    month = ds.createVariable("month", "i2", ("month",))
    hour = ds.createVariable("hour", "i2", ("hour",))
    lat_v = ds.createVariable("lat", "f4", ("lat",), zlib=True, complevel=compress_level)
    lon_v = ds.createVariable("lon", "f4", ("lon",), zlib=True, complevel=compress_level)
    month[:] = np.arange(1, 13, dtype=np.int16)
    hour[:] = np.arange(24, dtype=np.int16)
    lat_v[:] = lat.astype(np.float32)
    lon_v[:] = lon.astype(np.float32)
    lat_v.units = "degrees_north"
    lon_v.units = "degrees_east"

    fill = np.float32(np.nan)
    clim = ds.createVariable(
        "clim", "f4", ("month", "hour", "lat", "lon"),
        zlib=True, complevel=compress_level, fill_value=fill,
        chunksizes=(1, 24, 1, min(360, lon.size)),
    )
    threshold = ds.createVariable(
        "threshold", "f4", ("lat", "lon"),
        zlib=True, complevel=compress_level, fill_value=fill,
        chunksizes=(1, min(360, lon.size)),
    )
    valid_count = ds.createVariable(
        "valid_count", "i4", ("lat", "lon"),
        zlib=True, complevel=compress_level,
        chunksizes=(1, min(360, lon.size)),
    )
    clim.long_name = "ERA5Land month-hour capacity factor rolling-mean climatology"
    threshold.long_name = "ERA5Land low-resource anomaly percentile threshold"
    valid_count.long_name = "Finite anomaly count used for threshold"

    ds.tech = tech
    ds.threshold_source = "ERA5Land"
    ds.threshold_source_files = ",".join(str(p) for p in files)
    ds.baseline_years_requested = baseline_years
    ds.baseline_years_effective = baseline_effective
    ds.resource_variable = era5land_cf_var(tech)
    ds.resource_units = "1"
    ds.window_hours = "24"
    ds.window_steps = "24"
    ds.timestep_hours = "1"
    ds.percentile = "5"
    ds.climatology = "month_hour"
    ds.created_by = "scripts/precompute_low_resource_thresholds.py"
    return ds


def effective_years(files: list[Path]) -> str:
    years = sorted({int(p.stem.split("_")[-2]) for p in files})
    return f"{years[0]}-{years[-1]}" if years else ""


def process_tech(args, tech: str) -> Path | None:
    files, missing = find_monthly_files(args.cf_root, tech, args.baseline_years)
    if missing:
        msg = "缺少 ERA5Land CF 月文件：" + ", ".join(f"{y}-{m:02d}" for y, m in missing)
        if not args.allow_incomplete:
            raise FileNotFoundError(f"{tech}: {msg}")
        logger.warning("[%s] %s；按 --allow_incomplete 继续", tech, msg)
    if not files:
        raise FileNotFoundError(f"{tech}: 未找到 ERA5Land CF 文件")

    lat, lon, shape = read_static_grid(files[0])
    out_path = Path(args.output_dir) / threshold_filename(tech, args.baseline_years)
    logger.info("[%s] 月文件=%d 网格=%dx%d 输出=%s", tech, len(files), lat.size, lon.size, out_path)
    if args.dry_run:
        return out_path
    if out_path.exists() and not args.overwrite:
        logger.info("[%s] 输出已存在，跳过：%s", tech, out_path)
        return out_path
    if shape[1] != lat.size or shape[2] != lon.size:
        raise ValueError(f"{files[0]}: CF 变量形状与 lat/lon 不一致：{shape}")

    ds = create_output(
        out_path,
        tech=tech,
        lat=lat,
        lon=lon,
        files=files,
        baseline_years=args.baseline_years,
        baseline_effective=effective_years(files),
        compress_level=args.compress_level,
    )
    try:
        first_time: pd.DatetimeIndex | None = None
        for i0 in range(0, lat.size, args.lat_chunk):
            i1 = min(i0 + args.lat_chunk, lat.size)
            for j0 in range(0, lon.size, args.lon_chunk):
                j1 = min(j0 + args.lon_chunk, lon.size)
                times, block = load_block(files, tech, slice(i0, i1), slice(j0, j1))
                if first_time is None:
                    validate_time_axis(times, args.allow_incomplete)
                    first_time = times
                clim, threshold, valid_count = compute_threshold_block(block, times)
                n_lat = i1 - i0
                n_lon = j1 - j0
                ds["clim"][:, :, i0:i1, j0:j1] = clim.reshape(12, 24, n_lat, n_lon)
                ds["threshold"][i0:i1, j0:j1] = threshold.reshape(n_lat, n_lon)
                ds["valid_count"][i0:i1, j0:j1] = valid_count.reshape(n_lat, n_lon)
                logger.info("[%s] 写入 lat %d:%d lon %d:%d", tech, i0, i1, j0, j1)
    finally:
        ds.close()
    return out_path


def main() -> None:
    args = build_parser().parse_args()
    setup_logging("precompute_low_resource_thresholds")
    if args.lat_chunk < 1 or args.lon_chunk < 1:
        raise ValueError("--lat_chunk 和 --lon_chunk 必须为正整数")
    techs = ["wind", "solar"] if args.tech == "both" else [args.tech]
    for tech in techs:
        process_tech(args, tech)


if __name__ == "__main__":
    main()
