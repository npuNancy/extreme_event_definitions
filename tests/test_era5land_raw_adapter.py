"""Tests for era5land_raw adapter (deaccumulation, per-month processing)."""
from __future__ import annotations

import numpy as np
import pytest
from argparse import Namespace

from grid_extreme_signals.adapters.era5land_raw import (
    Era5LandRawAdapter,
    deaccumulate_era5land,
)
from tests.conftest import make_era5land_monthly


class TestDeaccumulate:
    def test_01_reset(self):
        """At hour==1, increment equals current accumulated value."""
        accum = np.array([10.0, 20.0, 35.0], dtype=np.float32).reshape(3, 1, 1)
        hours = np.array([1, 2, 3], dtype=np.int16)
        result = deaccumulate_era5land(accum, hours)
        # hour==1: inc = cur = 10
        assert result[0, 0, 0] == pytest.approx(10.0)
        # hour==2: inc = cur - prev = 20 - 10 = 10
        assert result[1, 0, 0] == pytest.approx(10.0)
        # hour==3: inc = 35 - 20 = 15
        assert result[2, 0, 0] == pytest.approx(15.0)

    def test_normal_diff(self):
        """Non-reset hours use difference."""
        accum = np.array([0.0, 5.0, 12.0, 18.0], dtype=np.float32).reshape(4, 1, 1)
        hours = np.array([2, 3, 4, 5], dtype=np.int16)
        result = deaccumulate_era5land(accum, hours, prev_boundary=np.array([[0.0]]))
        # hour==2, prev=0: inc = 0 - 0 = 0
        # hour==3: inc = 5 - 0 = 5
        # hour==4: inc = 12 - 5 = 7
        # hour==5: inc = 18 - 12 = 6
        np.testing.assert_allclose(result.squeeze(), [0.0, 5.0, 7.0, 6.0])

    def test_cross_month_boundary(self):
        """First step of month uses previous month boundary."""
        accum = np.array([3.0, 8.0], dtype=np.float32).reshape(2, 1, 1)
        hours = np.array([0, 1], dtype=np.int16)
        prev = np.array([[1.0]])
        result = deaccumulate_era5land(accum, hours, prev)
        # hour==0: inc = cur - prev = 3 - 1 = 2
        assert result[0, 0, 0] == pytest.approx(2.0)
        # hour==1: inc = cur = 8 (reset)
        assert result[1, 0, 0] == pytest.approx(8.0)

    def test_no_prev_month_zero(self):
        """Missing previous boundary at non-01:00 → increment = 0."""
        accum = np.array([5.0, 10.0], dtype=np.float32).reshape(2, 1, 1)
        hours = np.array([0, 1], dtype=np.int16)
        result = deaccumulate_era5land(accum, hours, None, missing_boundary_policy="zero")
        assert result[0, 0, 0] == pytest.approx(0.0)  # missing boundary
        assert result[1, 0, 0] == pytest.approx(10.0)  # reset

    def test_no_prev_month_nan(self):
        """Missing previous boundary at non-01:00 → increment = NaN."""
        accum = np.array([5.0], dtype=np.float32).reshape(1, 1, 1)
        hours = np.array([0], dtype=np.int16)
        result = deaccumulate_era5land(accum, hours, None, missing_boundary_policy="nan")
        assert np.isnan(result[0, 0, 0])

    def test_negative_clipped(self):
        """Negative increments from data anomalies are clipped to 0."""
        # Accum goes backward: 10 -> 5 (should not produce negative increment)
        accum = np.array([10.0, 5.0], dtype=np.float32).reshape(2, 1, 1)
        hours = np.array([2, 3], dtype=np.int16)
        result = deaccumulate_era5land(accum, hours, prev_boundary=np.array([[8.0]]))
        # inc[0] = 10 - 8 = 2 (positive, ok)
        # inc[1] = 5 - 10 = -5 → clipped: use cur=5 then max(5,0)=5
        assert result[0, 0, 0] >= 0
        assert result[1, 0, 0] >= 0

    def test_all_nonnegative(self):
        """All output values must be non-negative."""
        rng = np.random.RandomState(42)
        accum = rng.randn(24, 3, 4).astype(np.float32).cumsum(axis=0)
        hours = np.arange(24, dtype=np.int16) % 24
        result = deaccumulate_era5land(accum, hours)
        assert (result >= 0).all()


class TestEra5LandAdapter:
    def test_per_month_output(self, tmp_path):
        """Output should be organized per month."""
        for var in ("t2m", "u10", "v10", "tp", "ssrd", "d2m"):
            make_era5land_monthly(tmp_path, var=var, accumulated=(var in ("tp", "ssrd")))
        args = Namespace(
            data_dir=str(tmp_path),
            years="2024", months="1",
            output_root=str(tmp_path / "out"),
            era5land_d2m_root=None, dust_dir=None,
            allow_missing_optional=False,
        )
        adapter = Era5LandRawAdapter(args)
        tasks = adapter.iter_tasks(args)
        assert len(tasks) == 1
        assert tasks[0]["year"] == 2024
        assert tasks[0]["month"] == 1

    def test_multiple_months(self, tmp_path):
        """Multiple months should produce multiple tasks."""
        for m in [1, 2]:
            for var in ("t2m", "u10", "v10", "tp", "ssrd", "d2m"):
                make_era5land_monthly(tmp_path, var=var, month=m,
                                     accumulated=(var in ("tp", "ssrd")))
        args = Namespace(
            data_dir=str(tmp_path),
            years="2024", months="1,2",
            output_root=str(tmp_path / "out"),
            era5land_d2m_root=None, dust_dir=None,
            allow_missing_optional=False,
        )
        adapter = Era5LandRawAdapter(args)
        tasks = adapter.iter_tasks(args)
        assert len(tasks) == 2

    def test_load_weather_has_rh(self, tmp_path):
        """When d2m is available, rh_pct should be present."""
        for var in ("t2m", "u10", "v10", "tp", "ssrd", "d2m"):
            make_era5land_monthly(tmp_path, var=var, accumulated=(var in ("tp", "ssrd")))
        args = Namespace(
            data_dir=str(tmp_path),
            years="2024", months="1",
            output_root=str(tmp_path / "out"),
            era5land_d2m_root=None, dust_dir=None,
            allow_missing_optional=False,
        )
        adapter = Era5LandRawAdapter(args)
        tasks = adapter.iter_tasks(args)
        bundle = adapter.load_wind_weather(tasks[0])
        assert "rh_pct" in bundle.dataset.data_vars

    def test_no_d2m_skips_rh(self, tmp_path):
        """When d2m is missing, rh_pct should be in skipped_inputs."""
        for var in ("t2m", "u10", "v10", "tp", "ssrd"):
            make_era5land_monthly(tmp_path, var=var, accumulated=(var in ("tp", "ssrd")))
        args = Namespace(
            data_dir=str(tmp_path),
            years="2024", months="1",
            output_root=str(tmp_path / "out"),
            era5land_d2m_root=None, dust_dir=None,
            allow_missing_optional=True,
        )
        adapter = Era5LandRawAdapter(args)
        tasks = adapter.iter_tasks(args)
        bundle = adapter.load_wind_weather(tasks[0])
        assert "rh_pct" in bundle.skipped_inputs

    def test_no_deaccum_on_bcsd(self):
        """Architectural check: deaccumulate_era5land should only exist in era5land_raw."""
        from grid_extreme_signals import adapters
        # regional_bcsd should NOT import deaccumulate_era5land
        import importlib
        mod = importlib.import_module("grid_extreme_signals.adapters.regional_bcsd")
        assert not hasattr(mod, "deaccumulate_era5land")
