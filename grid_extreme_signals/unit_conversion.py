"""标准化气象变量的单位转换工具。

所有函数都是纯函数（无 I/O、无日志）。输入 numpy 数组和可选单位字符串，
返回目标单位下的 float32 数组。

当 ``allow_inference`` 为 *False*（默认）时，缺失或无法识别的单位字符串会抛出
:class:`ValueError`。
"""
from __future__ import annotations

import numpy as np


# ---------------------------------------------------------------------------
# 温度
# ---------------------------------------------------------------------------

def tas_to_celsius(
    arr: np.ndarray,
    units: str | None,
    *,
    allow_inference: bool = False,
) -> np.ndarray:
    """将温度转换为 °C。

    支持的源单位：``K`` / ``kelvin``、``degC`` / ``°C`` / ``C`` / ``celsius``。
    """
    x = np.asarray(arr, dtype=np.float32)
    units_l = (units or "").lower().replace(" ", "").replace("²", "2")

    if units_l in ("k", "kelvin"):
        return (x - 273.15).astype(np.float32)
    if units_l in ("degc", "°c", "c", "celsius"):
        return x.astype(np.float32)

    # 单位无法识别
    if not allow_inference:
        raise ValueError(
            f"无法识别温度单位：{units!r}。"
            "如需按数值大小启发式推断，请传入 allow_inference=True。"
        )
    finite = x[np.isfinite(x)]
    if finite.size and np.nanmedian(finite) > 100:
        return (x - 273.15).astype(np.float32)
    return x.astype(np.float32)


# ---------------------------------------------------------------------------
# 风速
# ---------------------------------------------------------------------------

def wind_to_ms(
    arr: np.ndarray,
    units: str | None,
    *,
    allow_inference: bool = False,
) -> np.ndarray:
    """将风速转换为 m s⁻¹。

    支持：``m s-1`` / ``m/s``、``km h-1`` / ``km/h``。
    """
    x = np.asarray(arr, dtype=np.float32)
    units_l = (units or "").lower().replace(" ", "")

    if units_l in ("m/s", "ms-1", "m.s-1"):
        return x.astype(np.float32)
    if units_l in ("km/h", "kmh-1", "km.h-1"):
        return (x / 3.6).astype(np.float32)

    if not allow_inference:
        raise ValueError(
            f"无法识别风速单位：{units!r}。"
            "如需默认按 m s-1 处理，请传入 allow_inference=True。"
        )
    return x.astype(np.float32)


# ---------------------------------------------------------------------------
# 降水率
# ---------------------------------------------------------------------------

def pr_to_mmh(
    arr: np.ndarray,
    units: str | None,
    *,
    timestep_hours: float | None = None,
    allow_inference: bool = False,
) -> np.ndarray:
    """将降水率转换为 mm h⁻¹。

    支持：
      - ``kg m-2 s-1`` / ``mm s-1``  → ×3600
      - ``mm day-1``                  → ÷24
      - ``mm h-1``                    → 不变
      - ``mm``（每个时间步累计量）      → ÷timestep_hours（需要 *timestep_hours*）

    .. warning::
       不要对 ERA5-Land ``tp`` 直接调用本函数；该变量需要先解累计
       （由 era5land_raw 适配器处理）。
    """
    x = np.asarray(arr, dtype=np.float32)
    units_l = (units or "").lower().replace(" ", "").replace("²", "2")

    if units_l in ("kgm-2s-1", "mms-1", "mm/s"):
        return (x * 3600.0).astype(np.float32)
    if units_l in ("mmd-1", "mm/day", "mmday-1"):
        return (x / 24.0).astype(np.float32)
    if units_l in ("mm/h", "mmh-1"):
        return x.astype(np.float32)
    if units_l in ("mm",):
        if timestep_hours is None:
            raise ValueError(
                "降水单位为 'mm'（每时间步累计量），但未提供 timestep_hours。"
            )
        return (x / timestep_hours).astype(np.float32)

    if not allow_inference:
        raise ValueError(
            f"无法识别降水单位：{units!r}。"
            "如需默认按 mm h-1 处理，请传入 allow_inference=True。"
        )
    return x.astype(np.float32)


# ---------------------------------------------------------------------------
# 地表短波辐射（rsds）
# ---------------------------------------------------------------------------

def rsds_to_wm2(
    arr: np.ndarray,
    units: str | None,
    *,
    timestep_seconds: float | None = None,
    allow_inference: bool = False,
) -> np.ndarray:
    """将地表短波辐射转换为 W m⁻²。

    支持：
      - ``W m-2``   → 不变
      - ``kW m-2``  → ×1000
      - ``J m-2``   → ÷timestep_seconds（需要 *timestep_seconds*）

    .. warning::
       不要对 ERA5-Land ``ssrd`` 直接调用本函数；该变量需要先解累计
       （由 era5land_raw 适配器处理）。
    """
    x = np.asarray(arr, dtype=np.float32)
    units_l = (units or "").lower().replace(" ", "").replace("²", "2")

    if units_l in ("wm-2", "w/m2", "w.m-2"):
        return x.astype(np.float32)
    if units_l in ("kwm-2", "kw/m2", "kw.m-2"):
        return (x * 1000.0).astype(np.float32)
    if units_l in ("jm-2", "j/m2", "j.m-2"):
        if timestep_seconds is None:
            raise ValueError(
                "辐射单位为 'J m-2'，但未提供 timestep_seconds。"
            )
        return (x / timestep_seconds).astype(np.float32)

    if not allow_inference:
        raise ValueError(
            f"无法识别辐射单位：{units!r}。"
            "如需默认按 W m-2 处理，请传入 allow_inference=True。"
        )
    return x.astype(np.float32)


# ---------------------------------------------------------------------------
# 相对湿度
# ---------------------------------------------------------------------------

def hurs_to_pct(
    arr: np.ndarray,
    units: str | None,
    *,
    allow_inference: bool = False,
) -> np.ndarray:
    """将相对湿度统一为百分比，并校验物理范围。

    支持百分比单位（``%``/``percent``/``pct``）和 0—1 比例单位
    （``1``/``fraction``/``dimensionless``）。允许最多 1 个百分点的轻微
    数值越界并裁剪到 ``[0, 100]``；更大的越界视为输入错误。
    """
    x = np.asarray(arr, dtype=np.float32)
    units_l = (units or "").lower().replace(" ", "")
    percent_units = {"%", "percent", "percentage", "pct"}
    fraction_units = {"1", "fraction", "dimensionless", "unitless"}

    if units_l in percent_units:
        result = x
    elif units_l in fraction_units:
        result = x * 100.0
    elif allow_inference:
        finite = x[np.isfinite(x)]
        if finite.size == 0:
            result = x
        elif float(np.nanmin(finite)) >= -0.01 and float(np.nanmax(finite)) <= 1.01:
            result = x * 100.0
        elif float(np.nanmin(finite)) >= -1.0 and float(np.nanmax(finite)) <= 101.0:
            result = x
        else:
            raise ValueError(
                "无法根据数值范围推断相对湿度单位；"
                f"范围={float(np.nanmin(finite))}—{float(np.nanmax(finite))}"
            )
    else:
        raise ValueError(
            f"无法识别相对湿度单位：{units!r}。"
            "如需按数值范围推断，请传入 allow_inference=True。"
        )

    finite_result = result[np.isfinite(result)]
    if finite_result.size:
        low = float(np.nanmin(finite_result))
        high = float(np.nanmax(finite_result))
        if low < -1.0 or high > 101.0:
            raise ValueError(f"相对湿度超出允许范围：{low}—{high}%")
    return np.clip(result, 0.0, 100.0).astype(np.float32)


# ---------------------------------------------------------------------------
# 相对湿度（Magnus 公式）
# ---------------------------------------------------------------------------

def magnus_rh(t2m_k: np.ndarray, d2m_k: np.ndarray) -> np.ndarray:
    """由 2 m 气温和露点温度计算相对湿度（%，二者均为 Kelvin）。

    使用 Magnus 公式：RH = 100 × e(Td) / es(T)，其中
    e(T) = 6.112 × exp(17.67 × T / (T + 243.5))，[T 单位为 °C]。
    """
    t = np.asarray(t2m_k, dtype=np.float32) - 273.15
    td = np.asarray(d2m_k, dtype=np.float32) - 273.15
    e = 6.112 * np.exp((17.67 * td) / (td + 243.5))
    es = 6.112 * np.exp((17.67 * t) / (t + 243.5))
    return np.clip(100.0 * e / es, 0.0, 100.0).astype(np.float32)
