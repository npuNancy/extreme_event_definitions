#!/usr/bin/env python3
"""步骤 E2b：合并21个 E2a 时间缓存块。"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd

from grid_extreme_signals import cf_low_resource
from scripts import precompute_low_resource_thresholds as full_precompute
from scripts.hpc_step1_common import (
    BASELINE_YEARS,
    atomic_path,
    copy_station_metadata,
    create_station_cache,
    decode_attrs,
    read_chunk_plan,
    require_output_available,
)
from tools.logging_utils import setup_entry_logging

logger = logging.getLogger("step1_split_E2b")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="合并完整 union station CF cache。")
    parser.add_argument("--time_chunk_dir", required=True)
    parser.add_argument("--chunk_plan_csv", required=True)
    parser.add_argument("--tech", choices=["wind", "solar"], required=True)
    parser.add_argument("--baseline_years", default=BASELINE_YEARS)
    parser.add_argument(
        "--threshold_interp",
        choices=["nearest_valid", "bilinear"],
        default="nearest_valid",
    )
    parser.add_argument("--output_cache", required=True)
    parser.add_argument("--compress_level", type=int, default=1)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def _cache_path(directory: Path, template: str, tech: str) -> Path:
    return directory / template.replace("{tech}", tech)


def _validate_chunk(
    path: Path,
    *,
    args: argparse.Namespace,
    row,
    reference: dict[str, np.ndarray] | None,
) -> tuple[pd.DatetimeIndex, dict[str, np.ndarray]]:
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"E2a 缓存不存在或为空：{path}")
    with cf_low_resource.open_h5(path, "r") as handle:
        attrs = decode_attrs(handle)
        expected = {
            "cache_kind": "era5land_union_station_cf_chunk",
            "tech": args.tech,
            "year_month_start": row.year_month_start,
            "year_month_end": row.year_month_end,
            "threshold_interp": args.threshold_interp,
        }
        for name, value in expected.items():
            if attrs.get(name) != str(value):
                raise ValueError(f"{path}: 属性 {name} 不匹配")
        times = cf_low_resource.read_time(handle)
        expected_periods = pd.period_range(
            row.year_month_start, row.year_month_end, freq="M"
        )
        actual_periods = pd.PeriodIndex(times, freq="M").unique()
        if actual_periods.tolist() != expected_periods.tolist():
            raise ValueError(f"{path}: 时间轴没有严格覆盖清单年月范围")
        if handle["cf"].shape != (len(times), len(handle["station"])):
            raise ValueError(f"{path}: cf 形状不正确")
        metadata = {
            name: handle[name][:]
            for name in (
                "union_station_index", "station_lon", "station_lat", "station_type",
                "era5_lat_idx", "era5_lon_idx", "era5_lat", "era5_lon", "weight",
            )
        }
        if not np.allclose(metadata["weight"].sum(axis=1), 1.0, atol=1e-5):
            raise ValueError(f"{path}: weight 权重和不等于 1")
        if reference is not None:
            for name, values in metadata.items():
                if not np.allclose(values, reference[name], equal_nan=True, atol=1e-6):
                    raise ValueError(f"{path}: 静态变量 {name} 与首个缓存块不一致")
    return times, metadata


def run(args: argparse.Namespace) -> Path:
    if args.baseline_years != BASELINE_YEARS:
        raise ValueError(f"超算 step1 基准期固定为 {BASELINE_YEARS}")
    plan = read_chunk_plan(args.chunk_plan_csv)
    directory = Path(args.time_chunk_dir)
    output = Path(args.output_cache)
    require_output_available(output, args.overwrite)
    chunks: list[tuple[Path, pd.DatetimeIndex]] = []
    reference = None
    reference_attrs: dict[str, str] | None = None
    for row in plan.itertuples(index=False):
        path = _cache_path(directory, row.cache_file, args.tech)
        times, metadata = _validate_chunk(path, args=args, row=row, reference=reference)
        reference = metadata if reference is None else reference
        if reference_attrs is None:
            with cf_low_resource.open_h5(path, "r") as handle:
                reference_attrs = decode_attrs(handle)
        chunks.append((path, times))
    assert reference is not None
    assert reference_attrs is not None
    all_times = pd.DatetimeIndex(
        np.concatenate([times.values for _, times in chunks])
    )
    full_precompute.validate_time_axis(all_times, allow_incomplete=False)
    expected_start = pd.Timestamp("2015-01-01 00:00:00")
    if all_times[0] != expected_start or all_times[-1].year != 2024:
        raise ValueError("完整缓存时间轴没有覆盖 2015-2024")

    tmp = atomic_path(output)
    if tmp.exists():
        tmp.unlink()
    ds = None
    try:
        ds = create_station_cache(
            tmp,
            n_time=len(all_times),
            n_station=len(reference["union_station_index"]),
            compress_level=args.compress_level,
        )
        ds["time"][:] = all_times.to_numpy(dtype="datetime64[s]").astype(np.int64)
        for name, values in reference.items():
            ds[name][:] = values
        offset = 0
        source_files = []
        manifest = []
        for path, times in chunks:
            with cf_low_resource.open_h5(path, "r") as handle:
                count = len(times)
                ds["cf"][offset:offset + count, :] = handle["cf"][:]
                source_files.extend(decode_attrs(handle).get("source_files", "").split(","))
            manifest.append({"path": str(path), "time_count": len(times)})
            offset += len(times)
            logger.info("已合并 %s，time=%d:%d", path, offset - len(times), offset)
        ds.cache_kind = "era5land_union_station_cf"
        ds.tech = args.tech
        ds.baseline_years = args.baseline_years
        ds.baseline_years_effective = BASELINE_YEARS
        ds.threshold_interp = args.threshold_interp
        ds.interpolation_method = (
            "bilinear_4point" if args.threshold_interp == "bilinear" else "nearest_valid"
        )
        ds.corner_order = reference_attrs.get("corner_order", "")
        ds.resource_variable = full_precompute.era5land_cf_var(args.tech)
        ds.source_files = ",".join(path for path in source_files if path)
        ds.chunk_plan_csv = str(args.chunk_plan_csv)
        ds.chunk_count = str(len(chunks))
        ds.chunk_manifest = json.dumps(manifest, ensure_ascii=False)
        ds.created_by = Path(__file__).name
        ds.close()
        ds = None
        os.replace(tmp, output)
    except Exception:
        if ds is not None:
            ds.close()
        if tmp.exists():
            tmp.unlink()
        raise
    return output


def main() -> None:
    setup_entry_logging("step1_split_E2b_merge_union_station_cf_cache")
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
