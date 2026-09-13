"""End-to-end equivalence test for the station-blocked streaming signal writer.

Builds a synthetic BCSD patch + stations, runs scripts/station_signals_patchify.run()
(which streams in station blocks), then recomputes the reference masks with the
original full-materialization pipeline inline and compares every signal bit.
"""
import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tools"))

NT, NY, NX = 200, 12, 14


def build(root: Path):
    rng = np.random.default_rng(5)
    times = pd.date_range("2015-01-01", periods=NT, freq="3h")
    lat = np.linspace(30.0, 33.0, NY)
    lon = np.linspace(110.0, 114.0, NX)
    bcsd = root / "bcsd" / "outputs" / "M" / "ssp126"
    scale = {"tas": 290.0, "uas": 3.0, "vas": 2.0, "hurs": 60.0, "pr": 5e-5, "rsds": 300.0}
    for v, s0 in scale.items():
        d = bcsd / v; d.mkdir(parents=True)
        data = np.abs(rng.normal(size=(NT, NY, NX)) * 0.2 + s0).astype(np.float32)
        units = {"tas": "K", "pr": "kg m-2 s-1", "rsds": "W m-2", "hurs": "%"}.get(v, "m/s")
        # uas carries the reference axis; other stamps offset 90 minutes so
        # the block-padded interpolation path is exercised.
        vt = times if v == "uas" else times + pd.Timedelta(minutes=90)
        ds = xr.Dataset({f"{v}_bcsd": (("time", "lat", "lon"), data, {"units": units})},
                        coords={"time": vt, "lat": lat, "lon": lon})
        fn = d / f"{v}_M_ssp126_P1.nc"
        ds.to_netcdf(fn)
        fn.with_suffix(".nc.json").write_text(json.dumps(
            {"patch_id": "P1", "variable": v, "model": "M", "scenario": "ssp126"}))
    n = 40
    stations = pd.DataFrame({
        "lon": rng.uniform(110.2, 113.8, n).round(4),
        "lat": rng.uniform(30.2, 32.8, n).round(4),
        "type": ["wind"] * n, "year": rng.integers(2015, 2040, n),
        "capacity_gw": rng.uniform(0.001, 0.9, n).round(6)})
    stations.to_csv(root / "stations.csv", index=False)
    (root / "patch_manifest.json").write_text(json.dumps(
        {"patches": {"P1": {"core_bbox_360": [110.0, 114.5, 30.0, 33.5]}}}))
    return lat, lon, times


def reference(root: Path, tech: str, stations_csv: Path | None = None):
    import station_signals_patchify as ssp
    from grid_extreme_signals import station_match as sm
    from grid_extreme_signals.unit_conversion import (hurs_to_pct, pr_to_mmh,
                                                      rsds_to_wm2, tas_to_celsius, wind_to_ms)
    import registry
    from tools import common
    stations = ssp._stations(str(stations_csv or root / "stations.csv"), tech,
                             json.loads((root / "patch_manifest.json").read_text())["patches"]["P1"]["core_bbox_360"])
    files = {v: root / "bcsd" / "outputs" / "M" / "ssp126" / v / f"{v}_M_ssp126_P1.nc" for v in ssp.NEEDED[tech]}
    opened = {v: xr.open_dataset(p) for v, p in files.items()}
    try:
        ref = opened["rsds" if tech == "solar" else "uas"]
        times = ref.time.values
        match = sm.match_regular_weighted(ref.lat.values, ref.lon.values, stations, method="nearest", max_dist=0.15)
        weather = {}
        for v, ds in opened.items():
            da = ds[f"{v}_bcsd"]
            if not np.array_equal(da.time.values, times):
                da = da.interp({"time": times})
            arr = np.asarray(da.transpose("time", "lat", "lon").values, dtype=np.float32)
            arr = sm.gather_to_stations_weighted(arr, match)
            units = da.attrs.get("units") or {"tas": "K", "pr": "kg m-2 s-1", "rsds": "W m-2"}[v]
            if v in ("uas", "vas"): arr = wind_to_ms(arr, units)
            elif v == "tas": arr = tas_to_celsius(arr, units)
            elif v == "hurs": arr = hurs_to_pct(arr, units)
            elif v == "pr": arr = pr_to_mmh(arr, units)
            elif v == "rsds": arr = rsds_to_wm2(arr, units)
            weather[{"tas": "temp_C", "uas": "wind_u_ms", "vas": "wind_v_ms",
                     "hurs": "rh_pct", "pr": "precip_mmh", "rsds": "rsds"}[v]] = arr
        weather["wind_ms"] = np.hypot(weather.pop("wind_u_ms"), weather.pop("wind_v_ms")).astype(np.float32)
        masks = registry.simple_signals(tech, weather, skip_missing=True)
        idx = ssp._pseudo(times)
        years = np.array([pd.Timestamp(t).year for t in times])
        base = (years >= 2015) & (years <= 2024)
        resource = weather[registry.LOWRES_RESOURCE[tech]]
        steps = common.window_steps_for_hours(3.0)
        roll = common.roll_centered(resource, steps)
        clim = common.clim288(roll, idx, base)
        anom = roll - clim[idx.month.to_numpy() - 1, idx.hour.to_numpy()]
        thr = np.nanpercentile(np.where(np.isfinite(anom[base]), anom[base], np.nan), 5, axis=0)
        kw = dict(base_mask=base, clim_tbl=clim, thr=thr, window_steps=steps)
        if tech == "solar":
            kw.update(lat=stations.lat.to_numpy(float), lon=stations.lon.to_numpy(float))
        low = registry.low_resource_signal(tech, {registry.LOWRES_RESOURCE[tech]: resource}, idx, **kw)
        masks["low_resource"] = low
        valid = match.valid[None, :]
        act = years[:, None] >= stations.activation_year.to_numpy(int)[None, :]
        return {f"signal_{k}": (np.asarray(v, bool) & valid & act).astype(np.int8) for k, v in masks.items()}
    finally:
        for ds in opened.values(): ds.close()


def test_streaming_matches_reference():
    for tech in ("wind","solar"):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            build(root)
            out_root = root / "out"
            args = type("A", (), {})()
            args.bcsd_root = str(root / "bcsd"); args.model = "M"; args.scenario = "ssp126"
            args.patch = "P1"; args.patch_manifest = str(root / "patch_manifest.json")
            args.stations_csv = str(root / "stations.csv"); args.tech = tech
            args.years = "2015-2015"; args.output_root = str(out_root)
            args.spatial_method = "nearest"; args.max_distance_deg = 0.15
            import station_signals_patchify as ssp
            rows = pd.read_csv(root / "stations.csv"); rows["type"] = tech
            tech_csv = root / f"stations_{tech}.csv"
            rows.to_csv(tech_csv, index=False)
            args.stations_csv = str(tech_csv)
            ssp.run(args)
            got = xr.open_dataset(out_root / "M" / "ssp126" / "P1" / f"{tech}.nc")
            ref = reference(root, tech, tech_csv)
            for name, values in ref.items():
                assert name in got.variables, f"missing {name}"
                np.testing.assert_array_equal(got[name].values, values, err_msg=name)
            got.close()
    print("streaming signal writer equivalence passed")


if __name__ == "__main__":
    test_streaming_matches_reference()
