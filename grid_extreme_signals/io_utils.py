"""Shared I/O utilities: file discovery, NetCDF variable resolution, atomic write.

Patterns are ported from the capacity factor reference scripts in
``calculate_bcsd_cfs/`` and adapted for extreme weather signal output.
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
# File discovery — regional BCSD
# =====================================================================

def find_bcsd_file(
    data_dir: str | Path,
    model: str,
    region: str,
    scenario: str,
    var: str,
) -> Path:
    """Find a BCSD variable file using cascading glob patterns.

    Priority:
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
                logger.warning("%s matched %d files, using first: %s", var, len(files), files[0])
            return Path(files[0])
    raise FileNotFoundError(
        f"Cannot find {var} file. Tried:\n  " + "\n  ".join(patterns)
    )


# =====================================================================
# File discovery — China CMFD BCSD
# =====================================================================

def find_china_bcsd_file(
    data_dir: str | Path,
    model: str,
    scenario: str,
    var: str,
) -> Path:
    """Find a China CMFD BCSD variable file.

    Priority:
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
                logger.warning("%s matched %d files, using first: %s", var, len(files), files[0])
            return Path(files[0])
    raise FileNotFoundError(
        f"Cannot find {var} file for China CMFD. Tried:\n  " + "\n  ".join(patterns)
    )


# =====================================================================
# File discovery — CORDEX NAM-12
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
    """Find CORDEX NAM-12 per-year files for a variable.

    Returns ``{year: Path}`` for each year in *year_range* that has a file.
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
        # Fallback: try without strict year extraction
        for f in files:
            result[0] = Path(f)  # single-file case
            break
    return result


# =====================================================================
# NetCDF variable name resolution
# =====================================================================

def get_var_name(ds: xr.Dataset, preferred: str, *, use_bcsd_suffix: bool = True) -> str:
    """Resolve the data variable name in *ds*.

    Priority (when *use_bcsd_suffix* is True):
      1. ``{preferred}_bcsd``
      2. ``{preferred}``
      3. the sole data variable (if exactly one)

    When *use_bcsd_suffix* is False, skip step 1.
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
# Spatial grid validation
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
    """Raise ``ValueError`` if any *other* dataset has a different lat/lon grid."""
    ref_lat = ds_ref[lat_name].values
    ref_lon = ds_ref[lon_name].values
    for name, ds in others.items():
        for cname, ref in [(lat_name, ref_lat), (lon_name, ref_lon)]:
            if cname not in ds.coords and cname not in ds.dims:
                raise KeyError(f"{name}: missing coordinate '{cname}'")
            if not _coord_values_close(ref, ds[cname].values, f"{name}.{cname}"):
                raise ValueError(f"{name}: {cname} grid does not match reference")


# =====================================================================
# DataArray preparation
# =====================================================================

def prepare_dataarray(
    ds: xr.Dataset,
    preferred_var: str,
    time_name: str,
    *spatial_names: str,
    use_bcsd_suffix: bool = True,
) -> xr.DataArray:
    """Extract variable, squeeze singleton extra dims, transpose to canonical order."""
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
# Region discovery
# =====================================================================

def is_valid_region_name(name: str) -> bool:
    """Filter out invalid region directory names."""
    return (
        bool(name)
        and not name.startswith("_")
        and not name.endswith("_")
        and not name.endswith("_repeated")
    )


def discover_regions(data_dir: str | Path, model: str) -> list[str]:
    """List valid region subdirectories under ``{data_dir}/{model}/``."""
    root = Path(data_dir) / model
    return sorted(
        p.name for p in root.glob("*")
        if p.is_dir() and is_valid_region_name(p.name)
    )


# =====================================================================
# Output file helpers
# =====================================================================

def output_complete(nc_path: str | Path) -> bool:
    """Return True if *nc_path* exists, is readable, and has positive time size."""
    p = Path(nc_path)
    if not p.exists():
        return False
    try:
        with xr.open_dataset(p) as ds:
            # Check that time dimension exists and is non-empty
            time_dims = [d for d in ds.dims if d in ("time", "valid_time")]
            if not time_dims:
                return False
            return ds.sizes[time_dims[0]] > 0
    except Exception:
        return False


def skip_existing(path: str | Path, overwrite: bool) -> bool:
    """Return True if the output should be skipped (exists and not overwriting)."""
    if overwrite:
        return False
    return output_complete(path)


# =====================================================================
# Atomic write
# =====================================================================

_HAS_NETCDF4 = False
try:
    import netCDF4  # noqa: F401
    _HAS_NETCDF4 = True
except ImportError:
    pass


def _strip_scipy_incompatible(encoding: dict | None) -> dict | None:
    """Remove encoding keys not supported by the scipy backend."""
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
    """Write *ds* to *path* atomically (tmp file + os.replace)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / f".{p.name}.tmp.{os.getpid()}"
    enc = _strip_scipy_incompatible(encoding)
    try:
        ds.to_netcdf(tmp, encoding=enc)
        os.replace(tmp, p)
    except BaseException:
        # Clean up partial file
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


# =====================================================================
# Signal / weather file writers
# =====================================================================

def write_signal_dataset(
    bundle: WeatherBundle,
    masks: dict[str, np.ndarray],
    out_path: str | Path,
    attrs_extra: dict[str, str] | None = None,
    compress_level: int = 4,
) -> None:
    """Write extreme-weather signal masks to a NetCDF file.

    Signal variables are stored as ``int8`` with ``flag_values`` / ``flag_meanings``
    attributes.  Spatial coordinates (including rotated pole) are preserved from
    *bundle.dataset*.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import registry as _reg

    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    # Build coordinate variables
    coords: dict[str, xr.Variable] = {}
    for cname in bundle.dataset.coords:
        coords[cname] = bundle.dataset.coords[cname].variable

    # Build data variables
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

    # Global attributes
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

    # Time alignment metadata
    if "time_alignment" in bundle.attrs_extra:
        ds.attrs["time_alignment"] = bundle.attrs_extra["time_alignment"]
    else:
        ds.attrs["time_alignment"] = "source_native"

    # Source identifiers
    for key in ("model", "region", "scenario", "year", "month"):
        if key in bundle.attrs_extra:
            ds.attrs[key] = bundle.attrs_extra[key]

    # Event metadata
    if attrs_extra:
        for k, v in attrs_extra.items():
            ds.attrs[k] = v

    ds.attrs["threshold_source"] = "extreme_event_definitions/events"
    ds.attrs["warning"] = (
        "event thresholds were calibrated on specific historical data; "
        "compare exposure rates across temporal resolutions with caution"
    )

    # Encoding: int8 with zlib for signals, float32 for coordinates
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

    # Preserve crs variable if present (for CORDEX rotated pole)
    if "crs" in bundle.dataset:
        ds["crs"] = bundle.dataset["crs"]

    atomic_write_netcdf(ds, p, encoding)


def write_weather_dataset(
    bundle: WeatherBundle,
    out_path: str | Path,
    compress_level: int = 4,
) -> None:
    """Write standardised weather variables to a NetCDF file.

    All data variables are stored as float32.
    """
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    # Build dataset from bundle
    data_vars = {}
    for vn in bundle.dataset.data_vars:
        da = bundle.dataset[vn]
        data_vars[vn] = da

    ds = xr.Dataset(data_vars, coords=bundle.dataset.coords)

    # Global attributes
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

    # Preserve crs if present
    if "crs" in bundle.dataset:
        ds["crs"] = bundle.dataset["crs"]

    # Encoding: float32 with zlib
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
