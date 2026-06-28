"""Time-axis utilities: coordinate discovery, interpolation, year/month parsing.

Supports both ``datetime64`` and ``cftime`` calendars used by CMIP6 data.
"""
from __future__ import annotations

import logging
from typing import Iterable

import numpy as np
import xarray as xr

logger = logging.getLogger(__name__)

# =====================================================================
# Coordinate name discovery
# =====================================================================

TIME_CANDIDATES = ("time", "valid_time")
LAT_CANDIDATES = ("lat", "latitude")
LON_CANDIDATES = ("lon", "longitude")
RLAT_CANDIDATES = ("rlat",)
RLON_CANDIDATES = ("rlon",)


def find_coord_name(ds: xr.Dataset, candidates: Iterable[str]) -> str:
    """Return the first candidate name found in *ds* coords or dims.

    Raises ``KeyError`` if none match.
    """
    for c in candidates:
        if c in ds.coords or c in ds.dims:
            return c
    raise KeyError(
        f"No coordinate found among candidates {list(candidates)}.  "
        f"Available: coords={list(ds.coords)}, dims={list(ds.dims)}"
    )


# =====================================================================
# Numeric time representation (handles datetime64 *and* cftime)
# =====================================================================

def datetime64_to_ns(values: np.ndarray) -> np.ndarray:
    """Convert time coordinate values to a strictly-monotonic numeric array.

    For ``datetime64`` arrays the result is ``int64`` nanoseconds.
    For ``cftime`` arrays the result is ``float64`` seconds from the first
    element (sufficient for ``np.searchsorted``).
    """
    arr = np.asarray(values)
    if arr.dtype.kind == "O":
        # cftime objects
        import cftime
        if isinstance(arr.flat[0], cftime.datetime):
            base = arr[0]
            offsets = np.array(
                [cftime.date2num(v, "seconds since 1970-01-01", calendar=v.calendar)
                 for v in arr],
                dtype=np.float64,
            )
            return offsets
        # fallback
        return arr.astype(np.float64)
    if np.issubdtype(arr.dtype, np.datetime64):
        return arr.astype("datetime64[ns]").astype(np.int64)
    return arr.astype(np.float64)


# =====================================================================
# Linear interpolation of instantaneous variables to target time axis
# =====================================================================

def interp_instantaneous_to_target(
    da: xr.DataArray,
    time_name: str,
    target_times: np.ndarray,
    *,
    fill_boundary: str = "nearest",
) -> np.ndarray:
    """Linearly interpolate an instantaneous variable to *target_times*.

    Parameters
    ----------
    da : xr.DataArray
        Source variable.  Must have a time dimension named *time_name*.
    time_name : str
        Name of the time dimension in *da*.
    target_times : np.ndarray
        Target time coordinate values (datetime64 or cftime).
    fill_boundary : str
        ``"nearest"`` — clamp out-of-range targets to the nearest source value.
        ``"nan"`` — set out-of-range targets to NaN.

    Returns
    -------
    np.ndarray
        Interpolated values, dtype float32, shape ``(len(target_times), ...)``.
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

    # Read only the needed slice to save memory
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
    # Broadcast w over spatial dims
    for _ in range(src_block.ndim - 1):
        w = w[:, None]
    w = np.broadcast_to(w, src_block[left_rel].shape)

    out = src_block[left_rel] * (1.0 - w) + src_block[right_rel] * w
    if fill_boundary == "nan":
        out[before | after] = np.nan
    return out.astype(np.float32)


# =====================================================================
# Year / month parsing helpers
# =====================================================================

def parse_years(years_str: str) -> tuple[int, int]:
    """Parse ``"YYYY"`` or ``"YYYY-YYYY"`` into ``(y0, y1)`` inclusive."""
    if "-" in years_str:
        parts = years_str.split("-", 1)
        return int(parts[0]), int(parts[1])
    y = int(years_str)
    return y, y


def parse_months(months_str: str | None) -> list[int]:
    """Parse ``""`` / ``None`` → all 12 months, or ``"1,2,3"`` → list."""
    if not months_str:
        return list(range(1, 13))
    return [int(m.strip()) for m in months_str.split(",") if m.strip()]


def build_time_index(
    time_da: xr.DataArray,
    years_str: str,
    months_str: str | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Select time steps matching *years* and *months*, return index arrays.

    Returns
    -------
    idx : np.ndarray (int)
        Integer indices into *time_da*.
    doy : np.ndarray (float32)
        Day-of-year for each selected step.
    hour_decimal : np.ndarray (float32)
        Decimal hour (e.g. 1.5 = 01:30) for each selected step.
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
    """Return integer indices where ``time_da.dt.year == year``."""
    mask = time_da.dt.year == year
    idx = np.where(mask.values)[0]
    if idx.size == 0:
        raise ValueError(f"No time steps for year={year}")
    return idx
