from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np
import pandas as pd

from grid_extreme_signals import cf_low_resource
from scripts import precompute_station_low_resource_thresholds as sparse_precompute


def _write_cf(path: Path, tech: str, times: pd.DatetimeIndex, values: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    var = "solar_cf" if tech == "solar" else "wind_cf"
    with h5py.File(path, "w") as f:
        seconds = times.to_numpy(dtype="datetime64[s]").astype(np.int64)
        d_time = f.create_dataset("time", data=seconds)
        d_time.attrs["units"] = np.bytes_("seconds since 1970-01-01")
        f.create_dataset("lat", data=np.array([1.0, 0.0], dtype=np.float32))
        f.create_dataset("lon", data=np.array([10.0, 11.0], dtype=np.float32))
        f.create_dataset(var, data=values.astype(np.float32))


def _write_sparse_threshold(path: Path, tech: str, threshold_value: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as f:
        f.attrs["threshold_kind"] = np.bytes_("sparse_station")
        f.attrs["threshold_source"] = np.bytes_("ERA5Land")
        f.attrs["baseline_years_effective"] = np.bytes_("2015-2025")
        f.attrs["tech"] = np.bytes_(tech)
        f.create_dataset("station", data=np.array([0], dtype=np.int32))
        f.create_dataset("station_lat", data=np.array([0.25], dtype=np.float32))
        f.create_dataset("station_lon", data=np.array([10.25], dtype=np.float32))
        f.create_dataset(
            "station_type",
            data=np.array([1 if tech == "wind" else 0], dtype=np.int8),
        )
        f.create_dataset("clim", data=np.zeros((12, 24, 1), dtype=np.float32))
        f.create_dataset("threshold", data=np.array([threshold_value], dtype=np.float32))


def test_bilinear_four_point_regular_uses_surrounding_grid():
    match = cf_low_resource.bilinear_four_point_regular(
        np.array([1.0, 0.0]),
        np.array([10.0, 11.0]),
        np.array([0.25]),
        np.array([10.25]),
    )

    assert match.lat_idx.tolist() == [[1, 1, 0, 0]]
    assert match.lon_idx.tolist() == [[0, 1, 0, 1]]
    np.testing.assert_allclose(match.weights.sum(axis=1), np.array([1.0]))
    np.testing.assert_allclose(
        match.weights[0],
        np.array([0.5625, 0.1875, 0.1875, 0.0625], dtype=np.float32),
    )


def test_precompute_sparse_threshold_file_schema(tmp_path):
    cf_root = tmp_path / "data" / "cfs"
    times = pd.date_range("2015-01-01", periods=48, freq="h")
    values = np.ones((48, 2, 2), dtype=np.float32)
    _write_cf(cf_root / "CFs_of_wind_ERA5Land" / "wind_cf_2015_01.nc",
              "wind", times, values)
    stations_csv = tmp_path / "stations.csv"
    stations_csv.write_text(
        "year,type,lon,lat,capacity_gw\n"
        "2030,wind,10.25,0.25,1.5\n",
        encoding="utf-8",
    )
    args = SimpleNamespace(
        cf_root=str(cf_root),
        stations_csv=str(stations_csv),
        output_dir=str(tmp_path / "thresholds"),
        baseline_years="2015",
        station_chunk=2,
        compress_level=1,
        allow_incomplete=True,
        overwrite=False,
        dry_run=False,
    )

    out_path = sparse_precompute.process_tech(args, "ssp126", "wind")

    assert out_path is not None
    with h5py.File(out_path, "r") as f:
        assert f.attrs["threshold_kind"] == b"sparse_station"
        assert f.attrs["scenario"] == b"ssp126"
        assert f["clim"].shape == (12, 24, 1)
        assert f["threshold"].shape == (1,)
        assert f["era5_lat_idx"].shape == (1, 4)
        np.testing.assert_allclose(f["weight"][:].sum(axis=1), np.array([1.0]))


def test_station_low_resource_uses_sparse_threshold(tmp_path):
    times = pd.date_range("2015-01-01", periods=16, freq="3h")
    cf_file = tmp_path / "target_cf.nc"
    threshold_file = tmp_path / "threshold_sparse.nc"
    _write_cf(cf_file, "wind", times, np.zeros((16, 2, 2), dtype=np.float32))
    _write_sparse_threshold(threshold_file, "wind", threshold_value=0.01)

    result = cf_low_resource.compute_station_low_resource(
        cf_file,
        "wind",
        times,
        np.array([0.25]),
        np.array([10.25]),
        threshold_file=threshold_file,
        max_dist=1.0,
    )

    assert result.threshold_file == threshold_file
    assert result.threshold_source == "ERA5Land"
    assert result.threshold_baseline_years == "2015-2025"
    assert result.valid.tolist() == [True]
    assert result.mask.dtype == np.int8
    assert result.mask.shape == (16, 1)
    assert result.mask[:, 0].sum() > 0
