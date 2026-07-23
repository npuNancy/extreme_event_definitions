"""Tests for io_utils module."""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from grid_extreme_signals.io_utils import (
    atomic_write_netcdf,
    discover_regions,
    get_var_name,
    is_valid_region_name,
    output_complete,
    skip_existing,
    validate_same_spatial_grid,
)


def _make_ds(**vars):
    """Create a Dataset with proper DataArray variables."""
    data_vars = {}
    for name, vals in vars.items():
        arr = np.asarray(vals)
        data_vars[name] = xr.DataArray(arr, dims=(f"dim_{name}",))
    return xr.Dataset(data_vars)


class TestGetVarName:
    def test_bcsd_suffix(self):
        ds = _make_ds(tas_bcsd=[1.0], other=[2.0])
        assert get_var_name(ds, "tas") == "tas_bcsd"

    def test_hurs_bcsd_suffix(self):
        ds = _make_ds(hurs_bcsd=[85.0], other=[2.0])
        assert get_var_name(ds, "hurs") == "hurs_bcsd"

    def test_plain_name(self):
        ds = _make_ds(tas=[1.0], other=[2.0])
        assert get_var_name(ds, "tas") == "tas"

    def test_sole_variable(self):
        ds = _make_ds(only_var=[1.0])
        assert get_var_name(ds, "tas") == "only_var"

    def test_ambiguous_raises(self):
        ds = _make_ds(a=[1.0], b=[2.0])
        with pytest.raises(KeyError, match="无法在数据集中解析变量"):
            get_var_name(ds, "tas")

    def test_no_bcsd_suffix(self):
        ds = _make_ds(tas_bcsd=[1.0], tas=[2.0])
        assert get_var_name(ds, "tas", use_bcsd_suffix=False) == "tas"


class TestIsValidRegionName:
    @pytest.mark.parametrize("name,expected", [
        ("Austria", True),
        ("_private", False),
        ("temp_", False),
        ("run_repeated", False),
        ("", False),
        ("China", True),
    ])
    def test_various(self, name, expected):
        assert is_valid_region_name(name) is expected


class TestDiscoverRegions:
    def test_discovers_valid(self, tmp_path):
        (tmp_path / "TEST-MODEL" / "Austria").mkdir(parents=True)
        (tmp_path / "TEST-MODEL" / "China").mkdir(parents=True)
        (tmp_path / "TEST-MODEL" / "_hidden").mkdir(parents=True)
        (tmp_path / "TEST-MODEL" / "temp_").mkdir(parents=True)
        regions = discover_regions(str(tmp_path), "TEST-MODEL")
        assert "Austria" in regions
        assert "China" in regions
        assert "_hidden" not in regions
        assert "temp_" not in regions


class TestValidateSameSpatialGrid:
    def test_matching_grids(self):
        lat = np.linspace(40, 50, 5)
        lon = np.linspace(0, 10, 10)
        ds_ref = xr.Dataset(coords={"lat": lat, "lon": lon})
        ds_other = xr.Dataset(coords={"lat": lat, "lon": lon})
        validate_same_spatial_grid(ds_ref, {"other": ds_other}, "lat", "lon")

    def test_mismatching_grids_raises(self):
        ds_ref = xr.Dataset(coords={"lat": np.linspace(40, 50, 5), "lon": np.linspace(0, 10, 10)})
        ds_other = xr.Dataset(coords={"lat": np.linspace(40, 50, 6), "lon": np.linspace(0, 10, 10)})
        with pytest.raises(ValueError, match="网格与参考网格不一致"):
            validate_same_spatial_grid(ds_ref, {"other": ds_other}, "lat", "lon")


class TestAtomicWrite:
    def test_creates_file(self, tmp_path):
        ds = xr.Dataset({"x": ("d", [1, 2, 3])})
        path = tmp_path / "output.nc"
        atomic_write_netcdf(ds, str(path))
        assert path.exists()
        result = xr.open_dataset(str(path))
        np.testing.assert_array_equal(result["x"].values, [1, 2, 3])
        result.close()

    def test_no_partial_on_error(self, tmp_path):
        """If write fails, no partial file should remain."""
        path = tmp_path / "output.nc"
        class BadDataset:
            def to_netcdf(self, *a, **kw):
                raise RuntimeError("simulated failure")
        with pytest.raises(RuntimeError):
            atomic_write_netcdf(BadDataset(), str(path))
        assert not path.exists()


class TestOutputComplete:
    def test_missing_file(self, tmp_path):
        assert output_complete(tmp_path / "nonexistent.nc") is False

    def test_valid_file(self, tmp_path):
        times = np.arange(
            np.datetime64("2020-01-01"),
            np.datetime64("2020-01-02"),
            np.timedelta64(1, "h"),
        )
        ds = xr.Dataset({"x": ("time", np.ones(24))}, coords={"time": times})
        path = tmp_path / "valid.nc"
        ds.to_netcdf(str(path))
        assert output_complete(path) is True


class TestSkipExisting:
    def test_overwrite(self, tmp_path):
        path = tmp_path / "file.nc"
        times = np.arange(np.datetime64("2020-01-01"), np.datetime64("2020-01-02"),
                          np.timedelta64(1, "h"))
        xr.Dataset({"x": ("time", np.ones(24))}, coords={"time": times}).to_netcdf(str(path))
        assert skip_existing(path, overwrite=True) is False
        assert skip_existing(path, overwrite=False) is True
