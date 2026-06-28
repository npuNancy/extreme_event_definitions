"""Tests for optional weather output (--save_weather)."""
from __future__ import annotations

from pathlib import Path
from argparse import Namespace

import numpy as np
import pytest
import xarray as xr

from grid_extreme_signals.signal_runner import run_signal_pipeline
from tests.conftest import make_bcsd_dataset


def _create_bcsd_data(tmp_path):
    for var in ("tas", "uas", "vas", "rsds", "pr"):
        make_bcsd_dataset(tmp_path, var=var)
    return tmp_path


def _base_args(tmp_path, save_weather=False):
    return Namespace(
        source="regional_bcsd",
        data_dir=str(tmp_path / "bcsd_outputs"),
        output_root=str(tmp_path / "output"),
        model="TEST-MODEL", region="TestRegion", scenario="ssp126",
        years="2015",
        months="", stations_dir=None,
        require_events=[], allow_missing_optional=True,
        allow_unit_inference=False,
        chunk_time=512, compress_level=4,
        overwrite=True, dry_run=False,
        save_weather=save_weather,
        gcm_model=None, realization=None, rcm_model=None,
        era5land_d2m_root=None, dust_dir=None,
    )


class TestDefaultNoWeatherDir:
    def test_no_weather_directory(self, tmp_path):
        """Without --save_weather, no weather/ directory is created."""
        _create_bcsd_data(tmp_path)
        args = _base_args(tmp_path, save_weather=False)
        from grid_extreme_signals.adapters.regional_bcsd import RegionalBcsdAdapter
        adapter = RegionalBcsdAdapter(args)
        run_signal_pipeline(adapter, args)

        weather_dir = Path(args.output_root) / "regional_bcsd" / "TEST-MODEL" / "TestRegion" / "ssp126" / "weather"
        assert not weather_dir.exists()


class TestSaveWeatherEnabled:
    def test_creates_weather_dir(self, tmp_path):
        """With --save_weather, weather/ directory exists."""
        _create_bcsd_data(tmp_path)
        args = _base_args(tmp_path, save_weather=True)
        from grid_extreme_signals.adapters.regional_bcsd import RegionalBcsdAdapter
        adapter = RegionalBcsdAdapter(args)
        run_signal_pipeline(adapter, args)

        weather_dir = Path(args.output_root) / "regional_bcsd" / "TEST-MODEL" / "TestRegion" / "ssp126" / "weather"
        assert weather_dir.exists()

    def test_creates_weather_files(self, tmp_path):
        """With --save_weather, weather_*.nc files are created."""
        _create_bcsd_data(tmp_path)
        args = _base_args(tmp_path, save_weather=True)
        from grid_extreme_signals.adapters.regional_bcsd import RegionalBcsdAdapter
        adapter = RegionalBcsdAdapter(args)
        run_signal_pipeline(adapter, args)

        weather_dir = Path(args.output_root) / "regional_bcsd" / "TEST-MODEL" / "TestRegion" / "ssp126" / "weather"
        weather_files = list(weather_dir.glob("weather_*.nc"))
        assert len(weather_files) > 0


class TestWeatherSavedAttr:
    def test_false_without_save(self, tmp_path):
        """Signal file attr weather_saved='false' when --save_weather not passed."""
        _create_bcsd_data(tmp_path)
        args = _base_args(tmp_path, save_weather=False)
        from grid_extreme_signals.adapters.regional_bcsd import RegionalBcsdAdapter
        adapter = RegionalBcsdAdapter(args)
        run_signal_pipeline(adapter, args)

        signals_dir = Path(args.output_root) / "regional_bcsd" / "TEST-MODEL" / "TestRegion" / "ssp126" / "signals"
        nc_files = list(signals_dir.glob("extreme_signals_wind_*.nc"))
        assert len(nc_files) > 0
        ds = xr.open_dataset(str(nc_files[0]))
        assert ds.attrs["weather_saved"] == "false"
        ds.close()

    def test_true_with_save(self, tmp_path):
        """Signal file attr weather_saved='true' when --save_weather is passed."""
        _create_bcsd_data(tmp_path)
        args = _base_args(tmp_path, save_weather=True)
        from grid_extreme_signals.adapters.regional_bcsd import RegionalBcsdAdapter
        adapter = RegionalBcsdAdapter(args)
        run_signal_pipeline(adapter, args)

        signals_dir = Path(args.output_root) / "regional_bcsd" / "TEST-MODEL" / "TestRegion" / "ssp126" / "signals"
        nc_files = list(signals_dir.glob("extreme_signals_wind_*.nc"))
        assert len(nc_files) > 0
        ds = xr.open_dataset(str(nc_files[0]))
        assert ds.attrs["weather_saved"] == "true"
        ds.close()


class TestSignalsIdenticalWithOrWithoutSaveWeather:
    def test_same_signal_values(self, tmp_path):
        """Signal values should be identical regardless of --save_weather."""
        _create_bcsd_data(tmp_path)

        # Run without save_weather
        args1 = _base_args(tmp_path / "run1", save_weather=False)
        args1.output_root = str(tmp_path / "run1")
        from grid_extreme_signals.adapters.regional_bcsd import RegionalBcsdAdapter
        adapter1 = RegionalBcsdAdapter(args1)
        run_signal_pipeline(adapter1, args1)

        # Run with save_weather
        args2 = _base_args(tmp_path / "run2", save_weather=True)
        args2.output_root = str(tmp_path / "run2")
        adapter2 = RegionalBcsdAdapter(args2)
        run_signal_pipeline(adapter2, args2)

        # Compare wind signal files
        path1 = Path(args1.output_root) / "regional_bcsd" / "TEST-MODEL" / "TestRegion" / "ssp126" / "signals"
        path2 = Path(args2.output_root) / "regional_bcsd" / "TEST-MODEL" / "TestRegion" / "ssp126" / "signals"

        files1 = sorted(path1.glob("extreme_signals_wind_*.nc"))
        files2 = sorted(path2.glob("extreme_signals_wind_*.nc"))
        assert len(files1) == len(files2)

        for f1, f2 in zip(files1, files2):
            ds1 = xr.open_dataset(str(f1))
            ds2 = xr.open_dataset(str(f2))
            for var in ds1.data_vars:
                if var.startswith("signal_"):
                    np.testing.assert_array_equal(
                        ds1[var].values, ds2[var].values,
                        err_msg=f"{var} differs between save_weather=False and True",
                    )
            ds1.close()
            ds2.close()
