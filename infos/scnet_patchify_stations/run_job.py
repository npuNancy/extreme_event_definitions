#!/usr/bin/env python3
"""Pinned-code compute-node wrapper for one claimed station workflow task."""
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


def load_pack(path):
    pack = ct.read_json(path)
    identity = pack.pop("identity")
    if ct.fingerprint(pack) != identity:
        raise ValueError("job pack manifest identity mismatch")
    pack["identity"] = identity
    return pack


def valid_receipt(row, pack):
    receipt = ct.read_json(row["receipt"])
    if (receipt["job_id"] != row["job_id"] or receipt["task_id"] != row["task_id"]
            or receipt["pack_identity"] != pack["identity"] or receipt["code_sha"] != pack["code_sha"]
            or receipt["status"] not in ("COMPLETED", "SKIPPED_NO_STATIONS")
            or receipt["artifact"] != ct.file_stat(receipt["output"])
            or receipt["output_sha256"] != ct.digest(receipt["output"])):
        raise ValueError("dependency receipt identity/artifact mismatch")
    return receipt


def execute(pack_path, task):
    pack = load_pack(pack_path)
    config = pack["campaign"]
    row = next(r for r in pack["jobs"] if r["task_id"] == task)
    user = pwd.getpwuid(os.getuid()).pw_name
    if user not in {w["username"] for w in pack["workers"]}:
        raise ValueError("account is not a station worker")
    job = os.environ.get("SLURM_JOB_ID", "")
    if not re.fullmatch(r"\d+", job):
        raise ValueError("scientific workflow requires a Slurm compute job")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
    if head != pack["code_sha"] or dirty:
        raise ValueError("checkout must be clean at the pinned code SHA")
    if ct.digest(Path(pack_path).parent / row["script"]) != row["script_sha256"]:
        raise ValueError("job script changed")
    center = Path(config["aggregate_root"])
    ledger = ct.read_json(center / "runtime/ledger.json")
    if ledger["pack_identity"] != pack["identity"]:
        raise ValueError("ledger belongs to another job pack")
    claim = ledger["tasks"][task]
    # sbatch may start a job before the controller persists its returned Job ID.
    for _ in range(10):
        if claim.get("job_id") is not None:
            break
        time.sleep(1)
        ledger = ct.read_json(center / "runtime/ledger.json")
        claim = ledger["tasks"][task]
    if claim.get("job_id") != job or claim.get("username") != user:
        raise ValueError("job is not the current claimed task owner")
    root = Path(config["worker_root_template"].format(username=user))
    share = Path("/work/share") / user
    root.mkdir(parents=True, exist_ok=True)
    if not root.resolve().is_relative_to(share.resolve()):
        raise ValueError("worker output escapes own share directory")
    attempt = root / "attempts" / task / job
    attempt.mkdir(parents=True, exist_ok=True)
    for dependency in row["depends_on"]:
        d = next(r for r in pack["jobs"] if r["unit_id"] == dependency)
        state = ledger["tasks"][d["task_id"]]
        if state["classification"] != "succeeded":
            raise ValueError("dependency not successful")
        valid_receipt(state, pack)
    from grid_extreme_signals import station_pipeline as pipeline
    started = time.monotonic()
    if row["stage"] == "prepare":
        if ct.digest(config["input_index"]) != pack["input_index_sha256"]:
            raise ValueError("source index differs from generated pack")
        pipeline.prepare(config, attempt / "shared", head)
        output = attempt / "shared/prepared.json"
        result = {"status": "COMPLETED"}
    else:
        prep_task = next(r for r in pack["jobs"] if r["stage"] == "prepare")
        prep_receipt = valid_receipt(ledger["tasks"][prep_task["task_id"]], pack)
        if ct.digest(center / "runtime/prepared.json") != prep_receipt["output_sha256"]:
            raise ValueError("central prepared manifest differs from preparation receipt")
        prepared = ct.read_json(center / "runtime/prepared.json")
        if prepared["release"]["code_sha"] != head or prepared["release"]["campaign"] != config:
            raise ValueError("prepared release differs from current campaign/code")
        if row["stage"] == "extract":
            prior = {}
            for previous in claim.get("history", []):
                old_root = Path(config["worker_root_template"].format(username=previous["username"]))
                partial = old_root / "attempts" / task / previous["job_id"] / "outputs" / row["key"] / "extraction.json"
                if partial.exists():
                    saved = ct.read_json(partial)
                    if saved.get("release_identity") == prepared["identity"]:
                        for record in saved["outputs"]:
                            if ct.completed(record["artifact"]["path"], record["identity"]):
                                prior[record["period"]] = record
            result = pipeline.extract_combination(prepared, row["key"], attempt / "outputs", head, list(prior.values()))
            output = attempt / "extraction.json"
            ct.atomic_json(output, result)
        else:
            dep = next(r for r in pack["jobs"] if r["unit_id"] == row["depends_on"][0])
            receipt = valid_receipt(ledger["tasks"][dep["task_id"]], pack)
            output = attempt / "audit.json"
            result = pipeline.audit(prepared, row["key"], ct.read_json(receipt["output"]), output)
    receipt = {"status": result["status"], "task_id": task, "unit_id": row["unit_id"],
               "job_id": job, "username": user, "pack_identity": pack["identity"], "code_sha": head,
               "output": str(output.resolve()), "artifact": ct.file_stat(output), "output_sha256": ct.digest(output),
               "wall_seconds": time.monotonic() - started}
    ct.atomic_json(root / "runtime/receipts" / task / f"{job}.json", receipt)
    return receipt


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pack", required=True)
    p.add_argument("--task", required=True)
    a = p.parse_args(argv)
    print(execute(a.pack, a.task)["status"])


if __name__ == "__main__":
    main()
