"""Shared test fixtures — synthetic NetCDF files for each data source format."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from argparse import Namespace

import numpy as np
import pytest
import xarray as xr

# Ensure project root importable
_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# =====================================================================
# Helpers for creating synthetic NetCDF files
# =====================================================================

def make_bcsd_dataset(
    tmp_path: Path,
    *,
    var: str,
    model: str = "TEST-MODEL",
    region: str = "TestRegion",
    scenario: str = "ssp126",
    years: str = "2015-2016",
    nlat: int = 4,
    nlon: int = 6,
    nt: int = 8,  # 3-hourly steps
) -> Path:
    """Create a minimal BCSD-format NetCDF with known values."""
    base = tmp_path / "bcsd_outputs" / model / region / model
    base.mkdir(parents=True, exist_ok=True)

    # Generate 3-hourly time axis for the year range
    y0, y1 = int(years.split("-")[0]), int(years.split("-")[-1])
    times = np.arange(
        np.datetime64(f"{y0}-01-01T00:00"),
        np.datetime64(f"{y0}-01-01T00:00") + np.timedelta64(nt, "h") * 3,
        np.timedelta64(3, "h"),
    ).astype("datetime64[ns]")[:nt]

    lat = np.linspace(45, 48, nlat, dtype=np.float32)
    lon = np.linspace(10, 15, nlon, dtype=np.float32)

    # Variable values: use a simple pattern so results are predictable
    rng = np.random.RandomState(42)
    values = rng.randn(nt, nlat, nlon).astype(np.float32)

    var_name = f"{var}_bcsd"
    ds = xr.Dataset(
        {var_name: xr.DataArray(values, dims=("time", "lat", "lon"),
                                 attrs={"units": _bcsd_units(var)})},
        coords={"time": times, "lat": lat, "lon": lon},
    )

    fname = f"{var}_3h_bcsd_on_0p1deg_{region}_{model}_{scenario}_{years}.nc"
    path = base / fname
    ds.to_netcdf(str(path))
    return path


def make_china_bcsd_dataset(
    tmp_path: Path,
    *,
    var: str,
    model: str = "TEST-MODEL",
    scenario: str = "ssp126",
    years: str = "2015-2016",
    nlat: int = 4,
    nlon: int = 6,
    nt: int = 8,
) -> Path:
    """Create a minimal China CMFD BCSD-format NetCDF."""
    base = tmp_path / "cmip6_downscaling_3hr" / model
    base.mkdir(parents=True, exist_ok=True)

    y0 = int(years.split("-")[0])
    times = np.arange(
        np.datetime64(f"{y0}-01-01T00:00"),
        np.datetime64(f"{y0}-01-01T00:00") + np.timedelta64(nt, "h") * 3,
        np.timedelta64(3, "h"),
    ).astype("datetime64[ns]")[:nt]

    lat = np.linspace(20, 50, nlat, dtype=np.float32)
    lon = np.linspace(75, 135, nlon, dtype=np.float32)

    rng = np.random.RandomState(42)
    values = rng.randn(nt, nlat, nlon).astype(np.float32)

    var_name = f"{var}_bcsd"
    ds = xr.Dataset(
        {var_name: xr.DataArray(values, dims=("time", "lat", "lon"),
                                 attrs={"units": _bcsd_units(var)})},
        coords={"time": times, "lat": lat, "lon": lon},
    )

    fname = f"{var}_3h_bcsd_on_0.1deg_china_{scenario}_{years}.nc"
    path = base / fname
    ds.to_netcdf(str(path))
    return path


def make_cordex_dataset(
    tmp_path: Path,
    *,
    var: str,
    gcm_model: str = "TEST-GCM",
    realization: str = "r1i1p1f1",
    rcm_model: str = "TEST-RCM",
    scenario: str = "ssp126",
    year: int = 2020,
    nrlat: int = 4,
    nrlon: int = 6,
    nt: int = 24,
) -> Path:
    """Create a minimal CORDEX NAM-12 rotated pole NetCDF."""
    base = (
        tmp_path / "CORDEX" / gcm_model / realization / rcm_model / scenario / var
    )
    base.mkdir(parents=True, exist_ok=True)

    times = np.arange(
        np.datetime64(f"{year}-01-01T00:00"),
        np.datetime64(f"{year}-01-01T00:00") + np.timedelta64(nt, "h"),
        np.timedelta64(1, "h"),
    ).astype("datetime64[ns]")[:nt]

    rlat = np.linspace(-20, 20, nrlat, dtype=np.float32)
    rlon = np.linspace(-30, 30, nrlon, dtype=np.float32)

    # 2D lat/lon
    lat2d = np.broadcast_to(
        np.linspace(30, 60, nrlat)[:, None], (nrlat, nrlon)
    ).astype(np.float32)
    lon2d = np.broadcast_to(
        np.linspace(-130, -60, nrlon)[None, :], (nrlat, nrlon)
    ).astype(np.float32)

    rng = np.random.RandomState(42)
    values = rng.randn(nt, nrlat, nrlon).astype(np.float32)

    ds = xr.Dataset(
        {var: xr.DataArray(values, dims=("time", "rlat", "rlon"),
                           attrs={"units": _cordex_units(var)})},
        coords={
            "time": times,
            "rlat": rlat,
            "rlon": rlon,
            "lat": xr.DataArray(lat2d, dims=("rlat", "rlon")),
            "lon": xr.DataArray(lon2d, dims=("rlat", "rlon")),
        },
    )

    fname = f"{var}_NAM-12_{gcm_model}_{realization}_{rcm_model}_{scenario}_{year}01010000-{year}12312300.nc"
    path = base / fname
    ds.to_netcdf(str(path))
    return path


def make_era5land_monthly(
    tmp_path: Path,
    *,
    var: str,
    year: int = 2024,
    month: int = 1,
    nlat: int = 4,
    nlon: int = 6,
    nt: int = 24,
    accumulated: bool = False,
) -> Path:
    """Create a minimal ERA5-Land monthly NetCDF."""
    base = tmp_path / "ERA5_land" / "global" / var
    base.mkdir(parents=True, exist_ok=True)

    times = np.arange(
        np.datetime64(f"{year}-{month:02d}-01T00:00"),
        np.datetime64(f"{year}-{month:02d}-01T00:00") + np.timedelta64(nt, "h"),
        np.timedelta64(1, "h"),
    ).astype("datetime64[ns]")[:nt]

    lat = np.linspace(40, 50, nlat, dtype=np.float32)
    lon = np.linspace(0, 10, nlon, dtype=np.float32)

    rng = np.random.RandomState(42)
    if accumulated:
        # Simulate daily accumulation: increasing within each day
        values = np.abs(rng.randn(nt, nlat, nlon)).astype(np.float32)
        # Make values accumulate within each day
        for i in range(nt):
            hour = i % 24
            if hour > 0:
                values[i] += values[i - 1] if (i % 24) > 0 else 0
    else:
        values = rng.randn(nt, nlat, nlon).astype(np.float32)

    units = _era5_units(var)
    ds = xr.Dataset(
        {var: xr.DataArray(values, dims=("valid_time", "latitude", "longitude"),
                           attrs={"units": units})},
        coords={
            "valid_time": times,
            "latitude": lat,
            "longitude": lon,
        },
    )

    fname = f"{var}_{year:04d}_{month:02d}.nc"
    path = base / fname
    ds.to_netcdf(str(path))
    return path


# =====================================================================
# Unit string helpers
# =====================================================================

def _bcsd_units(var: str) -> str:
    return {"pr": "kg m-2 s-1", "rsds": "W m-2", "tas": "K",
            "uas": "m s-1", "vas": "m s-1", "sfcWind": "m s-1"}.get(var, "unknown")


def _cordex_units(var: str) -> str:
    return _bcsd_units(var)


def _era5_units(var: str) -> str:
    return {"t2m": "K", "u10": "m s-1", "v10": "m s-1",
            "tp": "m", "ssrd": "J m-2", "d2m": "K"}.get(var, "unknown")


# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
def bcsd_data_dir(tmp_path):
    """Create a full set of BCSD files for regional_bcsd testing."""
    for var in ("tas", "uas", "vas", "rsds", "pr"):
        make_bcsd_dataset(tmp_path, var=var)
    return str(tmp_path / "bcsd_outputs")


@pytest.fixture
def china_data_dir(tmp_path):
    """Create a full set of China CMFD BCSD files."""
    for var in ("tas", "rsds", "sfcWind"):
        make_china_bcsd_dataset(tmp_path, var=var)
    return str(tmp_path / "cmip6_downscaling_3hr")


@pytest.fixture
def cordex_data_dir(tmp_path):
    """Create a full set of CORDEX NAM-12 files."""
    for var in ("tas", "uas", "vas", "rsds"):
        make_cordex_dataset(tmp_path, var=var)
    return str(tmp_path / "CORDEX")


@pytest.fixture
def era5land_data_dir(tmp_path):
    """Create a full set of ERA5-Land monthly files for one month."""
    for var in ("t2m", "u10", "v10", "tp", "ssrd", "d2m"):
        make_era5land_monthly(tmp_path, var=var, accumulated=(var in ("tp", "ssrd")))
    return str(tmp_path)


@pytest.fixture
def default_args():
    """Return a base Namespace for testing."""
    return Namespace(
        source="regional_bcsd",
        data_dir="data/bcsd_outputs",
        output_root="outputs/grid_extreme_signals",
        years="2015",
        months="",
        stations_dir=None,
        require_events=[],
        allow_missing_optional=False,
        allow_unit_inference=False,
        chunk_time=512,
        compress_level=4,
        overwrite=False,
        dry_run=False,
        save_weather=False,
        model=None,
        region=None,
        scenario=None,
        gcm_model=None,
        realization=None,
        rcm_model=None,
        era5land_d2m_root=None,
        dust_dir=None,
    )
