"""Independent dual-scenario campaign packs, receipts, recovery and progress."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from grid_extreme_signals import station_contract as ct
from infos.scnet_patchify_stations_new import create_jobs as jobs
from infos.scnet_patchify_stations_new import run_job as runner
from infos.scnet_patchify_stations_new import progress


@pytest.fixture
def inventory(tmp_path):
    _, patches = jobs.catalog()
    index = {"kind": "grid-v2-unified-index", "schema_version": 1, "selected_models": list(ct.MODELS),
             "analysis_years": "2015-2060", "combinations": {}}
    for model in ct.MODELS:
        for climate in ct.SCENARIOS:
            for patch in patches:
                for tech in ct.TECHS:
                    c = dict(model=model, scenario=climate, patch=patch, tech=tech, source_run_id="grid-v2", artifacts=[])
                    for start in range(2015, 2061, 5):
                        years = f"{start}-{min(start + 4, 2060)}"
                        c["artifacts"].append(dict(stage="signals", years=years,
                            output=f"/source/{model}/{climate}/{patch}/{tech}/{years}.nc"))
                    index["combinations"][ct.combo_key(c)] = c
    ct.atomic_json(tmp_path / "index.json", index)
    ct.atomic_json(tmp_path / "patches.json", {"patches": {p: {} for p in patches}})
    config = ct.read_json(jobs.HERE / "campaign.example.json")
    config.update(run_id="events_cs_test", aggregate_root="/work/home/acjpoxgsdu/extreme_stations_new/events_cs_test")
    ct.atomic_json(tmp_path / "campaign.json", config)
    return jobs.parser().parse_args(["--campaign", str(tmp_path / "campaign.json"),
        "--input-index", str(tmp_path / "index.json"), "--patch-manifest", str(tmp_path / "patches.json"),
        "--code-sha", "a" * 40, "--jobs-dir", str(tmp_path / "jobs")])


def test_full_pack_counts_identity_and_all_shells(inventory):
    pack, scripts = jobs.build(inventory)
    assert pack["counts"] == dict(prepare=1, extract=3384, publish=1)
    assert pack["jobs"][0]["cpus"] == 16
    assert "#SBATCH --cpus-per-task=16" in scripts[pack["jobs"][0]["script"]]
    assert all(row["cpus"] == 10 for row in pack["jobs"][1:-1])
    assert pack["jobs"][-1]["cpus"] == 16
    assert len(scripts) == len(pack["jobs"]) == len({r["task_id"] for r in pack["jobs"]}) == 3386
    assert pack["signal_files"] == 33840
    assert len(pack["workers"]) == 14
    assert pack["aggregator"] == "acjpoxgsdu"
    assert "acjpoxgsdu" not in {r["logical_owner"] for r in pack["jobs"]}
    assert len(pack["jobs"][-1]["depends_on"]) == 3384
    assert {(r["climate_scenario"], r["station_scenario"]) for r in pack["jobs"] if r["stage"] == "extract"} == {
        (c, s) for c in ct.SCENARIOS for s in ct.SCENARIOS}
    for body in scripts.values():
        assert "/work/home/" not in body and "#SBATCH --account" not in body
        assert 'source "$STATION_CLIMATE_ACTIVATE" climate' in body
        assert 'cd "$STATION_REPO"' in body
        assert "--output=logs/%x-%j.out" in body
        assert "sbatch" not in body
        subprocess.run(["bash", "-n"], input=body, text=True, check=True, capture_output=True)
    other = copy.copy(inventory)
    other.jobs_dir = str(Path(inventory.jobs_dir).parent / "another-account/jobs")
    assert jobs.build(other) == (pack, scripts)
    other.cpus_per_task = 12
    other.resource_profile = "events_new_v2"
    revised, _ = jobs.build(other)
    assert revised["campaign_identity"] == pack["campaign_identity"]
    assert revised["identity"] != pack["identity"]
    assert [r["task_id"] for r in revised["jobs"]] == [r["task_id"] for r in pack["jobs"]]


def test_cli_standard_library_dry_run_and_immutable_pack(inventory):
    cmd = [sys.executable, "-S", str(jobs.HERE / "create_jobs.py"), "--campaign", inventory.campaign,
           "--input-index", inventory.input_index, "--patch-manifest", inventory.patch_manifest,
           "--code-sha", inventory.code_sha, "--jobs-dir", inventory.jobs_dir]
    result = subprocess.run(cmd + ["--dry-run"], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["total_jobs"] == 3386
    target = Path(inventory.jobs_dir)
    assert not target.exists()
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    original = ct.digest(target / "manifest.json")
    pack = runner.load_pack(target / "manifest.json")
    assert len(pack["jobs"]) == 3386
    assert subprocess.run(cmd, capture_output=True).returncode == 2
    assert ct.digest(target / "manifest.json") == original
    pack["code_sha"] = "b" * 40
    ct.atomic_json(target / "manifest.json", pack)
    with pytest.raises(ValueError, match="identity/schema"):
        runner.load_pack(target / "manifest.json")


@pytest.mark.parametrize("change", ["missing_source", "wrong_patch", "station_alias", "old_root", "models", "zero_cpu"])
def test_invalid_production_scope(inventory, change):
    if change == "missing_source":
        src = ct.read_json(inventory.input_index)
        src["combinations"].pop(next(iter(src["combinations"])))
        ct.atomic_json(inventory.input_index, src)
    elif change == "wrong_patch":
        src = ct.read_json(inventory.patch_manifest)
        src["patches"].pop(next(iter(src["patches"])))
        ct.atomic_json(inventory.patch_manifest, src)
    elif change == "zero_cpu":
        inventory.cpus_per_task = 0
    else:
        config = ct.read_json(inventory.campaign)
        if change == "station_alias":
            config["stations"]["ssp560"] = config["stations"].pop("ssp585")
        elif change == "old_root":
            config["aggregate_root"] = "/work/share/acp6varuz3/extreme_grid/stations_v2"
        else:
            config["models"] = config["models"][:1]
        ct.atomic_json(inventory.campaign, config)
    with pytest.raises(ValueError):
        jobs.build(inventory)


def test_prepared_scope_and_progress(inventory):
    pack, _ = jobs.build(inventory)
    release = {"schema": ct.DUAL_SCENARIO_SCHEMA, "code_sha": pack["code_sha"], "campaign": pack["campaign"],
               "index_sha256": pack["input_index_sha256"], "patch_manifest_sha256": pack["patch_manifest_sha256"]}
    prep = {"identity": ct.fingerprint(release), "release": release,
            "combinations": {r["key"]: {k: r[k] for k in ("model", "climate_scenario", "station_scenario", "patch", "tech")}
                             for r in pack["jobs"] if r["stage"] == "extract"}}
    runner.prepared_contract(prep, pack)
    prep["combinations"].pop(next(iter(prep["combinations"])))
    with pytest.raises(ValueError, match="scope"):
        runner.prepared_contract(prep, pack)
    ledger = progress.initial_ledger(pack)
    body = progress.render(pack, ledger)
    assert body.count("0/94") == 36 and body.count("0/1") == 2
    assert "Asia/Shanghai" in body and "Station ssp585" in body
    for r in pack["jobs"]:
        state = ledger["tasks"][r["task_id"]]
        state.update(classification="succeeded", verified_at="2026-10-01T00:00:00+08:00", job_id="123")
    ledger["preparation"]["status"] = "verified"
    body = progress.render(pack, ledger)
    assert body.count("94/94 ✅") == 36 and body.count("1/1 ✅") == 2
    del ledger["tasks"][pack["jobs"][1]["task_id"]]["verified_at"]
    with pytest.raises(ValueError, match="verification"):
        progress.render(pack, ledger)


def mark_success(pack, ledger, row, shared, job, status, output):
    state = ledger["tasks"][row["task_id"]]
    state.update(classification="succeeded", job_id=job, verified_at="2026-10-01T00:00:00+08:00")
    receipt = dict(status=status, stage=row["stage"], task_id=row["task_id"], unit_id=row["unit_id"],
                   job_id=job, username=state["username"], assignment_version=state["assignment_version"],
                   pack_identity=state["pack_identity"], campaign_identity=pack["campaign_identity"], code_sha=pack["code_sha"],
                   output=str(output), artifact=ct.file_stat(output), output_sha256=ct.digest(output))
    ct.atomic_json(runner.receipt_path(shared, row["task_id"], job), receipt)


def test_runner_scientific_stages_recovery_and_receipts(tmp_path, monkeypatch):
    from tests.test_station_extraction import fixture_campaign
    from grid_extreme_signals.station_reader import open_station_signals

    config = fixture_campaign(tmp_path / "source")
    rows = [dict(stage="prepare", unit_id="prepare", task_id="prepare", depends_on=[], key=None)]
    for patch in ("P1", "P2"):
        key = f"CANESM5/climate_ssp126/station_ssp126/{patch}/wind"
        rows.append(dict(stage="extract", unit_id=patch, task_id=patch, key=key, depends_on=["prepare"],
                         periods=["2020-2020", "2021-2021"]))
    rows.append(dict(stage="publish", unit_id="publish", task_id="publish", key=None, depends_on=["P1", "P2"]))
    for row in rows:
        row["logical_owner"] = "acm83pfnji"
        row["cpus"] = 2
    pack = dict(campaign=config, code_sha="a" * 40, identity="pack", campaign_identity="campaign", jobs=rows,
                resource_profile="v1")
    ledger = progress.initial_ledger(pack)
    shared = tmp_path / "shared"
    # The full production guard is tested separately; exercise science with tiny real NC files.
    monkeypatch.setattr(runner, "prepared_contract", lambda p, _: None)
    for i, row in enumerate(rows):
        job = str(100 + i)
        claim = ledger["tasks"][row["task_id"]]
        status, output = runner.execute_science(pack, row, claim, ledger, shared, job)
        mark_success(pack, ledger, row, shared, job, status, output)
        receipt = runner.valid_receipt(claim, row, pack, shared)
        assert receipt["output"] == str(output)
        if row["stage"] == "extract":
            original = ct.read_json(output)
            claim["history"] = [dict(job_id=job)]
            status, recovered = runner.execute_science(pack, row, claim, ledger, shared, job + "0")
            assert ct.read_json(recovered)["outputs"] == original["outputs"]
    index = shared / "runtime/authoritative_index.json"
    assert index.is_file() and (shared / "runtime/coverage_summary.csv.gz").is_file()
    with open_station_signals(index, "CANESM5", "ssp126", "ssp126", "wind") as ds:
        assert ds.sizes["station"] == 5
    state = ledger["tasks"]["P1"]
    state["username"] = "ac2k2a2td1"
    with pytest.raises(ValueError, match="receipt identity"):
        runner.valid_receipt(state, rows[1], pack, shared)
    state["username"] = "acm83pfnji"
    state["classification"] = "unknown"
    with pytest.raises(ValueError, match="not verified"):
        runner.valid_receipt(state, rows[1], pack, shared)
    assert runner.valid_receipt(state, rows[1], pack, shared, require_success=False)["status"] == "COMPLETED"
    with pytest.raises(ValueError, match="outside new shared"):
        runner.inside(config["input_index"], shared)


def test_progress_file_is_ignored():
    path = jobs.HERE / "completion_status/progress.md"
    result = subprocess.run(["git", "check-ignore", str(path)], cwd=ROOT, capture_output=True)
    assert result.returncode == 0


def test_aggregator_cannot_execute(inventory, monkeypatch):
    from types import SimpleNamespace

    pack, _ = jobs.build(inventory)
    monkeypatch.setattr(runner.pwd, "getpwuid", lambda _: SimpleNamespace(pw_name="acjpoxgsdu"))
    with pytest.raises(ValueError, match="aggregator must not run"):
        runner.preflight(pack, pack["jobs"][0]["task_id"])
