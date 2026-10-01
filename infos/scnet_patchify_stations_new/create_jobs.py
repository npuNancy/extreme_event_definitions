#!/usr/bin/env python3
"""Generate complete, account-independent station-event Slurm packs; never submit."""
from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
from pathlib import Path
import re
import shlex
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
from grid_extreme_signals import station_contract as ct

AGGREGATOR = "acjpoxgsdu"
PREFIX = "station-events-new"


def catalog():
    with (HERE / "accounts.csv").open(encoding="utf-8-sig", newline="") as f:
        accounts = list(csv.DictReader(f))
    with (HERE / "作业分工/patch_assignment.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    workers = [r for r in accounts if r["role"] == "worker"]
    users = {r["username"] for r in workers}
    if (len(accounts) != 15 or len(workers) != 14 or len(users) != 14 or AGGREGATOR in users
            or [r["username"] for r in accounts if r["role"] == "aggregator"] != [AGGREGATOR]
            or len({r["host"] for r in accounts}) != 15):
        raise ValueError("expected fourteen workers and aggregator acjpoxgsdu")
    hosts = {r["username"]: r["host"] for r in workers}
    if len(rows) != 47 or len({r["patch_id"] for r in rows}) != 47:
        raise ValueError("expected exactly 47 unique patches")
    for r in rows:
        if (not re.fullmatch(r"R\d{2}C\d{2}", r["patch_id"]) or r["username"] not in users
                or r["host"] != hosts[r["username"]] or int(r["land_points_reference"]) < 0):
            raise ValueError("invalid patch assignment")
    return workers, {r["patch_id"]: r for r in rows}


def task_id(unit_id):
    return hashlib.sha256(unit_id.encode()).hexdigest()[:24]


def configuration(path):
    c = ct.read_json(path)
    ct.safe_name(c["run_id"])
    if (c["models"] != list(ct.MODELS) or c["analysis_years"] != "2015-2060"
            or set(c["stations"]) != set(ct.SCENARIOS) or c.get("bcsd_spot_check_accepted") is not True):
        raise ValueError("require four models, three canonical Station scenarios, 2015-2060 and accepted BCSD check")
    expected = Path("/work/home") / AGGREGATOR / "extreme_stations_new" / c["run_id"]
    if Path(c["aggregate_root"]) != expected:
        raise ValueError("aggregate_root must be a new run under aggregator home/extreme_stations_new")
    for value in (c["input_index"], c["patch_manifest"], *c["stations"].values()):
        if not Path(value).is_absolute() or "<" in value or "\n" in value:
            raise ValueError("input paths must be actual absolute paths")
    e = c["extraction"]
    if (set(e) != {"time_chunk", "station_chunk", "complevel"}
            or any(type(v) is not int for v in e.values()) or min(e["time_chunk"], e["station_chunk"]) < 1
            or not 0 <= e["complevel"] <= 9 or not 0 <= c["max_distance_deg"] <= 180):
        raise ValueError("invalid extraction encoding or matching distance")
    return c


def render(row, args, workers):
    command = ["python", "infos/scnet_patchify_stations_new/run_job.py", "--task", row["task_id"]]
    return "\n".join([
        "#!/usr/bin/env bash", f"#SBATCH --job-name={row['job_name']}",
        f"#SBATCH --partition={args.partition}", "#SBATCH --nodes=1", "#SBATCH --ntasks=1",
        f"#SBATCH --cpus-per-task={row['cpus']}", f"#SBATCH --time={args.time}",
        "#SBATCH --output=logs/%x-%j.out", "#SBATCH --error=logs/%x-%j.err",
        "set -eo pipefail", ': "${STATION_ENV_FILE:?set external campaign environment file}"',
        'source "$STATION_ENV_FILE"', ': "${STATION_CLIMATE_ACTIVATE:?set activation script}"',
        'source "$STATION_CLIMATE_ACTIVATE" climate', "set -euo pipefail", "umask 0002",
        "module load apps/git/2.30.2", 'run_user="$(id -un)"', 'case "$run_user" in',
        "  " + "|".join(w["username"] for w in workers) + ") ;;",
        '  *) echo "Unauthorized station-event worker: $run_user" >&2; exit 2 ;;', "esac",
        ': "${SLURM_JOB_ID:?requires Slurm}"', ': "${STATION_REPO:?set checkout}"',
        ': "${STATION_JOB_PACK:?set prepared pack directory}"',
        'export STATION_JOB_SCRIPT="$(realpath "${BASH_SOURCE[0]}")"',
        "export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1",
        'cd "$STATION_REPO"', shlex.join(command) + ' --pack "$STATION_JOB_PACK/manifest.json"', ""])


def build(args):
    workers, assignments = catalog()
    config = configuration(args.campaign)
    if not re.fullmatch(r"[0-9a-f]{40}", args.code_sha):
        raise ValueError("code-sha must be full lowercase Git SHA")
    for name in ("partition", "resource_profile"):
        ct.safe_name(getattr(args, name))
    if min(args.cpus_per_task, args.prepare_cpus, args.publish_cpus) < 1:
        raise ValueError("CPU counts must be positive")
    if (not re.fullmatch(r"(?:\d+-)?\d{2,3}:[0-5]\d:[0-5]\d", args.time)
            or not any(int(x) for x in re.split("[-:]", args.time))):
        raise ValueError("invalid walltime")
    source_path = args.input_index or config["input_index"]
    patch_path = args.patch_manifest or config["patch_manifest"]
    source = ct.read_json(source_path)
    patches = ct.read_json(patch_path)["patches"]
    if set(patches) != set(assignments) or source["analysis_years"] != config["analysis_years"]:
        raise ValueError("source patch inventory or analysis years differs from campaign")
    sources = ct.combinations(source, ct.MODELS)
    expected = {"/".join(x) for x in itertools.product(ct.MODELS, ct.SCENARIOS, patches, ct.TECHS)}
    if set(sources) != expected:
        raise ValueError("source index must contain all 1128 grid combinations")
    combos = ct.station_combinations(sources, ct.SCENARIOS)
    order = sorted(patches, key=lambda p: (-int(assignments[p]["land_points_reference"]), p))
    rows = [{"unit_id": PREFIX + "/prepare", "stage": "prepare", "key": None,
             "depends_on": [], "logical_owner": workers[0]["username"], "cpus": args.prepare_cpus}]
    for m, c, s, t, p in itertools.product(ct.MODELS, ct.SCENARIOS, ct.SCENARIOS, ct.TECHS, order):
        context = dict(model=m, climate_scenario=c, station_scenario=s, tech=t, patch=p)
        key = ct.combo_key(context)
        rows.append({**context, "unit_id": PREFIX + "/extract/" + key, "stage": "extract", "key": key,
                     "depends_on": [PREFIX + "/prepare"], "logical_owner": assignments[p]["username"],
                     "cpus": args.cpus_per_task, "periods": [a["years"] for a in combos[key]["signals"]]})
    rows.append({"unit_id": PREFIX + "/publish", "stage": "publish", "key": None,
                 "depends_on": [r["unit_id"] for r in rows if r["stage"] == "extract"],
                 "logical_owner": workers[0]["username"], "cpus": args.publish_cpus})
    scripts = {}
    for row in rows:
        row["task_id"] = task_id(row["unit_id"])
        row["job_name"] = (f"esnew_{row['model']}_c{row['climate_scenario'][3:]}_s{row['station_scenario'][3:]}_"
                           f"{row['tech']}_{row['patch']}" if row["stage"] == "extract" else "esnew_" + row["stage"])
        row["script"] = row["job_name"] + ".sh"
        body = render(row, args, workers)
        if row["script"] in scripts:
            raise ValueError("script collision")
        scripts[row["script"]] = body
        row["script_sha256"] = hashlib.sha256(body.encode()).hexdigest()
    if len({r["task_id"] for r in rows}) != len(rows):
        raise ValueError("task identity collision")
    release = {"campaign": config, "campaign_sha256": ct.digest(args.campaign), "code_sha": args.code_sha,
               "input_index_sha256": ct.digest(source_path), "patch_manifest_sha256": ct.digest(patch_path),
               "workers": workers, "aggregator": AGGREGATOR, "assignments": assignments}
    pack = {**release, "schema": "station-events-new-slurm-v1", "campaign_identity": ct.fingerprint(release),
            "resource_profile": args.resource_profile, "partition": args.partition, "time": args.time,
            "jobs": rows, "counts": {"prepare": 1, "extract": 3384, "publish": 1},
            "source_combinations": len(sources), "signal_files": sum(len(c["signals"]) for c in combos.values())}
    pack["identity"] = ct.fingerprint(pack)
    return pack, scripts


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--campaign", required=True)
    p.add_argument("--input-index", help="Optional local copy; jobs use the campaign input path")
    p.add_argument("--patch-manifest", help="Optional local copy; jobs use the campaign input path")
    p.add_argument("--code-sha", required=True)
    p.add_argument("--jobs-dir", required=True)
    p.add_argument("--resource-profile", default="events_new_v1")
    p.add_argument("--partition", default="wzhctest")
    p.add_argument("--cpus-per-task", type=int, default=10)
    p.add_argument("--prepare-cpus", type=int, default=10)
    p.add_argument("--publish-cpus", type=int, default=10)
    p.add_argument("--time", default="24:00:00")
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv=None):
    p = parser(); args = p.parse_args(argv)
    try:
        pack, scripts = build(args)
        target = Path(args.jobs_dir).expanduser().resolve()
        if not args.dry_run:
            if target.is_relative_to(ROOT):
                raise ValueError("job packs must be outside checkout")
            target.mkdir(parents=True, exist_ok=False)
            for name, body in scripts.items():
                with (target / name).open("x", encoding="utf-8") as stream:
                    stream.write(body)
                (target / name).chmod(0o750)
            ct.atomic_json(target / "manifest.json", pack)
        print(json.dumps({"counts": pack["counts"], "total_jobs": len(scripts),
                          "signal_files": pack["signal_files"], "dry_run": args.dry_run}))
    except (ValueError, KeyError, OSError) as exc:
        p.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
