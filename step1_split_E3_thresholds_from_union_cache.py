#!/usr/bin/env python3
"""步骤 E3：从完整 union cache 计算三个 SSP 稀疏阈值。"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import netCDF4
import numpy as np
import pandas as pd

from grid_extreme_signals import cf_low_resource
from scripts import precompute_low_resource_thresholds as full_precompute
from scripts import precompute_station_low_resource_thresholds as sparse_precompute
from scripts.hpc_step1_common import BASELINE_YEARS, SCENARIOS, atomic_path, decode_attrs
from tools.logging_utils import setup_entry_logging

logger = logging.getLogger("step1_split_E3")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从 union station CF cache 计算 SSP 阈值。")
    parser.add_argument("--union_station_cf_cache", required=True)
    parser.add_argument("--union_stations_csv", required=True)
    parser.add_argument("--index_map_ssp126", required=True)
    parser.add_argument("--index_map_ssp245", required=True)
    parser.add_argument("--index_map_ssp585", required=True)
    parser.add_argument("--tech", choices=["wind", "solar"], required=True)
    parser.add_argument("--baseline_years", default=BASELINE_YEARS)
    parser.add_argument(
        "--output_dir",
        default="outputs/low_resource_thresholds/sparse_station_ERA5Land_2015-2024",
    )
    parser.add_argument("--station_chunk", type=int, default=128)
    parser.add_argument("--compress_level", type=int, default=4)
    parser.add_argument(
        "--scenarios",
        default=",".join(SCENARIOS),
        help="只计算指定 SSP（逗号分隔），默认全部三个",
    )
    parser.add_argument(
        "--n_jobs",
        type=int,
        default=1,
        help="chunk 级并行进程数，需 ≤ Slurm -n；默认 1（串行）",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser


def _match_from_cache(handle, positions: np.ndarray) -> cf_low_resource.FourPointMatch:
    positions = np.asarray(positions)
    # h5py fancy indexing 要求索引递增；排序读取后再按原顺序恢复。
    order = np.argsort(positions, kind="stable")
    inv = np.empty(order.shape, dtype=np.int64)
    inv[order] = np.arange(order.size)
    sorted_pos = positions[order]
    return cf_low_resource.FourPointMatch(
        lat_idx=handle["era5_lat_idx"][sorted_pos][inv].astype(np.int64),
        lon_idx=handle["era5_lon_idx"][sorted_pos][inv].astype(np.int64),
        weights=handle["weight"][sorted_pos][inv].astype(np.float32),
        corner_lat=handle["era5_lat"][sorted_pos][inv].astype(np.float32),
        corner_lon=handle["era5_lon"][sorted_pos][inv].astype(np.float32),
    )


_WORKER_TIMES: pd.DatetimeIndex | None = None


def _compute_chunk(cache_path, chunk_positions):
    """单个 station chunk 的阈值计算（worker 函数，顶层定义以便 pickle）。

    串行与并行共用，保证两条路径结果一致。worker 自开 cache 只读句柄，避免把
    ~45MB 的 block 在进程间 pickle；times 在每个 worker 进程内首次读取并缓存。
    """
    global _WORKER_TIMES
    chunk_positions = np.asarray(chunk_positions)
    with cf_low_resource.open_h5(cache_path, "r") as handle:
        if _WORKER_TIMES is None:
            _WORKER_TIMES = cf_low_resource.read_time(handle)
        # h5py fancy indexing 要求索引递增；排序读取后恢复原 chunk 顺序。
        order = np.argsort(chunk_positions, kind="stable")
        inv = np.empty(order.shape, dtype=np.int64)
        inv[order] = np.arange(order.size)
        block = handle["cf"][:, chunk_positions[order]].astype(np.float32)[:, inv]
    return full_precompute.compute_threshold_block(block, _WORKER_TIMES)


def _scenario_output(args: argparse.Namespace, scenario: str) -> Path:
    return cf_low_resource.sparse_threshold_file_for_scenario_tech(
        args.output_dir, scenario, args.tech, args.baseline_years
    )


def run(args: argparse.Namespace) -> list[Path]:
    global _WORKER_TIMES
    _WORKER_TIMES = None  # 每次 run 重置，避免跨调用/跨测试复用旧 cache 的 times
    if args.baseline_years != BASELINE_YEARS:
        raise ValueError(f"超算 step1 基准期固定为 {BASELINE_YEARS}")
    if args.station_chunk < 1:
        raise ValueError("--station_chunk 必须为正整数")
    if args.n_jobs < 1:
        raise ValueError("--n_jobs 必须为正整数")
    selected = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    unknown = [s for s in selected if s not in SCENARIOS]
    if unknown:
        raise ValueError(f"不支持的 scenario: {unknown}，可选: {SCENARIOS}")
    outputs = [_scenario_output(args, scenario) for scenario in selected]
    existing = [path for path in outputs if path.exists()]
    if existing and not args.overwrite:
        raise FileExistsError(f"E3 输出已存在；确认后传 --overwrite：{existing[0]}")

    map_paths = {
        "ssp126": args.index_map_ssp126,
        "ssp245": args.index_map_ssp245,
        "ssp585": args.index_map_ssp585,
    }
    union = pd.read_csv(args.union_stations_csv)
    union_keys = set(union["union_station_index"].astype(int))
    cache_path = Path(args.union_station_cf_cache)
    temp_outputs: list[Path] = []
    with cf_low_resource.open_h5(cache_path, "r") as cache:
        attrs = decode_attrs(cache)
        expected_attrs = {
            "cache_kind": "era5land_union_station_cf",
            "tech": args.tech,
            "baseline_years": BASELINE_YEARS,
        }
        for name, expected in expected_attrs.items():
            if attrs.get(name) != expected:
                raise ValueError(f"union cache 属性 {name} 不兼容")
        if attrs.get("chunk_count") != "21":
            raise ValueError("union cache 没有合并完整的21个 E2a 缓存块")
        times = cf_low_resource.read_time(cache)
        full_precompute.validate_time_axis(times, allow_incomplete=False)
        cache_union_indices = cache["union_station_index"][:].astype(np.int64)
        position_by_union = {
            int(union_index): position
            for position, union_index in enumerate(cache_union_indices)
        }
        if not set(position_by_union).issubset(union_keys):
            raise ValueError("union cache 含并集场站表中不存在的索引")

        try:
            for scenario in selected:
                map_path = map_paths[scenario]
                mapping = pd.read_csv(map_path)
                mapping = (
                    mapping[mapping["type"] == args.tech]
                    .sort_values("scenario_station_index")
                    .reset_index(drop=True)
                )
                if mapping.empty:
                    raise ValueError(f"{scenario} 没有 {args.tech} 场站")
                try:
                    positions = np.array(
                        [position_by_union[int(value)] for value in mapping["union_station_index"]],
                        dtype=np.int64,
                    )
                except KeyError as exc:
                    raise ValueError(
                        f"{scenario} 映射引用了当前技术缓存中不存在的 union index：{exc}"
                    ) from exc
                stations = mapping[
                    ["lon", "lat", "type", "activation_year", "capacity_gw"]
                ].copy()
                match = _match_from_cache(cache, positions)
                if not np.allclose(match.weights.sum(axis=1), 1.0, atol=1e-5):
                    raise ValueError(f"{scenario}: weight 权重和不等于 1")
                output = _scenario_output(args, scenario)
                tmp = atomic_path(output)
                if tmp.exists():
                    tmp.unlink()
                temp_outputs.append(tmp)
                source_files = [
                    Path(path) for path in attrs.get("source_files", "").split(",") if path
                ]
                ds: netCDF4.Dataset | None = None
                try:
                    ds = sparse_precompute.create_sparse_output(
                        tmp,
                        scenario=scenario,
                        tech=args.tech,
                        stations=stations,
                        match=match,
                        files=source_files,
                        baseline_years=args.baseline_years,
                        baseline_effective=BASELINE_YEARS,
                        interpolation_method=attrs["interpolation_method"],
                        corner_order=attrs.get("corner_order", ""),
                        reuse_enabled=False,
                        reuse_from_scenario=None,
                        reuse_source_file=None,
                        reuse_station_count=0,
                        computed_station_count=len(stations),
                        station_cf_cache_enabled=True,
                        station_cf_cache_file=cache_path,
                        compress_level=args.compress_level,
                    )
                    chunk_ranges = [
                        (c0, min(c0 + args.station_chunk, len(stations)))
                        for c0 in range(0, len(stations), args.station_chunk)
                    ]
                    cache_path_str = str(cache_path)
                    chunk_positions_list = [positions[c0:c1] for c0, c1 in chunk_ranges]
                    if args.n_jobs <= 1:
                        results = [
                            _compute_chunk(cache_path_str, cp)
                            for cp in chunk_positions_list
                        ]
                    else:
                        from concurrent.futures import ProcessPoolExecutor

                        n = len(chunk_positions_list)
                        with ProcessPoolExecutor(max_workers=args.n_jobs) as ex:
                            results = list(ex.map(
                                _compute_chunk,
                                [cache_path_str] * n,
                                chunk_positions_list,
                                chunksize=max(1, n // (args.n_jobs * 8)),
                            ))
                    for (c0, c1), (clim, threshold, valid_count) in zip(chunk_ranges, results):
                        if not np.all(np.isfinite(threshold)):
                            raise ValueError(f"{scenario}: threshold 含 NaN 或 Inf")
                        if not np.all(valid_count > 0):
                            raise ValueError(f"{scenario}: valid_count 存在非正值")
                        ds["clim"][:, :, c0:c1] = clim
                        ds["threshold"][c0:c1] = threshold
                        ds["valid_count"][c0:c1] = valid_count
                        logger.info("[%s/%s] 已计算 station %d:%d", scenario, args.tech, c0, c1)
                    ds.created_by = Path(__file__).name
                    ds.union_station_cf_cache = str(cache_path)
                    ds.close()
                    ds = None
                except Exception:
                    if ds is not None:
                        ds.close()
                    raise
            for tmp, output in zip(temp_outputs, outputs, strict=True):
                os.replace(tmp, output)
        except Exception:
            for tmp in temp_outputs:
                if tmp.exists():
                    tmp.unlink()
            raise
    return outputs


def main() -> None:
    setup_entry_logging("step1_split_E3_thresholds_from_union_cache")
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
