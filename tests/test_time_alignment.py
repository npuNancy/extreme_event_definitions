"""Tests for time_alignment module."""
from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from grid_extreme_signals.time_alignment import (
    build_time_index,
    datetime64_to_ns,
    filter_year,
    find_coord_name,
    interp_instantaneous_to_target,
    parse_months,
    parse_years,
)


class TestFindCoordName:
    def test_present(self):
        ds = xr.Dataset(coords={"time": [1, 2], "lat": [3, 4], "lon": [5, 6]})
        assert find_coord_name(ds, ["time", "valid_time"]) == "time"

    def test_fallback(self):
        ds = xr.Dataset(coords={"valid_time": [1, 2], "latitude": [3, 4]})
        assert find_coord_name(ds, ["time", "valid_time"]) == "valid_time"
        assert find_coord_name(ds, ["lat", "latitude"]) == "latitude"

    def test_missing_raises(self):
        ds = xr.Dataset(coords={"x": [1, 2]})
        with pytest.raises(KeyError, match="未在候选项"):
            find_coord_name(ds, ["time", "valid_time"])


class TestDatetime64ToNs:
    def test_datetime64(self):
        times = np.array(["2020-01-01", "2020-01-02"], dtype="datetime64[ns]")
        result = datetime64_to_ns(times)
        assert result.dtype == np.int64
        assert result[1] > result[0]

    def test_numeric_passthrough(self):
        arr = np.array([1.0, 2.0, 3.0])
        result = datetime64_to_ns(arr)
        np.testing.assert_array_equal(result, arr)


class TestInterpInstantaneousToTarget:
    def test_exact_match(self):
        """When source and target times align, output equals input."""
        times = np.arange(
            np.datetime64("2020-01-01T00:00"),
            np.datetime64("2020-01-01T13:00"),
            np.timedelta64(3, "h"),
        )  # 5 elements: 00, 03, 06, 09, 12
        da = xr.DataArray(
            np.arange(len(times), dtype=np.float32).reshape(len(times), 1, 1),
            dims=("time", "lat", "lon"),
            coords={"time": times, "lat": [0], "lon": [0]},
        )
        result = interp_instantaneous_to_target(da, "time", times)
        np.testing.assert_allclose(result.squeeze(), np.arange(len(times)), atol=0.01)

    def test_boundary_nearest(self):
        """Out-of-range targets use nearest source value."""
        times = np.arange(
            np.datetime64("2020-01-01T03:00"),
            np.datetime64("2020-01-01T15:00"),
            np.timedelta64(3, "h"),
        )  # 4 elements: 03, 06, 09, 12
        da = xr.DataArray(
            np.arange(len(times), dtype=np.float32).reshape(len(times), 1, 1),
            dims=("time", "lat", "lon"),
            coords={"time": times, "lat": [0], "lon": [0]},
        )
        # Target before source range
        early = np.array([np.datetime64("2020-01-01T00:00")])
        result = interp_instantaneous_to_target(da, "time", early)
        np.testing.assert_allclose(result.squeeze(), [0.0])  # First source value

    def test_midpoint_interpolation(self):
        """Interpolates at midpoints between source times."""
        src_times = np.array([
            np.datetime64("2020-01-01T00:00"),
            np.datetime64("2020-01-01T06:00"),
        ])
        da = xr.DataArray(
            np.array([0.0, 6.0], dtype=np.float32).reshape(2, 1, 1),
            dims=("time", "lat", "lon"),
            coords={"time": src_times, "lat": [0], "lon": [0]},
        )
        mid = np.array([np.datetime64("2020-01-01T03:00")])
        result = interp_instantaneous_to_target(da, "time", mid)
        np.testing.assert_allclose(result.squeeze(), [3.0], atol=0.1)


class TestParseYears:
    def test_single_year(self):
        assert parse_years("2024") == (2024, 2024)

    def test_range(self):
        assert parse_years("2015-2060") == (2015, 2060)


class TestParseMonths:
    def test_empty(self):
        assert parse_months("") == list(range(1, 13))
        assert parse_months(None) == list(range(1, 13))

    def test_list(self):
        assert parse_months("1,2,3") == [1, 2, 3]


class TestBuildTimeIndex:
    def test_single_year(self):
        times = np.arange(
            np.datetime64("2015-01-01"),
            np.datetime64("2016-01-01"),
            np.timedelta64(3, "h"),
        ).astype("datetime64[ns]")
        da = xr.DataArray(times, dims=("time",), coords={"time": times})
        idx, doy, hour = build_time_index(da, "2015")
        assert idx.size > 0
        assert idx[-1] < len(times)

    def test_no_match_raises(self):
        times = np.arange(
            np.datetime64("2015-01-01"),
            np.datetime64("2015-02-01"),
            np.timedelta64(3, "h"),
        ).astype("datetime64[ns]")
        da = xr.DataArray(times, dims=("time",), coords={"time": times})
        with pytest.raises(ValueError, match="没有时间步"):
            build_time_index(da, "2099")


class TestFilterYear:
    def test_filters_correctly(self):
        times = np.arange(
            np.datetime64("2015-01-01"),
            np.datetime64("2017-01-01"),
            np.timedelta64(1, "D"),
        ).astype("datetime64[ns]")
        da = xr.DataArray(np.ones(len(times)), dims=("time",), coords={"time": times})
        idx = filter_year(da["time"], 2016)
        assert idx.size > 0
        # All selected dates should be in 2016
        selected = da["time"].isel(time=idx)
        assert (selected.dt.year == 2016).all()
