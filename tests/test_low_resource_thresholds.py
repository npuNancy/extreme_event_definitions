from __future__ import annotations

from pathlib import Path

import h5py
import numpy as np
import pandas as pd

from grid_extreme_signals import cf_low_resource
from scripts import precompute_low_resource_thresholds as precompute


def _write_cf(path: Path, tech: str, times: pd.DatetimeIndex, values: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    var = "solar_cf" if tech == "solar" else "wind_cf"
    with h5py.File(path, "w") as f:
        seconds = times.to_numpy(dtype="datetime64[s]").astype(np.int64)
        d_time = f.create_dataset("time", data=seconds)
        d_time.attrs["units"] = np.bytes_("seconds since 1970-01-01")
        f.create_dataset("lat", data=np.array([10.0], dtype=np.float32))
        f.create_dataset("lon", data=np.array([100.0], dtype=np.float32))
        f.create_dataset(var, data=values.astype(np.float32))


def _write_threshold(path: Path, tech: str, threshold_value: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as f:
        f.attrs["threshold_source"] = np.bytes_("ERA5Land")
        f.attrs["baseline_years_effective"] = np.bytes_("2015-2025")
        f.attrs["resource_variable"] = np.bytes_("wind_cf" if tech == "wind" else "solar_cf")
        f.create_dataset("lat", data=np.array([10.0], dtype=np.float32))
        f.create_dataset("lon", data=np.array([100.0], dtype=np.float32))
        f.create_dataset("clim", data=np.zeros((12, 24, 1, 1), dtype=np.float32))
        f.create_dataset("threshold", data=np.array([[threshold_value]], dtype=np.float32))


def test_find_monthly_files_reports_missing_months(tmp_path):
    cf_root = tmp_path / "data" / "cfs"
    root = cf_root / "CFs_of_wind_ERA5Land"
    root.mkdir(parents=True)
    for month in (1, 3):
        (root / f"wind_cf_2015_{month:02d}.nc").touch()

    files, missing = precompute.find_monthly_files(cf_root, "wind", "2015")

    assert [p.name for p in files] == ["wind_cf_2015_01.nc", "wind_cf_2015_03.nc"]
    assert (2015, 2) in missing
    assert len(missing) == 10


def test_compute_threshold_block_outputs_clim_and_percentile():
    times = pd.date_range("2015-01-01", periods=48, freq="h")
    cf = np.arange(48, dtype=np.float32)[:, None]

    clim, threshold, valid_count = precompute.compute_threshold_block(
        cf, times, pct=5.0, window_steps=24
    )

    roll = pd.DataFrame(cf).rolling(24, center=True, min_periods=24).mean().to_numpy()
    anom = roll.copy()
    months = times.month.to_numpy() - 1
    hours = times.hour.to_numpy()
    for i in range(len(times)):
        anom[i, 0] -= clim[months[i], hours[i], 0]

    assert clim.shape == (12, 24, 1)
    assert threshold.shape == (1,)
    assert valid_count[0] == np.isfinite(anom[:, 0]).sum()
    np.testing.assert_allclose(
        threshold[0],
        np.nanpercentile(np.where(np.isfinite(anom[:, 0]), anom[:, 0], np.nan), 5.0),
    )


def test_station_low_resource_uses_external_threshold(tmp_path):
    times = pd.date_range("2015-01-01", periods=16, freq="3h")
    cf_file = tmp_path / "target_cf.nc"
    threshold_file = tmp_path / "threshold.nc"
    _write_cf(cf_file, "wind", times, np.zeros((16, 1, 1), dtype=np.float32))
    _write_threshold(threshold_file, "wind", threshold_value=0.01)

    result = cf_low_resource.compute_station_low_resource(
        cf_file,
        "wind",
        times,
        np.array([10.0]),
        np.array([100.0]),
        threshold_file=threshold_file,
        max_dist=0.15,
    )

    assert result.threshold_file == threshold_file
    assert result.threshold_source == "ERA5Land"
    assert result.threshold_baseline_years == "2015-2025"
    assert result.mask.dtype == np.int8
    assert result.mask.shape == (16, 1)
    assert result.mask[:, 0].sum() > 0
    attrs = cf_low_resource.attrs(result)
    assert attrs["low_resource_threshold_file"] == str(threshold_file)
    assert attrs["low_resource_threshold_source"] == "ERA5Land"
    assert "low_resource_baseline_years" not in attrs
