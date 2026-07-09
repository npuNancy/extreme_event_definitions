#!/usr/bin/env python3
"""流程 B：直接生成场站级极端天气信号。

对每个已有气象数据的区域，本流程会：

  1. 通过现有多数据源适配器（如 ``RegionalBcsdAdapter``）加载并标准化气象数据，路径与 Phase 1 一致；
  2. 只选择落在该区域国家多边形内的场站（Natural Earth 点在多边形内判断），并以
     ``activation_year = min(year)`` 去重；
  3. 将每个场站匹配到最近网格（逐文件把经度归一到 ``[-180, 180)``；BCSD 经度约定随区域而变）；
  4. 将标准化气象变量提取到匹配网格，形成 ``(time, n_stations)`` 数组，并运行
     ``registry.simple_signals``；
  5. 应用投产年份掩膜（投产前信号为 0）和距离容差掩膜，然后按 ``(region, tech, scenario)``
     写出场站级 NetCDF。

本流程不写全网格信号，只写场站级输出。没有气象数据的国家不会被处理。

当前支持 ``--source regional_bcsd``。``china_cmfd_bcsd`` / ``cordex_nam12`` 已接入共享匹配层，
但数据尚未部署；NetCDF 到位后再启用。

示例::

    python scripts/station_signals_direct.py \\
        --source regional_bcsd \\
        --data_dir data/bcsd_outputs \\
        --model MIROC-ES2H \\
        --scenario ssp126 \\
        --stations_csv data/stations/stations_SSP1-2.6.csv \\
        --region Germany \\
        --years 2015-2050
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

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import registry  # noqa: E402
from grid_extreme_signals.adapters.regional_bcsd import RegionalBcsdAdapter  # noqa: E402
from grid_extreme_signals import station_match as sm  # noqa: E402
from grid_extreme_signals import cf_low_resource  # noqa: E402

logger = logging.getLogger("station_signals_direct")

DEFAULT_SHP = "/data6/yanxiaokai/project_climate/data/maps/natural_earth/ne_110m_admin_0_countries.shp"


# =====================================================================
# 命令行参数
# =====================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="流程 B：直接生成场站级极端天气信号（不输出网格文件）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--source", default="regional_bcsd",
                   choices=["regional_bcsd", "china_cmfd_bcsd", "cordex_nam12"],
                   help="数据源（当前支持 regional_bcsd，其它数据源暂缓）。")
    p.add_argument("--data_dir", required=True, help="输入数据根目录。")
    p.add_argument("--model", required=True, help="气候模式名称。")
    p.add_argument("--scenario", default=None,
                   help="情景代码（如 ssp126）；省略时从 --stations_csv 推断。")
    p.add_argument("--stations_csv", required=True,
                   help="场站选址 CSV（如 data/stations/stations_SSP1-2.6.csv）。")
    p.add_argument("--region", default="all",
                   help="区域名称或 'all'（默认：所有有数据的区域）。")
    p.add_argument("--years", required=True, help="年份范围：'YYYY' 或 'YYYY-YYYY'。")
    p.add_argument("--tech", choices=["wind", "solar", "both"], default="both")
    p.add_argument("--shp", default=DEFAULT_SHP, help="Natural Earth 国家边界 shapefile。")
    p.add_argument("--max_dist", type=float, default=sm.MAX_DIST_DEG,
                   help="最近邻网格距离容差（单位：度，默认 %(default)s）。")
    p.add_argument("--output_root", default="outputs/station_signals")
    p.add_argument("--compress_level", type=int, default=4)
    p.add_argument("--cf_root", default="../data/cfs",
                   help="容量因子数据根目录，用于默认启用的低资源事件。")
    p.add_argument("--lowres_baseline_years", default="2015-2029",
                   help="低资源事件基线期，默认 2015-2029。")
    p.add_argument("--lowres_cf_years", default="2015-2060",
                   help="CF 文件覆盖年份，用于查找 allmonths 文件。")
    p.add_argument("--lowres_station_chunk", type=int, default=128,
                   help="低资源事件计算的场站块大小。")
    p.add_argument("--lowres_time_chunk", type=int, default=512,
                   help="从 CF 文件读取的时间块大小。")
    p.add_argument("--no_low_resource", action="store_true",
                   help="跳过默认启用的风电/光伏低资源事件。")
    p.add_argument("--no_activation_mask", action="store_true",
                   help="保留所有年份信号（不把投产前年份置零）。")
    p.add_argument("--allow_unit_inference", action="store_true")
    p.add_argument("--allow_missing_optional", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--dry_run", action="store_true")
    return p


# =====================================================================
# 辅助函数
# =====================================================================

def _bundle_spatial_axes(bundle) -> tuple[str, str, str]:
    """返回规则经纬度 bundle 的 (time_name, lat_name, lon_name)。"""
    spatial = set(bundle.spatial_dims)
    sample = next(iter(bundle.dataset.data_vars.values()))
    time_name = next(d for d in sample.dims if d not in spatial)
    lat_name, lon_name = bundle.spatial_dims
    return time_name, lat_name, lon_name


def _gather_weather(bundle, match: sm.StationMatch) -> dict[str, np.ndarray]:
    """将 bundle 网格上的标准化气象变量抽取到场站。

    返回 ``{var_name: (time, n_stations) float32}``。
    """
    out: dict[str, np.ndarray] = {}
    for var in ("temp_C", "wind_ms", "precip_mmh", "rsds", "rh_pct", "dust_aod"):
        if var in bundle.dataset.data_vars:
            out[var] = sm.gather_to_stations(
                bundle.dataset[var].values.astype(np.float32), match
            )
    return out


def _activation_time_mask(times: np.ndarray, activation_years: np.ndarray,
                          enabled: bool) -> np.ndarray:
    """(T, n_sta) bool：场站在该时刻已投产则为 True。"""
    if not enabled:
        return np.ones((times.shape[0], activation_years.shape[0]), dtype=bool)
    years = pd.DatetimeIndex(times).year.to_numpy(np.int64)[:, None]
    return years >= activation_years[None, :]


def _skip(path: str, overwrite: bool) -> bool:
    if overwrite:
        return False
    return os.path.exists(path)


# =====================================================================
# 分技术类型处理
# =====================================================================

def _process_tech(adapter, args, country_stations: dict[str, pd.DataFrame],
                  region: str, scenario: str, tech: str,
                  country_shapes: dict) -> str | None:
    """处理一个 (region, tech)；若跳过则返回 None，否则返回输出路径。"""
    stations = country_stations.get(tech)
    if stations is None or len(stations) == 0:
        logger.info("[%s/%s] 国家内无场站，跳过", region, tech)
        return None

    y0, y1 = (int(x) for x in args.years.split("-")) if "-" in args.years else (int(args.years),) * 2
    out_path = str(
        Path(args.output_root) / args.source / args.model / region / scenario /
        f"station_signals_{tech}_{args.model}_{region}_{scenario}_{y0}-{y1}.nc"
    )
    if _skip(out_path, args.overwrite):
        logger.info("[%s/%s] 输出已存在，跳过（使用 --overwrite 重新计算）", region, tech)
        return out_path
    if args.dry_run:
        logger.info("[%s/%s] [试运行] 将写出 %s", region, tech, out_path)
        return out_path

    match: sm.StationMatch | None = None
    masks_acc: dict[str, list[np.ndarray]] = {}
    times_acc: list[np.ndarray] = []
    skipped_inputs: dict[str, str] = {}

    for year in range(y0, y1 + 1):
        task = {"data_dir": args.data_dir, "model": args.model, "region": region,
                "scenario": scenario, "year": year}
        try:
            bundle = (adapter.load_wind_weather(task) if tech == "wind"
                      else adapter.load_solar_weather(task))
        except (FileNotFoundError, ValueError) as e:
            logger.warning("[%s/%s/%d] 输入缺失：%s，停止年份循环", region, tech, year, e)
            break

        time_name, lat_name, lon_name = _bundle_spatial_axes(bundle)
        times = bundle.dataset[time_name].values
        if times.size == 0:
            logger.warning("[%s/%d] 时间轴为空，跳过该年", region, year)
            continue

        # 每个区域/技术只匹配一次（网格跨年份不变）
        if match is None:
            grid_lat = bundle.dataset[lat_name].values
            grid_lon = bundle.dataset[lon_name].values
            match = sm.match_regular(grid_lat, grid_lon, stations, max_dist=args.max_dist)
            n_bad = int((~match.valid).sum())
            if n_bad:
                logger.warning("[%s/%s] %d/%d 个场站超过最大距离=%.2f°（将置零）",
                               region, tech, n_bad, len(match), args.max_dist)
            skipped_inputs = dict(bundle.skipped_inputs)
            logger.info("[%s/%s] 已匹配 %d 个场站（网格 %dx%d，经度为360制=%s）",
                        region, tech, len(match),
                        grid_lat.size, grid_lon.size, sm.is_lon_360(grid_lon))

        weather = _gather_weather(bundle, match)
        if not weather:
            logger.warning("[%s/%s/%d] 未提取到气象变量，跳过该年", region, tech, year)
            continue
        masks = registry.simple_signals(tech, weather, skip_missing=True)
        for name, arr in masks.items():
            masks_acc.setdefault(name, []).append(arr.astype(bool))
        times_acc.append(times)
        del bundle, weather, masks

    if match is None or not masks_acc:
        logger.warning("[%s/%s] 未生成任何结果，跳过", region, tech)
        return None

    # 跨年份拼接
    times_all = np.concatenate(times_acc)
    masks_all = {name: np.concatenate(parts, axis=0) for name, parts in masks_acc.items()}

    # 投产年份掩膜 + 距离掩膜
    act = _activation_time_mask(
        times_all, match.stations["activation_year"].to_numpy(np.int64),
        enabled=not args.no_activation_mask,
    )
    valid = match.valid[None, :]  # (1, n_sta)

    lowres_attrs = {}
    lowres_valid = None
    lowres_skip_reason = None
    if not args.no_low_resource:
        cf_file = cf_low_resource.find_cf_file(
            args.cf_root, args.source, args.model, scenario, tech,
            region=region, years=args.lowres_cf_years,
        )
        if cf_file is None:
            lowres_skip_reason = f"未找到 CF 文件：cf_root={args.cf_root}"
        else:
            try:
                result = cf_low_resource.compute_station_low_resource(
                    cf_file,
                    tech,
                    times_all,
                    match.stations["lat"].to_numpy(np.float64),
                    match.stations["lon"].to_numpy(np.float64),
                    baseline_years=args.lowres_baseline_years,
                    max_dist=args.max_dist,
                    station_chunk=args.lowres_station_chunk,
                    time_chunk=args.lowres_time_chunk,
                )
                masks_all["low_resource"] = result.mask.astype(bool)
                lowres_valid = result.valid
                lowres_attrs = cf_low_resource.attrs(result)
            except Exception as e:
                logger.warning("[%s/%s] 低资源计算失败：%s", region, tech, e)
                lowres_skip_reason = f"低资源计算失败：{e}"
    else:
        lowres_skip_reason = "用户通过 --no_low_resource 关闭"

    supported = sorted(masks_all.keys())
    all_events = set(registry.SIMPLE[tech].keys())
    all_events.add("low_resource")
    skipped = sorted(all_events - set(supported))
    skipped_reasons = {ev: skipped_inputs.get(_first_req_var(tech, ev), f"{ev} 缺少输入")
                       for ev in skipped}
    if "low_resource" in skipped and lowres_skip_reason:
        skipped_reasons["low_resource"] = lowres_skip_reason

    out_masks: dict[str, np.ndarray] = {}
    for name, arr in masks_all.items():
        event_valid = valid
        if name == "low_resource" and lowres_valid is not None:
            event_valid = valid & lowres_valid[None, :]
        arr = arr & act & event_valid  # 投产前年份和超距离容差场站置零
        out_masks[f"signal_{name}"] = arr.astype(np.int8)

    sm.write_station_signals(
        out_path, out_masks, times_all, match, tech,
        source=args.source, model=args.model, region=region, scenario=scenario,
        source_csv=os.path.basename(args.stations_csv), pipeline="B",
        supported=supported, skipped=skipped, skipped_reasons=skipped_reasons,
        max_dist=args.max_dist, activation_mask_on=not args.no_activation_mask,
        compress_level=args.compress_level,
        attrs_extra=lowres_attrs,
    )
    logger.info("[%s/%s] 已写出 %s  事件=%s  场站=%d  时间步=%d",
                region, tech, out_path, supported, len(match), times_all.size)
    return out_path


def _first_req_var(tech: str, event_name: str) -> str:
    mapping = {
        "high_temp": "temp_C", "high_wind": "wind_ms", "icing": "rh_pct",
        "hot_humid": "rh_pct", "freezing_rain": "precip_mmh", "rainstorm": "precip_mmh",
        "cold_highwind": "temp_C", "high_humidity": "rh_pct", "dust": "dust_aod",
    }
    return mapping.get(event_name, "unknown")


# =====================================================================
# 主流程
# =====================================================================

def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    args = build_parser().parse_args()

    if args.source != "regional_bcsd":
        raise NotImplementedError(
            f"--source {args.source!r} 尚未在 Pipeline B 中启用"
            "（数据尚未部署）。当前仅支持 'regional_bcsd'。"
        )

    scenario = args.scenario or sm.infer_scenario_from_csv(args.stations_csv)
    logger.info("读取场站 %s（情景=%s）", args.stations_csv, scenario)
    stations_df = sm.load_stations(args.stations_csv)
    logger.info("读取国家边界 %s", args.shp)
    country_shapes = sm.load_country_shapes(args.shp)

    adapter = RegionalBcsdAdapter(SimpleNamespace(
        data_dir=args.data_dir, model=args.model, region=args.region,
        scenario=scenario, years=args.years, output_root=args.output_root,
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
        country_stations = {
            tech: sm.filter_stations_for_country(stations_df, geom, tech) for tech in techs
        }
        n_total = sum(len(v) for v in country_stations.values())
        if n_total == 0:
            logger.info("[%s] 国家内无场站，跳过", region)
            continue
        logger.info("[%s] 国家=%s 场站=%s",
                    region, country_name, {t: len(v) for t, v in country_stations.items()})
        for tech in techs:
            _process_tech(adapter, args, country_stations, region, scenario, tech, country_shapes)

    logger.info("流程 B 完成。")


if __name__ == "__main__":
    main()
