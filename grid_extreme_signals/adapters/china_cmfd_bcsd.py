"""中国区域 CMIP6–CMFD BCSD 数据适配器。

与 ``regional_bcsd`` 的主要区别：
  - 使用 **sfcWind**（10 m 标量风速），而不是 uas/vas 分量。
  - 数据布局中没有区域子目录。
  - ``pr`` 是可选变量；缺失时跳过降水事件。

文件布局::

    {data_dir}/{model}/{var}_3h_bcsd_on_0.1deg_china_{scenario}_*.nc
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import xarray as xr

from grid_extreme_signals.adapters.base import WeatherAdapter, WeatherBundle
from grid_extreme_signals.io_utils import (
    find_china_bcsd_file,
    get_var_name,
    output_complete,
    prepare_dataarray,
    skip_existing,
    validate_same_spatial_grid,
)
from grid_extreme_signals.time_alignment import (
    LAT_CANDIDATES,
    LON_CANDIDATES,
    TIME_CANDIDATES,
    filter_year,
    find_coord_name,
    interp_instantaneous_to_target,
    parse_years,
)
from grid_extreme_signals.unit_conversion import (
    pr_to_mmh,
    rsds_to_wm2,
    tas_to_celsius,
    wind_to_ms,
)

logger = logging.getLogger(__name__)


class ChinaCmfdBcsdAdapter(WeatherAdapter):
    """中国区域 CMIP6–CMFD BCSD 数据适配器。"""

    def __init__(self, args) -> None:
        self.data_dir = args.data_dir
        self.model = args.model
        self.scenario = args.scenario
        self.output_root = getattr(args, "output_root", "outputs/grid_extreme_signals")
        self.allow_unit_inference = getattr(args, "allow_unit_inference", False)
        self.allow_missing_optional = getattr(args, "allow_missing_optional", False)

    # ------------------------------------------------------------------
    # 任务迭代
    # ------------------------------------------------------------------

    def iter_tasks(self, args) -> list[dict]:
        y0, y1 = parse_years(args.years)
        tasks = []
        for y in range(y0, y1 + 1):
            tasks.append({
                "data_dir": self.data_dir,
                "model": self.model,
                "scenario": self.scenario,
                "year": y,
            })
        return tasks

    # ------------------------------------------------------------------
    # 输出路径
    # ------------------------------------------------------------------

    def _base_dir(self, task: dict, tech: str) -> Path:
        return (
            Path(self.output_root)
            / "china_cmfd_bcsd"
            / task["model"]
            / task["scenario"]
        )

    def signal_output_path(self, task: dict, tech: str) -> str:
        return str(
            self._base_dir(task, tech) / "signals" /
            f"extreme_signals_{tech}_china_{task['model']}_{task['scenario']}_{task['year']}.nc"
        )

    def weather_output_path(self, task: dict, tech: str) -> str:
        return str(
            self._base_dir(task, tech) / "weather" /
            f"weather_{tech}_china_{task['model']}_{task['scenario']}_{task['year']}.nc"
        )

    # ------------------------------------------------------------------
    # 共享辅助函数
    # ------------------------------------------------------------------

    def _open_and_prepare(
        self,
        task: dict,
        var: str,
        time_name: str,
        lat_name: str,
        lon_name: str,
    ) -> tuple[xr.DataArray, str, str | None]:
        fpath = find_china_bcsd_file(
            task["data_dir"], task["model"], task["scenario"], var,
        )
        ds = xr.open_dataset(fpath)
        var_name = get_var_name(ds, var)
        units = ds[var_name].attrs.get("units", None)
        da = prepare_dataarray(ds, var, time_name, lat_name, lon_name)
        ds.close()
        return da, str(fpath), units

    def _discover_coords(self, task: dict) -> tuple[str, str, str]:
        fpath = find_china_bcsd_file(
            task["data_dir"], task["model"], task["scenario"], "rsds",
        )
        ds = xr.open_dataset(fpath)
        time_name = find_coord_name(ds, TIME_CANDIDATES)
        lat_name = find_coord_name(ds, LAT_CANDIDATES)
        lon_name = find_coord_name(ds, LON_CANDIDATES)
        ds.close()
        return time_name, lat_name, lon_name

    def _filter_year(self, da: xr.DataArray, time_name: str, year: int) -> xr.DataArray:
        idx = filter_year(da[time_name], year)
        return da.isel({time_name: idx})

    # ------------------------------------------------------------------
    # 风电气象
    # ------------------------------------------------------------------

    def load_wind_weather(self, task: dict) -> WeatherBundle:
        time_name, lat_name, lon_name = self._discover_coords(task)
        year = task["year"]
        source_files = []
        skipped_inputs: dict[str, str] = {}

        # 必需：sfcWind
        sfcwind_da, f_sw, sw_units = self._open_and_prepare(
            task, "sfcWind", time_name, lat_name, lon_name,
        )
        source_files.append(f_sw)

        # 可选：tas
        try:
            tas_da, f_tas, tas_units = self._open_and_prepare(
                task, "tas", time_name, lat_name, lon_name,
            )
            source_files.append(f_tas)
        except FileNotFoundError:
            tas_da = None
            tas_units = None
            skipped_inputs["temp_C"] = "未找到 tas 文件"

        # 筛选年份
        sfcwind_da = self._filter_year(sfcwind_da, time_name, year)
        wind_time = sfcwind_da[time_name].values

        # 必要时插值 tas
        time_alignment = "sfcWind 原生瞬时值"
        if tas_da is not None:
            tas_da = self._filter_year(tas_da, time_name, year)
            tas_time = tas_da[time_name].values
            if not np.array_equal(tas_time, wind_time):
                logger.info("将 tas 插值到 sfcWind 时间轴")
                temp_C = tas_to_celsius(
                    interp_instantaneous_to_target(tas_da, time_name, wind_time),
                    tas_units,
                    allow_inference=self.allow_unit_inference,
                )
                time_alignment = "sfcWind 原生时间轴，tas 已插值"
            else:
                temp_C = tas_to_celsius(
                    tas_da.values, tas_units,
                    allow_inference=self.allow_unit_inference,
                )
        else:
            temp_C = None

        # 风速直接使用 sfcWind
        wind_ms = wind_to_ms(
            sfcwind_da.values, sw_units,
            allow_inference=self.allow_unit_inference,
        )

        # 构建数据集
        data_vars = {
            "wind_ms": xr.DataArray(wind_ms, dims=(time_name, lat_name, lon_name)),
        }
        if temp_C is not None:
            data_vars["temp_C"] = xr.DataArray(temp_C, dims=(time_name, lat_name, lon_name))

        ds = xr.Dataset(
            data_vars,
            coords={
                time_name: sfcwind_da[time_name],
                lat_name: sfcwind_da[lat_name],
                lon_name: sfcwind_da[lon_name],
            },
        )

        skipped_inputs.setdefault("rh_pct", "BCSD 无湿度数据")
        skipped_inputs.setdefault("dust_aod", "BCSD 无沙尘数据")
        skipped_inputs.setdefault("rsds", "风电信号不使用该变量")
        skipped_inputs.setdefault("precip_mmh", "风电信号不使用该变量")

        return WeatherBundle(
            source="china_cmfd_bcsd",
            tech="wind",
            dataset=ds,
            spatial_dims=(lat_name, lon_name),
            grid_kind="regular_latlon",
            target_time_axis=time_alignment,
            source_timestep_hours=3.0,
            source_files=source_files,
            skipped_inputs=skipped_inputs,
            attrs_extra={
                "model": task["model"],
                "region": "china",
                "scenario": task["scenario"],
                "year": str(year),
                "time_alignment": time_alignment,
            },
        )

    # ------------------------------------------------------------------
    # 光伏气象
    # ------------------------------------------------------------------

    def load_solar_weather(self, task: dict) -> WeatherBundle:
        time_name, lat_name, lon_name = self._discover_coords(task)
        year = task["year"]
        source_files = []
        skipped_inputs: dict[str, str] = {}

        # 必需：rsds（目标时间轴）
        rsds_da, f_rsds, rsds_units = self._open_and_prepare(
            task, "rsds", time_name, lat_name, lon_name,
        )
        source_files.append(f_rsds)
        rsds_da = self._filter_year(rsds_da, time_name, year)
        target_times = rsds_da[time_name].values

        # 必需：tas
        tas_da, f_tas, tas_units = self._open_and_prepare(
            task, "tas", time_name, lat_name, lon_name,
        )
        source_files.append(f_tas)
        tas_da = self._filter_year(tas_da, time_name, year)

        # 必需：sfcWind
        sfcwind_da, f_sw, sw_units = self._open_and_prepare(
            task, "sfcWind", time_name, lat_name, lon_name,
        )
        source_files.append(f_sw)
        sfcwind_da = self._filter_year(sfcwind_da, time_name, year)

        # 可选：pr
        pr_da = None
        pr_units = None
        try:
            pr_da, f_pr, pr_units = self._open_and_prepare(
                task, "pr", time_name, lat_name, lon_name,
            )
            source_files.append(f_pr)
            pr_da = self._filter_year(pr_da, time_name, year)
        except FileNotFoundError:
            skipped_inputs["precip_mmh"] = "未找到 pr 文件"
            logger.warning("未找到 pr 文件，将跳过降水事件")

        # 校验空间网格
        validate_same_spatial_grid(
            rsds_da.to_dataset(name="rsds"),
            {"tas": tas_da.to_dataset(name="tas"),
             "sfcWind": sfcwind_da.to_dataset(name="sfcWind")},
            lat_name, lon_name,
        )

        # 将瞬时变量插值到 rsds 时间轴（方案 4）
        tas_time = tas_da[time_name].values
        sw_time = sfcwind_da[time_name].values
        need_interp_tas = not np.array_equal(tas_time, target_times)
        need_interp_sw = not np.array_equal(sw_time, target_times)

        if need_interp_tas:
            logger.info("将 tas 插值到 rsds 时间轴")
            temp_C = tas_to_celsius(
                interp_instantaneous_to_target(tas_da, time_name, target_times),
                tas_units,
                allow_inference=self.allow_unit_inference,
            )
        else:
            temp_C = tas_to_celsius(tas_da.values, tas_units, allow_inference=self.allow_unit_inference)

        if need_interp_sw:
            logger.info("将 sfcWind 插值到 rsds 时间轴")
            wind_interp = interp_instantaneous_to_target(sfcwind_da, time_name, target_times)
            wind_ms = wind_to_ms(wind_interp, sw_units, allow_inference=self.allow_unit_inference)
        else:
            wind_ms = wind_to_ms(sfcwind_da.values, sw_units, allow_inference=self.allow_unit_inference)

        # rsds
        rsds_wm2 = rsds_to_wm2(
            rsds_da.values, rsds_units,
            timestep_seconds=3 * 3600,
            allow_inference=self.allow_unit_inference,
        )

        # pr
        precip_mmh = None
        if pr_da is not None:
            pr_time = pr_da[time_name].values
            if not np.array_equal(pr_time, target_times):
                raise ValueError(
                    "pr 时间轴与 rsds 时间轴不一致。"
                    "不要静默取交集或插值累计降水。"
                )
            precip_mmh = pr_to_mmh(
                pr_da.values, pr_units,
                timestep_hours=3.0,
                allow_inference=self.allow_unit_inference,
            )

        # 构建数据集
        data_vars = {
            "temp_C": xr.DataArray(temp_C, dims=(time_name, lat_name, lon_name)),
            "wind_ms": xr.DataArray(wind_ms, dims=(time_name, lat_name, lon_name)),
            "rsds": xr.DataArray(rsds_wm2, dims=(time_name, lat_name, lon_name)),
        }
        if precip_mmh is not None:
            data_vars["precip_mmh"] = xr.DataArray(precip_mmh, dims=(time_name, lat_name, lon_name))

        ds = xr.Dataset(
            data_vars,
            coords={
                time_name: rsds_da[time_name],
                lat_name: rsds_da[lat_name],
                lon_name: rsds_da[lon_name],
            },
        )

        skipped_inputs.setdefault("rh_pct", "BCSD 无湿度数据")
        skipped_inputs.setdefault("dust_aod", "BCSD 无沙尘数据")

        interp_desc = []
        if need_interp_tas:
            interp_desc.append("tas→rsds")
        if need_interp_sw:
            interp_desc.append("sfcWind→rsds")
        time_alignment = ("rsds 半点时间轴，" + "、".join(interp_desc) + " 已插值") if interp_desc else "rsds 原生时间轴"

        return WeatherBundle(
            source="china_cmfd_bcsd",
            tech="solar",
            dataset=ds,
            spatial_dims=(lat_name, lon_name),
            grid_kind="regular_latlon",
            target_time_axis=time_alignment,
            source_timestep_hours=3.0,
            source_files=source_files,
            skipped_inputs=skipped_inputs,
            attrs_extra={
                "model": task["model"],
                "region": "china",
                "scenario": task["scenario"],
                "year": str(year),
                "time_alignment": time_alignment,
            },
        )
