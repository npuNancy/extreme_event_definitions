#!/usr/bin/env python3
"""Step2 E1/E2 本地监控器共享实现。"""
from __future__ import annotations

import argparse
import csv
import fcntl
import json
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path

ACTIVE_STATES = {"PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "SUSPENDED"}
FAILED_STATES = {
    "FAILED",
    "OUT_OF_MEMORY",
    "TIMEOUT",
    "CANCELLED",
    "NODE_FAIL",
    "PREEMPTED",
    "BOOT_FAIL",
}


def build_parser(stage: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"本地监控 step2 {stage} 作业；默认补充提交本阶段未提交单元。"
    )
    parser.add_argument("--server", required=True, help="本地 SSH Host/别名。")
    parser.add_argument(
        "--remote-manifest",
        required=True,
        help="远端生成器写出的 campaign manifest 路径。",
    )
    parser.add_argument("--history-start", required=True, help="sacct 起始时间，如 2026-07-22。")
    parser.add_argument("--max-active-jobs", type=int, default=20)
    parser.add_argument("--interval", type=int, default=3600)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--no-submit",
        action="store_true",
        help="只检查并记录，不补充提交作业。",
    )
    parser.add_argument("--ssh-timeout", type=int, default=60)
    return parser


def _remote(
    server: str,
    command: str,
    *,
    timeout: int,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", server, command],
        check=check,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _remote_home(server: str, *, timeout: int) -> str:
    result = _remote(server, "printf '%s' \"$HOME\"", timeout=timeout)
    home = result.stdout.strip()
    if not home.startswith("/"):
        raise RuntimeError(f"无法解析 {server} 的远端 HOME：{home!r}")
    return home


def _resolve_remote_path(path: str, remote_home: str) -> str:
    if path == "~":
        return remote_home
    if path.startswith("~/"):
        return remote_home + path[1:]
    if not path.startswith("/"):
        raise ValueError("--remote-manifest 必须为绝对路径或 ~/... 路径")
    return path


def _load_manifest(server: str, path: str, *, timeout: int, stage: str) -> dict:
    result = _remote(server, f"cat {shlex.quote(path)}", timeout=timeout)
    manifest = json.loads(result.stdout)
    if manifest.get("stage") != stage:
        raise ValueError(
            f"manifest stage={manifest.get('stage')!r}，监控器要求 {stage!r}"
        )
    units = manifest.get("units")
    if not isinstance(units, list) or not units:
        raise ValueError("manifest units 不能为空")
    names = [unit.get("job_name") for unit in units]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise ValueError("manifest job_name 缺失或重复")
    return manifest


def _scheduler_state(value: str) -> str:
    state = value.split("+", 1)[0].strip().upper()
    if state.startswith("CANCELLED"):
        return "CANCELLED"
    return state


def _queue_records(server: str, *, timeout: int) -> dict[str, dict[str, str]]:
    result = _remote(
        server,
        "squeue -h -u \"$USER\" -o '%i|%j|%T'",
        timeout=timeout,
    )
    records: dict[str, dict[str, str]] = {}
    for line in result.stdout.splitlines():
        fields = line.split("|", 2)
        if len(fields) == 3:
            records[fields[1]] = {
                "job_id": fields[0],
                "job_name": fields[1],
                "state": _scheduler_state(fields[2]),
            }
    return records


def _accounting_records(
    server: str,
    history_start: str,
    *,
    timeout: int,
) -> dict[str, dict[str, str]]:
    command = (
        "sacct -n -P -u \"$USER\" "
        f"-S {shlex.quote(history_start)} -E now "
        "-o 'JobIDRaw,JobName%128,State,ExitCode,AllocCPUS,ReqMem,Elapsed,CPUTimeRAW,MaxRSS'"
    )
    result = _remote(server, command, timeout=timeout)
    latest: dict[str, dict[str, str]] = {}
    rows_by_id: dict[str, dict[str, str]] = {}
    step_max_rss: dict[str, str] = {}
    fields_out = (
        "job_id",
        "job_name",
        "state",
        "exit_code",
        "cpus",
        "req_mem",
        "elapsed",
        "cpu_time_raw",
        "max_rss",
    )
    for line in result.stdout.splitlines():
        fields = line.split("|")
        if len(fields) >= len(fields_out) and fields[1]:
            row = dict(zip(fields_out, fields[: len(fields_out)]))
            row["state"] = _scheduler_state(row["state"])
            if "." in row["job_id"]:
                base_id = row["job_id"].split(".", 1)[0]
                if row["max_rss"]:
                    step_max_rss[base_id] = row["max_rss"]
                continue
            latest[row["job_name"]] = row
            rows_by_id[row["job_id"]] = row
    for job_id, max_rss in step_max_rss.items():
        if job_id in rows_by_id:
            rows_by_id[job_id]["max_rss"] = max_rss
    return latest


def _stat_nonempty(
    server: str,
    paths_by_key: dict[str, str],
    *,
    timeout: int,
) -> dict[str, bool]:
    lines = ["set -u"]
    for key, path in paths_by_key.items():
        lines.append(
            f"if [ -s {shlex.quote(path)} ]; then "
            f"printf '%s\\t1\\n' {shlex.quote(key)}; "
            f"else printf '%s\\t0\\n' {shlex.quote(key)}; fi"
        )
    result = _remote(server, "\n".join(lines), timeout=timeout)
    status = {key: False for key in paths_by_key}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("\t")
        if sep and key in status:
            status[key] = value == "1"
    return status


def classify_unit(
    *,
    stage: str,
    output_exists: bool,
    queue_record: dict[str, str] | None,
    accounting_record: dict[str, str] | None,
    previous: dict | None,
) -> tuple[str, str]:
    """按调度状态和非空文件证据分类一个单元。"""
    if queue_record and queue_record["state"] in ACTIVE_STATES:
        return "ACTIVE", queue_record["state"]
    if accounting_record:
        state = accounting_record["state"]
        if state in ACTIVE_STATES:
            return "ACTIVE", state
        if state in FAILED_STATES:
            return "FAILED", state
        if state == "COMPLETED":
            if output_exists:
                return "SUCCEEDED", "COMPLETED + nonempty output"
            return "INCOMPLETE_OUTPUT", "COMPLETED but output missing/empty"
        return "UNKNOWN", state
    if previous and previous.get("job_id"):
        if previous.get("classification") in {"SUCCEEDED", "SUCCEEDED_RECORDED"} and output_exists:
            return "SUCCEEDED_RECORDED", "local snapshot + nonempty output"
        return "UNKNOWN_HISTORY", "已有提交记录但 sacct/squeue 无记录"
    if stage == "E1" and output_exists:
        return "SUCCEEDED_PREEXISTING", "nonempty output (no scheduler record)"
    return "NOT_SUBMITTED", "no scheduler record"


def _submit_one(
    *,
    server: str,
    script_path: str,
    workflow_prefix: str,
    max_active_jobs: int,
    timeout: int,
) -> tuple[str | None, str]:
    script = f"""set -euo pipefail
exec 9>\"$HOME/.bcsd_submit.lock\"
flock -n 9 || exit 75
account_active=$(squeue -h -u \"$USER\" -o '%i' | wc -l)
workflow_active=$(squeue -h -u \"$USER\" -o '%j' | awk -v p={shlex.quote(workflow_prefix)} 'index($0,p)==1 {{n++}} END {{print n+0}}')
if [ \"$account_active\" -ge 20 ]; then exit 76; fi
if [ \"$workflow_active\" -ge {max_active_jobs} ]; then exit 77; fi
sbatch --parsable {shlex.quote(script_path)}
"""
    command = f"bash -lc {shlex.quote(script)}"
    result = _remote(server, command, timeout=timeout, check=False)
    if result.returncode == 0:
        job_id = result.stdout.strip().split(";", 1)[0]
        if not job_id:
            raise RuntimeError(f"提交 {script_path} 后未返回 Job ID")
        return job_id, "submitted"
    reasons = {
        75: "submit lock busy",
        76: "account active-job cap reached",
        77: "workflow active-job cap reached",
    }
    if result.returncode in reasons:
        return None, reasons[result.returncode]
    detail = (result.stderr or result.stdout).strip()
    raise RuntimeError(f"sbatch 失败（exit={result.returncode}）：{detail}")


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def _write_completion(path: Path, rows: list[dict[str, str]]) -> None:
    fields = [
        "observed_at",
        "server",
        "stage",
        "unit_id",
        "model",
        "region",
        "scenario",
        "tech",
        "years",
        "job_name",
        "job_id",
        "scheduler_state",
        "exit_code",
        "elapsed",
        "cpus",
        "req_mem",
        "max_rss",
        "output_nonempty",
        "classification",
        "reason",
        "next_action",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def _append_usage(path: Path, *, server: str, stage: str, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    seconds = sum(
        int(record.get("cpu_time_raw", "0"))
        for record in records
        if str(record.get("cpu_time_raw", "")).isdigit()
    )
    states = [_scheduler_state(str(record.get("state", ""))) for record in records]
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow(
                [
                    "observed_at",
                    "server",
                    "stage",
                    "pending",
                    "running",
                    "completed",
                    "failed",
                    "core_hours",
                ]
            )
        writer.writerow(
            [
                datetime.now().astimezone().isoformat(),
                server,
                stage,
                states.count("PENDING"),
                states.count("RUNNING"),
                states.count("COMPLETED"),
                sum(state in FAILED_STATES for state in states),
                f"{seconds / 3600:.3f}",
            ]
        )


def _safe_file_token(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in value)


def monitor_once(
    *,
    stage: str,
    state_dir: Path,
    args: argparse.Namespace,
    manifest_path: str,
    manifest: dict,
) -> dict:
    campaign = manifest["campaign_id"]
    token = f"{_safe_file_token(args.server)}_{campaign}"
    snapshot_path = state_dir / f"latest_snapshot_{token}.json"
    snapshot = (
        json.loads(snapshot_path.read_text(encoding="utf-8"))
        if snapshot_path.exists()
        else {"units": {}}
    )
    queue = _queue_records(args.server, timeout=args.ssh_timeout)
    accounting = _accounting_records(
        args.server, args.history_start, timeout=args.ssh_timeout
    )
    units = manifest["units"]
    outputs = _stat_nonempty(
        args.server,
        {unit["unit_id"]: unit["expected_output"] for unit in units},
        timeout=args.ssh_timeout,
    )
    e1_ready = True
    if stage == "E2":
        prerequisites = _stat_nonempty(
            args.server,
            {unit["unit_id"]: unit["e1_expected_output"] for unit in units},
            timeout=args.ssh_timeout,
        )
        e1_ready = all(prerequisites.values())

    observed = datetime.now().astimezone().isoformat()
    current: dict[str, dict] = {}
    for unit in units:
        name = unit["job_name"]
        previous = snapshot.get("units", {}).get(unit["unit_id"])
        classification, reason = classify_unit(
            stage=stage,
            output_exists=outputs[unit["unit_id"]],
            queue_record=queue.get(name),
            accounting_record=accounting.get(name),
            previous=previous,
        )
        scheduler = queue.get(name) or accounting.get(name) or {}
        current[unit["unit_id"]] = {
            **unit,
            "job_id": scheduler.get("job_id", (previous or {}).get("job_id", "")),
            "scheduler_state": scheduler.get("state", ""),
            "exit_code": scheduler.get("exit_code", ""),
            "elapsed": scheduler.get("elapsed", ""),
            "cpus": scheduler.get("cpus", ""),
            "req_mem": scheduler.get("req_mem", ""),
            "max_rss": scheduler.get("max_rss", ""),
            "output_nonempty": outputs[unit["unit_id"]],
            "classification": classification,
            "reason": reason,
            "observed_at": observed,
        }

    stop_reason = ""
    if not args.no_submit and (stage != "E2" or e1_ready):
        for unit in units:
            record = current[unit["unit_id"]]
            if record["classification"] != "NOT_SUBMITTED":
                continue
            job_id, submit_reason = _submit_one(
                server=args.server,
                script_path=unit["script_path"],
                workflow_prefix=manifest["job_prefix"],
                max_active_jobs=args.max_active_jobs,
                timeout=args.ssh_timeout,
            )
            if not job_id:
                stop_reason = submit_reason
                break
            record.update(
                {
                    "job_id": job_id,
                    "scheduler_state": "SUBMITTED",
                    "classification": "ACTIVE",
                    "reason": "submitted by monitor",
                }
            )
            snapshot.update(
                {
                    "server": args.server,
                    "stage": stage,
                    "campaign_id": campaign,
                    "remote_manifest": manifest_path,
                    "units": current,
                    "updated_at": datetime.now().astimezone().isoformat(),
                }
            )
            _atomic_json(snapshot_path, snapshot)
    elif stage == "E2" and not e1_ready:
        stop_reason = "E2 gate closed: not all E1 outputs are nonempty"

    rows: list[dict[str, str]] = []
    for record in current.values():
        classification = record["classification"]
        if classification == "NOT_SUBMITTED":
            next_action = stop_reason or "submit when a slot is available"
        elif classification in {"FAILED", "INCOMPLETE_OUTPUT", "UNKNOWN", "UNKNOWN_HISTORY"}:
            next_action = "Agent inspect logs/accounting; no automatic retry"
        elif classification == "ACTIVE":
            next_action = "monitor"
        else:
            next_action = "none"
        rows.append(
            {
                key: str(value)
                for key, value in {
                    "observed_at": observed,
                    "server": args.server,
                    "stage": stage,
                    "unit_id": record["unit_id"],
                    "model": record["model"],
                    "region": record["region"],
                    "scenario": record["scenario"],
                    "tech": record["tech"],
                    "years": record["years"],
                    "job_name": record["job_name"],
                    "job_id": record["job_id"],
                    "scheduler_state": record["scheduler_state"],
                    "exit_code": record["exit_code"],
                    "elapsed": record["elapsed"],
                    "cpus": record["cpus"],
                    "req_mem": record["req_mem"],
                    "max_rss": record["max_rss"],
                    "output_nonempty": record["output_nonempty"],
                    "classification": classification,
                    "reason": record["reason"],
                    "next_action": next_action,
                }.items()
            }
        )

    snapshot.update(
        {
            "server": args.server,
            "stage": stage,
            "campaign_id": campaign,
            "remote_manifest": manifest_path,
            "e1_gate_ready": e1_ready,
            "stop_reason": stop_reason,
            "units": current,
            "updated_at": observed,
        }
    )
    _atomic_json(snapshot_path, snapshot)
    _write_completion(state_dir / f"completion_{token}.csv", rows)
    relevant = [accounting[name] for name in {u["job_name"] for u in units} if name in accounting]
    _append_usage(
        state_dir / f"usage_{token}.csv",
        server=args.server,
        stage=stage,
        records=relevant,
    )
    counts: dict[str, int] = {}
    for record in current.values():
        key = record["classification"]
        counts[key] = counts.get(key, 0) + 1
    print(
        json.dumps(
            {
                "server": args.server,
                "stage": stage,
                "campaign_id": campaign,
                "counts": counts,
                "e1_gate_ready": e1_ready,
                "stop_reason": stop_reason,
                "snapshot": str(snapshot_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return snapshot


def run_monitor(stage: str, state_dir: Path, args: argparse.Namespace) -> None:
    if not 1 <= args.max_active_jobs <= 20:
        raise ValueError("--max-active-jobs 必须在 1..20 范围内")
    if args.interval < 1:
        raise ValueError("--interval 必须为正整数")
    if args.ssh_timeout < 1:
        raise ValueError("--ssh-timeout 必须为正整数")
    state_dir.mkdir(parents=True, exist_ok=True)
    remote_home = _remote_home(args.server, timeout=args.ssh_timeout)
    manifest_path = _resolve_remote_path(args.remote_manifest, remote_home)
    manifest = _load_manifest(
        args.server,
        manifest_path,
        timeout=args.ssh_timeout,
        stage=stage,
    )
    lock_token = _safe_file_token(f"{args.server}_{manifest['campaign_id']}")
    lock_path = state_dir / f"monitor_{lock_token}.lock"
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit(
                f"已有 step2 {stage} 监控器在处理 {args.server}/{manifest['campaign_id']}"
            ) from exc
        while True:
            manifest = _load_manifest(
                args.server,
                manifest_path,
                timeout=args.ssh_timeout,
                stage=stage,
            )
            monitor_once(
                stage=stage,
                state_dir=state_dir,
                args=args,
                manifest_path=manifest_path,
                manifest=manifest,
            )
            if args.once:
                break
            now = time.time()
            next_boundary = (int(now) // args.interval + 1) * args.interval
            time.sleep(max(0.0, next_boundary - now))
