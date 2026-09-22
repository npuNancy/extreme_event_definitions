"""Native-grid correctness, calendar/halo, identity and restart regressions."""
import copy
import json
from pathlib import Path
import subprocess
import sys

import netCDF4
import numpy as np
import pandas as pd
import pytest
import xarray as xr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from grid_extreme_signals import grid_compute as compute
from grid_extreme_signals import grid_io as io
from grid_extreme_signals import unit_conversion as units
from tools import common
import registry


def build_grid(root, calendar="365_day", descending=False, sparse=True, ny=3, nx=4):
    """Two complete years; anomalies and missingness straddle the baseline edge."""
    root.mkdir(parents=True, exist_ok=True)
    lat = np.linspace(60., 90., ny, dtype=np.float64)
    if descending:
        lat = lat[::-1].copy()
    lon = np.arange(nx, dtype=np.float64) / 10
    nt = (366 + 365 if calendar in ("standard", "proleptic_gregorian") else 730) * 8
    hours = np.arange(nt, dtype=np.float64) * 3
    rng = np.random.default_rng(47)
    domain = np.ones((ny, nx), np.int8)
    if sparse:
        domain[:, :nx // 2] = 0
    shape = (nt, ny, nx)
    t = np.arange(nt)[:, None, None]
    arrays = {
        "tas": (292 + 24 * np.sin(t / 10) + rng.normal(size=shape)).astype("f4"),
        "uas": (10 + 10 * np.sin(t / 23) + rng.normal(size=shape)).astype("f4"),
        "vas": (2 + np.cos(t / 37) + rng.normal(size=shape)).astype("f4"),
        "hurs": (85 + 14 * np.sin(t / 31) + np.zeros(shape)).astype("f4"),
        "pr": (0.001 * (1 + np.sin(t / 19)) + np.zeros(shape)).astype("f4"),
        "rsds": (300 + 250 * np.sin(t / 17) + rng.normal(size=shape)).astype("f4"),
    }
    # Resource gaps and an unrelated ordinary-event gap.
    arrays["rsds"][42:45, 0, 3] = np.nan
    arrays["uas"][81, 1, 2] = np.nan
    arrays["pr"][100, 1, 2] = np.nan
    arrays["tas"][201, 1, 2] = np.inf
    for name, values in arrays.items():
        values[:, domain == 0] = np.nan
        path = root / "bcsd/outputs/M/ssp126" / name / "P1.nc"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Variables have both leading and lagging time shifts; reference clocks differ.
        shift = {"uas": 0., "vas": -1.5, "tas": 1.5, "hurs": 1.5, "pr": 1.5, "rsds": 1.5}[name]
        ds = xr.Dataset({name: (("time", "lat", "lon"), values, {"units": io.FALLBACK_UNITS[name]})},
                        coords={"time": ("time", hours + shift, {"units": "hours since 2024-01-01",
                                                               "calendar": calendar}),
                                "lat": lat, "lon": lon})
        ds.to_netcdf(path, encoding={name: {"zlib": True, "complevel": 1, "chunksizes": (80, 2, 2)}})
        io.sidecar(path).write_text(json.dumps({"patch_id": "P1", "variable": name,
                                               "model": "M", "scenario": "ssp126"}))
    xr.Dataset({"land_mask": (("lat", "lon"), domain)}, coords={"lat": lat, "lon": lon}).to_netcdf(root / "land.nc")
    (root / "patch.json").write_text(json.dumps({"patches": {"P1": {"core_bbox_360": [0, nx / 10, 60, 90]}}}))
    return lat, lon, domain


def args(root, stage, tech="solar", output="out", **updates):
    argv = ["--bcsd-root", str(root / "bcsd"), "--model", "M", "--scenario", "ssp126", "--patch", "P1",
            "--patch-manifest", str(root / "patch.json"), "--land-plan", str(root / "land.nc"),
            "--tech", tech, "--analysis-years", "2024-2025", "--baseline-years", "2024-2024",
            "--output-root", str(root / output), "--tile-shape", "2", "2", "--time-chunk", "239", "--timing-report"]
    if stage == "signals":
        argv += ["--baseline-file", str(root / "out/M/ssp126/P1" / tech / "baseline_2024-2024.nc")]
    result = compute.parser(stage).parse_args(argv)
    for key, value in updates.items():
        setattr(result, key, value)
    return result


def reference(root, tech):
    """Full-materialization xarray interpolation + original scientific definitions."""
    _, variables, _ = compute.supported(tech)
    opened = {v: xr.open_dataset(root / "bcsd/outputs/M/ssp126" / v / "P1.nc") for v in variables}
    try:
        ref = opened["uas" if tech == "wind" else "rsds"]
        times = ref.time.values
        weather = {}
        for v, ds in opened.items():
            da = ds[v]
            if not np.array_equal(da.time.values, times):
                da = da.interp(time=times)
            a = np.asarray(da.values, np.float32).reshape(len(times), -1)
            if v == "tas": weather["temp_C"] = units.tas_to_celsius(a, "K")
            elif v in ("uas", "vas"): weather[v] = a
            elif v == "hurs": weather["rh_pct"] = units.hurs_to_pct(a, "%")
            elif v == "pr": weather["precip_mmh"] = units.pr_to_mmh(a, "kg m-2 s-1")
            elif v == "rsds": weather["rsds"] = a
        weather["wind_ms"] = np.hypot(weather.pop("uas"), weather.pop("vas")).astype("f4")
        from scripts.station_signals_patchify import _pseudo
        idx = _pseudo(times)
        years = ref.time.dt.year.values
        base = years == 2024
        resource = weather[registry.LOWRES_RESOURCE[tech]]
        roll = common.roll_centered(resource, 8)
        with pytest.warns(RuntimeWarning):
            clim = common.clim288(roll, idx, base)
            anomaly = roll - clim[idx.month.to_numpy() - 1, idx.hour.to_numpy()]
            threshold = np.nanpercentile(np.where(np.isfinite(anomaly[base]), anomaly[base], np.nan), 5, axis=0)
        masks = registry.simple_signals(tech, weather)
        valid = {name: np.logical_and.reduce([np.isfinite(weather[v]) for v in registry.REQUIRED[tech][name]])
                 for name in masks}
        kw = dict(base_mask=base, clim_tbl=clim, thr=threshold, window_steps=8)
        if tech == "solar":
            lat, lon = np.meshgrid(ref.lat.values, ref.lon.values, indexing="ij")
            kw.update(lat=lat.ravel(), lon=lon.ravel())
        masks["low_resource"] = registry.low_resource_signal(tech, weather, idx, **kw)
        valid["low_resource"] = np.isfinite(anomaly) & np.isfinite(threshold)[None, :]
        with xr.open_dataset(root / "land.nc") as plan:
            domain = plan.land_mask.values.reshape(-1).astype(bool)
        signals = {"signal_" + name: np.where(valid[name] & domain[None, :], mask, io.FILL)
                   .astype("i1").reshape(len(times), len(ref.lat), len(ref.lon)) for name, mask in masks.items()}
        return signals, clim, threshold, times
    finally:
        for ds in opened.values(): ds.close()


@pytest.fixture(scope="module")
def grid(tmp_path_factory):
    root = tmp_path_factory.mktemp("native_grid")
    build_grid(root)
    return root


@pytest.mark.parametrize("tech", ["wind", "solar"])
def test_grid_matches_full_reference_and_original_station(grid, tech):
    baseline_path = compute.run(args(grid, "baseline", tech), "baseline")[0]
    output = compute.run(args(grid, "signals", tech), "signals")[0]
    expected, clim, threshold, times = reference(grid, tech)
    with netCDF4.Dataset(baseline_path) as base:
        base.set_auto_mask(False)
        np.testing.assert_array_equal(base["clim288"][:].reshape(12, 24, -1), clim)
        np.testing.assert_array_equal(base["p5"][:].reshape(-1), threshold)
        assert base["p5"].dtype == np.dtype("float64")
    with netCDF4.Dataset(output) as ds, netCDF4.Dataset(grid / "bcsd/outputs/M/ssp126" / ("uas" if tech == "wind" else "rsds") / "P1.nc") as src:
        ds.set_auto_mask(False)
        assert ds["signal_low_resource"].dimensions == ("time", "lat", "lon")
        assert "station" not in ds.dimensions
        for coord in ("time", "lat", "lon"):
            np.testing.assert_array_equal(ds[coord][:], src[coord][:])
            assert ds[coord].dtype == src[coord].dtype
        assert ds["time"].calendar == src["time"].calendar
        for name, values in expected.items():
            np.testing.assert_array_equal(ds[name][:], values, err_msg=name)
            assert ds[name]._FillValue == -127
        assert "signal_dust" not in ds.variables
        if tech == "solar": assert ds.skipped_events == "dust"
    with xr.open_dataset(output) as ds:
        assert np.isnan(ds.signal_low_resource.values[:, :, :2]).all()
        assert (ds.signal_low_resource.values == 0).any()
    # Original station pipeline at exact grid centres; only its invalid zeros differ.
    from scripts import station_signals_patchify as station
    with xr.open_dataset(grid / "land.nc") as plan:
        lat, lon = np.meshgrid(plan.lat.values, plan.lon.values, indexing="ij")
    csv = grid / f"stations_{tech}.csv"
    pd.DataFrame({"lon": lon.ravel(), "lat": lat.ravel(), "year": 2015, "type": tech,
                  "capacity_gw": 1.0}).to_csv(csv, index=False)
    sta = station.parser().parse_args(["--bcsd-root", str(grid / "bcsd"), "--model", "M", "--scenario", "ssp126",
        "--patch", "P1", "--patch-manifest", str(grid / "patch.json"), "--stations-csv", str(csv),
        "--tech", tech, "--output-root", str(grid / "stations"), "--overwrite"])
    station.run(sta)
    with xr.open_dataset(grid / "stations/M/ssp126/P1" / f"{tech}.nc") as old:
        for i, (y, x) in enumerate(zip(old.lat.values, old.lon.values)):
            iy = np.argmin(abs(lat[:, 0] - y)); ix = np.argmin(abs(lon[0] - x))
            for name, values in expected.items():
                valid = values[:, iy, ix] != io.FILL
                np.testing.assert_array_equal(old[name].values[valid, i], values[valid, iy, ix], err_msg=name)


@pytest.mark.parametrize("tech,processes", [("wind", 2), ("solar", 4)])
def test_multiprocess_year_segments_and_different_tiles(grid, tech, processes):
    compute.run(args(grid, "baseline", tech), "baseline")
    serial = compute.run(args(grid, "signals", tech), "signals")[0]
    # Recompute baseline with a different partition and processes as well.
    out_name = f"parallel_{tech}"
    baseline = compute.run(args(grid, "baseline", tech, output=out_name,
                                tile_shape=(1, 3), time_chunk=481, processes=processes), "baseline")[0]
    outputs = compute.run(args(grid, "signals", tech, output=out_name, baseline_file=str(baseline),
                               years_per_file=1, tile_shape=(1, 3), time_chunk=83, processes=processes), "signals")
    with netCDF4.Dataset(serial) as original:
        original.set_auto_mask(False)
        for name in original.variables:
            if not name.startswith("signal_"): continue
            arrays = []
            for path in outputs:
                with netCDF4.Dataset(path) as ds:
                    ds.set_auto_mask(False)
                    arrays.append(ds[name][:])
            np.testing.assert_array_equal(np.concatenate(arrays), original[name][:], err_msg=name)
    manifest = json.loads((outputs[0].parent / "manifest.json").read_text())
    assert len(manifest["completed"]) == 2
    assert manifest["expected_years"] == ["2024-2024", "2025-2025"]


def test_recovery_identity_and_empty_tiles(grid):
    compute.run(args(grid, "baseline"), "baseline")
    a = args(grid, "signals", output="recovery", years="2025-2025", processes=2)
    output = compute.run(a, "signals")[0]
    meta = io.complete(output)
    parts = [p for p in meta["timing"]["parts"] if p["status"] != "DOMAIN_EMPTY"]
    assert len(parts) == 2
    assert len(meta["tiles"]) == 4
    mtimes = {p["path"]: Path(p["path"]).stat().st_mtime_ns for p in parts}
    # Final without sidecar is incomplete; missing/corrupt parts alone are rebuilt.
    io.sidecar(output).unlink()
    damaged = Path(parts[0]["path"])
    damaged.write_bytes(b"broken netcdf")
    compute.run(a, "signals")
    assert damaged.stat().st_mtime_ns != mtimes[str(damaged)]
    assert Path(parts[1]["path"]).stat().st_mtime_ns == mtimes[parts[1]["path"]]
    timing = json.loads(Path(str(output) + ".timing.json").read_text())
    assert sorted(p["status"] for p in timing["parts"]) == ["COMPLETED", "DOMAIN_EMPTY", "DOMAIN_EMPTY", "REUSED"]
    changed = copy.deepcopy(a); changed.time_chunk = 57
    with pytest.raises(FileExistsError, match="different configuration"):
        compute.run(changed, "signals")
    changed.overwrite = True
    compute.run(changed, "signals")
    assert io.complete(output)["identity"] != meta["identity"]
    assert all(p["status"] != "REUSED" for p in io.complete(output)["timing"]["parts"])
    # A valid JSON document with the wrong schema must also be treated as incomplete.
    io.sidecar(output).write_text("[]")
    compute.run(changed, "signals")
    assert io.complete(output)


def test_baseline_mismatch_rejected(grid):
    compute.run(args(grid, "baseline"), "baseline")
    a = args(grid, "signals", output="mismatch", baseline_years="2024-2025")
    with pytest.raises(ValueError, match="baseline identity mismatch"):
        compute.run(a, "signals")


def test_gregorian_leap_descending_and_input_validation(tmp_path):
    build_grid(tmp_path, calendar="proleptic_gregorian", descending=True)
    baseline = compute.run(args(tmp_path, "baseline"), "baseline")[0]
    output = compute.run(args(tmp_path, "signals", baseline_file=str(baseline)), "signals")[0]
    expected, _, _, _ = reference(tmp_path, "solar")
    with netCDF4.Dataset(output) as ds:
        ds.set_auto_mask(False)
        np.testing.assert_array_equal(ds["signal_low_resource"][:], expected["signal_low_resource"])
        assert np.all(np.diff(ds["lat"][:]) < 0)
        assert len(ds["time"]) == 731 * 8
    path = tmp_path / "bcsd/outputs/M/ssp126/tas/P1.nc"
    with netCDF4.Dataset(path, "r+") as ds:
        ds["lon"][0] = 0.01
    with pytest.raises(ValueError, match="grid coordinate mismatch"):
        compute.run(args(tmp_path, "signals"), "signals")


def test_time_alignment_uses_actual_indexes_and_keeps_exact_nan_neighbours(grid):
    a = args(grid, "signals")
    compute.validate_args(a, "signals")
    with io.GridReader(a, compute.supported("solar")[1]) as reader:
        # hurs is exactly on rsds; tas/pr have same target phase; wind is shifted.
        for name in reader.datasets:
            got = reader.read(name, 70, 110, (0, 2, 2, 4))
            ds = reader.datasets[name]
            with xr.open_dataset(reader.files[name]) as src, xr.open_dataset(reader.files[reader.reference]) as ref:
                da = (src[name].isel(time=slice(70, 110)) if np.array_equal(src.time.values, ref.time.values)
                      else src[name].interp(time=ref.time.isel(time=slice(70, 110))))
                expected = da.isel(lat=slice(0, 2), lon=slice(2, 4)).values.astype("f4").reshape(40, 4)
                if name == "tas": expected = units.tas_to_celsius(expected, "K")
                if name == "pr": expected = units.pr_to_mmh(expected, "kg m-2 s-1")
                np.testing.assert_array_equal(got, expected, err_msg=name)


def test_low_resource_halo_and_night_order():
    # Previous raw event at night still marks a valid daylight next step.
    anomaly = np.array([[-2.], [2.], [np.nan], [-2.], [2.]], dtype="f4")
    threshold = np.array([-1.])
    night = np.array([[True], [False], [False], [False], [True]])
    actual = common.low_resource_from_anomaly(anomaly, threshold, night=night)
    np.testing.assert_array_equal(actual[:, 0], [False, True, False, True, False])
    resource_values = np.arange(41, dtype="f4")[:, None]
    full = common.roll_centered(resource_values, 8)
    for start, stop in ((0, 7), (7, 13), (13, 40), (40, 41)):
        lo, hi = max(0, start - 5), min(41, stop + 3)
        chunk = common.roll_centered(resource_values[lo:hi], 8)
        np.testing.assert_array_equal(chunk[start-lo:stop-lo], full[start:stop])


def test_cli_help_and_parameter_errors(grid):
    for script in ("prepare_grid_baseline.py", "grid_signals_patchify.py"):
        result = subprocess.run([str(ROOT / ".venv/bin/python"), str(ROOT / "scripts" / script), "--help"],
                                capture_output=True, text=True)
        assert result.returncode == 0
        assert "--land-plan" in result.stdout and "--stations-csv" not in result.stdout
    for override in ({"processes": 0}, {"tile_shape": (0, 2)}, {"time_chunk": 0},
                     {"analysis_years": "2025-2024"}, {"model": "../M"}):
        with pytest.raises(ValueError):
            compute.run(args(grid, "baseline", **override), "baseline")


def test_invalid_time_grid_and_input_identity(tmp_path):
    build_grid(tmp_path)
    a = args(tmp_path, "baseline", tech="wind")
    compute.validate_args(a, "baseline")
    path = tmp_path / "bcsd/outputs/M/ssp126/uas/P1.nc"
    with netCDF4.Dataset(path, "r+") as ds:
        ds["time"][25] += 1
    with pytest.raises(ValueError, match="spaced by 3 hours"):
        with io.GridReader(a, ("uas", "vas")): pass
    with netCDF4.Dataset(path, "r+") as ds:
        ds["time"][25] -= 1
        ds["time"].calendar = "360_day"
    with pytest.raises(ValueError, match="unsupported calendar"):
        with io.GridReader(a, ("uas", "vas")): pass
    with netCDF4.Dataset(path, "r+") as ds:
        ds["time"].calendar = "365_day"
    meta = json.loads(io.sidecar(path).read_text()); meta["model"] = "OTHER"
    io.sidecar(path).write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="input identity mismatch"):
        with io.GridReader(a, ("uas", "vas")): pass


def test_missing_baseline_samples_and_event_specific_validity(tmp_path):
    build_grid(tmp_path)
    root = tmp_path / "bcsd/outputs/M/ssp126"
    # Same timestamps for these variables make the event-specific example exact.
    with netCDF4.Dataset(root / "rsds/P1.nc", "r+") as ds:
        ds["rsds"][:365*8, 1, 2] = np.nan
    with netCDF4.Dataset(root / "hurs/P1.nc", "r+") as ds:
        ds["hurs"][300, 1, 2] = np.inf
    baseline = compute.run(args(tmp_path, "baseline"), "baseline")[0]
    output = compute.run(args(tmp_path, "signals"), "signals")[0]
    with netCDF4.Dataset(output) as ds, netCDF4.Dataset(baseline) as base:
        ds.set_auto_mask(False); base.set_auto_mask(False)
        assert np.isnan(base["p5"][1, 2])
        assert base["baseline_valid_count"][1, 2] == 0
        assert np.all(ds["signal_low_resource"][:, 1, 2] == io.FILL)
        assert ds["signal_rainstorm"][100, 1, 2] == io.FILL
        assert ds["signal_high_humidity"][100, 1, 2] != io.FILL
        assert ds["signal_high_humidity"][300, 1, 2] == io.FILL
        assert ds["signal_rainstorm"][300, 1, 2] != io.FILL


def test_all_domain_empty_and_incomplete_year(tmp_path):
    build_grid(tmp_path)
    with netCDF4.Dataset(tmp_path / "land.nc", "r+") as ds:
        ds["land_mask"][:] = 0
    baseline = compute.run(args(tmp_path, "baseline"), "baseline")[0]
    output = compute.run(args(tmp_path, "signals", processes=2), "signals")[0]
    assert all(p["status"] == "DOMAIN_EMPTY" for p in io.complete(output)["timing"]["parts"])
    with netCDF4.Dataset(output) as ds:
        ds.set_auto_mask(False)
        assert np.all(ds["signal_low_resource"][:] == io.FILL)
    with netCDF4.Dataset(baseline) as ds:
        ds.set_auto_mask(False)
        assert np.isnan(ds["p5"][:]).all()
    with pytest.raises(ValueError, match="incomplete year coverage"):
        io.select_years(netCDF4.num2date(np.arange(100)*3, "hours since 2024-02-01", "365_day"), "2024")


@pytest.mark.parametrize("calendar", ["365_day", "proleptic_gregorian"])
@pytest.mark.parametrize("phase", [0, 1.5, 3])
def test_native_year_start(calendar, phase):
    hours = np.arange(phase, 365 * 24, 3)
    dates = netCDF4.num2date(hours, "hours since 2015-01-01", calendar)
    assert io.select_years(dates, "2015") == (0, len(hours))


@pytest.mark.parametrize("phase", [3 + 1 / 3600, 4.5, 6])
def test_late_year_start_is_incomplete(phase):
    dates = netCDF4.num2date(np.arange(phase, 365 * 24, 3),
                            "hours since 2015-01-01", "365_day")
    with pytest.raises(ValueError, match="incomplete year coverage"):
        io.select_years(dates, "2015")


def test_input_changes_invalidate_baseline(tmp_path):
    build_grid(tmp_path)
    compute.run(args(tmp_path, "baseline"), "baseline")
    path = tmp_path / "bcsd/outputs/M/ssp126/rsds/P1.nc"
    with netCDF4.Dataset(path, "r+") as ds:
        ds["rsds"][900, 0, 2] = 0
    with pytest.raises(ValueError, match="baseline identity mismatch"):
        compute.run(args(tmp_path, "signals"), "signals")


def test_t_plus_one_propagation_across_chunks():
    rng = np.random.default_rng(102)
    resource_values = rng.normal(size=(90, 3)).astype("f4")
    resource_values[39, 1] = np.nan
    anomaly = common.roll_centered(resource_values, 8)
    threshold = np.array([-0.1, -0.2, 0.0])
    night = np.arange(90)[:, None] % 8 < 3
    full = common.low_resource_from_anomaly(anomaly, threshold, night=night)
    result = []
    for start in range(0, 90, 7):
        stop = min(start + 7, 90)
        lo, hi = max(0, start - 5), min(90, stop + 3)
        rolling = common.roll_centered(resource_values[lo:hi], 8)
        masks = common.low_resource_from_anomaly(rolling, threshold, night=night[lo:hi])
        result.append(masks[start-lo:stop-lo])
    np.testing.assert_array_equal(np.concatenate(result), full)


def test_overlapping_partitions_fail_before_writing(grid):
    baseline = compute.run(args(grid, "baseline"), "baseline")[0]
    first = compute.run(args(grid, "signals", output="overlap", years="2024", baseline_file=str(baseline)), "signals")[0]
    with pytest.raises(ValueError, match="overlapping signal files"):
        compute.run(args(grid, "signals", output="overlap", baseline_file=str(baseline)), "signals")
    assert not (first.parent / "signals_2024-2025.nc").exists()
