#!/usr/bin/env python3
"""监控昆山 step1 作业，并幂等提交 E2b/E3。"""
from __future__ import annotations

import argparse
import csv
import fcntl
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from infos.hpc_step1.create_jobs_kunshan import SERVER_CONFIG
from scripts.hpc_step1_common import write_json_atomic

STATE_DIR = _PROJECT_ROOT / "infos/hpc_step1/completion_status"
FAILED_STATES = {"FAILED", "CANCELLED", "OUT_OF_MEMORY", "TIMEOUT", "NODE_FAIL"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="监控昆山 step1 作业。")
    parser.add_argument(
        "--server",
        choices=["all", *sorted(SERVER_CONFIG)],
        default="all",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=3600)
    parser.add_argument("--history-start", required=True)
    return parser


def _remote(server: str, command: str, *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", server, command],
        check=check,
        text=True,
        capture_output=True,
    )


def _records(server: str, history_start: str) -> list[dict[str, str]]:
    command = (
        "sacct -X -n -P "
        f"-u \"$USER\" -S {history_start} -E now "
        "-o 'JobIDRaw,JobName%80,State,ExitCode,AllocCPUS,ReqMem,Elapsed,CPUTimeRAW'"
    )
    result = _remote(server, command)
    rows = []
    for line in result.stdout.splitlines():
        fields = line.split("|")
        if len(fields) >= 8 and fields[1].startswith("step1_"):
            rows.append(dict(zip(
                ("job_id", "job_name", "state", "exit_code", "cpus", "req_mem",
                 "elapsed", "cpu_time_raw"),
                fields[:8],
            )))
    return rows


def _queue_count(server: str) -> int:
    result = _remote(server, "squeue -h -u \"$USER\" -o '%i'")
    return len([line for line in result.stdout.splitlines() if line.strip()])


def _validate(server: str, stage: str, tech: str, project: str) -> tuple[bool, str]:
    command = (
        f"cd {project} && source {SERVER_CONFIG[server]['activate']} && "
        f"python scripts/hpc_step1_validate_thresholds.py --stage {stage} "
        f"--tech {tech} --project_dir {project}"
    )
    result = _remote(server, command, check=False)
    text = (result.stdout or result.stderr).strip().splitlines()
    return result.returncode == 0, text[-1] if text else ""


def _matching(records: list[dict[str, str]], job_name: str) -> list[dict[str, str]]:
    return [row for row in records if row["job_name"] == job_name]


def _latest_by_job_name(records: list[dict[str, str]]) -> list[dict[str, str]]:
    """保留 sacct 返回的每个作业名最新一次记录。"""
    latest: dict[str, dict[str, str]] = {}
    for row in records:
        latest[row["job_name"]] = row
    return list(latest.values())


def _submit_once(
    *,
    server: str,
    stage: str,
    tech: str,
    project: str,
    records: list[dict[str, str]],
    snapshot: dict,
) -> str | None:
    key = f"{server}:{tech}:{stage}"
    job_name = f"step1_{stage}_{tech}"
    if snapshot.get("submissions", {}).get(key):
        return None
    existing = _matching(records, job_name)
    if existing:
        snapshot.setdefault("submissions", {})[key] = existing[-1]["job_id"]
        return None
    queue = _remote(
        server,
        f"squeue -h -u \"$USER\" -n {job_name} -o '%i|%T'",
    ).stdout.strip()
    if queue:
        snapshot.setdefault("submissions", {})[key] = queue.split("|", 1)[0]
        return None
    valid, _ = _validate(server, stage, tech, project)
    if valid:
        snapshot.setdefault("valid_outputs", {})[key] = True
        return None
    if _queue_count(server) + 1 > 20:
        return None
    script = f"{project}/jobs/step1_low_resource/job_{job_name}.sh"
    result = _remote(server, f"sbatch --parsable {script}")
    job_id = result.stdout.strip().split(";", 1)[0]
    if not job_id:
        raise RuntimeError(f"{server} 提交 {stage} 后没有返回 Job ID")
    snapshot.setdefault("submissions", {})[key] = job_id
    snapshot["updated_at"] = datetime.now().astimezone().isoformat()
    write_json_atomic(STATE_DIR / "latest_snapshot.json", snapshot)
    return job_id


def _append_completion(
    server: str,
    tech: str,
    records: list[dict[str, str]],
    validations: dict[str, tuple[bool, str]],
) -> None:
    path = STATE_DIR / f"completion_{server}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow([
                "更新时间", "服务器", "阶段", "技术类型", "年月范围", "作业ID",
                "Slurm状态", "输出状态", "输出路径", "缓存路径", "运行时长",
                "分配核数", "申请内存GB", "峰值内存GB", "日志路径", "备注",
            ])
        now = datetime.now().astimezone().isoformat()
        for stage in ("E2a", "E2b", "E3"):
            prefix = f"step1_{stage}_{tech}"
            stage_rows = [row for row in records if row["job_name"].startswith(prefix)]
            valid, detail = validations[stage]
            if stage_rows:
                row = stage_rows[-1]
                state, job_id = row["state"], row["job_id"]
            else:
                state, job_id = "NOT_STARTED", ""
            writer.writerow([
                now, server, stage, tech, "", job_id, state,
                "VALID" if valid else state, "", "", row.get("elapsed", "") if stage_rows else "",
                row.get("cpus", "") if stage_rows else "",
                row.get("req_mem", "") if stage_rows else "", "", "", detail,
            ])


def _append_usage(server: str, history_start: str, records: list[dict[str, str]]) -> None:
    path = STATE_DIR / f"usage_{server}.csv"
    exists = path.exists()
    seconds = sum(
        int(row["cpu_time_raw"])
        for row in records
        if row["cpu_time_raw"].isdigit()
    )
    states = [row["state"].split("+", 1)[0] for row in records]
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow([
                "检查时间", "服务器", "统计起始日期", "等待作业数", "运行作业数",
                "完成作业数", "失败作业数", "累计核时", "备注",
            ])
        writer.writerow([
            datetime.now().astimezone().isoformat(), server, history_start,
            states.count("PENDING"), states.count("RUNNING"), states.count("COMPLETED"),
            sum(state in FAILED_STATES for state in states), f"{seconds / 3600:.3f}", "",
        ])


def monitor_server(server: str, history_start: str, snapshot: dict) -> None:
    config = SERVER_CONFIG[server]
    tech = config["tech"]
    project = config["project_dir"]
    records = _records(server, history_start)
    validations = {
        stage: _validate(server, stage, tech, project)
        for stage in ("E2a", "E2b", "E3")
    }
    e2a_failed = any(
        row["state"].split("+", 1)[0] in FAILED_STATES
        for row in _latest_by_job_name(records)
        if row["job_name"].startswith(f"step1_E2a_{tech}")
    )
    if validations["E2a"][0] and not e2a_failed:
        _submit_once(
            server=server, stage="E2b", tech=tech, project=project,
            records=records, snapshot=snapshot,
        )
    e2b_rows = _matching(records, f"step1_E2b_{tech}")
    e2b_failed = bool(
        e2b_rows and e2b_rows[-1]["state"].split("+", 1)[0] in FAILED_STATES
    )
    if validations["E2b"][0] and not e2b_failed:
        _submit_once(
            server=server, stage="E3", tech=tech, project=project,
            records=records, snapshot=snapshot,
        )
    _append_completion(server, tech, records, validations)
    _append_usage(server, history_start, records)


def _seconds_until_next_boundary(interval: int) -> float:
    if interval < 1:
        raise ValueError("--interval 必须为正整数")
    now = datetime.now()
    epoch = now.timestamp()
    next_epoch = (int(epoch) // interval + 1) * interval
    return max(0.0, next_epoch - epoch)


def main() -> None:
    args = build_parser().parse_args()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_DIR / "monitor.lock"
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("已有 monitor_step1_jobs.py 实例正在运行")
        state_path = STATE_DIR / "latest_snapshot.json"
        snapshot = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        servers = sorted(SERVER_CONFIG) if args.server == "all" else [args.server]
        while True:
            for server in servers:
                monitor_server(server, args.history_start, snapshot)
            snapshot["updated_at"] = datetime.now().astimezone().isoformat()
            write_json_atomic(state_path, snapshot)
            if args.once:
                break
            time.sleep(_seconds_until_next_boundary(args.interval))


if __name__ == "__main__":
    main()
