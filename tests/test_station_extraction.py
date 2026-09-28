"""Grid-event extraction contracts, with no meteorological input dependencies."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import netCDF4
import numpy as np
import pandas as pd
import pytest
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grid_extreme_signals import station_contract as ct
from grid_extreme_signals.station_catalog import build_catalog, load_catalog
from grid_extreme_signals.station_mapping import make_mapping, grid_metadata, nearest_axis
from grid_extreme_signals.station_pipeline import prepare, extract_combination, audit, publish
from grid_extreme_signals.station_reader import open_station_signals
from grid_extreme_signals.station_match import station_id


def fixture_campaign(root, calendar="365_day", descending=False, tech="wind"):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    manifest = {"patches": {"P1": {"core_bbox_360": [179.8, 180., 0., .2]},
                             "P2": {"core_bbox_360": [180., 180.2, 0., .2]}}}
    ct.atomic_json(root / "patch.json", manifest)
    raw = pd.DataFrame({"year": [2030, 2040, 2030, 2030, 2030, 2030],
                        "type": [tech] * 6, "lon": [179.9, 179.9, -180., 179.96, 170., 179.8],
                        "lat": [0., 0., .1, 0., 0., .1], "capacity_gw": [1., 2., 3., 4., 5., 6.]})
    raw.to_csv(root / "stations.csv", index=False)
    index = {"kind": "grid-v2-unified-index", "schema_version": 1, "selected_models": ["CANESM5"],
             "analysis_years": "2020-2021", "combinations": {}}
    for patch, lons in (("P1", [179.8, 179.9]), ("P2", [180., 180.1])):
        c = {"model": "CANESM5", "scenario": "ssp126", "tech": tech, "patch": patch,
             "source_run_id": "source-v2", "artifacts": []}
        for year in (2020, 2021):
            path = root / f"{patch}_{year}.nc"
            lat = np.array([.1, 0.] if descending else [0., .1])
            data = np.array([[[0, 1], [ct.FILL, 0]], [[1, 0], [ct.FILL, 1]]], dtype="i1")
            if descending:
                data = data[:, ::-1]
            mask = np.ones((2, 2), "i1")
            if patch == "P1":
                mask[0 if descending else 1, 0] = 0
            with netCDF4.Dataset(path, "w") as ds:
                for dim, n in (("time", 2), ("lat", 2), ("lon", 2)):
                    ds.createDimension(dim, n)
                ds.createVariable("lat", "f8", ("lat",))[:] = lat
                ds.createVariable("lon", "f8", ("lon",))[:] = lons
                t = ds.createVariable("time", "f8", ("time",))
                t.units = "hours since 2020-12-31 00:00:00"
                t.calendar = calendar
                t[:] = np.array([18., 21.] if year == 2020 else [24., 27.]) + (1.5 if tech == "solar" else 0)
                ds.createVariable("domain_mask", "i1", ("lat", "lon"))[:] = mask
                for name, values in (("signal_icing", data), ("signal_low_resource", np.where(data == 1, ct.FILL, data))):
                    v = ds.createVariable(name, "i1", ("time", "lat", "lon"), fill_value=ct.FILL,
                                          chunksizes=(1, 1, 1), zlib=True)
                    v[:] = values
                ds.setncatts(dict(model="CANESM5", scenario="ssp126", tech=tech, patch_id=patch, identity=f"{patch}-{year}",
                                 supported_events="icing,low_resource", skipped_events="", analysis_years="2020-2021"))
            ct.atomic_json(str(path) + ".json", {"status": "COMPLETED", "identity": f"{patch}-{year}",
                                                 "context": {"stage": "signals"}})
            c["artifacts"].append({"stage": "signals", "years": f"{year}-{year}", "output": str(path)})
        index["combinations"][patch] = c
    ct.atomic_json(root / "index.json", index)
    return {"models": ["CANESM5"], "analysis_years": "2020-2021", "input_index": str(root / "index.json"),
            "patch_manifest": str(root / "patch.json"), "stations": {"ssp126": str(root / "stations.csv")},
            "max_distance_deg": .15, "extraction": {"time_chunk": 1, "station_chunk": 2, "complevel": 1}}


@pytest.mark.parametrize("calendar,descending", [("365_day", False), ("proleptic_gregorian", True)])
@pytest.mark.parametrize("tech", ["wind", "solar"])
def test_full_pipeline_three_states_and_reader(tmp_path, calendar, descending, tech):
    config = fixture_campaign(tmp_path / "source", calendar, descending, tech)
    prepared = prepare(config, tmp_path / "shared", "a" * 40)
    mapping = next(iter(prepared["mappings"].values()))
    assert mapping["counts"] == {"MATCHED": 3, "OUTSIDE_DOMAIN": 1, "TOO_FAR": 0, "NO_SOURCE_PATCH": 1}
    assert mapping["cross_patch_count"] == 1
    assert prepared["catalogs"]["ssp126"]["catalogs"][tech]["count"] == 5
    audit_paths = {}
    for key, c in prepared["combinations"].items():
        result = extract_combination(prepared, key, tmp_path / "out", "a" * 40)
        again = extract_combination(prepared, key, tmp_path / "out", "a" * 40)
        assert result == again
        for artifact, record in zip(c["signals"], result["outputs"]):
            with netCDF4.Dataset(record["artifact"]["path"]) as dst, netCDF4.Dataset(artifact["path"]) as src:
                dst.set_auto_maskandscale(False); src.set_auto_maskandscale(False)
                np.testing.assert_array_equal(dst["time"][:], src["time"][:])
                assert dst["time"].calendar == calendar
                assert dst["lon"].dtype == np.dtype("f8")
                assert dst.activation_mask == "off"
                for event in ("icing", "low_resource"):
                    for s in range(len(dst.dimensions["station"])):
                        expected = (src["signal_" + event][:, int(dst["source_iy"][s]), int(dst["source_ix"][s])]
                                    if dst["mapping_status"][s] == 0 else np.full(2, ct.FILL))
                        np.testing.assert_array_equal(dst["signal_" + event][:, s], expected)
        path = tmp_path / (c["patch"] + "_audit.json")
        audit(prepared, key, result, path)
        audit_paths[key] = str(path)
    index = publish(prepared, audit_paths, tmp_path / "published")
    wanted = [station_id("ssp126", tech, 170., 0.), station_id("ssp126", tech, 179.9, 0.)]
    with open_station_signals(index, "CANESM5", "ssp126", tech, wanted) as ds:
        assert ds.sizes == {"time": 4, "station": 2}
        assert list(ds.station_id.values) == wanted
        assert np.isnan(ds.signal_icing[:, 0]).all()
        assert ds.mapping_status.values.tolist() == [3, 0]
        np.testing.assert_array_equal(ds.signal_icing[:, 1], [1, 0, 1, 0])
    with open_station_signals(index, "CANESM5", "ssp126", tech, wanted, years="2021-2021") as ds:
        assert ds.sizes["time"] == 2
    with open_station_signals(index, "CANESM5", "ssp126", tech, wanted[:1]) as ds:
        assert ds.sizes["time"] == 4 and np.isnan(ds.signal_icing).all()


def test_nearest_ties_domain_and_missing_patch(tmp_path):
    config = fixture_campaign(tmp_path)
    grids = {p: grid_metadata(tmp_path / f"{p}_2020.nc") for p in ("P1", "P2")}
    sites = pd.DataFrame({"station_id": ["a", "b", "c"], "lon": [179.95, -180.05, 170.], "lat": [0., 0., 0.]})
    frame = make_mapping(sites, grids, ct.read_json(config["patch_manifest"]))
    assert frame.source_patch.tolist() == ["P2", "P2", ""]  # periodic tie chooses -180
    assert frame.mapping_status.tolist() == [0, 0, 3]
    np.testing.assert_array_equal(nearest_axis(np.array([0., .1]), [.05]), [0.])
    frame = make_mapping(pd.DataFrame({"station_id": ["x"], "lon": [179.92], "lat": [0.]}),
                         grids, ct.read_json(config["patch_manifest"]), max_distance=.01)
    assert frame.mapping_status.iloc[0] == ct.STATUS["TOO_FAR"]


def test_catalog_validation_and_precision_collisions(tmp_path):
    config = fixture_campaign(tmp_path)
    path = Path(config["stations"]["ssp126"])
    raw = pd.read_csv(path)
    raw.loc[0, "capacity_gw"] = np.nan
    raw.to_csv(path, index=False)
    with pytest.raises(ValueError, match="invalid station"):
        build_catalog(path, "ssp126", tmp_path / "catalog")
    raw.loc[0, "capacity_gw"] = 1.
    raw.loc[1, "lon"] = 179.900001
    raw.to_csv(path, index=False)
    with pytest.raises(ValueError, match="重复|碰撞"):
        build_catalog(path, "ssp126", tmp_path / "catalog2")


def test_frozen_source_conflict_and_partial_retry(tmp_path):
    config = fixture_campaign(tmp_path / "source")
    p = prepare(config, tmp_path / "shared", "a" * 40)
    key = next(iter(p["combinations"]))
    result = extract_combination(p, key, tmp_path / "first", "a" * 40, years="2020-2020")
    recovered = extract_combination(p, key, tmp_path / "second", "a" * 40, prior=result["outputs"])
    assert recovered["outputs"][0]["artifact"]["path"].startswith(str(tmp_path / "first"))
    assert recovered["outputs"][1]["artifact"]["path"].startswith(str(tmp_path / "second"))
    record = recovered["outputs"][1]
    Path(record["artifact"]["path"] + ".json").unlink()
    with pytest.raises(FileExistsError):
        extract_combination(p, key, tmp_path / "second", "a" * 40)
    source = Path(p["combinations"][key]["signals"][0]["path"] + ".json")
    source.write_text(source.read_text() + " ")
    with pytest.raises(ValueError, match="source sidecar changed"):
        extract_combination(p, key, tmp_path / "third", "a" * 40)


def test_reject_period_gaps_and_audit_corruption(tmp_path):
    config = fixture_campaign(tmp_path / "source")
    idx = ct.read_json(config["input_index"])
    del idx["combinations"]["P1"]["artifacts"][0]
    with pytest.raises(ValueError, match="missing source periods"):
        ct.combinations(idx)
    p = prepare(config, tmp_path / "shared", "a" * 40)
    key = next(iter(p["combinations"]))
    result = extract_combination(p, key, tmp_path / "out", "a" * 40)
    path = result["outputs"][0]["artifact"]["path"]
    with netCDF4.Dataset(path, "r+") as ds:
        ds["signal_icing"][:] = 1
    with pytest.raises(ValueError, match="incomplete/changed"):
        audit(p, key, result, tmp_path / "audit.json")


def test_empty_technology_mapping_is_auditable(tmp_path):
    config = fixture_campaign(tmp_path / "source")
    raw = pd.read_csv(config["stations"]["ssp126"])
    raw["type"] = "solar"
    raw.to_csv(config["stations"]["ssp126"], index=False)
    p = prepare(config, tmp_path / "shared", "a" * 40)
    for key in p["combinations"]:
        result = extract_combination(p, key, tmp_path / "out", "a" * 40)
        assert result["status"] == "SKIPPED_NO_STATIONS"
        assert audit(p, key, result, tmp_path / (key.split("/")[2] + ".json"))["status"] == "SKIPPED_NO_STATIONS"


def test_same_grid_multiple_sites_and_chunk_independence(tmp_path):
    config = fixture_campaign(tmp_path / "source")
    csv = Path(config["stations"]["ssp126"])
    frame = pd.read_csv(csv)
    extra = frame.iloc[[0]].copy()
    extra["lon"] = 179.9001
    pd.concat([frame, extra]).to_csv(csv, index=False)
    prepared = prepare(config, tmp_path / "shared", "a" * 40)
    key = "CANESM5/ssp126/P1/wind"
    first = extract_combination(prepared, key, tmp_path / "out1", "a" * 40)
    prepared2 = copy.deepcopy(prepared)
    prepared2["release"]["campaign"]["extraction"]["time_chunk"] = 7
    second = extract_combination(prepared2, key, tmp_path / "out2", "a" * 40)
    for a, b in zip(first["outputs"], second["outputs"]):
        with xr.open_dataset(a["artifact"]["path"]) as ds, xr.open_dataset(b["artifact"]["path"]) as other:
            for name in ("signal_icing", "signal_low_resource"):
                xr.testing.assert_equal(ds[name], other[name])
                positions = np.flatnonzero((ds.lat.values == 0) & (abs(ds.lon.values - 179.9) < .001))
                assert len(positions) == 2
                np.testing.assert_array_equal(ds[name][:, positions[0]], ds[name][:, positions[1]])
