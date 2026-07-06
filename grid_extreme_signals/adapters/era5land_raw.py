"""ERA5-Land 全球逐小时原始数据适配器。

主要难点：
  - ``tp`` 和 ``ssrd`` 是**日累计**变量，需要解累计并处理跨月边界。
  - 全球 0.1° 网格过大，不能一次性加载，因此按月处理。
  - 通过 Magnus 公式由 ``t2m`` / ``d2m`` 计算相对湿度。
  - 可通过 ``--dust_dir`` 选择性接入 MERRA-2 沙尘数据。

文件布局::

    {data_dir}/ERA5_land/global/{var}/{var}_{YYYY}_{MM}.nc

变量：t2m、u10、v10、tp、ssrd、d2m。
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import xarray as xr

from grid_extreme_signals.adapters.base import WeatherAdapter, WeatherBundle
from grid_extreme_signals.time_alignment import (
    LAT_CANDIDATES,
    LON_CANDIDATES,
    TIME_CANDIDATES,
    find_coord_name,
    parse_years,
    parse_months,
)
from grid_extreme_signals.unit_conversion import magnus_rh

logger = logging.getLogger(__name__)


# =====================================================================
# ERA5-Land 解累计（仅此适配器使用）
# =====================================================================

def deaccumulate_era5land(
    current_accum: np.ndarray,
    hours: np.ndarray,
    prev_boundary: np.ndarray | None = None,
    *,
    missing_boundary_policy: str = "zero",
) -> np.ndarray:
    """将 ERA5-Land 日累计量转换为逐小时增量。

    Parameters
    ----------
    current_accum : np.ndarray
        形状 ``(T, ...)``，当前月份的累计值。
    hours : np.ndarray
        形状 ``(T,)``，每个时间步对应的整点小时（0–23）。
    prev_boundary : np.ndarray or None
        形状 ``(...)``，上一月最后一个累计值。若为 ``None`` 且首个时间步不是 01:00，
        则由 missing_boundary_policy 决定处理方式。
    missing_boundary_policy : str
        ``"zero"``：首小时增量置为 0（默认）。
        ``"nan"``：首小时增量置为 NaN。
        ``"current"``：使用当前累计值。

    Returns
    -------
    np.ndarray
        形状 ``(T, ...)``，逐小时非负增量。
    """
    cur = current_accum.astype(np.float32)
    T = cur.shape[0]
    if T == 0:
        return cur

    inc = np.empty_like(cur)

    # 第一个时间步
    has_prev = prev_boundary is not None
    if has_prev:
        inc[0] = cur[0] if hours[0] == 1 else (cur[0] - prev_boundary)
    else:
        if hours[0] == 1:
            inc[0] = cur[0]
        elif missing_boundary_policy == "zero":
            logger.warning("首个小时不是 01:00 且没有前月边界，增量置为 0")
            inc[0] = 0.0
        elif missing_boundary_policy == "nan":
            inc[0] = np.nan
        elif missing_boundary_policy == "current":
            inc[0] = cur[0]
        else:
            raise ValueError(f"Unsupported missing_boundary_policy={missing_boundary_policy!r}")

    # 后续时间步
    if T > 1:
        diff = cur[1:] - cur[:-1]
        reset = hours[1:] == 1
        # 将 reset 广播到空间维度
        for _ in range(cur.ndim - 1):
            reset = reset[:, None]
        reset = np.broadcast_to(reset, diff.shape)
        inc[1:] = np.where(reset, cur[1:], diff)

    # 防御性处理：数据异常导致负增量时使用当前值
    neg = inc < 0
    if np.any(neg):
        inc = np.where(neg, cur, inc)
    inc = np.maximum(inc, 0.0).astype(np.float32)
    return inc


def _previous_year_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def load_previous_accum_boundary(
    data_dir: str | Path,
    var: str,
    year: int,
    month: int,
    time_name: str,
    lat_name: str,
    lon_name: str,
) -> np.ndarray | None:
    """读取上一月最后一个时间步，作为解累计边界。"""
    py, pm = _previous_year_month(year, month)
    root = Path(data_dir) / "ERA5_land" / "global" / var
    prev_file = root / f"{var}_{py:04d}_{pm:02d}.nc"
    if not prev_file.exists():
        logger.warning("未找到上月文件：%s", prev_file)
        return None
    with xr.open_dataset(str(prev_file)) as ds:
        var_name = _get_era5land_var_name(ds, var)
        da = ds[var_name]
        # 压缩额外维度
        for dim in list(da.dims):
            if dim not in {time_name, lat_name, lon_name} and da.sizes[dim] == 1:
                da = da.isel({dim: 0}, drop=True)
        da = da.transpose(time_name, lat_name, lon_name)
        return da.isel({time_name: -1}).values.astype(np.float32)


def _get_era5land_var_name(ds: xr.Dataset, var: str) -> str:
    """解析 ERA5-Land 文件中的变量名。"""
    if var in ds.data_vars:
        return var
    if len(ds.data_vars) == 1:
        return list(ds.data_vars)[0]
    raise KeyError(f"Cannot find {var} in ERA5-Land file. Available: {list(ds.data_vars)}")


# =====================================================================
# 适配器类
# =====================================================================

class Era5LandRawAdapter(WeatherAdapter):
    """ERA5-Land 全球逐小时原始数据适配器。"""

    def __init__(self, args) -> None:
        self.data_dir = args.data_dir
        self.d2m_root = getattr(args, "era5land_d2m_root", None)
        self.dust_dir = getattr(args, "dust_dir", None)
        self.output_root = getattr(args, "output_root", "outputs/grid_extreme_signals")
        self.allow_missing_optional = getattr(args, "allow_missing_optional", False)

    # ------------------------------------------------------------------
    # 任务迭代（按月）
    # ------------------------------------------------------------------

    def iter_tasks(self, args) -> list[dict]:
        y0, y1 = parse_years(args.years)
        month_list = parse_months(getattr(args, "months", None))
        tasks = []
        for y in range(y0, y1 + 1):
            for m in month_list:
                tasks.append({
                    "data_dir": self.data_dir,
                    "year": y,
                    "month": m,
                })
        return tasks

    # ------------------------------------------------------------------
    # 输出路径
    # ------------------------------------------------------------------

    def _base_dir(self, task: dict, tech: str) -> Path:
        return (
            Path(self.output_root)
            / "era5land_raw"
            / "global"
            / str(task["year"])
            / f"{task['month']:02d}"
        )

    def signal_output_path(self, task: dict, tech: str) -> str:
        return str(
            self._base_dir(task, tech) / "signals" /
            f"extreme_signals_{tech}_ERA5Land_{task['year']}_{task['month']:02d}.nc"
        )

    def weather_output_path(self, task: dict, tech: str) -> str:
        return str(
            self._base_dir(task, tech) / "weather" /
            f"weather_{tech}_ERA5Land_{task['year']}_{task['month']:02d}.nc"
        )

    # ------------------------------------------------------------------
    # 文件辅助函数
    # ------------------------------------------------------------------

    def _era5land_file(self, var: str, year: int, month: int) -> Path:
        """构造 ERA5-Land 月文件路径。"""
        if var == "d2m" and self.d2m_root:
            root = Path(self.d2m_root)
        else:
            root = Path(self.data_dir) / "ERA5_land" / "global" / var
        return root / f"{var}_{year:04d}_{month:02d}.nc"

    def _open_var(
        self,
        var: str,
        year: int,
        month: int,
        time_name: str,
        lat_name: str,
        lon_name: str,
    ) -> tuple[xr.DataArray, str | None]:
        """打开 ERA5-Land 月文件，并返回预处理后的 DataArray。"""
        fpath = self._era5land_file(var, year, month)
        ds = xr.open_dataset(str(fpath))
        var_name = _get_era5land_var_name(ds, var)
        units = ds[var_name].attrs.get("units", None)
        da = ds[var_name]
        # 压缩额外维度
        for dim in list(da.dims):
            if dim not in {time_name, lat_name, lon_name} and da.sizes[dim] == 1:
                da = da.isel({dim: 0}, drop=True)
        da = da.transpose(time_name, lat_name, lon_name)
        ds.close()
        return da, units

    def _discover_coords(self, year: int, month: int) -> tuple[str, str, str]:
        fpath = self._era5land_file("t2m", year, month)
        ds = xr.open_dataset(str(fpath))
        time_name = find_coord_name(ds, TIME_CANDIDATES)
        lat_name = find_coord_name(ds, LAT_CANDIDATES)
        lon_name = find_coord_name(ds, LON_CANDIDATES)
        ds.close()
        return time_name, lat_name, lon_name

    # ------------------------------------------------------------------
    # 共享气象加载
    # ------------------------------------------------------------------

    def _load_weather(self, task: dict, tech: str) -> WeatherBundle:
        """风电和光伏共用的核心气象加载逻辑（ERA5-Land 使用同一时间轴）。"""
        year = task["year"]
        month = task["month"]
        source_files = []
        skipped_inputs: dict[str, str] = {}

        time_name, lat_name, lon_name = self._discover_coords(year, month)

        # --- 加载变量 ---
        t2m_da, _ = self._open_var("t2m", year, month, time_name, lat_name, lon_name)
        source_files.append(str(self._era5land_file("t2m", year, month)))

        u10_da, _ = self._open_var("u10", year, month, time_name, lat_name, lon_name)
        source_files.append(str(self._era5land_file("u10", year, month)))

        v10_da, _ = self._open_var("v10", year, month, time_name, lat_name, lon_name)
        source_files.append(str(self._era5land_file("v10", year, month)))

        # 解累计 tp（m → mm）
        tp_da, tp_units = self._open_var("tp", year, month, time_name, lat_name, lon_name)
        source_files.append(str(self._era5land_file("tp", year, month)))

        hours = tp_da[time_name].dt.hour.values.astype(np.int16)
        tp_prev = load_previous_accum_boundary(
            task["data_dir"], "tp", year, month, time_name, lat_name, lon_name,
        )
        tp_inc = deaccumulate_era5land(tp_da.values, hours, tp_prev)
        precip_mmh = tp_inc * 1000.0  # m → mm

        # 解累计 ssrd（J/m² → W/m²）
        ssrd_da, _ = self._open_var("ssrd", year, month, time_name, lat_name, lon_name)
        source_files.append(str(self._era5land_file("ssrd", year, month)))

        ssrd_prev = load_previous_accum_boundary(
            task["data_dir"], "ssrd", year, month, time_name, lat_name, lon_name,
        )
        ssrd_inc = deaccumulate_era5land(ssrd_da.values, hours, ssrd_prev)
        rsds_wm2 = ssrd_inc / 3600.0  # J/m² → W/m² (per hour)

        # 由 t2m/d2m 计算相对湿度
        rh_pct = None
        try:
            d2m_da, _ = self._open_var("d2m", year, month, time_name, lat_name, lon_name)
            source_files.append(str(self._era5land_file("d2m", year, month)))
            rh_pct = magnus_rh(t2m_da.values, d2m_da.values)
        except (FileNotFoundError, KeyError):
            skipped_inputs["rh_pct"] = "未找到 d2m 文件"
            logger.warning("未找到 d2m 文件，将跳过湿度事件")

        # 温度（°C）
        temp_C = t2m_da.values.astype(np.float32) - 273.15

        # 由分量计算风速
        wind_ms = np.sqrt(
            u10_da.values.astype(np.float32) ** 2
            + v10_da.values.astype(np.float32) ** 2,
        )

        # 构建数据集
        # 为保持一致性，将坐标重命名为规范名称
        coord_time = t2m_da[time_name]
        coord_lat = t2m_da[lat_name]
        coord_lon = t2m_da[lon_name]

        dims = (time_name, lat_name, lon_name)
        data_vars = {
            "temp_C": xr.DataArray(temp_C, dims=dims),
            "wind_ms": xr.DataArray(wind_ms, dims=dims),
            "precip_mmh": xr.DataArray(precip_mmh, dims=dims),
            "rsds": xr.DataArray(rsds_wm2, dims=dims),
        }
        if rh_pct is not None:
            data_vars["rh_pct"] = xr.DataArray(rh_pct, dims=dims)

        # 可选沙尘
        dust_aod = None
        if self.dust_dir:
            dust_aod = self._load_dust(task, time_name, lat_name, lon_name, t2m_da)
            if dust_aod is not None:
                data_vars["dust_aod"] = xr.DataArray(dust_aod, dims=dims)
                source_files.append("MERRA-2 dust data")
            else:
                skipped_inputs["dust_aod"] = "未找到沙尘文件"
        else:
            skipped_inputs["dust_aod"] = "未配置 dust_dir"

        ds = xr.Dataset(
            data_vars,
            coords={
                time_name: coord_time,
                lat_name: coord_lat,
                lon_name: coord_lon,
            },
        )

        return WeatherBundle(
            source="era5land_raw",
            tech=tech,
            dataset=ds,
            spatial_dims=(lat_name, lon_name),
            grid_kind="regular_latlon",
            target_time_axis="ERA5-Land native hourly",
            source_timestep_hours=1.0,
            source_files=source_files,
            skipped_inputs=skipped_inputs,
            attrs_extra={
                "model": "ERA5-Land",
                "region": "global",
                "year": str(year),
                "month": f"{month:02d}",
                "time_alignment": "ERA5-Land native hourly",
            },
        )

    def _load_dust(
        self,
        task: dict,
        time_name: str,
        lat_name: str,
        lon_name: str,
        ref_da: xr.DataArray,
    ) -> np.ndarray | None:
        """加载 MERRA-2 沙尘 AOD，并重网格到 ERA5-Land 网格。

        Phase 1：通过 xarray.interp 做简单最近邻重网格。
        """
        import glob as _glob
        year = task["year"]
        month = task["month"]
        dust_dir = Path(self.dust_dir)

        # 查找该月逐日 MERRA-2 文件
        pattern = str(dust_dir / f"{year}" / f"{month:02d}" / f"MERRA2.tavg1_2d_aer_Nx.{year}{month:02d}*.nc4")
        dust_files = sorted(_glob.glob(pattern))
        if not dust_files:
            logger.warning("未找到 MERRA-2 沙尘文件：%s", pattern)
            return None

        # 读取并拼接
        datasets = []
        for f in dust_files:
            ds = xr.open_dataset(f)
            if "DUEXTTAU" in ds.data_vars:
                datasets.append(ds["DUEXTTAU"])
        if not datasets:
            return None

        dust_da = xr.concat(datasets, dim="time")

        # 通过最近邻重网格到 ERA5-Land 网格
        era5_lat = ref_da[lat_name].values
        era5_lon = ref_da[lon_name].values

        # MERRA-2 使用 lat/lon；必要时调整经度约定
        merra_lon = dust_da.coords["lon"].values
        if merra_lon.min() < 0 and era5_lon.min() >= 0:
            # MERRA-2 为 [-180,180)，ERA5-Land 为 [0,360)
            era5_lon_for_interp = era5_lon.copy()
            era5_lon_for_interp[era5_lon_for_interp > 180] -= 360
        else:
            era5_lon_for_interp = era5_lon

        dust_regridded = dust_da.interp(
            lat=xr.DataArray(era5_lat, dims=[lat_name]),
            lon=xr.DataArray(era5_lon_for_interp, dims=[lon_name]),
            method="nearest",
        )

        # 对齐时间
        ref_time = ref_da[time_name].values
        dust_aligned = dust_regridded.interp(
            {list(dust_regridded.dims)[0]: ref_time},
            method="nearest",
        )
        return dust_aligned.values.astype(np.float32)

    # ------------------------------------------------------------------
    # 风电 / 光伏（同一数据，不同技术标签）
    # ------------------------------------------------------------------

    def load_wind_weather(self, task: dict) -> WeatherBundle:
        return self._load_weather(task, "wind")

    def load_solar_weather(self, task: dict) -> WeatherBundle:
        return self._load_weather(task, "solar")
