#!/usr/bin/env python3
"""校验超算 step1 E2a/E2b/E3 输出并返回 JSON。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from grid_extreme_signals import cf_low_resource
from scripts.hpc_step1_common import BASELINE_YEARS, SCENARIOS, decode_attrs, read_chunk_plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="校验超算 step1 阶段输出。")
    parser.add_argument("--stage", choices=["E2a", "E2b", "E3"], required=True)
    parser.add_argument("--tech", choices=["wind", "solar"], required=True)
    parser.add_argument("--project_dir", default=".")
    return parser


def _validate_cache(path: Path, *, kind: str, tech: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(path)
    with cf_low_resource.open_h5(path, "r") as handle:
        attrs = decode_attrs(handle)
        if attrs.get("cache_kind") != kind or attrs.get("tech") != tech:
            raise ValueError(f"{path}: cache_kind/tech 不匹配")
        if handle["cf"].shape != (len(handle["time"]), len(handle["station"])):
            raise ValueError(f"{path}: cf 形状错误")
        if not np.allclose(handle["weight"][:].sum(axis=1), 1.0, atol=1e-5):
            raise ValueError(f"{path}: weight 权重和错误")
        times = cf_low_resource.read_time(handle)
        if not times.is_monotonic_increasing or times.has_duplicates:
            raise ValueError(f"{path}: 时间轴未递增或存在重复")


def validate(args: argparse.Namespace) -> dict:
    project = Path(args.project_dir)
    union_root = project / "outputs/cache/era5land_union_station_cf"
    if args.stage == "E2a":
        plan = read_chunk_plan(union_root / "union_stations/e2a_chunk_plan_2015-2024.csv")
        paths = []
        for row in plan.itertuples(index=False):
            path = union_root / "time_chunks" / row.cache_file.replace("{tech}", args.tech)
            _validate_cache(
                path, kind="era5land_union_station_cf_chunk", tech=args.tech
            )
            with cf_low_resource.open_h5(path, "r") as handle:
                attrs = decode_attrs(handle)
                if attrs.get("year_month_start") != row.year_month_start:
                    raise ValueError(f"{path}: year_month_start 与清单不一致")
                if attrs.get("year_month_end") != row.year_month_end:
                    raise ValueError(f"{path}: year_month_end 与清单不一致")
                periods = pd.PeriodIndex(cf_low_resource.read_time(handle), freq="M").unique()
                expected = pd.period_range(
                    row.year_month_start, row.year_month_end, freq="M"
                )
                if periods.tolist() != expected.tolist():
                    raise ValueError(f"{path}: 时间轴没有覆盖清单年月范围")
            paths.append(path)
        return {"valid": True, "stage": "E2a", "tech": args.tech, "count": len(paths)}
    if args.stage == "E2b":
        path = (
            union_root / "merged_cache"
            / f"station_cf_union_{args.tech}_ERA5Land_2015-2024_nearest_valid.nc"
        )
        _validate_cache(path, kind="era5land_union_station_cf", tech=args.tech)
        with cf_low_resource.open_h5(path, "r") as handle:
            attrs = decode_attrs(handle)
            if attrs.get("baseline_years") != BASELINE_YEARS:
                raise ValueError("E2b baseline_years 不匹配")
            if attrs.get("chunk_count") != "21":
                raise ValueError("E2b chunk_count 不是21")
            times = cf_low_resource.read_time(handle)
            if times[0] != pd.Timestamp("2015-01-01 00:00:00"):
                raise ValueError("E2b 起始时间不是 2015-01-01 00:00")
            if times[-1] != pd.Timestamp("2024-12-31 23:00:00"):
                raise ValueError("E2b 结束时间不是 2024-12-31 23:00")
        return {"valid": True, "stage": "E2b", "tech": args.tech, "path": str(path)}

    output_dir = (
        project / "outputs/low_resource_thresholds"
        / "sparse_station_ERA5Land_2015-2024"
    )
    paths = [
        cf_low_resource.sparse_threshold_file_for_scenario_tech(
            output_dir, scenario, args.tech, BASELINE_YEARS
        )
        for scenario in SCENARIOS
    ]
    for scenario, path in zip(SCENARIOS, paths, strict=True):
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(path)
        with cf_low_resource.open_h5(path, "r") as handle:
            attrs = decode_attrs(handle)
            if attrs.get("threshold_kind") != "sparse_station":
                raise ValueError(f"{path}: threshold_kind 错误")
            if attrs.get("scenario") != scenario or attrs.get("tech") != args.tech:
                raise ValueError(f"{path}: scenario/tech 错误")
            n_station = len(handle["station"])
            if handle["clim"].shape != (12, 24, n_station):
                raise ValueError(f"{path}: clim 形状错误")
            if not np.all(np.isfinite(handle["threshold"][:])):
                raise ValueError(f"{path}: threshold 含非有限值")
            if not np.all(handle["valid_count"][:] > 0):
                raise ValueError(f"{path}: valid_count 含非正值")
            if not np.allclose(handle["weight"][:].sum(axis=1), 1.0, atol=1e-5):
                raise ValueError(f"{path}: weight 权重和错误")
    return {"valid": True, "stage": "E3", "tech": args.tech, "count": len(paths)}


def main() -> None:
    args = build_parser().parse_args()
    try:
        result = validate(args)
    except Exception as exc:
        result = {
            "valid": False,
            "stage": args.stage,
            "tech": args.tech,
            "error": str(exc),
        }
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
