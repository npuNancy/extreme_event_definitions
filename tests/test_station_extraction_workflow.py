"""Immutable job packs and conservative scheduler/receipt state transitions."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "infos/scnet_patchify_stations"
sys.path.insert(0, str(ROOT))
from grid_extreme_signals import station_contract as ct


def module(name):
    spec = importlib.util.spec_from_file_location("stations_" + name, HERE / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


jobs = module("create_jobs")
control = module("control_loop")
runner = module("run_job")


def index_fixture(path, full=False):
    models = ct.MODELS if full else ct.MODELS[:1]
    scenarios = ct.SCENARIOS if full else ct.SCENARIOS[:1]
    patches = [f"P{i:02}" for i in range(47)] if full else ["P1"]
    index = {"kind": "grid-v2-unified-index", "schema_version": 1, "selected_models": list(models),
             "analysis_years": "2015-2060", "combinations": {}}
    for model in models:
        for ssp in scenarios:
            for patch in patches:
                for tech in ct.TECHS:
                    c = {"model": model, "scenario": ssp, "patch": patch, "tech": tech,
                         "source_run_id": "grid-v2", "artifacts": []}
                    for year in range(2015, 2061, 5):
                        period = f"{year}-{min(year + 4, 2060)}"
                        c["artifacts"].append({"stage": "signals", "years": period, "output": f"/source/{model}/{ssp}/{patch}/{tech}/{period}.nc"})
                    index["combinations"][ct.combo_key(c)] = c
    ct.atomic_json(path, index)
    return index


def campaign_fixture(path, full=False):
    c = ct.read_json(HERE / "campaign.json")
    c["accounts_file"] = str(HERE / "accounts.csv")
    if not full:
        c["models"] = list(ct.MODELS[:1])
    ct.atomic_json(path, c)
    return c


def test_full_four_model_inventory_and_shell(tmp_path):
    index_fixture(tmp_path / "index.json", full=True)
    campaign_fixture(tmp_path / "campaign.json", full=True)
    pack, scripts = jobs.build(tmp_path / "campaign.json", tmp_path / "index.json", "a" * 40)
    assert len(pack["workers"]) == 13
    worker_users = {w["username"] for w in pack["workers"]}
    assert "aczlvkl1ac" not in worker_users
    assert {r["logical_owner"] for r in pack["jobs"]} == worker_users
    assert pack["aggregator"]["username"] == "acp6varuz3"
    assert pack["campaign"]["aggregate_root"] == "/work/share/acp6varuz3/extreme_grid/stations_v2"
    assert pack["counts"] == {"prepare": 1, "extract": 1128, "audit": 1128}
    assert pack["signal_files"] == 11280
    assert len(set(r["task_id"] for r in pack["jobs"])) == 2257
    for body in scripts.values():
        subprocess.run(["bash", "-n"], input=body, text=True, check=True, capture_output=True)
        assert "source /work/home/acbpgywfpz/miniconda3/bin/activate climate" in body
        assert "bcsd-root" not in body and "sbatch" not in body
    dependencies = {r["unit_id"] for r in pack["jobs"]}
    assert all(set(r["depends_on"]) <= dependencies for r in pack["jobs"])


def test_generator_dry_run_is_standard_library_and_immutable(tmp_path):
    index_fixture(tmp_path / "index.json")
    campaign_fixture(tmp_path / "campaign.json")
    target = tmp_path / "pack"
    cmd = [sys.executable, "-S", str(HERE / "create_jobs.py"), "--campaign", str(tmp_path / "campaign.json"),
           "--input-index", str(tmp_path / "index.json"), "--code-sha", "b" * 40, "--jobs-dir", str(target)]
    result = subprocess.run(cmd + ["--dry-run"], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["total_jobs"] == 5
    assert not target.exists()
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    original = ct.digest(target / "manifest.json")
    assert subprocess.run(cmd, capture_output=True).returncode == 2
    assert ct.digest(target / "manifest.json") == original
    runner.load_pack(target / "manifest.json")
    corrupt = ct.read_json(target / "manifest.json")
    corrupt["code_sha"] = "c" * 40
    ct.atomic_json(target / "manifest.json", corrupt)
    with pytest.raises(ValueError, match="identity mismatch"):
        runner.load_pack(target / "manifest.json")


@pytest.mark.parametrize("scheduler,receipt,attempt,expected", [
    (None, True, 1, "unknown"),
    ({"state": "COMPLETED", "exit_code": "0:0"}, True, 1, "succeeded"),
    ({"state": "COMPLETED", "exit_code": "0:0"}, False, 1, "incomplete_output"),
    ({"state": "COMPLETED", "exit_code": "1:0"}, True, 1, "incomplete_output"),
    ({"state": "NODE_FAIL"}, False, 2, "retryable"),
    ({"state": "NODE_FAIL"}, False, 3, "deterministic_failure"),
    ({"state": "TIMEOUT"}, False, 1, "resource_failure"),
    ({"state": "OUT_OF_MEMORY"}, False, 1, "resource_failure"),
    ({"state": "CANCELLED"}, False, 1, "deterministic_failure"),
])
def test_scheduler_never_infers_success(scheduler, receipt, attempt, expected):
    assert control.classify({"attempt": attempt}, scheduler, receipt, 2) == expected


def test_account_cap_claim_and_unknown_submission(tmp_path, monkeypatch):
    index_fixture(tmp_path / "index.json")
    campaign_fixture(tmp_path / "campaign.json")
    pack, scripts = jobs.build(tmp_path / "campaign.json", tmp_path / "index.json", "a" * 40)
    pack["workers"] = pack["workers"][:1]
    config = pack["campaign"]
    config["aggregate_root"] = str(tmp_path / "center")
    config["worker_root_template"] = str(tmp_path / "{username}")
    pack.pop("identity")
    pack["identity"] = ct.fingerprint(pack)
    user = pack["workers"][0]["username"]
    root = tmp_path / user
    pack_dir = root / "jobs" / config["resource_profile"]
    for relative, body in scripts.items():
        path = pack_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    ct.atomic_json(pack_dir / "manifest.json", pack)
    (root / "logs").mkdir()
    monkeypatch.setattr(control.pwd, "getpwuid", lambda _: SimpleNamespace(pw_name="acp6varuz3"))
    queue = {str(j): {"state": "RUNNING", "name": "other-project"} for j in range(20)}
    monkeypatch.setattr(control, "queue", lambda _: dict(queue))
    monkeypatch.setattr(control, "accounting", lambda *args: {})
    calls = []
    def remote(*args):
        calls.append(args)
        raise subprocess.TimeoutExpired("sbatch", 45)
    monkeypatch.setattr(control, "remote", remote)
    lock = str(tmp_path / "shared.lock")
    status = tmp_path / "completion_status"
    ledger = control.cycle(pack, status, submit=True, submit_lock=lock)
    assert not calls
    assert (status / "progress.md").exists()
    queue.pop("19")
    monkeypatch.setattr(control, "remote", lambda *args: "STATION_NO_SLOT\n")
    ledger = control.cycle(pack, status, submit=True, submit_lock=lock)
    first = ledger["tasks"][pack["jobs"][0]["task_id"]]
    assert first["classification"] == "not_submitted" and first["attempt"] == 0
    assert first["job_id"] is None
    monkeypatch.setattr(control, "remote", remote)
    ledger = control.cycle(pack, status, submit=True, submit_lock=lock)
    assert len(calls) == 1
    first = ledger["tasks"][pack["jobs"][0]["task_id"]]
    assert first["classification"] == "unknown" and first["attempt"] == 1
    control.cycle(pack, status, submit=True, submit_lock=lock)
    assert len(calls) == 1  # Lost submission receipt must never cause blind duplicate submission.
    assert all(s["attempt"] == 0 for k, s in ledger["tasks"].items() if k != first["task_id"])


def test_guarded_submit_protocol(monkeypatch):
    calls = []
    def remote(host, argv):
        calls.append(argv)
        subprocess.run(["bash", "-n"], input=argv[2], text=True, check=True)
        return "12345;cluster\n"
    monkeypatch.setattr(control, "remote", remote)
    worker = {"host": "worker", "username": "account"}
    assert control.guarded_submit(worker, ["sbatch", "--parsable", "/job.sh"], 20) == "12345"
    assert '.bcsd_submit.lock' in calls[0][2] and 'squeue -r' in calls[0][2]
    assert calls[0][4:] == ["account", "20", "sbatch", "--parsable", "/job.sh"]


def test_scheduling_limit_preserves_pack_dependencies_and_account_caps(tmp_path, monkeypatch):
    index_fixture(tmp_path / "index.json")
    campaign_fixture(tmp_path / "campaign.json")
    pack, scripts = jobs.build(tmp_path / "campaign.json", tmp_path / "index.json", "a" * 40)
    pack["workers"] = pack["workers"][:2]
    config = pack["campaign"]
    config.update(aggregate_root=str(tmp_path / "center"), worker_root_template=str(tmp_path / "{username}"),
                  global_active_limit=1, account_active_limit=1)
    pack.pop("identity")
    pack["identity"] = ct.fingerprint(pack)
    original = copy.deepcopy(pack)
    queues = {w["username"]: {} for w in pack["workers"]}
    for user in queues:
        root = tmp_path / user
        directory = root / "jobs" / config["resource_profile"]
        for relative, body in scripts.items():
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        ct.atomic_json(directory / "manifest.json", pack)
        (root / "logs").mkdir()
    ledger = control.initialize(pack)
    ledger["tasks"][pack["jobs"][0]["task_id"]]["classification"] = "succeeded"
    ct.atomic_json(Path(config["aggregate_root"]) / "runtime/ledger.json", ledger)
    monkeypatch.setattr(control.pwd, "getpwuid", lambda _: SimpleNamespace(pw_name="acp6varuz3"))
    monkeypatch.setattr(control, "queue", lambda w: dict(queues[w["username"]]))
    monkeypatch.setattr(control, "accounting", lambda *args: {})
    calls = []
    def submit(worker, argv, limit):
        queue = queues[worker["username"]]
        assert len(queue) < limit == 1
        job = str(100 + len(calls))
        queue[job] = {"state": "RUNNING", "name": "station"}
        calls.append(worker["username"])
        return job
    monkeypatch.setattr(control, "guarded_submit", submit)
    args = (pack, tmp_path / "status", True, str(tmp_path / "shared.lock"))
    control.cycle(*args)
    assert len(calls) == 1
    ledger = control.cycle(*args, global_active_limit=2)
    assert len(calls) == 2 and len(set(calls)) == 2
    assert ledger["submission_policy"]["global_active_limit"] == 2
    for row in pack["jobs"]:
        if row["stage"] == "audit":
            assert ledger["tasks"][row["task_id"]]["classification"] == "not_submitted"
    ledger = control.cycle(*args, global_active_limit=1)
    assert len(calls) == 2
    assert sum(s["classification"] == "active" for s in ledger["tasks"].values()) == 2
    for invalid in (0, -1, 3, True):
        with pytest.raises(ValueError, match="global active limit"):
            control.cycle(*args, global_active_limit=invalid)
    assert pack == original
    assert not ledger.get("deployment_errors")


def test_accounting_keeps_peak_across_steps(monkeypatch):
    raw = ("123|COMPLETED|0:0|00:05:27||pilot|\n"
           "123.batch|COMPLETED|0:0|00:05:27|700M|batch|\n"
           "123.extern|COMPLETED|0:0|00:05:27|2516K|extern|\n")
    monkeypatch.setattr(control, "remote", lambda *args: raw)
    result = control.accounting({"host": "worker", "username": "account"}, "2026-09-28")
    assert result["123"]["step_max_rss"] == "700M"
    assert result["123"]["state"] == "COMPLETED"


def test_startup_io_retry_requires_absent_attempt_and_has_one_retry(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text("{}")
    state = {"classification": "deterministic_failure", "scheduler_state": "FAILED", "exit_code": "1:0",
             "attempt": 1, "task_id": "task", "job_id": "123",
             "log_tail": 'ledger = ct.read_json(center / "runtime/ledger.json")\nOSError: [Errno 5] Input/output error\n'}
    assert control.retryable_startup_io(state, tmp_path, 2, ledger_path)
    assert not control.retryable_startup_io(state, tmp_path, 0, ledger_path)
    for changes in ({"attempt": 2}, {"classification": "unknown"}, {"scheduler_state": "CANCELLED"},
                    {"exit_code": "0:9"}, {"log_tail": "OSError: [Errno 5] Input/output error"},
                    {"log_tail": state["log_tail"] + "ValueError: different terminal failure"}):
        assert not control.retryable_startup_io(dict(state, **changes), tmp_path, 2, ledger_path)
    ledger_path.unlink()
    assert not control.retryable_startup_io(state, tmp_path, 2, ledger_path)
    ledger_path.write_text("{}")
    attempt = tmp_path / "attempts/task/123"
    attempt.mkdir(parents=True)
    assert not control.retryable_startup_io(state, tmp_path, 2, ledger_path)


def test_missing_startup_ledger_retry_requires_exact_restored_path(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    state = {"classification": "deterministic_failure", "scheduler_state": "FAILED", "exit_code": "1:0",
             "attempt": 1, "task_id": "task", "job_id": "123",
             "log_tail": 'ledger = ct.read_json(center / "runtime/ledger.json")\n'
                         f"FileNotFoundError: [Errno 2] No such file or directory: {str(ledger_path)!r}\n"}
    assert not control.retryable_startup_io(state, tmp_path, 2, ledger_path)
    ledger_path.write_text("{}")
    assert control.retryable_startup_io(state, tmp_path, 2, ledger_path)
    assert not control.retryable_startup_io(dict(state, attempt=2), tmp_path, 2, ledger_path)
    wrong = dict(state, log_tail=state["log_tail"].replace(str(ledger_path), str(tmp_path / "input.nc")))
    assert not control.retryable_startup_io(wrong, tmp_path, 2, ledger_path)
    (tmp_path / "attempts/task/123").mkdir(parents=True)
    assert not control.retryable_startup_io(state, tmp_path, 2, ledger_path)


def test_unprepared_worker_does_not_block_other_accounts(tmp_path):
    pack = {"campaign": {"worker_root_template": str(tmp_path / "{username}"), "resource_profile": "v1"}, "jobs": []}
    pack["identity"] = ct.fingerprint(pack)
    root = tmp_path / "ready"
    (root / "logs").mkdir(parents=True)
    ct.atomic_json(root / "jobs/v1/manifest.json", pack)
    ledger = {}
    snapshots = {"unavailable": {}, "ready": {}}
    assert control.ready_workers(pack, ledger, snapshots) == ["ready"]
    assert set(ledger["deployment_errors"]) == {"unavailable"}
    assert set(snapshots) == {"unavailable", "ready"}  # Continue monitoring active jobs on both accounts.


def test_lock_excludes_concurrent_writer(tmp_path):
    with ct.lock(tmp_path / "lock"):
        with pytest.raises(BlockingIOError):
            with ct.lock(tmp_path / "lock"):
                pass


def test_receipt_identity_and_content_hash(tmp_path):
    output = tmp_path / "result.json"
    ct.atomic_json(output, {"status": "COMPLETED"})
    pack = {"identity": "pack", "code_sha": "a" * 40}
    row = {"task_id": "task", "job_id": "123", "receipt": str(tmp_path / "receipt.json")}
    receipt = {"status": "COMPLETED", "task_id": "task", "job_id": "123", "pack_identity": "pack",
               "code_sha": "a" * 40, "output": str(output), "artifact": ct.file_stat(output),
               "output_sha256": ct.digest(output)}
    ct.atomic_json(row["receipt"], receipt)
    assert runner.valid_receipt(row, pack)["job_id"] == "123"
    with pytest.raises(ValueError, match="identity/artifact"):
        runner.valid_receipt(dict(row, job_id="456"), pack)
    output.write_text('{"status":"FAILED"}')
    with pytest.raises(ValueError, match="identity/artifact"):
        runner.valid_receipt(row, pack)
