"""信号运行器：组织加载、标准化、识别、写出流程。

该模块是四类数据源适配器共享的核心逻辑，由 CLI 入口调用，并驱动逐任务处理循环。
"""
from __future__ import annotations

import gc
import logging
import os
import sys
from pathlib import Path

import numpy as np

# 确保项目根目录可导入
_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import registry

from grid_extreme_signals.adapters.base import WeatherBundle
from grid_extreme_signals.io_utils import (
    skip_existing,
    write_signal_dataset,
    write_weather_dataset,
)
from grid_extreme_signals import cf_low_resource

logger = logging.getLogger(__name__)


# =====================================================================
# 事件 → 所需变量映射
# =====================================================================

# 每个事件所需的最小统一变量集合
_EVENT_REQUIRED_VARS: dict[str, list[str]] = {
    "high_temp": ["temp_C"],
    "high_wind": ["wind_ms"],
    "icing": ["temp_C", "rh_pct"],
    "hot_humid": ["temp_C", "rh_pct"],
    "freezing_rain": ["temp_C", "precip_mmh"],
    "rainstorm": ["precip_mmh"],
    "cold_highwind": ["temp_C", "wind_ms"],
    "high_humidity": ["rh_pct"],
    "dust": ["dust_aod"],
}


def _event_required_var(tech: str, event_name: str) -> str:
    """返回事件所需的第一个变量名（用于日志）。"""
    return _EVENT_REQUIRED_VARS.get(event_name, ["unknown"])[0]


# =====================================================================
# 主流程
# =====================================================================

def run_signal_pipeline(adapter, args) -> None:
    """运行完整的信号生成流程。

    参数
    ----
    adapter : WeatherAdapter
        已实例化的数据源适配器。
    args : argparse.Namespace
        解析后的命令行参数。
    """
    # --- 场站筛选保护（Phase 1）---------------------------------
    if getattr(args, "stations_dir", None) is not None:
        raise NotImplementedError(
            "Station-filter mode is reserved but not implemented yet. "
            "Omit --stations_dir to run all-grid mode."
        )

    output_root = getattr(args, "output_root", "outputs/grid_extreme_signals")
    compress_level = getattr(args, "compress_level", 4)
    overwrite = getattr(args, "overwrite", False)
    save_weather = getattr(args, "save_weather", False)
    dry_run = getattr(args, "dry_run", False)
    require_events = getattr(args, "require_events", [])
    low_resource_enabled = not getattr(args, "no_low_resource", False)
    cf_root = getattr(args, "cf_root", "data/cfs")
    lowres_threshold_dir = getattr(
        args, "lowres_threshold_dir", str(cf_low_resource.default_threshold_dir())
    )
    lowres_cf_years = getattr(args, "lowres_cf_years", "2015-2060")
    lowres_grid_lat_chunk = getattr(args, "lowres_grid_lat_chunk", 1)

    tasks = adapter.iter_tasks(args)
    logger.info("任务总数：%d", len(tasks))

    for task in tasks:
        _log_task(adapter, task)
        for tech in ("wind", "solar"):
            signal_path = adapter.signal_output_path(task, tech)
            if skip_existing(signal_path, overwrite):
                logger.info("[跳过] %s", signal_path)
                continue

            if dry_run:
                logger.info("[试运行] 将写出 %s", signal_path)
                continue

            # --- 加载气象数据 ---
            try:
                if tech == "wind":
                    bundle = adapter.load_wind_weather(task)
                else:
                    bundle = adapter.load_solar_weather(task)
            except (FileNotFoundError, ValueError) as e:
                logger.warning("%s 输入缺失：%s，跳过任务", tech, e)
                continue

            # --- 为注册表提取 numpy 数组 ---
            weather_dict = _bundle_to_weather_dict(bundle)

            # --- 计算信号 ---
            masks = registry.simple_signals(tech, weather_dict, skip_missing=True)
            lowres_attrs = {}
            lowres_skip_reason = None
            if low_resource_enabled:
                try:
                    time_name = _bundle_time_name(bundle)
                    cf_file = cf_low_resource.find_cf_file(
                        cf_root,
                        bundle.source,
                        task.get("model", getattr(args, "model", "")),
                        task.get("scenario", getattr(args, "scenario", "")),
                        tech,
                        region=task.get("region", getattr(args, "region", None)),
                        years=lowres_cf_years,
                    )
                    if cf_file is None:
                        lowres_skip_reason = f"未找到 CF 文件：cf_root={cf_root}"
                    else:
                        threshold_file = cf_low_resource.threshold_file_for_tech(
                            lowres_threshold_dir, tech
                        )
                        if not threshold_file.exists():
                            lowres_skip_reason = f"未找到 ERA5Land 低资源阈值文件：{threshold_file}"
                        else:
                            result = cf_low_resource.compute_grid_low_resource(
                                cf_file,
                                tech,
                                bundle.dataset[time_name].values,
                                threshold_file=threshold_file,
                                lat_chunk=lowres_grid_lat_chunk,
                            )
                            masks["low_resource"] = result.mask.astype(bool)
                            lowres_attrs = cf_low_resource.attrs(result)
                except Exception as e:
                    logger.warning("%s 低资源计算失败：%s", tech, e)
                    lowres_skip_reason = f"低资源计算失败：{e}"
            else:
                lowres_skip_reason = "用户通过 --no_low_resource 关闭"

            # --- 支持/跳过的事件 ---
            supported = sorted(masks.keys())
            all_events = set(registry.SIMPLE[tech].keys())
            all_events.add("low_resource")
            skipped = sorted(all_events - set(supported))
            skipped_reasons = {}
            for ev in skipped:
                if ev == "low_resource" and lowres_skip_reason:
                    reason = lowres_skip_reason
                else:
                    req_var = _event_required_var(tech, ev)
                    reason = bundle.skipped_inputs.get(req_var, f"missing {req_var}")
                skipped_reasons[ev] = reason

            # --- 检查 --require_events ---
            for req in require_events:
                if req not in masks:
                    raise RuntimeError(
                        f"Required event '{req}' could not be computed. "
                        f"Missing input: {bundle.skipped_inputs}"
                    )

            # --- 构建输出属性 ---
            attrs_extra = dict(bundle.attrs_extra)
            attrs_extra["supported_events"] = ",".join(supported)
            attrs_extra["skipped_events"] = ",".join(skipped)
            attrs_extra["skipped_event_reasons"] = "; ".join(
                f"{k}: {v}" for k, v in skipped_reasons.items()
            )
            attrs_extra["weather_saved"] = str(save_weather).lower()
            attrs_extra.update(lowres_attrs)

            # --- 写出信号文件 ---
            write_signal_dataset(
                bundle, masks, signal_path,
                attrs_extra=attrs_extra,
                compress_level=compress_level,
            )
            logger.info("[完成] %s  事件=%s", signal_path, supported)

            # --- 按需写出气象文件 ---
            if save_weather:
                weather_path = adapter.weather_output_path(task, tech)
                write_weather_dataset(bundle, weather_path, compress_level)
                logger.info("[气象] %s", weather_path)

            # --- 释放内存 ---
            del bundle, weather_dict, masks
            gc.collect()

    logger.info("流程完成。")


# =====================================================================
# 辅助函数
# =====================================================================

def _bundle_to_weather_dict(bundle: WeatherBundle) -> dict[str, np.ndarray]:
    """从 *bundle* 提取统一气象变量，并转换为 float32 numpy 数组。"""
    weather: dict[str, np.ndarray] = {}
    for var_name in ("temp_C", "wind_ms", "precip_mmh", "rsds", "rh_pct", "dust_aod"):
        if var_name in bundle.dataset.data_vars:
            weather[var_name] = bundle.dataset[var_name].values.astype(np.float32)
    return weather


def _bundle_time_name(bundle: WeatherBundle) -> str:
    """返回 bundle 的时间维度名。"""
    spatial = set(bundle.spatial_dims)
    sample = next(iter(bundle.dataset.data_vars.values()))
    return next(d for d in sample.dims if d not in spatial)


def _log_task(adapter, task: dict) -> None:
    """记录当前任务的可读描述。"""
    parts = [f"{k}={v}" for k, v in task.items() if v is not None]
    logger.info("正在处理：%s", " ".join(parts))
