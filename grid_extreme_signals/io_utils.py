"""共享 I/O 工具：文件发现、NetCDF 变量解析和原子写入。

文件匹配模式移植自 ``calculate_bcsd_cfs/`` 中的容量因子参考脚本，并适配到极端天气信号输出。
"""
from __future__ import annotations

import glob
import logging
import os
from pathlib import Path
from typing import Sequence

import numpy as np
import xarray as xr

from grid_extreme_signals.adapters.base import WeatherBundle

logger = logging.getLogger(__name__)


# =====================================================================
# 文件发现 — regional BCSD
# =====================================================================

def find_bcsd_file(
    data_dir: str | Path,
    model: str,
    region: str,
    scenario: str,
    var: str,
) -> Path:
    """使用级联 glob 模式查找 BCSD 变量文件。

    优先级：
      1. ``{var}_3h_bcsd_on_0p1deg_{region}_{model}_{scenario}_*.nc``
      2. ``{var}_*_{region}_{model}_{scenario}_*.nc``
      3. ``{var}_*{scenario}*.nc``
    """
    base = Path(data_dir) / model / region / model
    patterns = [
        str(base / f"{var}_3h_bcsd_on_0p1deg_{region}_{model}_{scenario}_*.nc"),
        str(base / f"{var}_*_{region}_{model}_{scenario}_*.nc"),
        str(base / f"{var}_*{scenario}*.nc"),
    ]
    for pat in patterns:
        files = sorted(glob.glob(pat))
        if files:
            if len(files) > 1:
                logger.warning("%s 匹配到 %d 个文件，使用第一个：%s", var, len(files), files[0])
            return Path(files[0])
    raise FileNotFoundError(
        f"Cannot find {var} file. Tried:\n  " + "\n  ".join(patterns)
    )


# =====================================================================
# 文件发现 — China CMFD BCSD
# =====================================================================

def find_china_bcsd_file(
    data_dir: str | Path,
    model: str,
    scenario: str,
    var: str,
) -> Path:
    """查找中国区域 CMFD BCSD 变量文件。

    优先级：
      1. ``{var}_3h_bcsd_on_0.1deg_china_{scenario}_*.nc``
      2. ``{var}_*china_{scenario}_*.nc``
      3. ``{var}_*{scenario}*.nc``
    """
    base = Path(data_dir) / model
    patterns = [
        str(base / f"{var}_3h_bcsd_on_0.1deg_china_{scenario}_*.nc"),
        str(base / f"{var}_*china_{scenario}_*.nc"),
        str(base / f"{var}_*{scenario}*.nc"),
    ]
    for pat in patterns:
        files = sorted(glob.glob(pat))
        if files:
            if len(files) > 1:
                logger.warning("%s 匹配到 %d 个文件，使用第一个：%s", var, len(files), files[0])
            return Path(files[0])
    raise FileNotFoundError(
        f"Cannot find {var} file for China CMFD. Tried:\n  " + "\n  ".join(patterns)
    )


# =====================================================================
# 文件发现 — CORDEX NAM-12
# =====================================================================

def find_cordex_var_files(
    data_dir: str | Path,
    gcm_model: str,
    realization: str,
    rcm_model: str,
    scenario: str,
    var: str,
    year_range: tuple[int, int],
) -> dict[int, Path]:
    """查找某个变量的 CORDEX NAM-12 分年文件。

    对 *year_range* 中每个有文件的年份返回 ``{year: Path}``。
    """
    import re
    base = Path(data_dir) / gcm_model / realization / rcm_model / scenario / var
    pattern = str(base / f"{var}_NAM-12_{gcm_model}_*.nc")
    files = sorted(glob.glob(pattern))
    result: dict[int, Path] = {}
    for f in files:
        m = re.search(r"_(\d{4})\d{8}-\d{12}\.nc$", os.path.basename(f))
        if m:
            y = int(m.group(1))
            if year_range[0] <= y <= year_range[1]:
                result[y] = Path(f)
    if not result:
        # 兜底：不做严格年份提取
        for f in files:
            result[0] = Path(f)  # 单文件情况
            break
    return result


# =====================================================================
# NetCDF 变量名解析
# =====================================================================

def get_var_name(ds: xr.Dataset, preferred: str, *, use_bcsd_suffix: bool = True) -> str:
    """解析 *ds* 中的数据变量名。

    优先级（当 *use_bcsd_suffix* 为 True 时）：
      1. ``{preferred}_bcsd``
      2. ``{preferred}``
      3. 唯一的数据变量（当数据集中刚好只有一个数据变量时）

    当 *use_bcsd_suffix* 为 False 时，跳过第 1 步。
    """
    if use_bcsd_suffix:
        candidate = f"{preferred}_bcsd"
        if candidate in ds.data_vars:
            return candidate
    if preferred in ds.data_vars:
        return preferred
    if len(ds.data_vars) == 1:
        return list(ds.data_vars)[0]
    raise KeyError(
        f"Cannot resolve variable '{preferred}' in dataset.  "
        f"Available: {list(ds.data_vars)}"
    )


# =====================================================================
# 空间网格校验
# =====================================================================

def _coord_values_close(a: np.ndarray, b: np.ndarray, label: str = "", atol: float = 1e-6) -> bool:
    try:
        return np.allclose(a, b, rtol=0.0, atol=atol, equal_nan=True)
    except (ValueError, TypeError):
        return False


def validate_same_spatial_grid(
    ds_ref: xr.Dataset,
    others: dict[str, xr.Dataset],
    lat_name: str,
    lon_name: str,
) -> None:
    """若任一 *other* 数据集的 lat/lon 网格不同，则抛出 ``ValueError``。"""
    ref_lat = ds_ref[lat_name].values
    ref_lon = ds_ref[lon_name].values
    for name, ds in others.items():
        for cname, ref in [(lat_name, ref_lat), (lon_name, ref_lon)]:
            if cname not in ds.coords and cname not in ds.dims:
                raise KeyError(f"{name}: missing coordinate '{cname}'")
            if not _coord_values_close(ref, ds[cname].values, f"{name}.{cname}"):
                raise ValueError(f"{name}: {cname} grid does not match reference")


# =====================================================================
# DataArray 预处理
# =====================================================================

def prepare_dataarray(
    ds: xr.Dataset,
    preferred_var: str,
    time_name: str,
    *spatial_names: str,
    use_bcsd_suffix: bool = True,
) -> xr.DataArray:
    """提取变量，压缩单元素额外维度，并转置为标准维度顺序。"""
    var = get_var_name(ds, preferred_var, use_bcsd_suffix=use_bcsd_suffix)
    da = ds[var]
    canonical = {time_name, *spatial_names}
    for dim in list(da.dims):
        if dim not in canonical:
            if da.sizes[dim] == 1:
                da = da.isel({dim: 0}, drop=True)
            else:
                raise ValueError(
                    f"Variable {var} has non-singleton extra dim {dim}={da.sizes[dim]}"
                )
    return da.transpose(time_name, *spatial_names)


# =====================================================================
# 区域发现
# =====================================================================

def is_valid_region_name(name: str) -> bool:
    """过滤无效区域目录名。"""
    return (
        bool(name)
        and not name.startswith("_")
        and not name.endswith("_")
        and not name.endswith("_repeated")
    )


def discover_regions(data_dir: str | Path, model: str) -> list[str]:
    """列出 ``{data_dir}/{model}/`` 下的有效区域子目录。"""
    root = Path(data_dir) / model
    return sorted(
        p.name for p in root.glob("*")
        if p.is_dir() and is_valid_region_name(p.name)
    )


# =====================================================================
# 输出文件辅助函数
# =====================================================================

def output_complete(nc_path: str | Path) -> bool:
    """若 *nc_path* 存在、可读取且 time 长度为正，则返回 True。"""
    p = Path(nc_path)
    if not p.exists():
        return False
    try:
        with xr.open_dataset(p) as ds:
            # 检查 time 维度存在且非空
            time_dims = [d for d in ds.dims if d in ("time", "valid_time")]
            if not time_dims:
                return False
            return ds.sizes[time_dims[0]] > 0
    except Exception:
        return False


def skip_existing(path: str | Path, overwrite: bool) -> bool:
    """若输出应被跳过（已存在且不覆盖），返回 True。"""
    if overwrite:
        return False
    return output_complete(path)


# =====================================================================
# 原子写入
# =====================================================================

_HAS_NETCDF4 = False
try:
    import netCDF4  # noqa: F401
    _HAS_NETCDF4 = True
except ImportError:
    pass


def _strip_scipy_incompatible(encoding: dict | None) -> dict | None:
    """移除 scipy 后端不支持的 encoding 键。"""
    if encoding is None or _HAS_NETCDF4:
        return encoding
    cleaned = {}
    for var, enc in encoding.items():
        e = {k: v for k, v in enc.items() if k in ("dtype", "_FillValue")}
        cleaned[var] = e
    return cleaned


def atomic_write_netcdf(
    ds: xr.Dataset,
    path: str | Path,
    encoding: dict | None = None,
) -> None:
    """以原子方式将 *ds* 写到 *path*（临时文件 + os.replace）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / f".{p.name}.tmp.{os.getpid()}"
    enc = _strip_scipy_incompatible(encoding)
    try:
        ds.to_netcdf(tmp, encoding=enc)
        os.replace(tmp, p)
    except BaseException:
        # 清理未完成文件
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


# =====================================================================
# 信号/气象文件写出
# =====================================================================

def write_signal_dataset(
    bundle: WeatherBundle,
    masks: dict[str, np.ndarray],
    out_path: str | Path,
    attrs_extra: dict[str, str] | None = None,
    compress_level: int = 4,
) -> None:
    """将极端天气信号掩膜写入 NetCDF 文件。

    信号变量以 ``int8`` 存储，并带有 ``flag_values`` / ``flag_meanings`` 属性。
    空间坐标（包括旋转极点网格）从 *bundle.dataset* 保留。
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import registry as _reg

    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    # 构建坐标变量
    coords: dict[str, xr.Variable] = {}
    for cname in bundle.dataset.coords:
        coords[cname] = bundle.dataset.coords[cname].variable

    # 构建数据变量
    data_vars: dict[str, xr.DataArray] = {}
    spatial_key = ",".join(bundle.spatial_dims)

    for sig_name, sig_arr in masks.items():
        da = xr.DataArray(
            sig_arr.astype(np.int8),
            dims=bundle.dataset.dims,
            attrs={
                "flag_values": "0, 1",
                "flag_meanings": "false true",
                "long_name": f"Extreme weather signal: {sig_name}",
            },
        )
        data_vars[f"signal_{sig_name}"] = da

    ds = xr.Dataset(data_vars, coords=coords)

    # 全局属性
    ds.attrs["source"] = bundle.source
    ds.attrs["tech"] = bundle.tech
    ds.attrs["grid_mode"] = "all_grid"
    ds.attrs["stations_dir"] = "None"
    ds.attrs["grid_kind"] = bundle.grid_kind
    ds.attrs["spatial_dims"] = spatial_key
    ds.attrs["source_timestep_hours"] = str(bundle.source_timestep_hours)
    ds.attrs["target_time_axis"] = bundle.target_time_axis
    ds.attrs["longitude_convention"] = "preserved_from_source"
    ds.attrs["interpolation_space"] = "none"

    # 时间对齐元数据
    if "time_alignment" in bundle.attrs_extra:
        ds.attrs["time_alignment"] = bundle.attrs_extra["time_alignment"]
    else:
        ds.attrs["time_alignment"] = "source_native"

    # 数据源标识
    for key in ("model", "region", "scenario", "year", "month"):
        if key in bundle.attrs_extra:
            ds.attrs[key] = bundle.attrs_extra[key]

    # 事件元数据
    if attrs_extra:
        for k, v in attrs_extra.items():
            ds.attrs[k] = v

    ds.attrs["threshold_source"] = "extreme_event_definitions/events"
    ds.attrs["warning"] = (
        "event thresholds were calibrated on specific historical data; "
        "compare exposure rates across temporal resolutions with caution"
    )

    # 编码：信号变量使用带 zlib 的 int8，坐标使用 float32
    encoding: dict[str, dict] = {}
    for vn in ds.data_vars:
        encoding[vn] = {
            "dtype": "int8",
            "zlib": True,
            "complevel": compress_level,
        }
    for cn in ds.coords:
        if ds.coords[cn].dtype.kind == "f":
            encoding[cn] = {"dtype": "float32", "zlib": True, "complevel": compress_level}

    # 若存在 crs 变量则保留（用于 CORDEX 旋转极点网格）
    if "crs" in bundle.dataset:
        ds["crs"] = bundle.dataset["crs"]

    atomic_write_netcdf(ds, p, encoding)


def write_weather_dataset(
    bundle: WeatherBundle,
    out_path: str | Path,
    compress_level: int = 4,
) -> None:
    """将标准化气象变量写入 NetCDF 文件。

    所有数据变量均以 float32 存储。
    """
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    # 从 bundle 构建数据集
    data_vars = {}
    for vn in bundle.dataset.data_vars:
        da = bundle.dataset[vn]
        data_vars[vn] = da

    ds = xr.Dataset(data_vars, coords=bundle.dataset.coords)

    # 全局属性
    ds.attrs["source"] = bundle.source
    ds.attrs["grid_mode"] = "all_grid"
    ds.attrs["stations_dir"] = "None"
    ds.attrs["grid_kind"] = bundle.grid_kind
    ds.attrs["spatial_dims"] = ",".join(bundle.spatial_dims)
    ds.attrs["source_timestep_hours"] = str(bundle.source_timestep_hours)
    ds.attrs["target_time_axis"] = bundle.target_time_axis
    ds.attrs["longitude_convention"] = "preserved_from_source"
    ds.attrs["interpolation_space"] = "none"

    for key in ("model", "region", "scenario", "year", "month"):
        if key in bundle.attrs_extra:
            ds.attrs[key] = bundle.attrs_extra[key]

    # 若存在 crs 则保留
    if "crs" in bundle.dataset:
        ds["crs"] = bundle.dataset["crs"]

    # 编码：float32 + zlib
    encoding: dict[str, dict] = {}
    for vn in ds.data_vars:
        encoding[vn] = {
            "dtype": "float32",
            "zlib": True,
            "complevel": compress_level,
        }
    for cn in ds.coords:
        if ds.coords[cn].dtype.kind == "f":
            encoding[cn] = {"dtype": "float32", "zlib": True, "complevel": compress_level}

    atomic_write_netcdf(ds, p, encoding)
