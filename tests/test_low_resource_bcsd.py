from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import cftime

import registry
from scripts import station_signals_direct
from tools import common


def test_bcsd_window_steps_and_regular_axis() -> None:
    times = pd.date_range("2015-01-01", periods=16, freq="3h")
    assert common.window_steps_for_hours(3.0) == 8
    assert station_signals_direct._validate_low_resource_time_axis(times)[1] == 3.0
    noleap = np.array(
        [cftime.DatetimeNoLeap(2015, 1, 1, hour) for hour in (0, 3, 6)],
        dtype=object,
    )
    assert station_signals_direct._validate_low_resource_time_axis(noleap)[1] == 3.0


def test_low_resource_uses_bcsd_wind_ms_and_marks_next_step() -> None:
    times = pd.date_range("2015-01-01", "2024-12-31 21:00", freq="3h")
    resource = np.ones((len(times), 1), dtype=np.float32)
    resource[100, 0] = 0.0
    base = station_signals_direct._low_resource_baseline_mask(times)
    result = registry.low_resource_signal(
        "wind",
        {"wind_ms": resource},
        times,
        base_mask=base,
        window_steps=8,
    )
    assert result.shape == resource.shape
    assert result.dtype == bool
    assert not result[:4].any()


def test_solar_low_resource_uses_rsds_and_night_filter() -> None:
    times = pd.date_range("2015-01-01", "2024-12-31 21:00", freq="3h")
    resource = np.ones((len(times), 1), dtype=np.float32)
    base = station_signals_direct._low_resource_baseline_mask(times)
    result = registry.low_resource_signal(
        "solar",
        {"rsds": resource},
        times,
        lat=np.array([35.0]),
        lon=np.array([0.0]),
        base_mask=base,
        window_steps=8,
    )
    assert result.shape == resource.shape
    assert not result[(times.hour == 0), 0].any()


def test_low_resource_rejects_missing_baseline_and_irregular_axis() -> None:
    with pytest.raises(ValueError, match="覆盖完整基线期"):
        station_signals_direct._low_resource_baseline_mask(
            pd.date_range("2030-01-01", periods=8, freq="3h")
        )
    with pytest.raises(ValueError, match="步长不均匀"):
        station_signals_direct._validate_low_resource_time_axis(
            pd.DatetimeIndex(["2015-01-01", "2015-01-01 03:00", "2015-01-01 07:00"])
        )
