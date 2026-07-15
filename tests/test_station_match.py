"""Unit tests for grid_extreme_signals.station_match.

Covers the highest-risk logic called out in the implementation plan:
longitude convention detection/normalisation (region-dependent for BCSD),
circular longitude distance at the ±180° seam, regular + 2D nearest-cell,
country polygon filtering with activation-year deduplication.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import h5py
import numpy as np
import pandas as pd
import pytest

_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from grid_extreme_signals import station_match as sm


# ---------------------------------------------------------------------
# Longitude convention
# ---------------------------------------------------------------------

class TestLongitudeConvention:
    def test_to_180_wraps(self):
        assert sm.lon_to_180(359.0) == pytest.approx(-1.0)
        assert sm.lon_to_180(181.0) == pytest.approx(-179.0)
        assert sm.lon_to_180(0.0) == pytest.approx(0.0)

    def test_to_360_wraps(self):
        assert sm.lon_to_360(-1.0) == pytest.approx(359.0)
        assert sm.lon_to_360(-180.0) == pytest.approx(180.0)

    def test_is_lon_360_detection(self):
        # Germany-like (positive, small) is ambiguous but treated as [-180,180)
        assert sm.is_lon_360(np.array([5.0, 10.0, 15.0])) is False
        # Portugal-like stored as [0,360)
        assert sm.is_lon_360(np.array([328.7, 340.0, 353.7])) is True
        # a region crossing into >180
        assert sm.is_lon_360(np.array([-10.0, 200.0])) is True

    def test_normalize_preserves_already_180(self):
        grid = np.array([-10.0, 0.0, 10.0])
        np.testing.assert_array_equal(sm.normalize_grid_lon(grid), grid)

    def test_normalize_converts_360_to_180(self):
        # Portugal: 328.7 -> -31.3, 353.7 -> -6.3
        out = sm.normalize_grid_lon(np.array([328.7, 353.7]))
        np.testing.assert_allclose(out, [-31.3, -6.3], atol=1e-6)


# ---------------------------------------------------------------------
# Nearest-cell, regular grid (circular longitude)
# ---------------------------------------------------------------------

class TestNearestRegular:
    def test_exact_match(self):
        grid_lat = np.array([0.0, 10.0, 20.0])
        grid_lon = np.array([0.0, 0.1, 0.2])
        sta = pd.DataFrame({"lat": [10.0], "lon": [0.1]})
        lat_idx, lon_idx, dist = sm.nearest_index_regular(
            grid_lat, grid_lon, sta.lat.to_numpy(), sta.lon.to_numpy())
        assert lat_idx[0] == 1 and lon_idx[0] == 1
        assert dist[0] == pytest.approx(0.0)

    def test_circular_longitude_at_seam(self):
        """A station at 179.95 must match grid 179.9, not wrap to -179.9."""
        grid_lat = np.array([0.0])
        grid_lon = np.array([-179.9, -0.1, 0.1, 179.9])
        sta = pd.DataFrame({"lat": [0.0], "lon": [179.95]})
        _, lon_idx, dist = sm.nearest_index_regular(
            grid_lat, grid_lon, sta.lat.to_numpy(), sta.lon.to_numpy())
        assert lon_idx[0] == 3  # 179.9
        assert dist[0] == pytest.approx(0.05, abs=1e-9)

    def test_dist_is_max_of_dlat_dlon(self):
        grid_lat = np.array([0.0, 5.0])
        grid_lon = np.array([0.0, 5.0])
        sta = pd.DataFrame({"lat": [4.0], "lon": [2.0]})  # dlat=1, dlon=2 -> max=2
        _, _, dist = sm.nearest_index_regular(
            grid_lat, grid_lon, sta.lat.to_numpy(), sta.lon.to_numpy())
        assert dist[0] == pytest.approx(2.0)


# ---------------------------------------------------------------------
# Nearest-cell, 2D rotated grid
# ---------------------------------------------------------------------

class TestNearest2D:
    def test_kdtree_nearest(self):
        lat2d = np.array([[0.0, 0.0, 0.0], [10.0, 10.0, 10.0]])
        lon2d = np.array([[0.0, 0.1, 0.2], [0.0, 0.1, 0.2]])
        sta_lat = np.array([10.0])
        sta_lon = np.array([0.15])  # nearest is (rlat=1, rlon=1) at lon 0.1
        idx0, idx1, dist = sm.nearest_index_2d(lat2d, lon2d, sta_lat, sta_lon)
        assert idx0[0] == 1 and idx1[0] == 1
        assert dist[0] == pytest.approx(0.05, abs=1e-6)


# ---------------------------------------------------------------------
# match_regular + gather
# ---------------------------------------------------------------------

class TestMatchAndGather:
    def test_match_regular_valid_flag(self):
        grid_lat = np.array([0.0, 10.0])
        grid_lon = np.array([0.0, 0.1])
        sta = pd.DataFrame({"lat": [10.0, 10.0], "lon": [0.0, 50.0]})  # 2nd is far
        m = sm.match_regular(grid_lat, grid_lon, sta, max_dist=0.15)
        assert m.valid[0] is np.True_ or m.valid[0]
        assert not m.valid[1]
        assert m.grid_kind == "regular_latlon"

    def test_gather_shape_and_values(self):
        grid_lat = np.array([0.0, 10.0])
        grid_lon = np.array([0.0, 0.1])
        sta = pd.DataFrame({"lat": [10.0], "lon": [0.1]})
        m = sm.match_regular(grid_lat, grid_lon, sta)
        arr = np.arange(3 * 2 * 2).reshape(3, 2, 2).astype(float)
        out = sm.gather_to_stations(arr, m)
        assert out.shape == (3, 1)
        # station (lat_idx=1, lon_idx=1) -> arr[:,1,1]
        np.testing.assert_array_equal(out[:, 0], arr[:, 1, 1])

    def test_match_regular_weighted_nearest_matches_existing(self):
        grid_lat = np.array([0.0, 10.0])
        grid_lon = np.array([0.0, 0.1])
        sta = pd.DataFrame({"lat": [10.0], "lon": [0.1]})

        nearest = sm.match_regular(grid_lat, grid_lon, sta)
        weighted = sm.match_regular_weighted(grid_lat, grid_lon, sta, method="nearest")

        assert weighted.method == "nearest"
        assert weighted.idx0.shape == (1, 1)
        assert weighted.idx0[0, 0] == nearest.idx0[0]
        assert weighted.idx1[0, 0] == nearest.idx1[0]
        np.testing.assert_allclose(weighted.weight, np.array([[1.0]], dtype=np.float32))

    def test_match_regular_weighted_bilinear_values(self):
        grid_lat = np.array([1.0, 0.0])
        grid_lon = np.array([10.0, 11.0])
        sta = pd.DataFrame({"lat": [0.25], "lon": [10.25]})

        match = sm.match_regular_weighted(grid_lat, grid_lon, sta, method="bilinear", max_dist=1.0)
        arr = np.array([[[10.0, 20.0], [30.0, 40.0]]], dtype=np.float32)
        out = sm.gather_to_stations_weighted(arr, match)

        assert match.idx0.tolist() == [[1, 1, 0, 0]]
        assert match.idx1.tolist() == [[0, 1, 0, 1]]
        np.testing.assert_allclose(match.weight.sum(axis=1), np.array([1.0]))
        np.testing.assert_allclose(
            match.weight[0],
            np.array([0.5625, 0.1875, 0.1875, 0.0625], dtype=np.float32),
        )
        assert out[0, 0] == pytest.approx(27.5)

    def test_weighted_gather_renormalizes_missing_values(self):
        grid_lat = np.array([1.0, 0.0])
        grid_lon = np.array([10.0, 11.0])
        sta = pd.DataFrame({"lat": [0.25], "lon": [10.25]})
        match = sm.match_regular_weighted(grid_lat, grid_lon, sta, method="bilinear", max_dist=1.0)
        arr = np.array([[[10.0, 20.0], [30.0, np.nan]]], dtype=np.float32)

        out = sm.gather_to_stations_weighted(arr, match)

        expected = (0.5625 * 30.0 + 0.1875 * 10.0 + 0.0625 * 20.0) / (0.5625 + 0.1875 + 0.0625)
        assert out[0, 0] == pytest.approx(expected)

    def test_match_2d_weighted_rejects_bilinear(self):
        lat2d = np.array([[0.0, 0.0], [1.0, 1.0]])
        lon2d = np.array([[0.0, 1.0], [0.0, 1.0]])
        sta = pd.DataFrame({"lat": [0.5], "lon": [0.5]})

        with pytest.raises(ValueError, match="只支持"):
            sm.match_2d_weighted(lat2d, lon2d, sta, method="bilinear")

    def test_write_station_signals_records_bilinear_weights(self):
        stations = pd.DataFrame({
            "lon": [10.25],
            "lat": [0.25],
            "activation_year": [2030],
            "capacity_gw": [1.0],
        })
        match = sm.StationSpatialWeights(
            stations=stations,
            idx0=np.array([[1, 1, 0, 0]], dtype=np.int64),
            idx1=np.array([[0, 1, 0, 1]], dtype=np.int64),
            weight=np.array([[0.5625, 0.1875, 0.1875, 0.0625]], dtype=np.float32),
            dist_deg=np.array([0.25], dtype=np.float64),
            valid=np.array([True]),
            grid_kind="regular_latlon",
            method="bilinear",
        )
        with tempfile.TemporaryDirectory() as d:
            out_path = Path(d) / "station_signals.nc"
            sm.write_station_signals(
                out_path,
                {"signal_high_wind": np.array([[1]], dtype=np.int8)},
                np.array([np.datetime64("2030-01-01T00:00:00")]),
                match,
                "wind",
                source="regional_bcsd",
                model="TEST",
                region="Nowhere",
                scenario="ssp126",
                source_csv="stations.csv",
                pipeline="B",
                supported=["high_wind"],
                skipped=[],
                skipped_reasons={},
                max_dist=1.0,
                activation_mask_on=True,
                compress_level=1,
            )
            with h5py.File(out_path, "r") as f:
                method = f.attrs["match_method"]
                if isinstance(method, bytes):
                    method = method.decode("utf-8")
                points = f.attrs["match_weight_points"]
                if isinstance(points, bytes):
                    points = points.decode("utf-8")
                assert method == "bilinear"
                assert str(points) == "4"
                assert f["match_idx0"].shape == (1, 4)
                assert f["match_idx1"].shape == (1, 4)
                assert f["match_weight"].shape == (1, 4)
                np.testing.assert_allclose(f["match_weight"][:].sum(axis=1), [1.0])


# ---------------------------------------------------------------------
# Country filtering + activation-year dedup
# ---------------------------------------------------------------------

class TestStationFiltering:
    def _stations(self):
        return pd.DataFrame({
            "lon": [10.0, 10.0, 10.0, 50.0],
            "lat": [50.0, 50.0, 50.0, 50.0],
            "type": ["wind", "wind", "solar", "wind"],
            "year": [2050, 2030, 2040, 2050],
            "capacity_gw": [1.0, 1.0, 2.0, 3.0],
        })

    def test_dedup_keeps_min_year_as_activation(self):
        from shapely.geometry import box
        geom = box(0, 0, 60, 60)  # contains all points
        out = sm.filter_stations_for_country(self._stations(), geom, "wind")
        # wind: (10,50) in {2050,2030} -> dedup activation=2030; (50,50) -> 2050
        assert len(out) == 2
        act = {(round(float(r.lon), 1), int(r.activation_year))
               for r in out.itertuples()}
        assert (10.0, 2030) in act
        assert (50.0, 2050) in act

    def test_polygon_excludes_outside(self):
        from shapely.geometry import box
        geom = box(0, 0, 20, 60)  # excludes lon=50
        out = sm.filter_stations_for_country(self._stations(), geom, "wind")
        lons = out["lon"].tolist()
        assert 50.0 not in lons
        assert 10.0 in lons

    def test_activation_time_mask(self):
        times = np.array([np.datetime64(f"{y}-01-01") for y in (2025, 2031, 2041)])
        ay = np.array([2030, 2040])
        years = pd.DatetimeIndex(times).year.to_numpy(np.int64)[:, None]
        m = years >= ay[None, :]
        # 2025 < both -> [F,F]; 2031 in [2030,2040) -> [T,F]; 2041 >= both -> [T,T]
        assert m.tolist() == [[False, False], [True, False], [True, True]]
