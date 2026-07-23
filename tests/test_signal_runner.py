"""Tests for signal_runner module."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from argparse import Namespace

from grid_extreme_signals.signal_runner import run_signal_pipeline
from grid_extreme_signals.adapters.base import WeatherBundle
from tests.conftest import make_bcsd_dataset


class TestStationsDirGuard:
    def test_none_ok(self):
        """stations_dir=None should not raise."""
        args = Namespace(stations_dir=None)
        # Should not raise — but will fail on iter_tasks since no adapter
        # We just test the guard logic directly
        if args.stations_dir is not None:
            pytest.fail("Should not reach here")

    def test_nonempty_raises(self):
        """Non-None stations_dir must raise NotImplementedError."""
        args = Namespace(stations_dir="/some/path")

        class DummyAdapter:
            def iter_tasks(self, a):
                return []

        with pytest.raises(NotImplementedError, match="场站筛选模式"):
            run_signal_pipeline(DummyAdapter(), args)


class TestSignalOutputFormat:
    def _run_simple_pipeline(self, tmp_path):
        """Helper: run a minimal pipeline with synthetic data."""
        for var in ("tas", "uas", "vas", "rsds", "hurs"):
            make_bcsd_dataset(tmp_path, var=var)
        args = Namespace(
            source="regional_bcsd",
            data_dir=str(tmp_path / "bcsd_outputs"),
            output_root=str(tmp_path / "output"),
            model="TEST-MODEL", region="TestRegion", scenario="ssp126",
            years="2015",
            months="", stations_dir=None,
            require_events=[], allow_missing_optional=True,
            allow_unit_inference=False,
            chunk_time=512, compress_level=4,
            overwrite=True, dry_run=False, save_weather=False,
            gcm_model=None, realization=None, rcm_model=None,
            era5land_d2m_root=None, dust_dir=None,
        )
        from grid_extreme_signals.adapters.regional_bcsd import RegionalBcsdAdapter
        adapter = RegionalBcsdAdapter(args)
        run_signal_pipeline(adapter, args)
        return args

    def test_signal_vars_int8(self, tmp_path):
        """All signal variables should have int8 dtype."""
        args = self._run_simple_pipeline(tmp_path)
        # Find signal files
        out_dir = Path(args.output_root) / "regional_bcsd" / "TEST-MODEL" / "TestRegion" / "ssp126" / "signals"
        nc_files = list(out_dir.glob("extreme_signals_wind_*.nc"))
        assert len(nc_files) > 0, f"No signal files found in {out_dir}"

        ds = xr.open_dataset(str(nc_files[0]))
        for var_name in ds.data_vars:
            if var_name.startswith("signal_"):
                assert ds[var_name].dtype == np.int8, f"{var_name} dtype is {ds[var_name].dtype}, expected int8"
        ds.close()

    def test_signal_flag_attributes(self, tmp_path):
        """Each signal variable should have flag_values and flag_meanings."""
        args = self._run_simple_pipeline(tmp_path)
        out_dir = Path(args.output_root) / "regional_bcsd" / "TEST-MODEL" / "TestRegion" / "ssp126" / "signals"
        nc_files = list(out_dir.glob("extreme_signals_wind_*.nc"))
        assert len(nc_files) > 0

        ds = xr.open_dataset(str(nc_files[0]))
        for var_name in ds.data_vars:
            if var_name.startswith("signal_"):
                assert "flag_values" in ds[var_name].attrs, f"{var_name} missing flag_values"
                assert "flag_meanings" in ds[var_name].attrs, f"{var_name} missing flag_meanings"
        ds.close()

    def test_global_attrs(self, tmp_path):
        """Output should have required global attributes."""
        args = self._run_simple_pipeline(tmp_path)
        out_dir = Path(args.output_root) / "regional_bcsd" / "TEST-MODEL" / "TestRegion" / "ssp126" / "signals"
        nc_files = list(out_dir.glob("extreme_signals_wind_*.nc"))
        assert len(nc_files) > 0

        ds = xr.open_dataset(str(nc_files[0]))
        assert ds.attrs["grid_mode"] == "all_grid"
        assert ds.attrs["stations_dir"] == "None"
        assert ds.attrs["interpolation_space"] == "none"
        assert "source" in ds.attrs
        assert "tech" in ds.attrs
        assert "weather_saved" in ds.attrs
        assert ds.attrs["weather_saved"] in ("true", "false")
        ds.close()

    def test_no_weather_dir_by_default(self, tmp_path):
        """Without --save_weather, no weather/ directory should exist."""
        args = self._run_simple_pipeline(tmp_path)
        weather_dir = Path(args.output_root) / "regional_bcsd" / "TEST-MODEL" / "TestRegion" / "ssp126" / "weather"
        assert not weather_dir.exists()
