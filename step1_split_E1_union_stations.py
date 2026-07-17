#!/usr/bin/env python3
"""步骤 E1：计算三个 SSP 场站并集、映射和 E2a 作业清单。"""
from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path

import pandas as pd

from scripts.hpc_step1_common import (
    BASELINE_YEARS,
    CHUNK_PLAN_NAME,
    SCENARIOS,
    UNION_FILE_NAME,
    build_chunk_plan,
    normalize_station_key,
    sha256_file,
    write_csv_atomic,
    write_json_atomic,
)
from scripts.precompute_station_low_resource_thresholds import _prepare_stations
from tools.logging_utils import setup_entry_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="计算三个 SSP 的场站并集和索引映射。")
    parser.add_argument("--stations_csv_ssp126", required=True)
    parser.add_argument("--stations_csv_ssp245", required=True)
    parser.add_argument("--stations_csv_ssp585", required=True)
    parser.add_argument(
        "--output_dir",
        default="outputs/cache/era5land_union_station_cf/union_stations",
    )
    parser.add_argument("--key", default="lon,lat,type", choices=["lon,lat,type"])
    parser.add_argument("--overwrite", action="store_true")
    return parser


def run(args: argparse.Namespace) -> Path:
    output_dir = Path(args.output_dir)
    inputs = {
        "ssp126": Path(args.stations_csv_ssp126),
        "ssp245": Path(args.stations_csv_ssp245),
        "ssp585": Path(args.stations_csv_ssp585),
    }
    outputs = [
        output_dir / UNION_FILE_NAME,
        output_dir / "union_stations_manifest.json",
        output_dir / CHUNK_PLAN_NAME,
        *(output_dir / f"station_index_map_{scenario}.csv" for scenario in SCENARIOS),
    ]
    existing = [path for path in outputs if path.exists()]
    if existing and not args.overwrite:
        raise FileExistsError(f"E1 输出已存在；确认后传 --overwrite：{existing[0]}")

    union_rows: list[dict[str, object]] = []
    key_to_union: dict[tuple[float, float, str], int] = {}
    mappings: dict[str, pd.DataFrame] = {}
    scenario_counts: dict[str, int] = {}
    for scenario, csv_path in inputs.items():
        rows: list[dict[str, object]] = []
        scenario_frames = []
        for tech in ("wind", "solar"):
            frame = _prepare_stations(csv_path, tech)
            frame["type"] = tech
            scenario_frames.append(frame)
        stations = pd.concat(scenario_frames, ignore_index=True)
        scenario_counts[scenario] = len(stations)
        for scenario_index, row in enumerate(stations.itertuples(index=False)):
            key = normalize_station_key(row.lon, row.lat, row.type)
            union_index = key_to_union.get(key)
            if union_index is None:
                union_index = len(union_rows)
                key_to_union[key] = union_index
                union_rows.append({
                    "union_station_index": union_index,
                    "lon": key[0],
                    "lat": key[1],
                    "type": key[2],
                })
            rows.append({
                "scenario_station_index": scenario_index,
                "union_station_index": union_index,
                "lon": key[0],
                "lat": key[1],
                "type": key[2],
                "activation_year": int(row.activation_year),
                "capacity_gw": float(row.capacity_gw),
            })
        mappings[scenario] = pd.DataFrame(rows)

    union = pd.DataFrame(union_rows)
    if union.duplicated(["lon", "lat", "type"]).any():
        raise ValueError("并集场站仍存在重复的 (lon,lat,type)")
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv_atomic(union, output_dir / UNION_FILE_NAME)
    for scenario, mapping in mappings.items():
        if mapping["scenario_station_index"].tolist() != list(range(len(mapping))):
            raise ValueError(f"{scenario} 映射没有保持原始场站顺序")
        write_csv_atomic(mapping, output_dir / f"station_index_map_{scenario}.csv")
    write_csv_atomic(build_chunk_plan(), output_dir / CHUNK_PLAN_NAME)
    manifest = {
        "manifest_kind": "era5land_union_stations",
        "baseline_years": BASELINE_YEARS,
        "key": args.key,
        "union_station_count": len(union),
        "scenario_station_counts": scenario_counts,
        "created_at": datetime.now().astimezone().isoformat(),
        "created_by": Path(__file__).name,
        "git_commit": os.environ.get("GIT_COMMIT", ""),
        "inputs": {
            scenario: {
                "path": str(path),
                "size": path.stat().st_size,
                "mtime_ns": path.stat().st_mtime_ns,
                "sha256": sha256_file(path),
            }
            for scenario, path in inputs.items()
        },
    }
    write_json_atomic(output_dir / "union_stations_manifest.json", manifest)
    return output_dir


def main() -> None:
    setup_entry_logging("step1_split_E1_union_stations")
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
