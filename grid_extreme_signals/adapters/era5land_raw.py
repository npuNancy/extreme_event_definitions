"""Adapter for raw ERA5-Land global hourly data.

Key challenges:
  - ``tp`` and ``ssrd`` are **daily accumulated** quantities requiring deaccumulation
    with cross-month boundary handling.
  - Global 0.1° grid is too large to load at once → process per month.
  - Relative humidity from ``t2m`` / ``d2m`` via Magnus formula.
  - Optional MERRA-2 dust via ``--dust_dir``.

File layout::

    {data_dir}/ERA5_land/global/{var}/{var}_{YYYY}_{MM}.nc

Variables: t2m, u10, v10, tp, ssrd, d2m.
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
# ERA5-Land deaccumulation (ONLY used by this adapter)
# =====================================================================

def deaccumulate_era5land(
    current_accum: np.ndarray,
    hours: np.ndarray,
    prev_boundary: np.ndarray | None = None,
    *,
    missing_boundary_policy: str = "zero",
) -> np.ndarray:
    """Convert ERA5-Land daily accumulated values to hourly increments.

    Parameters
    ----------
    current_accum : np.ndarray
        Shape ``(T, ...)`` — accumulated values for the current month.
    hours : np.ndarray
        Shape ``(T,)`` — integer hours (0–23) for each time step.
    prev_boundary : np.ndarray or None
        Shape ``(...)`` — last accumulated value from the previous month.
        If ``None`` and the first step is not 01:00, the policy determines behavior.
    missing_boundary_policy : str
        ``"zero"`` — set first hour increment to 0 (default).
        ``"nan"`` — set to NaN.
        ``"current"`` — use current cumulative value.

    Returns
    -------
    np.ndarray
        Shape ``(T, ...)`` — hourly increments (non-negative).
    """
    cur = current_accum.astype(np.float32)
    T = cur.shape[0]
    if T == 0:
        return cur

    inc = np.empty_like(cur)

    # First time step
    has_prev = prev_boundary is not None
    if has_prev:
        inc[0] = cur[0] if hours[0] == 1 else (cur[0] - prev_boundary)
    else:
        if hours[0] == 1:
            inc[0] = cur[0]
        elif missing_boundary_policy == "zero":
            logger.warning("First hour is not 01:00 and no previous boundary — increment set to 0")
            inc[0] = 0.0
        elif missing_boundary_policy == "nan":
            inc[0] = np.nan
        elif missing_boundary_policy == "current":
            inc[0] = cur[0]
        else:
            raise ValueError(f"Unsupported missing_boundary_policy={missing_boundary_policy!r}")

    # Remaining steps
    if T > 1:
        diff = cur[1:] - cur[:-1]
        reset = hours[1:] == 1
        # Broadcast reset over spatial dims
        for _ in range(cur.ndim - 1):
            reset = reset[:, None]
        reset = np.broadcast_to(reset, diff.shape)
        inc[1:] = np.where(reset, cur[1:], diff)

    # Defensive: negative increments from data anomalies → use current value
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
    """Read the last time step of the previous month for deaccumulation boundary."""
    py, pm = _previous_year_month(year, month)
    root = Path(data_dir) / "ERA5_land" / "global" / var
    prev_file = root / f"{var}_{py:04d}_{pm:02d}.nc"
    if not prev_file.exists():
        logger.warning("Previous month file not found: %s", prev_file)
        return None
    with xr.open_dataset(str(prev_file)) as ds:
        var_name = _get_era5land_var_name(ds, var)
        da = ds[var_name]
        # Squeeze extra dims
        for dim in list(da.dims):
            if dim not in {time_name, lat_name, lon_name} and da.sizes[dim] == 1:
                da = da.isel({dim: 0}, drop=True)
        da = da.transpose(time_name, lat_name, lon_name)
        return da.isel({time_name: -1}).values.astype(np.float32)


def _get_era5land_var_name(ds: xr.Dataset, var: str) -> str:
    """Resolve variable name in ERA5-Land file."""
    if var in ds.data_vars:
        return var
    if len(ds.data_vars) == 1:
        return list(ds.data_vars)[0]
    raise KeyError(f"Cannot find {var} in ERA5-Land file. Available: {list(ds.data_vars)}")


# =====================================================================
# Adapter class
# =====================================================================

class Era5LandRawAdapter(WeatherAdapter):
    """Adapter for raw ERA5-Land global hourly data."""

    def __init__(self, args) -> None:
        self.data_dir = args.data_dir
        self.d2m_root = getattr(args, "era5land_d2m_root", None)
        self.dust_dir = getattr(args, "dust_dir", None)
        self.output_root = getattr(args, "output_root", "outputs/grid_extreme_signals")
        self.allow_missing_optional = getattr(args, "allow_missing_optional", False)

    # ------------------------------------------------------------------
    # Task iteration (per month)
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
    # Output paths
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
    # File helpers
    # ------------------------------------------------------------------

    def _era5land_file(self, var: str, year: int, month: int) -> Path:
        """Construct ERA5-Land monthly file path."""
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
        """Open an ERA5-Land monthly file and return prepared DataArray."""
        fpath = self._era5land_file(var, year, month)
        ds = xr.open_dataset(str(fpath))
        var_name = _get_era5land_var_name(ds, var)
        units = ds[var_name].attrs.get("units", None)
        da = ds[var_name]
        # Squeeze extra dims
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
    # Shared weather loading
    # ------------------------------------------------------------------

    def _load_weather(self, task: dict, tech: str) -> WeatherBundle:
        """Core weather loading for both wind and solar (same time axis for ERA5-Land)."""
        year = task["year"]
        month = task["month"]
        source_files = []
        skipped_inputs: dict[str, str] = {}

        time_name, lat_name, lon_name = self._discover_coords(year, month)

        # --- Load variables ---
        t2m_da, _ = self._open_var("t2m", year, month, time_name, lat_name, lon_name)
        source_files.append(str(self._era5land_file("t2m", year, month)))

        u10_da, _ = self._open_var("u10", year, month, time_name, lat_name, lon_name)
        source_files.append(str(self._era5land_file("u10", year, month)))

        v10_da, _ = self._open_var("v10", year, month, time_name, lat_name, lon_name)
        source_files.append(str(self._era5land_file("v10", year, month)))

        # Deaccumulate tp (m → mm)
        tp_da, tp_units = self._open_var("tp", year, month, time_name, lat_name, lon_name)
        source_files.append(str(self._era5land_file("tp", year, month)))

        hours = tp_da[time_name].dt.hour.values.astype(np.int16)
        tp_prev = load_previous_accum_boundary(
            task["data_dir"], "tp", year, month, time_name, lat_name, lon_name,
        )
        tp_inc = deaccumulate_era5land(tp_da.values, hours, tp_prev)
        precip_mmh = tp_inc * 1000.0  # m → mm

        # Deaccumulate ssrd (J/m² → W/m²)
        ssrd_da, _ = self._open_var("ssrd", year, month, time_name, lat_name, lon_name)
        source_files.append(str(self._era5land_file("ssrd", year, month)))

        ssrd_prev = load_previous_accum_boundary(
            task["data_dir"], "ssrd", year, month, time_name, lat_name, lon_name,
        )
        ssrd_inc = deaccumulate_era5land(ssrd_da.values, hours, ssrd_prev)
        rsds_wm2 = ssrd_inc / 3600.0  # J/m² → W/m² (per hour)

        # Relative humidity from t2m/d2m
        rh_pct = None
        try:
            d2m_da, _ = self._open_var("d2m", year, month, time_name, lat_name, lon_name)
            source_files.append(str(self._era5land_file("d2m", year, month)))
            rh_pct = magnus_rh(t2m_da.values, d2m_da.values)
        except (FileNotFoundError, KeyError):
            skipped_inputs["rh_pct"] = "d2m file not found"
            logger.warning("d2m file not found — humidity events will be skipped")

        # Temperature in °C
        temp_C = t2m_da.values.astype(np.float32) - 273.15

        # Wind speed from components
        wind_ms = np.sqrt(
            u10_da.values.astype(np.float32) ** 2
            + v10_da.values.astype(np.float32) ** 2,
        )

        # Build dataset
        # Rename coords to canonical names for consistency
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

        # Optional dust
        dust_aod = None
        if self.dust_dir:
            dust_aod = self._load_dust(task, time_name, lat_name, lon_name, t2m_da)
            if dust_aod is not None:
                data_vars["dust_aod"] = xr.DataArray(dust_aod, dims=dims)
                source_files.append("MERRA-2 dust data")
            else:
                skipped_inputs["dust_aod"] = "dust files not found"
        else:
            skipped_inputs["dust_aod"] = "dust_dir not configured"

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
        """Load MERRA-2 dust AOD and regrid to ERA5-Land grid.

        Phase 1: simple nearest-neighbor regridding via xarray.interp.
        """
        import glob as _glob
        year = task["year"]
        month = task["month"]
        dust_dir = Path(self.dust_dir)

        # Find daily MERRA-2 files for this month
        pattern = str(dust_dir / f"{year}" / f"{month:02d}" / f"MERRA2.tavg1_2d_aer_Nx.{year}{month:02d}*.nc4")
        dust_files = sorted(_glob.glob(pattern))
        if not dust_files:
            logger.warning("No MERRA-2 dust files found: %s", pattern)
            return None

        # Read and concatenate
        datasets = []
        for f in dust_files:
            ds = xr.open_dataset(f)
            if "DUEXTTAU" in ds.data_vars:
                datasets.append(ds["DUEXTTAU"])
        if not datasets:
            return None

        dust_da = xr.concat(datasets, dim="time")

        # Regrid to ERA5-Land grid via nearest neighbor
        era5_lat = ref_da[lat_name].values
        era5_lon = ref_da[lon_name].values

        # MERRA-2 uses lat/lon; adjust longitude convention if needed
        merra_lon = dust_da.coords["lon"].values
        if merra_lon.min() < 0 and era5_lon.min() >= 0:
            # MERRA-2 is [-180,180), ERA5-Land is [0,360)
            era5_lon_for_interp = era5_lon.copy()
            era5_lon_for_interp[era5_lon_for_interp > 180] -= 360
        else:
            era5_lon_for_interp = era5_lon

        dust_regridded = dust_da.interp(
            lat=xr.DataArray(era5_lat, dims=[lat_name]),
            lon=xr.DataArray(era5_lon_for_interp, dims=[lon_name]),
            method="nearest",
        )

        # Align time
        ref_time = ref_da[time_name].values
        dust_aligned = dust_regridded.interp(
            {list(dust_regridded.dims)[0]: ref_time},
            method="nearest",
        )
        return dust_aligned.values.astype(np.float32)

    # ------------------------------------------------------------------
    # Wind / Solar (same data, different tech tag)
    # ------------------------------------------------------------------

    def load_wind_weather(self, task: dict) -> WeatherBundle:
        return self._load_weather(task, "wind")

    def load_solar_weather(self, task: dict) -> WeatherBundle:
        return self._load_weather(task, "solar")
