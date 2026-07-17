#!/usr/bin/env python3
"""步骤 E2a：按指定年月范围抽取并集场站 ERA5Land CF 缓存。"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from grid_extreme_signals import cf_low_resource
from scripts import precompute_low_resource_thresholds as full_precompute
from scripts import precompute_station_low_resource_thresholds as sparse_precompute
from scripts.hpc_step1_common import (
    atomic_path,
    create_station_cache,
    require_output_available,
)
from tools.logging_utils import setup_entry_logging

logger = logging.getLogger("step1_split_E2a")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="抽取指定时间块的 union station CF cache。")
    parser.add_argument(
        "--cf_root", default=os.path.expandvars("$HOME/data/cfs")
    )
    parser.add_argument("--union_stations_csv", required=True)
    parser.add_argument("--tech", choices=["wind", "solar"], required=True)
    parser.add_argument("--year_month_start", required=True)
    parser.add_argument("--year_month_end", required=True)
    parser.add_argument(
        "--threshold_interp",
        choices=["nearest_valid", "bilinear"],
        default="nearest_valid",
    )
    parser.add_argument(
        "--output_dir",
        default="outputs/cache/era5land_union_station_cf/time_chunks",
    )
    parser.add_argument("--compress_level", type=int, default=1)
    parser.add_argument("--cache_time_chunk", type=int, default=None)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def _month_files(
    cf_root: str | Path,
    tech: str,
    start: str,
    end: str,
) -> tuple[list[Path], pd.PeriodIndex]:
    months = pd.period_range(start, end, freq="M")
    if months.empty:
        raise ValueError("年月范围不能为空")
    root = full_precompute.era5land_cf_dir(cf_root, tech)
    prefix = "wind_cf" if tech == "wind" else "solar_cf"
    files = [root / f"{prefix}_{month.year}_{month.month:02d}.nc" for month in months]
    missing = [path for path in files if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"缺少 ERA5Land CF 文件：{missing[0]}")
    return files, months


def output_path(args: argparse.Namespace) -> Path:
    chunk_id = (
        args.year_month_start.replace("-", "_")
        if args.year_month_start == args.year_month_end
        else f"{args.year_month_start.replace('-', '_')}_{args.year_month_end.replace('-', '_')}"
    )
    return Path(args.output_dir) / f"station_cf_union_{args.tech}_{chunk_id}.nc"


def run(args: argparse.Namespace) -> Path:
    files, months = _month_files(
        args.cf_root, args.tech, args.year_month_start, args.year_month_end
    )
    out_path = output_path(args)
    require_output_available(out_path, args.overwrite)
    union = pd.read_csv(args.union_stations_csv)
    required = {"union_station_index", "lon", "lat", "type"}
    if missing := required.difference(union.columns):
        raise ValueError(f"并集场站文件缺少列：{sorted(missing)}")
    stations = union[union["type"] == args.tech].copy().reset_index(drop=True)
    if stations.empty:
        raise ValueError(f"并集场站中没有 {args.tech} 场站")
    stations["activation_year"] = 0
    stations["capacity_gw"] = np.nan

    lat, lon, shape = full_precompute.read_static_grid(files[0])
    if shape[1:] != (lat.size, lon.size):
        raise ValueError(f"{files[0]}: CF 网格形状与 lat/lon 不一致")
    match_args = SimpleNamespace(threshold_interp=args.threshold_interp)
    match = sparse_precompute._compute_station_match(
        match_args,
        scenario="union",
        tech=args.tech,
        lat=lat,
        lon=lon,
        stations=stations,
        files=files,
    )
    if not np.allclose(match.weights.sum(axis=1), 1.0, atol=1e-5):
        raise ValueError("场站匹配权重和不等于 1")
    all_times = sparse_precompute._read_all_times(files)
    full_precompute.validate_time_axis(all_times, allow_incomplete=False)

    tmp = atomic_path(out_path)
    if tmp.exists():
        tmp.unlink()
    ds = None
    try:
        ds = create_station_cache(
            tmp,
            n_time=len(all_times),
            n_station=len(stations),
            compress_level=args.compress_level,
        )
        ds["time"][:] = sparse_precompute._time_seconds(all_times)
        ds["union_station_index"][:] = stations["union_station_index"].to_numpy(np.int32)
        ds["station_lon"][:] = stations["lon"].to_numpy(np.float32)
        ds["station_lat"][:] = stations["lat"].to_numpy(np.float32)
        ds["station_type"][:] = np.full(
            len(stations), sparse_precompute._station_type_code(args.tech), dtype=np.int8
        )
        ds["era5_lat_idx"][:] = match.lat_idx.astype(np.int32)
        ds["era5_lon_idx"][:] = match.lon_idx.astype(np.int32)
        ds["era5_lat"][:] = match.corner_lat.astype(np.float32)
        ds["era5_lon"][:] = match.corner_lon.astype(np.float32)
        ds["weight"][:] = match.weights.astype(np.float32)

        var_name = full_precompute.era5land_cf_var(args.tech)
        global_t0 = 0
        for file_index, path in enumerate(files, start=1):
            with cf_low_resource.open_h5(path, "r") as source:
                data = source[var_name]
                native_chunk = int(data.chunks[0]) if data.chunks else int(data.shape[0])
                time_chunk = args.cache_time_chunk or native_chunk
                if time_chunk < 1:
                    raise ValueError("--cache_time_chunk 必须为正整数")
                for t0 in range(0, int(data.shape[0]), time_chunk):
                    t1 = min(t0 + time_chunk, int(data.shape[0]))
                    slab = data[t0:t1, :, :].astype(np.float32, copy=False)
                    ds["cf"][global_t0 + t0:global_t0 + t1, :] = (
                        sparse_precompute._gather_station_cf_from_slab(slab, match)
                    )
                global_t0 += int(data.shape[0])
            logger.info("已抽取月文件 %d/%d：%s", file_index, len(files), path)
        if global_t0 != len(all_times):
            raise ValueError("写入缓存的时间长度与输入时间轴不一致")
        interpolation_method, corner_order = sparse_precompute._interpolation_metadata(
            args.threshold_interp
        )
        ds.cache_kind = "era5land_union_station_cf_chunk"
        ds.tech = args.tech
        ds.year_month_start = args.year_month_start
        ds.year_month_end = args.year_month_end
        ds.expected_month_count = str(len(months))
        ds.threshold_interp = args.threshold_interp
        ds.interpolation_method = interpolation_method
        ds.corner_order = corner_order
        ds.resource_variable = var_name
        ds.source_files = ",".join(str(path) for path in files)
        ds.union_stations_csv = str(args.union_stations_csv)
        ds.created_by = Path(__file__).name
        ds.close()
        ds = None
        os.replace(tmp, out_path)
    except Exception:
        if ds is not None:
            ds.close()
        if tmp.exists():
            tmp.unlink()
        raise
    return out_path


def main() -> None:
    setup_entry_logging("step1_split_E2a_extract_union_station_cf_monthly")
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
