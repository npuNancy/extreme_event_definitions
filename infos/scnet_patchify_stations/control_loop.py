#!/usr/bin/env python3
"""One submit/monitor/publish cycle, run on the aggregator with shared mounts.

Submission is opt-in. The caller schedules repeated cycles; no background daemon
or automatic job submission is started when generating a job pack.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
import pwd
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from grid_extreme_signals import station_contract as ct
from infos.scnet_patchify_stations.run_job import load_pack, valid_receipt


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def remote(host, argv):
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host,
                             shlex.join(argv)], capture_output=True, text=True, timeout=45, check=True)
    return result.stdout


def queue(worker):
    lines = remote(worker["host"], ["squeue", "-r", "-h", "-u", worker["username"], "-o", "%i|%T|%j"]).splitlines()
    result = {}
    for line in lines:
        if line.strip():
            job, state, name = line.strip().split("|", 2)
            result[job] = {"state": state, "name": name}
    return result


def guarded_submit(worker, argv, limit):
    """Share the account's BCSD lock as well as the controller's global lock."""
    script = '''set -euo pipefail
test "$(id -un)" = "$1"
exec 9>"$HOME/.bcsd_submit.lock"
if ! flock -x -w 5 9; then printf 'STATION_NO_SLOT\\n'; exit 0; fi
queue=$(squeue -r -h -u "$1" -o '%i')
count=$(printf '%s\\n' "$queue" | awk 'NF {n++} END {print n+0}')
if [ "$count" -ge "$2" ]; then printf 'STATION_NO_SLOT\\n'; exit 0; fi
shift 2
exec "$@"
'''
    answer = remote(worker["host"], ["bash", "-c", script, "station-submit",
                                     worker["username"], str(limit), *argv]).strip()
    if answer == "STATION_NO_SLOT":
        return None
    job = answer.split(";")[0]
    if not job.isdigit():
        raise ValueError("unrecognized sbatch response")
    return job


def accounting(worker, since):
    lines = remote(worker["host"], ["sacct", "-n", "-P", "-u", worker["username"], "-S", since,
                                  "--format=JobIDRaw,State,ExitCode,Elapsed,MaxRSS,JobName%100"]).splitlines()
    result = {}
    for line in lines:
        fields = line.split("|")
        if len(fields) >= 6 and fields[0]:
            job, state, exit_code, elapsed, rss, name = fields[:6]
            if "." in job:
                parent = job.split(".")[0]
                previous = result.setdefault(parent, {}).get("step_max_rss", "")
                if rss_bytes(rss) >= rss_bytes(previous):
                    result[parent]["step_max_rss"] = rss
                continue
            result.setdefault(job, {}).update(state=state.split()[0].rstrip("+"), exit_code=exit_code,
                                               elapsed=elapsed, max_rss=rss, name=name)
    return result


def rss_bytes(value):
    if not value:
        return 0
    suffix = value[-1].upper()
    return float(value[:-1]) * 1024 ** ("KMGTP".index(suffix) + 1) if suffix in "KMGTP" else float(value)


def initialize(pack):
    return {"pack_identity": pack["identity"], "started_at": now(), "tasks": {
        r["task_id"]: {"task_id": r["task_id"], "unit_id": r["unit_id"], "classification": "not_submitted",
                       "attempt": 0, "history": [], "job_id": None, "reason": "", "updated_at": now()}
        for r in pack["jobs"]}}


def ready_workers(pack, ledger, snapshots):
    """Keep an unavailable deployment from blocking other accounts' ready work."""
    config = pack["campaign"]
    ready = []
    ledger["deployment_errors"] = {}
    for user in snapshots:
        root = Path(config["worker_root_template"].format(username=user))
        try:
            if not (root / "logs").is_dir():
                raise ValueError(f"worker logs directory not prepared: {root}")
            if load_pack(root / "jobs" / config["resource_profile"] / "manifest.json")["identity"] != pack["identity"]:
                raise ValueError("worker job pack differs")
            ready.append(user)
        except (OSError, ValueError, KeyError) as exc:
            ledger["deployment_errors"][user] = str(exc)
    return ready


def classify(state, scheduler, receipt_ok, max_retries):
    if scheduler is None or "state" not in scheduler:
        return "unknown"
    status = scheduler["state"]
    if status == "COMPLETED":
        return "succeeded" if scheduler.get("exit_code") == "0:0" and receipt_ok else "incomplete_output"
    if status in ("NODE_FAIL", "PREEMPTED"):
        return "retryable" if state["attempt"] <= max_retries else "deterministic_failure"
    if status in ("OUT_OF_MEMORY", "TIMEOUT"):
        return "resource_failure"
    if status in ("FAILED", "CANCELLED", "BOOT_FAIL", "DEADLINE", "REVOKED"):
        return "deterministic_failure"
    return "unknown"


def retryable_startup_io(state, root, max_retries):
    tail = state.get("log_tail", "").rstrip()
    if (state["classification"] != "deterministic_failure" or state.get("scheduler_state") != "FAILED"
            or state.get("exit_code") != "1:0" or not 0 < state["attempt"] <= min(1, max_retries)
            or 'ledger = ct.read_json(center / "runtime/ledger.json")' not in tail
            or not tail.endswith("OSError: [Errno 5] Input/output error")):
        return False
    try:
        (Path(root) / "attempts" / state["task_id"] / state["job_id"]).stat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def progress(pack, ledger, directory):
    from zoneinfo import ZoneInfo
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    counts = dict(Counter(s["classification"] for s in ledger["tasks"].values()))
    lines = ["# 场站极端事件作业进度", "", f"Last checked: {stamp}", "",
             "Next check: " + (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(minutes=15)).isoformat(timespec="seconds"), "",
             "分类计数：" + str(counts), "",
             "账号查询异常：" + str(ledger.get("account_errors", {})), "",
             "部署未就绪：" + str(ledger.get("deployment_errors", {})), "",
             "本轮全局并发上限：" + str(ledger.get("submission_policy", {}).get("global_active_limit", pack["campaign"]["global_active_limit"])), "",
             "| Unit | Stage | Account | Job | State | Class | Attempt | Evidence | Reason | Next action |",
             "|---|---|---|---|---|---|---:|---|---|---|"]
    for row in pack["jobs"]:
        s = ledger["tasks"][row["task_id"]]
        action = {"not_submitted": "wait for dependencies/slots", "retryable": "bounded retry",
                  "succeeded": "release dependencies", "active": "monitor"}.get(s["classification"], "inspect evidence")
        values = [row["unit_id"], row["stage"], s.get("username", "-"), s.get("job_id", "-"),
                  s.get("scheduler_state", "-"), s["classification"], s["attempt"], s.get("receipt", "-"), s.get("reason", ""), action]
        lines.append("| " + " | ".join(str(v).replace("|", "/").replace("\n", " ") for v in values) + " |")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    ct.atomic_json(directory / "ledger.json", ledger)
    tmp = ct.temporary(directory / "progress.md")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, directory / "progress.md")


def _publish(pack, ledger, center):
    from grid_extreme_signals.station_pipeline import publish
    audits = {}
    for row in pack["jobs"]:
        if row["stage"] == "audit":
            receipt = valid_receipt(ledger["tasks"][row["task_id"]], pack)
            audits[row["key"]] = receipt["output"]
    prepared = ct.read_json(center / "runtime/prepared.json")
    index = publish(prepared, audits, center)
    for key, c in index["combinations"].items():
        period = pack["campaign"]["analysis_years"]
        links = [(Path(c["audit"]["path"]), center / "outputs" / key / f"audit_{period}.json")]
        for record in c["outputs"]:
            src = Path(record["artifact"]["path"])
            dest = center / "outputs" / key / src.name
            links.extend([(src, dest), (Path(str(src) + ".json"), Path(str(dest) + ".json"))])
        for src, dest in links:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.is_symlink() and dest.resolve() == src.resolve():
                continue
            if dest.exists() or dest.is_symlink():
                raise FileExistsError(f"conflicting publication link: {dest}")
            dest.symlink_to(src)
    ledger["published_at"] = now()


def cycle(pack, status_dir, submit=False, submit_lock=None, global_active_limit=None):
    config = pack["campaign"]
    active_limit = config["global_active_limit"] if global_active_limit is None else global_active_limit
    maximum = config["account_active_limit"] * len(pack["workers"])
    if type(active_limit) is not int or not 1 <= active_limit <= maximum:
        raise ValueError(f"global active limit must be an integer between 1 and {maximum}")
    center = Path(config["aggregate_root"])
    if pwd.getpwuid(os.getuid()).pw_name != pack["aggregator"]["username"]:
        raise ValueError("run controller on the aggregator account with shared mounts")
    if submit and not submit_lock:
        raise ValueError("--submit requires --submit-lock pointing to the established cross-project shared lock")
    ledger_path = center / "runtime/ledger.json"
    workers = {w["username"]: w for w in pack["workers"]}
    unit_lookup = {r["unit_id"]: r["task_id"] for r in pack["jobs"]}
    with ct.lock(center / "runtime/.controller.lock"):
        ledger = ct.read_json(ledger_path) if ledger_path.exists() else initialize(pack)
        if ledger["pack_identity"] != pack["identity"]:
            raise ValueError("existing ledger belongs to another immutable job pack")
        ledger["submission_policy"] = {"global_active_limit": active_limit,
                                       "controller_sha256": ct.digest(__file__)}
        snapshots, accounts = {}, {}
        for user, worker in workers.items():
            try:
                snapshots[user] = queue(worker)
                since = (datetime.fromisoformat(ledger["started_at"]) - timedelta(days=1)).date().isoformat()
                accounts[user] = accounting(worker, since)
                ledger.setdefault("account_errors", {}).pop(user, None)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                snapshots.pop(user, None)
                ledger.setdefault("account_errors", {})[user] = str(exc)
        try:
            for row in pack["jobs"]:
                s = ledger["tasks"][row["task_id"]]
                user = s.get("username")
                if not user or s["classification"] == "succeeded":
                    continue
                s["updated_at"] = now()
                if user not in snapshots:
                    s.update(classification="unknown", reason="account scheduler query failed")
                    continue
                if not s.get("job_id"):
                    history = {h["job_id"] for h in s["history"]}
                    candidates = {j for j, v in {**accounts[user], **snapshots[user]}.items()
                                  if v.get("name") == row["job_name"] and j not in history}
                    if len(candidates) == 1:
                        s["job_id"] = candidates.pop()
                    else:
                        s.update(classification="unknown", reason="submission receipt unresolved; do not duplicate")
                        continue
                job = s["job_id"]
                root = Path(config["worker_root_template"].format(username=user))
                s["receipt"] = str(root / "runtime/receipts" / row["task_id"] / f"{job}.json")
                if job in snapshots[user]:
                    s.update(classification="active", scheduler_state=snapshots[user][job]["state"], reason="")
                    continue
                facts = accounts[user].get(job)
                receipt = None
                try:
                    receipt = valid_receipt(s, pack)
                    s["reason"] = ""
                except (OSError, ValueError, KeyError) as exc:
                    s["reason"] = str(exc)
                s["classification"] = classify(s, facts, receipt is not None, config["max_retries"])
                if facts:
                    s.update(scheduler_state=facts.get("state", "unknown"), exit_code=facts.get("exit_code", ""),
                             elapsed=facts.get("elapsed", ""), max_rss=facts.get("max_rss") or facts.get("step_max_rss", ""))
                if s["classification"] == "succeeded" and row["stage"] == "prepare":
                    ct.atomic_json(center / "runtime/prepared.json", ct.read_json(receipt["output"]))
                if s["classification"] not in ("succeeded", "active"):
                    try:
                        log = root / "logs" / f"{row['job_name']}-{job}.err"
                        with log.open("rb") as stream:
                            stream.seek(max(0, log.stat().st_size - 4096))
                            s["log_tail"] = stream.read().decode(errors="replace")
                    except OSError:
                        pass
                    if retryable_startup_io(s, root, config["max_retries"]):
                        s.update(classification="retryable", reason="startup ledger read EIO; one retry before any output")
            ct.atomic_json(ledger_path, ledger)
            if submit:
                ready = ready_workers(pack, ledger, snapshots)
                with ct.lock(submit_lock):
                    active = sum(s["classification"] in ("active", "submitting", "unknown") for s in ledger["tasks"].values())
                    for row in pack["jobs"]:
                        if active >= active_limit:
                            break
                        s = ledger["tasks"][row["task_id"]]
                        if s["classification"] not in ("not_submitted", "retryable"):
                            continue
                        if any(ledger["tasks"][unit_lookup[d]]["classification"] != "succeeded" for d in row["depends_on"]):
                            continue
                        eligible = sorted(ready, key=lambda u: (len(snapshots[u]), u != row["logical_owner"], u))
                        user = next((u for u in eligible if len(snapshots[u]) < config["account_active_limit"]), None)
                        if user is None:
                            break
                        worker = workers[user]
                        current = queue(worker)  # Account-wide recheck while holding the cross-project lock.
                        snapshots[user] = current
                        if len(current) >= config["account_active_limit"]:
                            continue
                        root = Path(config["worker_root_template"].format(username=user))
                        pack_dir = root / "jobs" / config["resource_profile"]
                        if load_pack(pack_dir / "manifest.json")["identity"] != pack["identity"]:
                            raise ValueError("worker job pack is missing or differs")
                        script = pack_dir / row["script"]
                        if ct.digest(script) != row["script_sha256"]:
                            raise ValueError("worker script checksum mismatch")
                        # Directory permissions are established in deployment, not changed by this controller.
                        if not (root / "logs").is_dir():
                            raise ValueError(f"worker logs directory not prepared: {root}")
                        previous_state = dict(s, history=list(s["history"]))
                        if s.get("job_id"):
                            s["history"].append({"job_id": s["job_id"], "username": s["username"]})
                        s.update(classification="submitting", username=user, job_id=None, attempt=s["attempt"] + 1,
                                 submitted_at=now(), updated_at=now())
                        ct.atomic_json(ledger_path, ledger)
                        repo = config["repository_template"].format(username=user)
                        argv = ["env", f"STATION_REPO={repo}", f"STATION_JOB_PACK={pack_dir}",
                                "sbatch", "--parsable", "-A", user, f"--chdir={root}", "--export=ALL", str(script)]
                        try:
                            answer = guarded_submit(worker, argv, config["account_active_limit"])
                            if answer is None:
                                s.clear()
                                s.update(previous_state, reason="account lock busy or slots filled before submission")
                                ct.atomic_json(ledger_path, ledger)
                                continue
                            s.update(job_id=answer, classification="active", scheduler_state="SUBMITTED")
                            snapshots[user][answer] = {"state": "SUBMITTED", "name": row["job_name"]}
                        except (OSError, ValueError, subprocess.SubprocessError) as exc:
                            s.update(classification="unknown", reason=f"submission uncertain: {exc}")
                        active += 1
                        ct.atomic_json(ledger_path, ledger)
            if all(s["classification"] == "succeeded" for s in ledger["tasks"].values()) and "published_at" not in ledger:
                _publish(pack, ledger, center)
        finally:
            ledger["checked_at"] = now()
            ct.atomic_json(ledger_path, ledger)
            progress(pack, ledger, status_dir)
        return ledger


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pack", required=True)
    p.add_argument("--status-dir", default=str(Path(__file__).with_name("completion_status")))
    p.add_argument("--submit", action="store_true")
    p.add_argument("--submit-lock", help="Existing shared lock coordinated across all projects/accounts")
    p.add_argument("--global-active-limit", type=int,
                   help="Scheduling limit for this cycle; defaults to the frozen campaign value")
    a = p.parse_args(argv)
    ledger = cycle(load_pack(a.pack), a.status_dir, a.submit, a.submit_lock, a.global_active_limit)
    print(dict(Counter(s["classification"] for s in ledger["tasks"].values())))


if __name__ == "__main__":
    main()
