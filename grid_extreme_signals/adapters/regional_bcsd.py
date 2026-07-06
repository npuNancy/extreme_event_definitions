"""CMIP6–ERA5Land BCSD 数据适配器（多数区域/国家）。

文件布局::

    {data_dir}/{model}/{region}/{model}/{var}_3h_bcsd_on_0p1deg_{region}_{model}_{scenario}_*.nc

变量：pr、rsds、tas、uas、vas。时间分辨率为 3 小时，空间网格为规则经纬度。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

import numpy as np
import xarray as xr

from grid_extreme_signals.adapters.base import WeatherAdapter, WeatherBundle
from grid_extreme_signals.io_utils import (
    discover_regions,
    find_bcsd_file,
    get_var_name,
    is_valid_region_name,
    output_complete,
    prepare_dataarray,
    skip_existing,
    validate_same_spatial_grid,
)
from grid_extreme_signals.time_alignment import (
    LAT_CANDIDATES,
    LON_CANDIDATES,
    TIME_CANDIDATES,
    build_time_index,
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


def _regional_bcsd_pr_units(units: str | None) -> str | None:
    """返回 regional BCSD 输出的降水单位。

    Step 6 的 BCSD 文件可能丢失 ``units`` 属性，但 ``pr_bcsd`` 数值是以
    kg m-2 s-1（等价于 mm s-1）表示的降水通量。
    """
    if isinstance(units, bytes):
        units = units.decode("utf-8")
    if units is None or str(units).strip() == "":
        return "kg m-2 s-1"
    return units


class RegionalBcsdAdapter(WeatherAdapter):
    """regional CMIP6–ERA5Land BCSD 数据适配器。"""

    def __init__(self, args) -> None:
        self.data_dir = args.data_dir
        self.model = args.model
        self.region = args.region
        self.scenario = args.scenario
        self.output_root = getattr(args, "output_root", "outputs/grid_extreme_signals")
        self.allow_unit_inference = getattr(args, "allow_unit_inference", False)
        self.allow_missing_optional = getattr(args, "allow_missing_optional", False)

    # ------------------------------------------------------------------
    # 任务迭代
    # ------------------------------------------------------------------

    def iter_tasks(self, args) -> list[dict]:
        y0, y1 = parse_years(args.years)
        regions = self._resolve_regions()
        tasks = []
        for region in regions:
            for y in range(y0, y1 + 1):
                tasks.append({
                    "data_dir": self.data_dir,
                    "model": self.model,
                    "region": region,
                    "scenario": self.scenario,
                    "year": y,
                })
        return tasks

    def _resolve_regions(self) -> list[str]:
        if self.region == "all":
            return discover_regions(self.data_dir, self.model)
        if not is_valid_region_name(self.region):
            raise ValueError(f"Invalid region name: {self.region!r}")
        return [self.region]

    # ------------------------------------------------------------------
    # 输出路径
    # ------------------------------------------------------------------

    def _base_dir(self, task: dict, tech: str) -> Path:
        return (
            Path(self.output_root)
            / "regional_bcsd"
            / task["model"]
            / task["region"]
            / task["scenario"]
        )

    def signal_output_path(self, task: dict, tech: str) -> str:
        return str(
            self._base_dir(task, tech) / "signals" /
            f"extreme_signals_{tech}_{task['model']}_{task['region']}_{task['scenario']}_{task['year']}.nc"
        )

    def weather_output_path(self, task: dict, tech: str) -> str:
        return str(
            self._base_dir(task, tech) / "weather" /
            f"weather_{tech}_{task['model']}_{task['region']}_{task['scenario']}_{task['year']}.nc"
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
        """打开文件、解析变量名，并返回预处理后的 DataArray 与单位。"""
        fpath = find_bcsd_file(
            task["data_dir"], task["model"], task["region"],
            task["scenario"], var,
        )
        ds = xr.open_dataset(fpath)
        var_name = get_var_name(ds, var)
        units = ds[var_name].attrs.get("units", None)
        da = prepare_dataarray(ds, var, time_name, lat_name, lon_name)
        ds.close()
        return da, str(fpath), units

    def _discover_coords(self, task: dict) -> tuple[str, str, str]:
        """从样例文件中发现坐标名。"""
        fpath = find_bcsd_file(
            task["data_dir"], task["model"], task["region"],
            task["scenario"], "rsds",
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

        # 必需：uas、vas
        uas_da, f_uas, uas_units = self._open_and_prepare(task, "uas", time_name, lat_name, lon_name)
        vas_da, f_vas, vas_units = self._open_and_prepare(task, "vas", time_name, lat_name, lon_name)
        source_files.extend([f_uas, f_vas])

        # 可选：tas（high_temp 信号需要）
        try:
            tas_da, f_tas, tas_units = self._open_and_prepare(task, "tas", time_name, lat_name, lon_name)
            source_files.append(f_tas)
        except FileNotFoundError:
            tas_da = None
            tas_units = None
            skipped_inputs["temp_C"] = "未找到 tas 文件"

        # 筛选目标年份
        uas_da = self._filter_year(uas_da, time_name, year)
        vas_da = self._filter_year(vas_da, time_name, year)
        wind_time = uas_da[time_name].values

        # 必要时将 tas 插值到风速时间轴
        if tas_da is not None:
            tas_da = self._filter_year(tas_da, time_name, year)
            tas_time = tas_da[time_name].values
            if not np.array_equal(tas_time, wind_time):
                logger.info("将 tas 插值到风速时间轴")
                temp_C = tas_to_celsius(
                    interp_instantaneous_to_target(tas_da, time_name, wind_time),
                    tas_units,
                    allow_inference=self.allow_unit_inference,
                )
                time_alignment = "tas interpolated to uas/vas time"
            else:
                temp_C = tas_to_celsius(
                    tas_da.values, tas_units,
                    allow_inference=self.allow_unit_inference,
                )
                time_alignment = "uas/vas instantaneous native"
        else:
            temp_C = None
            time_alignment = "uas/vas instantaneous native"

        # 由分量计算风速
        wind_ms = np.sqrt(
            uas_da.values.astype(np.float32) ** 2
            + vas_da.values.astype(np.float32) ** 2
        )

        # 构建数据集
        ds = xr.Dataset(
            {
                "wind_ms": xr.DataArray(wind_ms, dims=(time_name, lat_name, lon_name)),
            },
            coords={
                time_name: uas_da[time_name],
                lat_name: uas_da[lat_name],
                lon_name: uas_da[lon_name],
            },
        )
        if temp_C is not None:
            ds["temp_C"] = xr.DataArray(temp_C, dims=(time_name, lat_name, lon_name))

        # 标记不可用输入
        skipped_inputs.setdefault("rh_pct", "BCSD 无湿度数据")
        skipped_inputs.setdefault("dust_aod", "BCSD 无沙尘数据")
        skipped_inputs.setdefault("rsds", "风电信号不使用该变量")
        skipped_inputs.setdefault("precip_mmh", "风电信号不使用该变量")

        return WeatherBundle(
            source="regional_bcsd",
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
                "region": task["region"],
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

        # 必需：rsds（光伏目标时间轴）
        rsds_da, f_rsds, rsds_units = self._open_and_prepare(task, "rsds", time_name, lat_name, lon_name)
        source_files.append(f_rsds)
        rsds_da = self._filter_year(rsds_da, time_name, year)
        target_times = rsds_da[time_name].values

        # 必需：tas
        tas_da, f_tas, tas_units = self._open_and_prepare(task, "tas", time_name, lat_name, lon_name)
        source_files.append(f_tas)
        tas_da = self._filter_year(tas_da, time_name, year)

        # 必需：uas、vas
        uas_da, f_uas, uas_units = self._open_and_prepare(task, "uas", time_name, lat_name, lon_name)
        vas_da, f_vas, vas_units = self._open_and_prepare(task, "vas", time_name, lat_name, lon_name)
        source_files.extend([f_uas, f_vas])
        uas_da = self._filter_year(uas_da, time_name, year)
        vas_da = self._filter_year(vas_da, time_name, year)

        # 可选：pr
        pr_da = None
        pr_units = None
        try:
            pr_da, f_pr, pr_units = self._open_and_prepare(task, "pr", time_name, lat_name, lon_name)
            source_files.append(f_pr)
            pr_da = self._filter_year(pr_da, time_name, year)
        except FileNotFoundError:
            if not self.allow_missing_optional:
                logger.warning("%s/%s 未找到 pr 文件，将跳过降水事件",
                               task["model"], task["region"])
            skipped_inputs["precip_mmh"] = "未找到 pr 文件"

        # 校验空间网格
        validate_same_spatial_grid(
            rsds_da.to_dataset(name="rsds"),
            {"tas": tas_da.to_dataset(name="tas"),
             "uas": uas_da.to_dataset(name="uas"),
             "vas": vas_da.to_dataset(name="vas")},
            lat_name, lon_name,
        )

        # 将瞬时变量插值到 rsds 时间轴（方案 4）
        tas_time = tas_da[time_name].values
        uas_time = uas_da[time_name].values
        need_interp_tas = not np.array_equal(tas_time, target_times)
        need_interp_wind = not np.array_equal(uas_time, target_times)

        if need_interp_tas:
            logger.info("将 tas 插值到 rsds 时间轴")
            temp_C = tas_to_celsius(
                interp_instantaneous_to_target(tas_da, time_name, target_times),
                tas_units,
                allow_inference=self.allow_unit_inference,
            )
        else:
            temp_C = tas_to_celsius(tas_da.values, tas_units, allow_inference=self.allow_unit_inference)

        if need_interp_wind:
            logger.info("将 uas/vas 插值到 rsds 时间轴")
            uas_interp = interp_instantaneous_to_target(uas_da, time_name, target_times)
            vas_interp = interp_instantaneous_to_target(vas_da, time_name, target_times)
            wind_ms = np.sqrt(uas_interp ** 2 + vas_interp ** 2)
        else:
            wind_ms = np.sqrt(
                uas_da.values.astype(np.float32) ** 2
                + vas_da.values.astype(np.float32) ** 2
            )

        # rsds 单位转换（BCSD rsds 通常已经是 W m-2）
        rsds_wm2 = rsds_to_wm2(
            rsds_da.values, rsds_units,
            timestep_seconds=3 * 3600,
            allow_inference=self.allow_unit_inference,
        )

        # pr 处理
        precip_mmh = None
        if pr_da is not None:
            pr_time = pr_da[time_name].values
            if not np.array_equal(pr_time, target_times):
                raise ValueError(
                    "pr time axis does not match rsds time axis. "
                    "Do not silently intersect or interpolate accumulated precipitation. "
                    "Check your input data or provide matching time axes."
                )
            precip_mmh = pr_to_mmh(
                pr_da.values, _regional_bcsd_pr_units(pr_units),
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

        # 标记不可用输入
        skipped_inputs.setdefault("rh_pct", "BCSD 无湿度数据")
        skipped_inputs.setdefault("dust_aod", "BCSD 无沙尘数据")

        interp_desc = []
        if need_interp_tas:
            interp_desc.append("tas→rsds")
        if need_interp_wind:
            interp_desc.append("uas/vas→rsds")
        time_alignment = ("rsds half-point, " + ", ".join(interp_desc) + " interpolated") if interp_desc else "rsds native"

        return WeatherBundle(
            source="regional_bcsd",
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
                "region": task["region"],
                "scenario": task["scenario"],
                "year": str(year),
                "time_alignment": time_alignment,
            },
        )
