"""Unit conversion utilities for standardised weather variables.

All functions are pure (no I/O, no logging).  They take numpy arrays and
optional unit strings, returning float32 arrays in the target unit.

When ``allow_inference`` is *False* (the default), a missing or unrecognised
unit string raises :class:`ValueError`.
"""
from __future__ import annotations

import numpy as np


# ---------------------------------------------------------------------------
# Temperature
# ---------------------------------------------------------------------------

def tas_to_celsius(
    arr: np.ndarray,
    units: str | None,
    *,
    allow_inference: bool = False,
) -> np.ndarray:
    """Convert temperature to °C.

    Supported source units: ``K`` / ``kelvin``, ``degC`` / ``°C`` / ``C`` / ``celsius``.
    """
    x = np.asarray(arr, dtype=np.float32)
    units_l = (units or "").lower().replace(" ", "").replace("²", "2")

    if units_l in ("k", "kelvin"):
        return (x - 273.15).astype(np.float32)
    if units_l in ("degc", "°c", "c", "celsius"):
        return x.astype(np.float32)

    # No recognised unit
    if not allow_inference:
        raise ValueError(
            f"Temperature unit not recognised: {units!r}.  "
            "Pass allow_inference=True to guess by magnitude."
        )
    finite = x[np.isfinite(x)]
    if finite.size and np.nanmedian(finite) > 100:
        return (x - 273.15).astype(np.float32)
    return x.astype(np.float32)


# ---------------------------------------------------------------------------
# Wind speed
# ---------------------------------------------------------------------------

def wind_to_ms(
    arr: np.ndarray,
    units: str | None,
    *,
    allow_inference: bool = False,
) -> np.ndarray:
    """Convert wind speed to m s⁻¹.

    Supported: ``m s-1`` / ``m/s``, ``km h-1`` / ``km/h``.
    """
    x = np.asarray(arr, dtype=np.float32)
    units_l = (units or "").lower().replace(" ", "")

    if units_l in ("m/s", "ms-1", "m.s-1"):
        return x.astype(np.float32)
    if units_l in ("km/h", "kmh-1", "km.h-1"):
        return (x / 3.6).astype(np.float32)

    if not allow_inference:
        raise ValueError(
            f"Wind speed unit not recognised: {units!r}.  "
            "Pass allow_inference=True to assume m s-1."
        )
    return x.astype(np.float32)


# ---------------------------------------------------------------------------
# Precipitation rate
# ---------------------------------------------------------------------------

def pr_to_mmh(
    arr: np.ndarray,
    units: str | None,
    *,
    timestep_hours: float | None = None,
    allow_inference: bool = False,
) -> np.ndarray:
    """Convert precipitation rate to mm h⁻¹.

    Supported:
      - ``kg m-2 s-1`` / ``mm s-1``  → ×3600
      - ``mm day-1``                  → ÷24
      - ``mm h-1``                    → identity
      - ``mm`` (per time step)        → ÷timestep_hours  (requires *timestep_hours*)

    .. warning::
       Do **not** call this for ERA5-Land ``tp`` — that variable needs
       deaccumulation first (handled by the era5land_raw adapter).
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
                "Precipitation unit is 'mm' (per time step) but "
                "timestep_hours was not provided."
            )
        return (x / timestep_hours).astype(np.float32)

    if not allow_inference:
        raise ValueError(
            f"Precipitation unit not recognised: {units!r}.  "
            "Pass allow_inference=True to assume mm h-1."
        )
    return x.astype(np.float32)


# ---------------------------------------------------------------------------
# Surface solar radiation (rsds)
# ---------------------------------------------------------------------------

def rsds_to_wm2(
    arr: np.ndarray,
    units: str | None,
    *,
    timestep_seconds: float | None = None,
    allow_inference: bool = False,
) -> np.ndarray:
    """Convert surface shortwave radiation to W m⁻².

    Supported:
      - ``W m-2``   → identity
      - ``kW m-2``  → ×1000
      - ``J m-2``   → ÷timestep_seconds  (requires *timestep_seconds*)

    .. warning::
       Do **not** call this for ERA5-Land ``ssrd`` — that variable needs
       deaccumulation first (handled by the era5land_raw adapter).
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
                "Radiation unit is 'J m-2' but timestep_seconds was not provided."
            )
        return (x / timestep_seconds).astype(np.float32)

    if not allow_inference:
        raise ValueError(
            f"Radiation unit not recognised: {units!r}.  "
            "Pass allow_inference=True to assume W m-2."
        )
    return x.astype(np.float32)


# ---------------------------------------------------------------------------
# Relative humidity (Magnus formula)
# ---------------------------------------------------------------------------

def magnus_rh(t2m_k: np.ndarray, d2m_k: np.ndarray) -> np.ndarray:
    """Relative humidity (%) from 2 m temperature and dew-point (both Kelvin).

    Uses the Magnus formula:  RH = 100 × e(Td) / es(T)
    where e(T) = 6.112 × exp(17.67 × T / (T + 243.5))  [T in °C].
    """
    t = np.asarray(t2m_k, dtype=np.float32) - 273.15
    td = np.asarray(d2m_k, dtype=np.float32) - 273.15
    e = 6.112 * np.exp((17.67 * td) / (td + 243.5))
    es = 6.112 * np.exp((17.67 * t) / (t + 243.5))
    return np.clip(100.0 * e / es, 0.0, 100.0).astype(np.float32)
