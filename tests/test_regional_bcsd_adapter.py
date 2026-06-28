"""Tests for regional_bcsd adapter."""
from __future__ import annotations

import numpy as np
import pytest
from argparse import Namespace

from grid_extreme_signals.adapters.regional_bcsd import RegionalBcsdAdapter
from tests.conftest import make_bcsd_dataset


class TestIterTasks:
    def test_single_region(self, tmp_path):
        for var in ("tas", "uas", "vas", "rsds", "pr"):
            make_bcsd_dataset(tmp_path, var=var)
        args = Namespace(
            data_dir=str(tmp_path / "bcsd_outputs"),
            model="TEST-MODEL", region="TestRegion", scenario="ssp126",
            years="2015-2016", output_root=str(tmp_path / "out"),
            allow_unit_inference=False, allow_missing_optional=False,
        )
        adapter = RegionalBcsdAdapter(args)
        tasks = adapter.iter_tasks(args)
        assert len(tasks) == 2  # 2015, 2016
        assert tasks[0]["year"] == 2015
        assert tasks[0]["region"] == "TestRegion"

    def test_all_regions(self, tmp_path):
        for var in ("tas", "uas", "vas", "rsds", "pr"):
            make_bcsd_dataset(tmp_path, var=var, region="RegionA")
            make_bcsd_dataset(tmp_path, var=var, region="RegionB")
        args = Namespace(
            data_dir=str(tmp_path / "bcsd_outputs"),
            model="TEST-MODEL", region="all", scenario="ssp126",
            years="2015", output_root=str(tmp_path / "out"),
            allow_unit_inference=False, allow_missing_optional=False,
        )
        adapter = RegionalBcsdAdapter(args)
        tasks = adapter.iter_tasks(args)
        regions = {t["region"] for t in tasks}
        assert "RegionA" in regions
        assert "RegionB" in regions


class TestLoadWindWeather:
    def test_structure(self, tmp_path):
        for var in ("tas", "uas", "vas", "rsds", "pr"):
            make_bcsd_dataset(tmp_path, var=var)
        args = Namespace(
            data_dir=str(tmp_path / "bcsd_outputs"),
            model="TEST-MODEL", region="TestRegion", scenario="ssp126",
            years="2015-2016", output_root=str(tmp_path / "out"),
            allow_unit_inference=False, allow_missing_optional=False,
        )
        adapter = RegionalBcsdAdapter(args)
        tasks = adapter.iter_tasks(args)
        bundle = adapter.load_wind_weather(tasks[0])

        assert bundle.source == "regional_bcsd"
        assert bundle.tech == "wind"
        assert bundle.grid_kind == "regular_latlon"
        assert "wind_ms" in bundle.dataset.data_vars
        assert "temp_C" in bundle.dataset.data_vars
        assert "rh_pct" not in bundle.dataset.data_vars
        assert bundle.skipped_inputs.get("rh_pct") is not None


class TestLoadSolarWeather:
    def test_structure(self, tmp_path):
        for var in ("tas", "uas", "vas", "rsds", "pr"):
            make_bcsd_dataset(tmp_path, var=var)
        args = Namespace(
            data_dir=str(tmp_path / "bcsd_outputs"),
            model="TEST-MODEL", region="TestRegion", scenario="ssp126",
            years="2015-2016", output_root=str(tmp_path / "out"),
            allow_unit_inference=False, allow_missing_optional=False,
        )
        adapter = RegionalBcsdAdapter(args)
        tasks = adapter.iter_tasks(args)
        bundle = adapter.load_solar_weather(tasks[0])

        assert bundle.source == "regional_bcsd"
        assert bundle.tech == "solar"
        assert "temp_C" in bundle.dataset.data_vars
        assert "wind_ms" in bundle.dataset.data_vars
        assert "rsds" in bundle.dataset.data_vars
        assert "precip_mmh" in bundle.dataset.data_vars

    def test_pr_optional(self, tmp_path):
        """When pr file is missing, precip_mmh should be skipped."""
        for var in ("tas", "uas", "vas", "rsds"):
            make_bcsd_dataset(tmp_path, var=var)
        # No pr file
        args = Namespace(
            data_dir=str(tmp_path / "bcsd_outputs"),
            model="TEST-MODEL", region="TestRegion", scenario="ssp126",
            years="2015-2016", output_root=str(tmp_path / "out"),
            allow_unit_inference=False, allow_missing_optional=True,
        )
        adapter = RegionalBcsdAdapter(args)
        tasks = adapter.iter_tasks(args)
        bundle = adapter.load_solar_weather(tasks[0])
        assert "precip_mmh" not in bundle.dataset.data_vars
        assert "precip_mmh" in bundle.skipped_inputs


class TestOutputPaths:
    def test_signal_path(self, tmp_path):
        for var in ("tas", "uas", "vas", "rsds", "pr"):
            make_bcsd_dataset(tmp_path, var=var)
        args = Namespace(
            data_dir=str(tmp_path / "bcsd_outputs"),
            model="TEST-MODEL", region="TestRegion", scenario="ssp126",
            years="2015", output_root="outputs/test",
            allow_unit_inference=False, allow_missing_optional=False,
        )
        adapter = RegionalBcsdAdapter(args)
        tasks = adapter.iter_tasks(args)
        path = adapter.signal_output_path(tasks[0], "wind")
        assert "regional_bcsd" in path
        assert "TEST-MODEL" in path
        assert "TestRegion" in path
        assert "2015" in path
