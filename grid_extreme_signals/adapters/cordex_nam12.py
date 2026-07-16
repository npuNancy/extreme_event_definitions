"""CMIP6–CORDEX NAM-12 数据适配器（旋转极点网格）。

**关键要求**：输出必须保留旋转极点网格结构：
  - 主维度：``(time, rlat, rlon)``
  - 二维辅助坐标：``lat(rlat, rlon)``、``lon(rlat, rlon)``
  - 网格映射：``crs`` 变量

文件布局::

    {data_dir}/{gcm_model}/{realization}/{rcm_model}/{scenario}/{var}/{var}_NAM-12_{gcm_model}_*.nc

变量：rsds、tas、uas、vas（pr 可选）。时间分辨率为逐小时。
"""
from __future__ import annotations

import glob
import logging
import os
import re
from pathlib import Path

import numpy as np
import xarray as xr

from grid_extreme_signals.adapters.base import WeatherAdapter, WeatherBundle
from grid_extreme_signals.io_utils import (
    find_cordex_var_files,
    get_var_name,
    output_complete,
    skip_existing,
)
from grid_extreme_signals.time_alignment import (
    RLAT_CANDIDATES,
    RLON_CANDIDATES,
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
)

logger = logging.getLogger(__name__)


class CordexNam12Adapter(WeatherAdapter):
    """CMIP6–CORDEX NAM-12 旋转极点数据适配器。"""

    def __init__(self, args) -> None:
        self.data_dir = args.data_dir
        self.gcm_model = args.gcm_model
        self.realization = args.realization
        self.rcm_model = args.rcm_model
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
                "gcm_model": self.gcm_model,
                "realization": self.realization,
                "rcm_model": self.rcm_model,
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
            / "cordex_nam12"
            / task["gcm_model"]
            / task["realization"]
            / task["rcm_model"]
            / task["scenario"]
        )

    def signal_output_path(self, task: dict, tech: str) -> str:
        return str(
            self._base_dir(task, tech) / "signals" /
            f"extreme_signals_{tech}_NAM-12_{task['gcm_model']}_{task['realization']}_{task['rcm_model']}_{task['scenario']}_{task['year']}.nc"
        )

    def weather_output_path(self, task: dict, tech: str) -> str:
        return str(
            self._base_dir(task, tech) / "weather" /
            f"weather_{tech}_NAM-12_{task['gcm_model']}_{task['realization']}_{task['rcm_model']}_{task['scenario']}_{task['year']}.nc"
        )

    # ------------------------------------------------------------------
    # 文件处理
    # ------------------------------------------------------------------

    def _find_files_for_year(
        self, task: dict, var: str, year: int,
    ) -> Path | None:
        """查找指定变量和年份对应的 CORDEX 文件。"""
        base = (
            Path(task["data_dir"])
            / task["gcm_model"]
            / task["realization"]
            / task["rcm_model"]
            / task["scenario"]
            / var
        )
        pattern = str(base / f"{var}_NAM-12_{task['gcm_model']}_*.nc")
        files = sorted(glob.glob(pattern))
        if not files:
            return None
        # 尝试查找指定年份文件
        for f in files:
            m = re.search(r"_(\d{4})\d{8}-\d{12}\.nc$", os.path.basename(f))
            if m and int(m.group(1)) == year:
                return Path(f)
        # 若没有年份匹配，则使用第一个文件（部分数据是单文件）
        if len(files) == 1:
            return Path(files[0])
        return Path(files[0])

    def _open_var(
        self,
        task: dict,
        var: str,
        year: int,
    ) -> tuple[xr.Dataset, xr.DataArray, str, str | None]:
        """打开 CORDEX 变量文件，并返回 (ds, da, var_name, units)。"""
        fpath = self._find_files_for_year(task, var, year)
        if fpath is None:
            raise FileNotFoundError(f"未找到 CORDEX 文件：变量={var}, 年份={year}")

        ds = xr.open_dataset(str(fpath))
        time_name = find_coord_name(ds, TIME_CANDIDATES)
        rlat_name = find_coord_name(ds, RLAT_CANDIDATES)
        rlon_name = find_coord_name(ds, RLON_CANDIDATES)

        var_name = get_var_name(ds, var, use_bcsd_suffix=False)
        da = ds[var_name]

        # 压缩单长度额外维度
        canonical = {time_name, rlat_name, rlon_name}
        for dim in list(da.dims):
            if dim not in canonical and da.sizes[dim] == 1:
                da = da.isel({dim: 0}, drop=True)
        da = da.transpose(time_name, rlat_name, rlon_name)

        units = ds[var_name].attrs.get("units", None)
        return ds, da, time_name, units

    def _discover_coord_names(self, task: dict, year: int) -> tuple[str, str, str]:
        """从样例文件中发现 time、rlat、rlon 名称。"""
        fpath = self._find_files_for_year(task, "rsds", year)
        if fpath is None:
            fpath = self._find_files_for_year(task, "tas", year)
        if fpath is None:
            raise FileNotFoundError("未找到任何可用于坐标发现的 CORDEX 文件")
        ds = xr.open_dataset(str(fpath))
        time_name = find_coord_name(ds, TIME_CANDIDATES)
        rlat_name = find_coord_name(ds, RLAT_CANDIDATES)
        rlon_name = find_coord_name(ds, RLON_CANDIDATES)
        ds.close()
        return time_name, rlat_name, rlon_name

    def _filter_year(self, da: xr.DataArray, time_name: str, year: int) -> xr.DataArray:
        idx = filter_year(da[time_name], year)
        return da.isel({time_name: idx})

    def _extract_aux_coords(
        self, ds: xr.Dataset, rlat_name: str, rlon_name: str,
    ) -> dict[str, xr.DataArray]:
        """从 CORDEX 数据集中提取二维 lat/lon 和 crs。"""
        aux = {}
        for cname in ("lat", "latitude", "lon", "longitude"):
            if cname in ds.coords:
                aux[cname] = ds[cname]
        if "crs" in ds or "crs" in ds.data_vars:
            aux["crs"] = ds["crs"] if "crs" in ds else ds.data_vars["crs"]
        return aux

    # ------------------------------------------------------------------
    # 风电气象
    # ------------------------------------------------------------------

    def load_wind_weather(self, task: dict) -> WeatherBundle:
        year = task["year"]
        source_files = []
        skipped_inputs: dict[str, str] = {}

        # 打开 uas、vas（必需）
        ds_uas, uas_da, time_name, uas_units = self._open_var(task, "uas", year)
        _, vas_da, _, vas_units = self._open_var(task, "vas", year)
        rlat_name = find_coord_name(ds_uas, RLAT_CANDIDATES)
        rlon_name = find_coord_name(ds_uas, RLON_CANDIDATES)

        source_files.append(str(self._find_files_for_year(task, "uas", year)))
        source_files.append(str(self._find_files_for_year(task, "vas", year)))

        # 可选：tas
        try:
            ds_tas, tas_da, _, tas_units = self._open_var(task, "tas", year)
            source_files.append(str(self._find_files_for_year(task, "tas", year)))
        except FileNotFoundError:
            tas_da = None
            tas_units = None
            skipped_inputs["temp_C"] = "未找到 tas 文件"
            ds_tas = None

        # 筛选年份
        uas_da = self._filter_year(uas_da, time_name, year)
        vas_da = self._filter_year(vas_da, time_name, year)
        wind_time = uas_da[time_name].values

        # 必要时插值 tas
        time_alignment = "uas/vas 原生 :00 瞬时值"
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
                time_alignment = "uas/vas :00 时间轴，tas 已插值"
            else:
                temp_C = tas_to_celsius(tas_da.values, tas_units, allow_inference=self.allow_unit_inference)
        else:
            temp_C = None

        # 风速
        wind_ms = np.sqrt(
            uas_da.values.astype(np.float32) ** 2
            + vas_da.values.astype(np.float32) ** 2,
        )

        # 提取辅助坐标
        aux_coords = self._extract_aux_coords(ds_uas, rlat_name, rlon_name)

        # 构建数据集
        coords = {
            time_name: uas_da[time_name],
            rlat_name: uas_da[rlat_name],
            rlon_name: uas_da[rlon_name],
        }
        coords.update(aux_coords)

        data_vars = {
            "wind_ms": xr.DataArray(wind_ms, dims=(time_name, rlat_name, rlon_name)),
        }
        if temp_C is not None:
            data_vars["temp_C"] = xr.DataArray(temp_C, dims=(time_name, rlat_name, rlon_name))

        ds = xr.Dataset(data_vars, coords=coords)

        skipped_inputs.setdefault("rh_pct", "CORDEX 无湿度数据")
        skipped_inputs.setdefault("dust_aod", "CORDEX 无沙尘数据")
        skipped_inputs.setdefault("rsds", "风电信号不使用该变量")
        skipped_inputs.setdefault("precip_mmh", "风电信号不使用该变量")

        ds_uas.close()
        if ds_tas is not None:
            ds_tas.close()

        return WeatherBundle(
            source="cordex_nam12",
            tech="wind",
            dataset=ds,
            spatial_dims=(rlat_name, rlon_name),
            grid_kind="rotated_pole",
            target_time_axis=time_alignment,
            source_timestep_hours=1.0,
            source_files=source_files,
            skipped_inputs=skipped_inputs,
            attrs_extra={
                "model": task["gcm_model"],
                "region": "NAM-12",
                "scenario": task["scenario"],
                "year": str(year),
                "time_alignment": time_alignment,
            },
        )

    # ------------------------------------------------------------------
    # 光伏气象
    # ------------------------------------------------------------------

    def load_solar_weather(self, task: dict) -> WeatherBundle:
        year = task["year"]
        source_files = []
        skipped_inputs: dict[str, str] = {}

        # 必需：rsds（目标时间轴，通常为 :30）
        ds_rsds, rsds_da, time_name, rsds_units = self._open_var(task, "rsds", year)
        rlat_name = find_coord_name(ds_rsds, RLAT_CANDIDATES)
        rlon_name = find_coord_name(ds_rsds, RLON_CANDIDATES)
        source_files.append(str(self._find_files_for_year(task, "rsds", year)))

        rsds_da = self._filter_year(rsds_da, time_name, year)
        target_times = rsds_da[time_name].values

        # 必需：tas、uas、vas
        ds_tas, tas_da, _, tas_units = self._open_var(task, "tas", year)
        ds_uas, uas_da, _, uas_units = self._open_var(task, "uas", year)
        _, vas_da, _, vas_units = self._open_var(task, "vas", year)
        source_files.append(str(self._find_files_for_year(task, "tas", year)))
        source_files.append(str(self._find_files_for_year(task, "uas", year)))
        source_files.append(str(self._find_files_for_year(task, "vas", year)))

        tas_da = self._filter_year(tas_da, time_name, year)
        uas_da = self._filter_year(uas_da, time_name, year)
        vas_da = self._filter_year(vas_da, time_name, year)

        # 可选：pr
        pr_da = None
        pr_units = None
        try:
            ds_pr, pr_da_raw, _, pr_units = self._open_var(task, "pr", year)
            source_files.append(str(self._find_files_for_year(task, "pr", year)))
            pr_da = self._filter_year(pr_da_raw, time_name, year)
        except FileNotFoundError:
            skipped_inputs["precip_mmh"] = "未找到 pr 文件"
            logger.warning("CORDEX 未找到 pr 文件，将跳过降水事件")
            ds_pr = None

        # 将瞬时变量插值到 rsds 时间轴
        tas_time = tas_da[time_name].values
        uas_time = uas_da[time_name].values
        need_interp = not np.array_equal(tas_time, target_times)

        if need_interp:
            logger.info("将 tas/uas/vas 插值到 rsds :30 时间轴")
            temp_C = tas_to_celsius(
                interp_instantaneous_to_target(tas_da, time_name, target_times),
                tas_units,
                allow_inference=self.allow_unit_inference,
            )
            uas_interp = interp_instantaneous_to_target(uas_da, time_name, target_times)
            vas_interp = interp_instantaneous_to_target(vas_da, time_name, target_times)
            wind_ms = np.sqrt(uas_interp ** 2 + vas_interp ** 2)
            time_alignment = "rsds :30 半点时间轴，tas/uas/vas 已插值"
        else:
            temp_C = tas_to_celsius(tas_da.values, tas_units, allow_inference=self.allow_unit_inference)
            wind_ms = np.sqrt(
                uas_da.values.astype(np.float32) ** 2
                + vas_da.values.astype(np.float32) ** 2,
            )
            time_alignment = "rsds 原生时间轴"

        # rsds
        rsds_wm2 = rsds_to_wm2(
            rsds_da.values, rsds_units,
            timestep_seconds=3600,
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
                timestep_hours=1.0,
                allow_inference=self.allow_unit_inference,
            )

        # 提取辅助坐标
        aux_coords = self._extract_aux_coords(ds_rsds, rlat_name, rlon_name)

        # 构建数据集
        coords = {
            time_name: rsds_da[time_name],
            rlat_name: rsds_da[rlat_name],
            rlon_name: rsds_da[rlon_name],
        }
        coords.update(aux_coords)

        data_vars = {
            "temp_C": xr.DataArray(temp_C, dims=(time_name, rlat_name, rlon_name)),
            "wind_ms": xr.DataArray(wind_ms, dims=(time_name, rlat_name, rlon_name)),
            "rsds": xr.DataArray(rsds_wm2, dims=(time_name, rlat_name, rlon_name)),
        }
        if precip_mmh is not None:
            data_vars["precip_mmh"] = xr.DataArray(precip_mmh, dims=(time_name, rlat_name, rlon_name))

        ds = xr.Dataset(data_vars, coords=coords)

        skipped_inputs.setdefault("rh_pct", "CORDEX 无湿度数据")
        skipped_inputs.setdefault("dust_aod", "CORDEX 无沙尘数据")

        ds_rsds.close()
        ds_tas.close()
        ds_uas.close()
        if ds_pr is not None:
            ds_pr.close()

        return WeatherBundle(
            source="cordex_nam12",
            tech="solar",
            dataset=ds,
            spatial_dims=(rlat_name, rlon_name),
            grid_kind="rotated_pole",
            target_time_axis=time_alignment,
            source_timestep_hours=1.0,
            source_files=source_files,
            skipped_inputs=skipped_inputs,
            attrs_extra={
                "model": task["gcm_model"],
                "region": "NAM-12",
                "scenario": task["scenario"],
                "year": str(year),
                "time_alignment": time_alignment,
            },
        )
