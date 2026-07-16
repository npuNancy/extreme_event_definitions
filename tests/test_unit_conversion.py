"""Tests for unit_conversion module."""
from __future__ import annotations

import numpy as np
import pytest

from grid_extreme_signals.unit_conversion import (
    magnus_rh,
    pr_to_mmh,
    rsds_to_wm2,
    tas_to_celsius,
    wind_to_ms,
)


class TestTasToCelsius:
    def test_kelvin(self):
        arr = np.array([273.15, 373.15], dtype=np.float32)
        result = tas_to_celsius(arr, "K")
        np.testing.assert_allclose(result, [0.0, 100.0], atol=0.01)

    def test_kelvin_fullname(self):
        arr = np.array([273.15], dtype=np.float32)
        result = tas_to_celsius(arr, "kelvin")
        np.testing.assert_allclose(result, [0.0], atol=0.01)

    def test_celsius_identity(self):
        arr = np.array([0.0, 100.0], dtype=np.float32)
        result = tas_to_celsius(arr, "degC")
        np.testing.assert_allclose(result, [0.0, 100.0])

    def test_missing_units_raises(self):
        with pytest.raises(ValueError, match="无法识别温度单位"):
            tas_to_celsius(np.array([300.0]), None)

    def test_missing_units_inference(self):
        # Median > 100 → treated as Kelvin
        arr = np.array([250.0, 300.0, 350.0], dtype=np.float32)
        result = tas_to_celsius(arr, None, allow_inference=True)
        np.testing.assert_allclose(result, arr - 273.15, atol=0.01)

    def test_missing_units_inference_celsius(self):
        # Median < 100 → treated as Celsius
        arr = np.array([10.0, 20.0, 30.0], dtype=np.float32)
        result = tas_to_celsius(arr, None, allow_inference=True)
        np.testing.assert_allclose(result, arr)


class TestWindToMs:
    def test_ms_identity(self):
        arr = np.array([10.0, 20.0], dtype=np.float32)
        result = wind_to_ms(arr, "m s-1")
        np.testing.assert_allclose(result, [10.0, 20.0])

    def test_kmh_to_ms(self):
        arr = np.array([36.0], dtype=np.float32)
        result = wind_to_ms(arr, "km/h")
        np.testing.assert_allclose(result, [10.0], atol=0.01)

    def test_missing_units_raises(self):
        with pytest.raises(ValueError, match="无法识别风速单位"):
            wind_to_ms(np.array([10.0]), None)

    def test_missing_units_inference(self):
        arr = np.array([10.0], dtype=np.float32)
        result = wind_to_ms(arr, None, allow_inference=True)
        np.testing.assert_allclose(result, [10.0])


class TestPrToMmh:
    def test_kg_m2_s1(self):
        # 1 kg m-2 s-1 = 3600 mm/h
        arr = np.array([1e-5], dtype=np.float32)
        result = pr_to_mmh(arr, "kg m-2 s-1")
        np.testing.assert_allclose(result, [0.036], atol=1e-4)

    def test_mmh_identity(self):
        arr = np.array([2.0], dtype=np.float32)
        result = pr_to_mmh(arr, "mm/h")
        np.testing.assert_allclose(result, [2.0])

    def test_mm_day(self):
        arr = np.array([24.0], dtype=np.float32)
        result = pr_to_mmh(arr, "mm/day")
        np.testing.assert_allclose(result, [1.0])

    def test_mm_per_timestep_requires_hours(self):
        with pytest.raises(ValueError, match="timestep_hours"):
            pr_to_mmh(np.array([3.0]), "mm")

    def test_mm_per_timestep(self):
        # 3 mm per 3-hour step → 1 mm/h
        arr = np.array([3.0], dtype=np.float32)
        result = pr_to_mmh(arr, "mm", timestep_hours=3.0)
        np.testing.assert_allclose(result, [1.0])

    def test_missing_units_raises(self):
        with pytest.raises(ValueError):
            pr_to_mmh(np.array([1.0]), None)


class TestRsdsToWm2:
    def test_wm2_identity(self):
        arr = np.array([500.0], dtype=np.float32)
        result = rsds_to_wm2(arr, "W m-2")
        np.testing.assert_allclose(result, [500.0])

    def test_kwm2(self):
        arr = np.array([0.5], dtype=np.float32)
        result = rsds_to_wm2(arr, "kW m-2")
        np.testing.assert_allclose(result, [500.0])

    def test_jm2_requires_timestep(self):
        with pytest.raises(ValueError, match="timestep_seconds"):
            rsds_to_wm2(np.array([3600.0]), "J m-2")

    def test_jm2_per_timestep(self):
        # 3600 J/m² over 1 hour = 1 W/m²
        arr = np.array([3600.0], dtype=np.float32)
        result = rsds_to_wm2(arr, "J m-2", timestep_seconds=3600)
        np.testing.assert_allclose(result, [1.0])


class TestMagnusRh:
    def test_known_values(self):
        # At 20°C (293.15 K) and 10°C dew point (283.15 K), RH ≈ 52%
        rh = magnus_rh(
            np.array([293.15], dtype=np.float32),
            np.array([283.15], dtype=np.float32),
        )
        assert 40.0 < rh[0] < 65.0  # Approximate range

    def test_saturated(self):
        # t2m == d2m → RH = 100%
        rh = magnus_rh(
            np.array([293.15], dtype=np.float32),
            np.array([293.15], dtype=np.float32),
        )
        np.testing.assert_allclose(rh, [100.0], atol=0.1)

    def test_clipped_0_100(self):
        # RH should be clipped to [0, 100]
        rh = magnus_rh(
            np.array([200.0], dtype=np.float32),
            np.array([300.0], dtype=np.float32),
        )
        assert rh[0] >= 0.0
        assert rh[0] <= 100.0
