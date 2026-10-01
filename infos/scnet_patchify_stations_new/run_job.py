#!/usr/bin/env python3
"""Run one claimed prepare, extraction+audit, or publication job on a compute node."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from grid_extreme_signals import station_contract as ct
from infos.scnet_patchify_stations_new.create_jobs import AGGREGATOR, catalog


def load_pack(path):
    pack = ct.read_json(path)
    body = {k: v for k, v in pack.items() if k != "identity"}
    if pack["identity"] != ct.fingerprint(body) or pack["schema"] != "station-events-new-slurm-v1":
        raise ValueError("job pack identity/schema mismatch")
    return pack


def inside(path, root):
    path = Path(path).resolve(strict=True)
    if not path.is_relative_to(Path(root).resolve(strict=True)):
        raise ValueError("artifact outside new shared run")
    return path


def receipt_path(shared, task, job):
    ct.safe_name(task)
    if not re.fullmatch(r"\d+", str(job)):
        raise ValueError("invalid Slurm JobID")
    return Path(shared) / "runtime/receipts" / task / f"{job}.json"


def valid_receipt(state, row, pack, shared, *, require_success=True):
    if require_success and state["classification"] != "succeeded":
        raise ValueError("dependency not verified successful")
    r = ct.read_json(receipt_path(shared, row["task_id"], state["job_id"]))
    if (r["task_id"] != row["task_id"] or r["unit_id"] != row["unit_id"]
            or r["stage"] != row["stage"] or r["job_id"] != state["job_id"] or r["username"] != state["username"]
            or r["pack_identity"] != state["pack_identity"] or r["campaign_identity"] != pack["campaign_identity"]
            or r["code_sha"] != pack["code_sha"] or r["status"] not in ("COMPLETED", "SKIPPED_NO_STATIONS")
            or r["assignment_version"] != state["assignment_version"]):
        raise ValueError("dependency receipt identity mismatch")
    output = inside(r["output"], shared)
    if r["artifact"] != ct.file_stat(output) or r["output_sha256"] != ct.digest(output):
        raise ValueError("dependency receipt artifact changed")
    if row["stage"] != "extract" and r["status"] != "COMPLETED":
        raise ValueError("global stage cannot be an empty station unit")
    if row["stage"] == "extract":
        audit = ct.read_json(output)
        if audit["status"] != r["status"] or ct.combo_key(audit["combination"]) != row["key"]:
            raise ValueError("audit belongs to another unit")
        if r["status"] == "SKIPPED_NO_STATIONS":
            if audit["station_count"] != 0 or audit["outputs"]:
                raise ValueError("invalid empty station audit")
        else:
            if audit["station_count"] < 1 or [a["period"] for a in audit["outputs"]] != row["periods"]:
                raise ValueError("audit output periods incomplete")
            for a in audit["outputs"]:
                path = inside(a["artifact"]["path"], shared)
                if (a["contract"]["combination"] != row["key"]
                        or a["artifact"] != ct.file_stat(path) or not ct.completed(path, a["identity"])):
                    raise ValueError("audited station artifact changed")
    return r


def prepared_contract(prepared, pack):
    expected = {r["key"] for r in pack["jobs"] if r["stage"] == "extract"}
    release = prepared["release"]
    if (prepared["identity"] != ct.fingerprint(release) or release["schema"] != ct.DUAL_SCENARIO_SCHEMA
            or release["code_sha"] != pack["code_sha"] or release["campaign"] != pack["campaign"]
            or release["index_sha256"] != pack["input_index_sha256"]
            or release["patch_manifest_sha256"] != pack["patch_manifest_sha256"]
            or len(expected) != 3384 or set(prepared["combinations"]) != expected):
        raise ValueError("prepared release/scope mismatch")
    for key, c in prepared["combinations"].items():
        if key != ct.combo_key(c):
            raise ValueError("prepared combination fields mismatch")


def preflight(pack, task):
    row = next(r for r in pack["jobs"] if r["task_id"] == task)
    user = pwd.getpwuid(os.getuid()).pw_name
    workers, _ = catalog()
    if user not in {w["username"] for w in workers}:
        raise ValueError("unauthorized worker; aggregator must not run jobs")
    job = os.environ.get("SLURM_JOB_ID", "")
    if not re.fullmatch(r"\d+", job) or os.environ.get("SLURM_ARRAY_TASK_ID"):
        raise ValueError("requires an individual Slurm compute job")
    if os.environ.get("SLURM_JOB_ACCOUNT") != user or int(os.environ.get("SLURM_CPUS_PER_TASK", "0")) < row["cpus"]:
        raise ValueError("billing account or CPU allocation mismatch")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
    if Path(os.environ["STATION_REPO"]).resolve() != ROOT or head != pack["code_sha"] or dirty:
        raise ValueError("requires clean checkout at pinned SHA")
    if ct.digest(os.environ["STATION_JOB_SCRIPT"]) != row["script_sha256"]:
        raise ValueError("executed script differs from frozen job pack")
    shared = Path(os.environ["STATION_SHARED_ROOT"]).resolve(strict=True)
    base = (Path("/work/home") / AGGREGATOR).resolve(strict=True)
    expected = base / "extreme_stations_new" / pack["campaign"]["run_id"]
    if (shared != expected or Path(pack["campaign"]["aggregate_root"]).resolve() != shared
            or not shared.is_relative_to(base)):
        raise ValueError("results must reside physically in aggregator home/extreme_stations_new/run_id")
    config = inside(os.environ["STATION_CAMPAIGN"], shared)
    if ct.digest(config) != pack["campaign_sha256"] or ct.read_json(config) != pack["campaign"]:
        raise ValueError("campaign configuration changed")
    if row["stage"] == "prepare":
        for key in ("input_index", "patch_manifest"):
            if ct.digest(pack["campaign"][key]) != pack[key + "_sha256"]:
                raise ValueError("source JSON differs from frozen pack")
    for _ in range(30):
        ledger = ct.read_json(shared / "runtime/ledger.json")
        if ledger["campaign_identity"] != pack["campaign_identity"]:
            raise ValueError("ledger campaign identity mismatch")
        if ledger.get("preparation", {}).get("status") != "verified":
            raise ValueError("preparation gates have not been verified")
        claim = ledger["tasks"][task]
        if claim.get("job_id") is not None:
            break
        time.sleep(1)
    if (claim.get("job_id") != job or claim["username"] != user or claim["pack_identity"] != pack["identity"]
            or claim["classification"] not in ("submitting", "active")):
        raise ValueError("job does not own current claim")
    return row, claim, ledger, shared, job, user


def execute_science(pack, row, claim, ledger, shared, job):
    from grid_extreme_signals import station_pipeline as pipeline

    shared = Path(shared)
    attempt = shared / "attempts" / row["task_id"] / job
    attempt.mkdir(parents=True, exist_ok=True)
    by_unit = {r["unit_id"]: r for r in pack["jobs"]}
    dependencies = {}
    for uid in row["depends_on"]:
        dep = by_unit[uid]
        dependencies[uid] = valid_receipt(ledger["tasks"][dep["task_id"]], dep, pack, shared)
    if row["stage"] == "prepare":
        ct.atomic_json(shared / "runtime/acl_probe" / f"compute-{job}.json",
                       {"job_id": job, "username": pwd.getpwuid(os.getuid()).pw_name,
                        "shared_root": str(shared.resolve())})
        prepared = pipeline.prepare(pack["campaign"], attempt / "shared", pack["code_sha"],
                                    workers=min(16, row["cpus"]))
        prepared_contract(prepared, pack)
        return "COMPLETED", attempt / "shared/prepared.json"
    prep_row = next(r for r in pack["jobs"] if r["stage"] == "prepare")
    prep_receipt = valid_receipt(ledger["tasks"][prep_row["task_id"]], prep_row, pack, shared)
    prepared = ct.read_json(prep_receipt["output"])
    prepared_contract(prepared, pack)
    if row["stage"] == "extract":
        prior = {}
        for old in claim.get("history", []):
            old_job = old.get("job_id")
            if old_job is None:
                continue
            receipt_path(shared, row["task_id"], old_job)  # validate JobID before building paths
            partial = shared / "attempts" / row["task_id"] / old_job / "outputs" / row["key"] / "extraction.json"
            if partial.exists():
                saved = ct.read_json(inside(partial, shared))
                if saved.get("release_identity") == prepared["identity"] and saved.get("key") == row["key"]:
                    for record in saved["outputs"]:
                        path = inside(record["artifact"]["path"], shared)
                        if ct.completed(path, record["identity"]):
                            prior[record["period"]] = record
        extraction = pipeline.extract_combination(prepared, row["key"], attempt / "outputs", pack["code_sha"],
                                                   prior=list(prior.values()))
        ct.atomic_json(attempt / "extraction.json", extraction)
        output = attempt / "audit.json"
        result = pipeline.audit(prepared, row["key"], extraction, output)
        return result["status"], output
    audits = {by_unit[uid]["key"]: r["output"] for uid, r in dependencies.items()}
    pipeline.publish(prepared, audits, attempt / "publication", workers=min(16, row["cpus"]))
    # Publish the stable entry point only after the complete audited index exists.
    for name in ("coverage_summary.csv.gz", "authoritative_index.json"):
        source = attempt / "publication/runtime" / name
        target = shared / "runtime" / name
        if target.exists():
            if ct.digest(target) != ct.digest(source):
                raise ValueError("published index conflicts; use a new run")
        else:
            import shutil
            tmp = ct.temporary(target)
            try:
                shutil.copyfile(source, tmp)
                os.replace(tmp, target)
            finally:
                tmp.unlink(missing_ok=True)
    return "COMPLETED", shared / "runtime/authoritative_index.json"


def execute(pack_path, task):
    pack = load_pack(pack_path)
    row, claim, ledger, shared, job, user = preflight(pack, task)
    receipt = receipt_path(shared, task, job)
    started = time.monotonic()
    # The lock key is independent of execution account, attempt and resource profile.
    with ct.lock(shared / "runtime/unit_locks" / (task + ".lock")):
        if receipt.exists():
            raise FileExistsError("JobID receipt already exists")
        status, output = execute_science(pack, row, claim, ledger, shared, job)
        if ct.digest(os.environ["STATION_CAMPAIGN"]) != pack["campaign_sha256"]:
            raise ValueError("campaign changed during job")
        result = {"status": status, "stage": row["stage"], "task_id": task, "unit_id": row["unit_id"],
                  "job_id": job, "username": user, "assignment_version": claim["assignment_version"],
                  "campaign_identity": pack["campaign_identity"], "pack_identity": pack["identity"],
                  "code_sha": pack["code_sha"], "resource_profile": pack["resource_profile"],
                  "output": str(output.resolve()), "artifact": ct.file_stat(output), "output_sha256": ct.digest(output),
                  "wall_seconds": time.monotonic() - started}
        ct.atomic_json(receipt, result)
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pack", required=True)
    p.add_argument("--task", required=True)
    args = p.parse_args(argv)
    print(execute(args.pack, args.task)["status"])


if __name__ == "__main__":
    main()
