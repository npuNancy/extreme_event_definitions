"""Regression test: extreme station gather with cropping matches full-grid gather."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from grid_extreme_signals import station_match as sm  # noqa: E402


def synth_grid(nt=24, ny=12, nx=15, seed=11):
    rng = np.random.default_rng(seed)
    times = pd.date_range("2015-01-01", periods=nt, freq="D")
    lat = np.linspace(-50.0, 50.0, ny)
    lon = np.linspace(10.0, 40.0, nx)
    return xr.DataArray(rng.normal(size=(nt, ny, nx)).astype(np.float32),
                        coords={"time": times, "lat": lat, "lon": lon},
                        dims=("time", "lat", "lon"))


def stations_frame(pts):
    return pd.DataFrame({"lon": [p[0] for p in pts], "lat": [p[1] for p in pts],
                         "capacity_gw": [1.0] * len(pts), "year": [2030] * len(pts)})


def run_case(da, stations, method):
    full_match = sm.match_regular_weighted(da.lat.values, da.lon.values, stations, method=method)
    full = sm.gather_to_stations_weighted(np.asarray(da.transpose("time", "lat", "lon").values), full_match)

    used_lat = np.unique(full_match.idx0); used_lon = np.unique(full_match.idx1)
    lat_pos = np.full(full_match.idx0.max() + 1, -1, dtype=np.int64); lat_pos[used_lat] = np.arange(len(used_lat))
    lon_pos = np.full(full_match.idx1.max() + 1, -1, dtype=np.int64); lon_pos[used_lon] = np.arange(len(used_lon))
    cropped_match = sm.StationSpatialWeights(full_match.stations, lat_pos[full_match.idx0], lon_pos[full_match.idx1],
                                             full_match.weight, full_match.dist_deg, full_match.valid,
                                             grid_kind=full_match.grid_kind, method=full_match.method)
    small = da.isel(lat=used_lat, lon=used_lon)
    cropped = sm.gather_to_stations_weighted(np.asarray(small.transpose("time", "lat", "lon").values), cropped_match)
    assert np.array_equal(full, cropped), f"gather mismatch for method={method}"


def test_crop_matches_full():
    da = synth_grid()
    pts = [(12.3, -11.7), (33.9, 41.2), (25.0, 0.05), (10.01, 49.5), (40.0, 33.3), (19.95, 22.05)]
    stations = stations_frame(pts)
    run_case(da, stations, "nearest")
    run_case(da, stations, "bilinear")


def test_dense_band():
    da = synth_grid()
    pts = [(lon, 3.0) for lon in np.linspace(11.0, 39.0, 30)]
    run_case(da, stations_frame(pts), "nearest")
    run_case(da, stations_frame(pts), "bilinear")


if __name__ == "__main__":
    test_crop_matches_full()
    test_dense_band()
    print("all extreme gather-crop regression tests passed")
