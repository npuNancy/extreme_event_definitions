"""Tests for cordex_nam12 adapter (rotated pole grid)."""
from __future__ import annotations

import numpy as np
import pytest
from argparse import Namespace

from grid_extreme_signals.adapters.cordex_nam12 import CordexNam12Adapter
from tests.conftest import make_cordex_dataset


class TestRotatedPoleDims:
    def test_output_dims(self, tmp_path):
        for var in ("tas", "uas", "vas", "rsds"):
            make_cordex_dataset(tmp_path, var=var)
        args = Namespace(
            data_dir=str(tmp_path / "CORDEX"),
            gcm_model="TEST-GCM", realization="r1i1p1f1",
            rcm_model="TEST-RCM", scenario="ssp126",
            years="2020", output_root=str(tmp_path / "out"),
            allow_unit_inference=False, allow_missing_optional=False,
        )
        adapter = CordexNam12Adapter(args)
        tasks = adapter.iter_tasks(args)
        bundle = adapter.load_wind_weather(tasks[0])

        assert set(["time", "rlat", "rlon"]) <= set(bundle.dataset.dims)
        assert bundle.grid_kind == "rotated_pole"
        assert bundle.spatial_dims == ("rlat", "rlon")


class Test2dLatLonPreserved:
    def test_auxiliary_coords(self, tmp_path):
        for var in ("tas", "uas", "vas", "rsds"):
            make_cordex_dataset(tmp_path, var=var)
        args = Namespace(
            data_dir=str(tmp_path / "CORDEX"),
            gcm_model="TEST-GCM", realization="r1i1p1f1",
            rcm_model="TEST-RCM", scenario="ssp126",
            years="2020", output_root=str(tmp_path / "out"),
            allow_unit_inference=False, allow_missing_optional=False,
        )
        adapter = CordexNam12Adapter(args)
        tasks = adapter.iter_tasks(args)
        bundle = adapter.load_wind_weather(tasks[0])

        # Check that lat/lon exist as coordinates
        assert "lat" in bundle.dataset.coords or "latitude" in bundle.dataset.coords
        assert "lon" in bundle.dataset.coords or "longitude" in bundle.dataset.coords

        # Check that lat/lon are 2D
        lat_coord = bundle.dataset.coords.get("lat", bundle.dataset.coords.get("latitude"))
        lon_coord = bundle.dataset.coords.get("lon", bundle.dataset.coords.get("longitude"))
        assert lat_coord.ndim == 2
        assert lon_coord.ndim == 2
        assert set(lat_coord.dims) == {"rlat", "rlon"}
        assert set(lon_coord.dims) == {"rlat", "rlon"}


class TestNoSpatialInterpolation:
    def test_grid_preserved(self, tmp_path):
        for var in ("tas", "uas", "vas", "rsds"):
            make_cordex_dataset(tmp_path, var=var)
        args = Namespace(
            data_dir=str(tmp_path / "CORDEX"),
            gcm_model="TEST-GCM", realization="r1i1p1f1",
            rcm_model="TEST-RCM", scenario="ssp126",
            years="2020", output_root=str(tmp_path / "out"),
            allow_unit_inference=False, allow_missing_optional=False,
        )
        adapter = CordexNam12Adapter(args)
        tasks = adapter.iter_tasks(args)
        bundle = adapter.load_wind_weather(tasks[0])
        # Grid should be rotated pole, not lat/lon
        assert "lat" not in bundle.dataset.dims
        assert "lon" not in bundle.dataset.dims


class TestAnnualFileDiscovery:
    def test_finds_year_files(self, tmp_path):
        make_cordex_dataset(tmp_path, var="tas", year=2020)
        make_cordex_dataset(tmp_path, var="tas", year=2021)
        args = Namespace(
            data_dir=str(tmp_path / "CORDEX"),
            gcm_model="TEST-GCM", realization="r1i1p1f1",
            rcm_model="TEST-RCM", scenario="ssp126",
            years="2020-2021", output_root=str(tmp_path / "out"),
            allow_unit_inference=False, allow_missing_optional=False,
        )
        adapter = CordexNam12Adapter(args)
        tasks = adapter.iter_tasks(args)
        assert len(tasks) == 2
        years = {t["year"] for t in tasks}
        assert years == {2020, 2021}
