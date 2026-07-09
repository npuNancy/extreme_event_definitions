#!/usr/bin/env python3
"""流程 A：先生成网格信号，再抽取场站级极端天气信号。

两步流程：

  1. （第一阶段）通过现有第一阶段运行器为指定区域/年份生成全网格极端天气信号，产物与
     ``scripts/generate_multi_source_grid_signals.py`` 完全一致。若网格信号文件已存在，则跳过该步
     （通过 ``--grid_signals_dir`` 复用）。
  2. （第二阶段）读取每个网格信号 NetCDF，把本国场站匹配到网格（逐文件归一经度），将每个
     ``signal_<event>`` 掩膜抽取为 ``[(time, n_stations)]``，应用投产年份和距离容差掩膜，
     并按 ``(region, tech, scenario)`` 写出一个场站级 NetCDF；schema 与流程 B 相同。

与流程 B 的取舍：本流程会计算每个网格，因此更慢，但会留下全网格信号文件供其它用途使用，
也可以从已有网格运行中派生场站信号。

一致性：相同输入下，流程 A 和流程 B 必须得到逐位一致的场站掩膜（A 抽取已计算掩膜，
B 先抽取气象再计算）；见 ``tests/test_pipeline_consistency.py``。

示例::

    # 复用已有第一阶段网格运行结果
    python scripts/station_signals_from_grid.py \\
        --source regional_bcsd --data_dir data/bcsd_outputs \\
        --model MIROC-ES2H --scenario ssp126 \\
        --stations_csv data/stations/stations_SSP1-2.6.csv \\
        --region Germany --years 2030 \\
        --grid_signals_dir outputs/grid_extreme_signals

    # 或者先生成网格信号，再抽取场站信号
    python scripts/station_signals_from_grid.py ... --run_phase1
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import xarray as xr

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import registry  # noqa: E402
from grid_extreme_signals.adapters.regional_bcsd import RegionalBcsdAdapter  # noqa: E402
from grid_extreme_signals import station_match as sm  # noqa: E402

logger = logging.getLogger("station_signals_from_grid")

DEFAULT_SHP = "/data6/yanxiaokai/project_climate/data/maps/natural_earth/ne_110m_admin_0_countries.shp"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="流程 A：从第一阶段全网格信号派生场站级信号。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--source", default="regional_bcsd",
                   choices=["regional_bcsd", "china_cmfd_bcsd", "cordex_nam12"])
    p.add_argument("--data_dir", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--scenario", default=None, help="省略时从 --stations_csv 推断。")
    p.add_argument("--stations_csv", required=True)
    p.add_argument("--region", default="all")
    p.add_argument("--years", required=True)
    p.add_argument("--tech", choices=["wind", "solar", "both"], default="both")
    p.add_argument("--shp", default=DEFAULT_SHP)
    p.add_argument("--max_dist", type=float, default=sm.MAX_DIST_DEG)
    p.add_argument("--grid_signals_dir", default="outputs/grid_extreme_signals",
                   help="第一阶段网格信号根目录（从这里读取）。")
    p.add_argument("--output_root", default="outputs/station_signals")
    p.add_argument("--compress_level", type=int, default=4)
    p.add_argument("--cf_root", default="../data/cfs",
                   help="容量因子数据根目录，用于默认启用的低资源事件。")
    p.add_argument("--lowres_baseline_years", default="2015-2029",
                   help="低资源事件基线期，默认 2015-2029。")
    p.add_argument("--lowres_cf_years", default="2015-2060",
                   help="CF 文件覆盖年份，用于查找 allmonths 文件。")
    p.add_argument("--lowres_grid_lat_chunk", type=int, default=1,
                   help="第一阶段网格低资源计算的纬向块大小。")
    p.add_argument("--no_low_resource", action="store_true",
                   help="跳过默认启用的风电/光伏低资源事件。")
    p.add_argument("--no_activation_mask", action="store_true")
    p.add_argument("--allow_unit_inference", action="store_true")
    p.add_argument("--allow_missing_optional", action="store_true")
    p.add_argument("--run_phase1", action="store_true",
                   help="抽取前通过第一阶段运行器生成缺失的网格信号。")
    p.add_argument("--overwrite_grid", action="store_true",
                   help="与 --run_phase1 搭配使用；即使网格信号已存在也重新计算。")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--dry_run", action="store_true")
    return p


# ---------------------------------------------------------------------
# 第一阶段网格信号生成（复用现有运行器）
# ---------------------------------------------------------------------

def _grid_signal_path(grid_root, source, model, region, scenario, tech, year) -> str:
    return str(
        Path(grid_root) / source / model / region / scenario / "signals" /
        f"extreme_signals_{tech}_{model}_{region}_{scenario}_{year}.nc"
    )


def _ensure_grid_signals(adapter, args, region, scenario, techs) -> None:
    """为缺失的网格信号文件运行第一阶段（仅在 --run_phase1 时）。"""
    from grid_extreme_signals.signal_runner import run_signal_pipeline
    y0, y1 = (int(x) for x in args.years.split("-")) if "-" in args.years else (int(args.years),) * 2
    missing = []
    for tech in techs:
        for year in range(y0, y1 + 1):
            p = _grid_signal_path(args.grid_signals_dir, args.source, args.model,
                                  region, scenario, tech, year)
            if args.overwrite_grid or not os.path.exists(p):
                missing.append((tech, year))
    if not missing:
        return
    logger.info("[%s] 为 %d 个缺失网格文件运行第一阶段：%s",
                region, len(missing), missing)
    phase_args = SimpleNamespace(**vars(args))
    phase_args.output_root = args.grid_signals_dir
    phase_args.region = region
    phase_args.overwrite = args.overwrite_grid
    phase_args.save_weather = False
    phase_args.stations_dir = None
    phase_args.require_events = []
    phase_args.dry_run = False
    phase_args.compress_level = args.compress_level
    # 限定到当前区域；run_signal_pipeline 会遍历适配器任务。
    run_signal_pipeline(adapter, phase_args)


# ---------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------

def _bundle_axes_from_grid(ds) -> tuple[str, str, str]:
    """从网格信号数据集中发现 (time, lat, lon) 维度名。"""
    dims = list(ds.dims)
    time_name = next((d for d in dims if d.lower().startswith("time")), dims[0])
    lat_candidates = [d for d in dims if d.lower() in ("lat", "latitude", "rlat")]
    lon_candidates = [d for d in dims if d.lower() in ("lon", "longitude", "rlon")]
    lat_name = lat_candidates[0] if lat_candidates else dims[1]
    lon_name = lon_candidates[0] if lon_candidates else dims[2]
    return time_name, lat_name, lon_name


def _process_region(adapter, args, country_stations, region, scenario, techs) -> None:
    y0, y1 = (int(x) for x in args.years.split("-")) if "-" in args.years else (int(args.years),) * 2

    for tech in techs:
        stations = country_stations.get(tech)
        if stations is None or len(stations) == 0:
            logger.info("[%s/%s] 无场站，跳过", region, tech)
            continue

        out_path = str(
            Path(args.output_root) / args.source / args.model / region / scenario /
            f"station_signals_{tech}_{args.model}_{region}_{scenario}_{y0}-{y1}.nc"
        )
        if not args.overwrite and os.path.exists(out_path):
            logger.info("[%s/%s] 输出已存在，跳过", region, tech)
            continue
        if args.dry_run:
            logger.info("[%s/%s] [试运行] 将写出 %s", region, tech, out_path)
            continue

        match: sm.StationMatch | None = None
        masks_acc: dict[str, list[np.ndarray]] = {}
        times_acc: list[np.ndarray] = []
        skipped_reasons: dict[str, str] = {}
        lowres_attrs: dict[str, str] = {}

        for year in range(y0, y1 + 1):
            gp = _grid_signal_path(args.grid_signals_dir, args.source, args.model,
                                   region, scenario, tech, year)
            if not os.path.exists(gp):
                logger.warning("[%s/%s/%d] 网格信号缺失：%s，跳过该年",
                               region, tech, year, gp)
                continue
            ds = xr.open_dataset(gp)
            time_name, lat_name, lon_name = _bundle_axes_from_grid(ds)
            sig_vars = [v for v in ds.data_vars if v.startswith("signal_")]
            if not sig_vars:
                ds.close()
                logger.warning("[%s/%s/%d] 网格文件中无信号变量，跳过", region, tech, year)
                continue
            if not args.no_low_resource and "signal_low_resource" not in sig_vars:
                ds.close()
                raise RuntimeError(
                    f"{gp} 缺少 signal_low_resource；请重新运行第一阶段并保持默认低资源启用，"
                    "或显式传入 --no_low_resource 跳过低资源。"
                )

            times = ds[time_name].values
            if times.size == 0:
                ds.close()
                continue

            if match is None:
                grid_lat = ds[lat_name].values
                grid_lon = ds[lon_name].values
                match = sm.match_regular(grid_lat, grid_lon, stations, max_dist=args.max_dist)
                n_bad = int((~match.valid).sum())
                if n_bad:
                    logger.warning("[%s/%s] %d/%d 个场站超过最大距离=%.2f°",
                                   region, tech, n_bad, len(match), args.max_dist)
                # 若网格文件属性中有跳过事件原因，则读取它们
                for ev in ds.attrs.get("skipped_events", "").split(","):
                    ev = ev.strip()
                    if ev:
                        skipped_reasons[ev] = ds.attrs.get("skipped_event_reasons", f"{ev} 缺少输入")
                for key in (
                    "low_resource_source",
                    "low_resource_cf_file",
                    "low_resource_baseline_years",
                    "low_resource_window_hours",
                    "low_resource_window_steps",
                    "low_resource_timestep_hours",
                    "low_resource_mark_next_step",
                ):
                    if key in ds.attrs:
                        lowres_attrs[key] = ds.attrs[key]
                logger.info("[%s/%s] 已匹配 %d 个场站（网格 %dx%d，经度为360制=%s）",
                            region, tech, len(match), grid_lat.size, grid_lon.size,
                            sm.is_lon_360(grid_lon))

            for v in sig_vars:
                gathered = sm.gather_to_stations(ds[v].values.astype(bool), match)
                masks_acc.setdefault(v, []).append(gathered)
            times_acc.append(times)
            ds.close()

        if match is None or not masks_acc:
            logger.warning("[%s/%s] 未生成任何结果，跳过", region, tech)
            continue

        times_all = np.concatenate(times_acc)
        masks_all = {v: np.concatenate(parts, axis=0) for v, parts in masks_acc.items()}

        act = _activation_time_mask(
            times_all, match.stations["activation_year"].to_numpy(np.int64),
            enabled=not args.no_activation_mask)
        valid = match.valid[None, :]

        supported = sorted(v[len("signal_"):] for v in masks_all)
        all_events = set(registry.SIMPLE[tech].keys())
        all_events.add("low_resource")
        skipped = sorted(all_events - set(supported))
        if args.no_low_resource and "low_resource" in skipped:
            skipped_reasons["low_resource"] = "用户通过 --no_low_resource 关闭"

        out_masks = {v: (arr & act & valid).astype(np.int8) for v, arr in masks_all.items()}
        sm.write_station_signals(
            out_path, out_masks, times_all, match, tech,
            source=args.source, model=args.model, region=region, scenario=scenario,
            source_csv=os.path.basename(args.stations_csv), pipeline="A",
            supported=supported, skipped=skipped, skipped_reasons=skipped_reasons,
            max_dist=args.max_dist, activation_mask_on=not args.no_activation_mask,
            compress_level=args.compress_level,
            attrs_extra=lowres_attrs,
        )
        logger.info("[%s/%s] 已写出 %s  事件=%s  场站=%d",
                    region, tech, out_path, supported, len(match))


def _activation_time_mask(times, activation_years, enabled):
    if not enabled:
        return np.ones((times.shape[0], activation_years.shape[0]), dtype=bool)
    years = pd.DatetimeIndex(times).year.to_numpy(np.int64)[:, None]
    return years >= activation_years[None, :]


# ---------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    args = build_parser().parse_args()

    if args.source != "regional_bcsd":
        raise NotImplementedError(
            f"--source {args.source!r} 尚未在 Pipeline A 中启用"
            "（数据尚未部署）。当前仅支持 'regional_bcsd'。"
        )

    scenario = args.scenario or sm.infer_scenario_from_csv(args.stations_csv)
    logger.info("读取场站 %s（情景=%s）", args.stations_csv, scenario)
    stations_df = sm.load_stations(args.stations_csv)
    country_shapes = sm.load_country_shapes(args.shp)

    adapter = RegionalBcsdAdapter(SimpleNamespace(
        data_dir=args.data_dir, model=args.model, region=args.region,
        scenario=scenario, years=args.years, output_root=args.grid_signals_dir,
        allow_unit_inference=args.allow_unit_inference,
        allow_missing_optional=args.allow_missing_optional,
    ))
    regions = adapter._resolve_regions()
    techs = ["wind", "solar"] if args.tech == "both" else [args.tech]
    logger.info("区域数：%d | 技术类型：%s", len(regions), techs)

    for region in regions:
        country_name = sm.bcsd_region_to_ne_name(region)
        geom = country_shapes.get(country_name)
        if geom is None:
            logger.warning("[%s] 未匹配到国家边界 '%s'，跳过", region, country_name)
            continue
        country_stations = {tech: sm.filter_stations_for_country(stations_df, geom, tech)
                            for tech in techs}
        if sum(len(v) for v in country_stations.values()) == 0:
            logger.info("[%s] 无场站，跳过", region)
            continue
        if args.run_phase1:
            _ensure_grid_signals(adapter, args, region, scenario, techs)
        _process_region(adapter, args, country_stations, region, scenario, techs)

    logger.info("流程 A 完成。")


if __name__ == "__main__":
    main()
