"""Tests for china_cmfd_bcsd adapter."""
from __future__ import annotations

import numpy as np
import pytest
from argparse import Namespace

from grid_extreme_signals.adapters.china_cmfd_bcsd import ChinaCmfdBcsdAdapter
from tests.conftest import make_china_bcsd_dataset


class TestSfcWindDirect:
    def test_wind_ms_from_sfcwind(self, tmp_path):
        """wind_ms should come directly from sfcWind, not sqrt(uas^2+vas^2)."""
        for var in ("tas", "rsds", "sfcWind"):
            make_china_bcsd_dataset(tmp_path, var=var)
        args = Namespace(
            data_dir=str(tmp_path / "cmip6_downscaling_3hr"),
            model="TEST-MODEL", scenario="ssp126",
            years="2015-2016", output_root=str(tmp_path / "out"),
            allow_unit_inference=False, allow_missing_optional=False,
        )
        adapter = ChinaCmfdBcsdAdapter(args)
        tasks = adapter.iter_tasks(args)
        bundle = adapter.load_wind_weather(tasks[0])
        assert "wind_ms" in bundle.dataset.data_vars
        # wind_ms should have the same shape as the source
        assert bundle.dataset["wind_ms"].ndim == 3  # (time, lat, lon)


class TestPrOptional:
    def test_pr_missing_skips_events(self, tmp_path):
        """Without pr, freezing_rain and rainstorm should be skipped."""
        for var in ("tas", "rsds", "sfcWind"):
            make_china_bcsd_dataset(tmp_path, var=var)
        args = Namespace(
            data_dir=str(tmp_path / "cmip6_downscaling_3hr"),
            model="TEST-MODEL", scenario="ssp126",
            years="2015-2016", output_root=str(tmp_path / "out"),
            allow_unit_inference=False, allow_missing_optional=True,
        )
        adapter = ChinaCmfdBcsdAdapter(args)
        tasks = adapter.iter_tasks(args)
        bundle = adapter.load_solar_weather(tasks[0])
        assert "precip_mmh" not in bundle.dataset.data_vars
        assert "precip_mmh" in bundle.skipped_inputs


class TestNoRegionDimension:
    def test_no_region_in_task(self, tmp_path):
        for var in ("tas", "rsds", "sfcWind"):
            make_china_bcsd_dataset(tmp_path, var=var)
        args = Namespace(
            data_dir=str(tmp_path / "cmip6_downscaling_3hr"),
            model="TEST-MODEL", scenario="ssp126",
            years="2015", output_root=str(tmp_path / "out"),
            allow_unit_inference=False, allow_missing_optional=False,
        )
        adapter = ChinaCmfdBcsdAdapter(args)
        tasks = adapter.iter_tasks(args)
        assert len(tasks) == 1
        assert "region" not in tasks[0]


class TestRequireEvents:
    def test_missing_pr_raises(self, tmp_path):
        """--require_events rainstorm without pr should raise."""
        import sys
        sys.path.insert(0, str(tmp_path))
        for var in ("tas", "rsds", "sfcWind"):
            make_china_bcsd_dataset(tmp_path, var=var)
        args = Namespace(
            data_dir=str(tmp_path / "cmip6_downscaling_3hr"),
            model="TEST-MODEL", scenario="ssp126",
            years="2015-2016", output_root=str(tmp_path / "out"),
            allow_unit_inference=False, allow_missing_optional=True,
            require_events=["rainstorm"], save_weather=False,
            overwrite=True, dry_run=False, compress_level=4,
            stations_dir=None, chunk_time=512,
        )
        adapter = ChinaCmfdBcsdAdapter(args)
        from grid_extreme_signals.signal_runner import run_signal_pipeline
        with pytest.raises(RuntimeError, match="Required event"):
            run_signal_pipeline(adapter, args)
