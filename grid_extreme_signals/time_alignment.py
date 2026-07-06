"""时间轴工具：坐标发现、插值、年份/月分解析。

支持 CMIP6 数据常用的 ``datetime64`` 和 ``cftime`` 日历。
"""
from __future__ import annotations

import logging
from typing import Iterable

import numpy as np
import xarray as xr

logger = logging.getLogger(__name__)

# =====================================================================
# 坐标名发现
# =====================================================================

TIME_CANDIDATES = ("time", "valid_time")
LAT_CANDIDATES = ("lat", "latitude")
LON_CANDIDATES = ("lon", "longitude")
RLAT_CANDIDATES = ("rlat",)
RLON_CANDIDATES = ("rlon",)


def find_coord_name(ds: xr.Dataset, candidates: Iterable[str]) -> str:
    """返回 *ds* 的坐标或维度中第一个匹配的候选名称。

    若没有匹配项则抛出 ``KeyError``。
    """
    for c in candidates:
        if c in ds.coords or c in ds.dims:
            return c
    raise KeyError(
        f"No coordinate found among candidates {list(candidates)}.  "
        f"Available: coords={list(ds.coords)}, dims={list(ds.dims)}"
    )


# =====================================================================
# 数值化时间表示（同时处理 datetime64 和 cftime）
# =====================================================================

def datetime64_to_ns(values: np.ndarray) -> np.ndarray:
    """将时间坐标值转换为严格单调的数值数组。

    ``datetime64`` 数组会转换为 ``int64`` 纳秒；``cftime`` 数组会转换为相对首个
    元素的 ``float64`` 秒数（足够用于 ``np.searchsorted``）。
    """
    arr = np.asarray(values)
    if arr.dtype.kind == "O":
        # cftime 对象
        import cftime
        if isinstance(arr.flat[0], cftime.datetime):
            base = arr[0]
            offsets = np.array(
                [cftime.date2num(v, "seconds since 1970-01-01", calendar=v.calendar)
                 for v in arr],
                dtype=np.float64,
            )
            return offsets
        # 兜底路径
        return arr.astype(np.float64)
    if np.issubdtype(arr.dtype, np.datetime64):
        return arr.astype("datetime64[ns]").astype(np.int64)
    return arr.astype(np.float64)


# =====================================================================
# 将瞬时变量线性插值到目标时间轴
# =====================================================================

def interp_instantaneous_to_target(
    da: xr.DataArray,
    time_name: str,
    target_times: np.ndarray,
    *,
    fill_boundary: str = "nearest",
) -> np.ndarray:
    """将瞬时变量线性插值到 *target_times*。

    参数
    ----
    da : xr.DataArray
        源变量，必须有名为 *time_name* 的时间维度。
    time_name : str
        *da* 中的时间维度名。
    target_times : np.ndarray
        目标时间坐标值（datetime64 或 cftime）。
    fill_boundary : str
        ``"nearest"`` 表示越界目标取最近源值；``"nan"`` 表示越界目标置为 NaN。

    返回
    ----
    np.ndarray
        插值结果，dtype 为 float32，形状为 ``(len(target_times), ...)``。
    """
    src_times = da[time_name].values
    src_num = datetime64_to_ns(src_times)
    tgt_num = datetime64_to_ns(target_times)

    if src_num.ndim != 1:
        raise ValueError(f"{da.name or 'variable'} time coordinate is not 1-D.")
    if src_num.size < 1:
        raise ValueError(f"{da.name or 'variable'} time coordinate is empty.")
    if np.any(np.diff(src_num) <= 0):
        raise ValueError(f"{da.name or 'variable'} time not strictly increasing.")

    nsrc = src_num.size
    right = np.searchsorted(src_num, tgt_num, side="left")
    right = np.clip(right, 0, nsrc - 1)
    left = np.clip(right - 1, 0, nsrc - 1)

    exact = src_num[right] == tgt_num
    left[exact] = right[exact]

    before = tgt_num <= src_num[0]
    after = tgt_num >= src_num[-1]
    if fill_boundary == "nearest":
        left[before] = 0
        right[before] = 0
        left[after] = nsrc - 1
        right[after] = nsrc - 1
    elif fill_boundary != "nan":
        raise ValueError(f"Unsupported fill_boundary={fill_boundary!r}")

    # 只读取所需切片以节省内存
    i0 = int(min(left.min(), right.min()))
    i1 = int(max(left.max(), right.max())) + 1
    src_block = da.isel({time_name: slice(i0, i1)}).values.astype(np.float32)

    left_rel = left - i0
    right_rel = right - i0

    left_t = src_num[left].astype(np.float64)
    right_t = src_num[right].astype(np.float64)
    tgt_t = tgt_num.astype(np.float64)
    denom = right_t - left_t
    with np.errstate(invalid="ignore", divide="ignore"):
        w = np.where(denom != 0, (tgt_t - left_t) / denom, 0.0).astype(np.float32)
    # 将 w 广播到空间维度
    for _ in range(src_block.ndim - 1):
        w = w[:, None]
    w = np.broadcast_to(w, src_block[left_rel].shape)

    out = src_block[left_rel] * (1.0 - w) + src_block[right_rel] * w
    if fill_boundary == "nan":
        out[before | after] = np.nan
    return out.astype(np.float32)


# =====================================================================
# 年份/月分解析辅助函数
# =====================================================================

def parse_years(years_str: str) -> tuple[int, int]:
    """将 ``"YYYY"`` 或 ``"YYYY-YYYY"`` 解析为闭区间 ``(y0, y1)``。"""
    if "-" in years_str:
        parts = years_str.split("-", 1)
        return int(parts[0]), int(parts[1])
    y = int(years_str)
    return y, y


def parse_months(months_str: str | None) -> list[int]:
    """解析月份：``""`` / ``None`` 表示全部 12 个月，``"1,2,3"`` 表示列表。"""
    if not months_str:
        return list(range(1, 13))
    return [int(m.strip()) for m in months_str.split(",") if m.strip()]


def build_time_index(
    time_da: xr.DataArray,
    years_str: str,
    months_str: str | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """选择匹配 *years* 和 *months* 的时间步，并返回索引数组。

    返回
    ----
    idx : np.ndarray (int)
        指向 *time_da* 的整数索引。
    doy : np.ndarray (float32)
        每个选中时间步对应的年内日序。
    hour_decimal : np.ndarray (float32)
        每个选中时间步对应的十进制小时（例如 1.5 = 01:30）。
    """
    y0, y1 = parse_years(years_str)
    month_list = parse_months(months_str)

    mask = (
        (time_da.dt.year >= y0)
        & (time_da.dt.year <= y1)
        & time_da.dt.month.isin(month_list)
    )
    idx = np.where(mask.values)[0]
    if idx.size == 0:
        raise ValueError(f"No time steps match years={years_str}, months={months_str or 'all'}")

    selected = time_da.isel({time_da.dims[0]: idx})
    doy = selected.dt.dayofyear.values.astype(np.float32)
    hour = selected.dt.hour.values.astype(np.float32)
    minute = getattr(selected.dt, "minute", None)
    if minute is not None:
        minute = minute.values.astype(np.float32)
    else:
        minute = np.float32(0.0)
    hour_decimal = hour + minute / 60.0
    return idx, doy, hour_decimal


def filter_year(
    time_da: xr.DataArray,
    year: int,
) -> np.ndarray:
    """返回满足 ``time_da.dt.year == year`` 的整数索引。"""
    mask = time_da.dt.year == year
    idx = np.where(mask.values)[0]
    if idx.size == 0:
        raise ValueError(f"No time steps for year={year}")
    return idx
