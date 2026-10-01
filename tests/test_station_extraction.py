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
    coverage = pd.read_csv(tmp_path / "published/runtime/coverage_summary.csv.gz").iloc[0]
    assert coverage.ssp_source_rows == 6 and coverage.station_count == 5
    assert coverage.completed_combinations == 2 and coverage.empty_combinations == 0
    assert coverage.matched_station_fraction == pytest.approx(3 / 5)
    assert coverage.distance_exact + coverage.distance_within_half_cell_nonexact + coverage.distance_beyond_half_cell == 5
    assert coverage.signal_icing_valid_count + coverage.signal_icing_missing_count == 16
    assert coverage.signal_icing_valid_fraction_in_outputs + coverage.signal_icing_missing_fraction_in_outputs == pytest.approx(1)
    wanted = [station_id("ssp126", tech, 170., 0.), station_id("ssp126", tech, 179.9, 0.)]
    with open_station_signals(index, "CANESM5", "ssp126", "ssp126", tech, wanted) as ds:
        assert ds.sizes == {"time": 4, "station": 2}
        assert list(ds.station_id.values) == wanted
        assert np.isnan(ds.signal_icing[:, 0]).all()
        assert ds.mapping_status.values.tolist() == [3, 0]
        np.testing.assert_array_equal(ds.signal_icing[:, 1], [1, 0, 1, 0])
    with open_station_signals(index, "CANESM5", "ssp126", "ssp126", tech, wanted, years="2021-2021") as ds:
        assert ds.sizes["time"] == 2
    with open_station_signals(index, "CANESM5", "ssp126", "ssp126", tech, wanted[:1]) as ds:
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
        assert audit(p, key, result, tmp_path / (key.split("/")[-2] + ".json"))["status"] == "SKIPPED_NO_STATIONS"


def test_same_grid_multiple_sites_and_chunk_independence(tmp_path):
    config = fixture_campaign(tmp_path / "source")
    csv = Path(config["stations"]["ssp126"])
    frame = pd.read_csv(csv)
    extra = frame.iloc[[0]].copy()
    extra["lon"] = 179.9001
    pd.concat([frame, extra]).to_csv(csv, index=False)
    prepared = prepare(config, tmp_path / "shared", "a" * 40)
    key = "CANESM5/climate_ssp126/station_ssp126/P1/wind"
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


def test_full_counterfactual_inventory():
    sources = {}
    for model in ct.MODELS:
        for climate in ct.SCENARIOS:
            for tech in ct.TECHS:
                for patch in range(47):
                    c = dict(model=model, scenario=climate, tech=tech, patch=f"P{patch:02}")
                    sources[ct.combo_key(c)] = c
    units = ct.station_combinations(sources, ("ssp126", "ssp245", "ssp560"))
    assert len(units) == 3384
    assert {(c["climate_scenario"], c["station_scenario"]) for c in units.values()} == {
        (climate, station) for climate in ct.SCENARIOS for station in ct.SCENARIOS}
    assert all(ct.combo_key(c) == key for key, c in units.items())
    with pytest.raises(ValueError, match="duplicate Station"):
        ct.station_combinations(sources, ("ssp560", "ssp585"))
    with pytest.raises(ValueError, match="Climate"):
        ct.scenario("ssp560")


def counterfactual_campaign(root, tech, calendar):
    import shutil

    config = fixture_campaign(root, tech=tech, calendar=calendar)
    index = ct.read_json(config["input_index"])
    originals = list(index["combinations"].values())
    index["combinations"] = {}
    for i, climate in enumerate(ct.SCENARIOS):
        for original in originals:
            c = copy.deepcopy(original)
            c["scenario"] = climate
            for a in c["artifacts"]:
                source = Path(a["output"])
                target = source.with_name(climate + "_" + source.name)
                shutil.copyfile(source, target)
                identity = f"{climate}-{target.stem}"
                with netCDF4.Dataset(target, "r+") as ds:
                    ds.set_auto_maskandscale(False)
                    ds.scenario, ds.identity = climate, identity
                    if i == 1:
                        for name in ("signal_icing", "signal_low_resource"):
                            values = ds[name][:]
                            ds[name][:] = np.where(values == ct.FILL, ct.FILL, 1 - values)
                    elif i == 2:
                        ds["signal_icing"][:] = np.where(ds["signal_icing"][:] == ct.FILL, ct.FILL, 0)
                ct.atomic_json(str(target) + ".json", dict(status="COMPLETED", identity=identity,
                                                          context={"stage": "signals"}))
                a["output"] = str(target)
            index["combinations"][ct.combo_key(c)] = c
    ct.atomic_json(config["input_index"], index)
    raw = pd.read_csv(config["stations"]["ssp126"])
    config["stations"] = {}
    for i, station in enumerate(("ssp126", "ssp245", "ssp560")):
        extra = raw.iloc[[0]].copy()
        extra["lon"] = 179.91 + .01 * i
        path = root / f"stations_{station}.csv"
        pd.concat([raw, extra]).to_csv(path, index=False)
        config["stations"][station] = str(path)
    return config


def test_parallel_prepare_matches_serial_and_reuses_identical_grids(tmp_path, monkeypatch, capsys):
    from grid_extreme_signals import station_pipeline as pipeline

    config = counterfactual_campaign(tmp_path / "source", "wind", "365_day")
    # A changed Climate mask must get separate mappings for every Station scenario.
    for year in (2020, 2021):
        with netCDF4.Dataset(tmp_path / f"source/ssp585_P2_{year}.nc", "r+") as ds:
            ds["domain_mask"][0, 0] = 0
    calls = []
    original = pipeline.build_mapping

    def tracked(*args):
        calls.append(args[0]["sha256"])
        return original(*args)

    monkeypatch.setattr(pipeline, "build_mapping", tracked)
    serial = prepare(config, tmp_path / "shared", "a" * 40)
    assert len(calls) == len(serial["mappings"]) == 6
    calls.clear()
    parallel = prepare(config, tmp_path / "shared", "a" * 40, workers=2)
    assert parallel == serial
    assert len(calls) == 6
    logs = capsys.readouterr().out
    assert "workers=2" in logs and "sources 6/6" in logs and "18 units, 6 unique mappings" in logs
    for key in parallel["combinations"]:
        first = extract_combination(serial, key, tmp_path / "out", "a" * 40)
        assert extract_combination(parallel, key, tmp_path / "out", "a" * 40) == first


def test_parallel_prepare_rejects_incomplete_source(tmp_path):
    config = fixture_campaign(tmp_path / "source")
    path = tmp_path / "source/P2_2021.nc.json"
    meta = ct.read_json(path)
    meta["status"] = "PARTIAL"
    ct.atomic_json(path, meta)
    with pytest.raises(ValueError, match="not a completed signals artifact"):
        prepare(config, tmp_path / "shared", "a" * 40, workers=2)
    assert not (tmp_path / "shared/prepared.json").exists()


@pytest.mark.parametrize("tech,calendar", [("wind", "365_day"), ("solar", "proleptic_gregorian")])
def test_counterfactual_pipeline_and_cli(tmp_path, tech, calendar):
    import subprocess

    config = counterfactual_campaign(tmp_path / "source", tech, calendar)
    p = prepare(config, tmp_path / "shared", "a" * 40)
    assert len(p["combinations"]) == 18
    assert set(p["catalogs"]) == set(ct.SCENARIOS)
    audits = {}
    results = {}
    for key, c in p["combinations"].items():
        r = extract_combination(p, key, tmp_path / "out", "a" * 40)
        results[key] = r
        assert extract_combination(p, key, tmp_path / "out", "a" * 40) == r
        path = tmp_path / "audits" / key / "audit.json"
        audit(p, key, r, path)
        audits[key] = str(path)
        for record in r["outputs"]:
            assert record["contract"]["combination"] == key
            with netCDF4.Dataset(record["artifact"]["path"]) as ds:
                assert ds.climate_scenario == c["climate_scenario"]
                assert ds.station_scenario == c["station_scenario"]
                assert ds.schema_version == ct.DUAL_SCENARIO_SCHEMA
                assert list(ds["station_id"][:]) == [station_id(c["station_scenario"], tech, x, y)
                                                   for x, y in zip(ds["lon"][:], ds["lat"][:])]
    index = publish(p, audits, tmp_path / "published")
    coverage = pd.read_csv(tmp_path / "published/runtime/coverage_summary.csv.gz")
    assert len(coverage) == 9
    assert set(zip(coverage.climate_scenario, coverage.station_scenario)) == {
        (c, s) for c in ct.SCENARIOS for s in ct.SCENARIOS}
    assert (coverage.station_count == 6).all()
    assert (coverage.completed_combinations == 2).all()
    for climate in ct.SCENARIOS:
        expected = {"ssp126": [1, 0, 1, 0], "ssp245": [0, 1, 0, 1], "ssp585": [0, 0, 0, 0]}[climate]
        for station in ct.SCENARIOS:
            ids = [station_id(station, tech, 170., 0.), station_id(station, tech, 179.9, 0.)]
            label = "ssp560" if station == "ssp585" else station
            with open_station_signals(index, "CANESM5", climate, label, tech, ids) as ds:
                assert list(ds.station_id.values) == ids
                assert np.isnan(ds.signal_icing[:, 0]).all()
                np.testing.assert_array_equal(ds.signal_icing[:, 1], expected)
                assert ds.attrs["station_scenario"] == station
                assert ds.attrs["climate_scenario"] == climate
            with open_station_signals(index, "CANESM5", climate, label, tech, ids, "2021-2021") as ds:
                np.testing.assert_array_equal(ds.signal_icing[:, 1], expected[2:])
    with pytest.raises(ValueError, match="unknown station"):
        open_station_signals(index, "CANESM5", "ssp126", "ssp245", tech,
                             [station_id("ssp126", tech, 179.9, 0.)])
    key = f"CANESM5/climate_ssp126/station_ssp585/P1/{tech}"
    other = f"CANESM5/climate_ssp245/station_ssp585/P1/{tech}"
    with pytest.raises(FileExistsError, match="conflicting"):
        extract_combination(p, other, tmp_path / "retry", "a" * 40, prior=results[key]["outputs"])
    wrong = dict(results[key], key=other)
    with pytest.raises(ValueError, match="combination identity"):
        audit(p, other, wrong, tmp_path / "bad_audit.json")
    cli = Path(__file__).resolve().parents[1] / "scripts/extract_station_events.py"
    result = subprocess.run([sys.executable, str(cli), "--prepared", str(tmp_path / "shared/prepared.json"),
                             "--model", "CANESM5", "--climate-scenario", "ssp126", "--station-scenario", "ssp560",
                             "--tech", tech, "--patch", "P1", "--output-root", str(tmp_path / "cli"),
                             "--code-sha", "a" * 40, "--years", "2020-2020"],
                            check=True, capture_output=True, text=True)
    assert result.stdout.strip() == "COMPLETED"
    record = ct.read_json(tmp_path / "cli" / key / "signals_2020-2020.nc.json")
    assert record["identity"] == results[key]["outputs"][0]["identity"]


def test_prepare_rejects_duplicate_station_alias(tmp_path):
    config = fixture_campaign(tmp_path / "source")
    config["stations"].update(ssp585=config["stations"]["ssp126"], ssp560=config["stations"]["ssp126"])
    with pytest.raises(ValueError, match="duplicate Station file alias"):
        prepare(config, tmp_path / "shared", "a" * 40)


def test_parallel_publication_matches_serial_and_rejects_changed_outputs(tmp_path):
    config = fixture_campaign(tmp_path / "source")
    prepared = prepare(config, tmp_path / "shared", "a" * 40)
    audits = {}
    for n, key in enumerate(prepared["combinations"]):
        extraction = extract_combination(prepared, key, tmp_path / "outputs", "a" * 40)
        path = tmp_path / f"audit_{n}.json"
        audit(prepared, key, extraction, path)
        audits[key] = str(path)
    serial = publish(prepared, audits, tmp_path / "serial")
    parallel = publish(prepared, audits, tmp_path / "parallel", workers=2)
    assert parallel == serial
    pd.testing.assert_frame_equal(
        pd.read_csv(tmp_path / "serial/runtime/coverage_summary.csv.gz"),
        pd.read_csv(tmp_path / "parallel/runtime/coverage_summary.csv.gz"))
    output = next(iter(parallel["combinations"].values()))["outputs"][0]["artifact"]["path"]
    Path(output + ".json").unlink()
    with pytest.raises(ValueError, match="changed after audit"):
        publish(prepared, audits, tmp_path / "rejected", workers=2)
    assert not (tmp_path / "rejected/runtime/authoritative_index.json").exists()
    # Accepted metadata publication does not revisit any scientific or mapping file.
    for combination in parallel["combinations"].values():
        for record in combination["outputs"]:
            Path(record["artifact"]["path"]).unlink(missing_ok=True)
            Path(record["artifact"]["path"] + ".json").unlink(missing_ok=True)
    for mapping in prepared["mappings"].values():
        for path in mapping["files"]:
            Path(path).unlink(missing_ok=True)
    metadata = publish(prepared, audits, tmp_path / "metadata", workers=2, verify_outputs=False)
    assert metadata == serial
    pd.testing.assert_frame_equal(
        pd.read_csv(tmp_path / "serial/runtime/coverage_summary.csv.gz"),
        pd.read_csv(tmp_path / "metadata/runtime/coverage_summary.csv.gz"))
