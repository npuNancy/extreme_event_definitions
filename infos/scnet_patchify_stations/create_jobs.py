#!/usr/bin/env python3
"""Generate immutable station-extraction Slurm packs (standard library only)."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import re
import shlex
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from grid_extreme_signals import station_contract as ct


def task_id(unit_id):
    return hashlib.sha256(unit_id.encode()).hexdigest()[:24]


def configuration(path):
    config = ct.read_json(path)
    accounts = Path(config["accounts_file"])
    if not accounts.is_absolute():
        accounts = Path(path).resolve().parent / accounts
    with accounts.open() as f:
        rows = list(csv.DictReader(f))
    if len({r["username"] for r in rows}) != len(rows) or len({r["host"] for r in rows}) != len(rows):
        raise ValueError("duplicate accounts/hosts")
    workers = [r for r in rows if r["role"] == "worker"]
    aggregators = [r for r in rows if r["role"] == "aggregator"]
    if not workers or len(aggregators) != 1 or len(workers) + len(aggregators) != len(rows):
        raise ValueError("expected workers and exactly one aggregator")
    for row in rows:
        ct.safe_name(row["username"]); ct.safe_name(row["host"])
    for name in ("run_id", "partition", "resource_profile"):
        ct.safe_name(config[name])
    if not config["models"] or len(set(config["models"])) != len(config["models"]) or set(config["models"]) - set(ct.MODELS):
        raise ValueError("invalid or duplicate model selection")
    ct.years(config["analysis_years"])
    if not 0 <= config["max_distance_deg"] <= 180:
        raise ValueError("invalid match distance")
    for name in ("account_active_limit", "global_active_limit"):
        if not isinstance(config[name], int) or config[name] < 1:
            raise ValueError("active limits must be positive integers")
    if config["account_active_limit"] > 20 or config["max_retries"] not in (0, 1, 2):
        raise ValueError("account limit/retry ceiling exceeded")
    for stage in ("prepare", "extract", "audit"):
        r = config["resources"][stage]
        if not isinstance(r["cpus"], int) or r["cpus"] < 1 or not re.fullmatch(r"(?:\d+-)?\d{2,3}:[0-5]\d:[0-5]\d", r["time"]):
            raise ValueError("invalid resource profile")
    e = config["extraction"]
    if min(e["time_chunk"], e["station_chunk"]) < 1 or not 0 <= e["complevel"] <= 9:
        raise ValueError("invalid output encoding")
    for row in workers:
        output = Path(config["worker_root_template"].format(username=row["username"]))
        share = Path("/work/share") / row["username"]
        if not output.is_relative_to(share) or output == share:
            raise ValueError("worker outputs must be below their own share directory")
    agg = Path(config["aggregate_root"])
    if not agg.is_relative_to(Path("/work/share") / aggregators[0]["username"]):
        raise ValueError("aggregate root must belong to aggregator")
    return config, workers, aggregators[0]


def render(row, config, workers):
    r = config["resources"][row["stage"]]
    command = ["python", "infos/scnet_patchify_stations/run_job.py", "--task", row["task_id"]]
    return "\n".join([
        "#!/usr/bin/env bash", f"#SBATCH --job-name={row['job_name']}",
        f"#SBATCH --partition={config['partition']}", "#SBATCH --nodes=1", "#SBATCH --ntasks=1",
        f"#SBATCH --cpus-per-task={r['cpus']}", f"#SBATCH --time={r['time']}",
        "#SBATCH --output=logs/%x-%j.out", "#SBATCH --error=logs/%x-%j.err",
        "set -eo pipefail", "source /work/home/acbpgywfpz/miniconda3/bin/activate climate",
        "set -euo pipefail", 'run_user="$(id -un)"', 'case "$run_user" in',
        "  " + "|".join(w["username"] for w in workers) + ") ;;",
        '  *) echo "Unauthorized station worker: $run_user" >&2; exit 2 ;;', "esac",
        ': "${STATION_REPO:?set STATION_REPO}"', ': "${STATION_JOB_PACK:?set STATION_JOB_PACK}"',
        "export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1",
        'cd "$STATION_REPO"', shlex.join(command) + ' --pack "$STATION_JOB_PACK/manifest.json"', ""])


def build(campaign, index, code_sha):
    if not re.fullmatch("[0-9a-f]{40}", code_sha):
        raise ValueError("code-sha must have 40 lowercase hex characters")
    config, workers, aggregator = configuration(campaign)
    source = ct.read_json(index)
    combos = ct.combinations(source, config["models"])
    if config["analysis_years"] != source["analysis_years"]:
        raise ValueError("analysis years differ from source")
    base = "station-v1/prepare"
    rows = [{"unit_id": base, "stage": "prepare", "depends_on": [], "key": None,
             "logical_owner": workers[0]["username"]}]
    for i, key in enumerate(combos):
        extract = "station-v1/extract/" + key
        rows += [{"unit_id": extract, "stage": "extract", "key": key, "depends_on": [base],
                  "logical_owner": workers[i % len(workers)]["username"]},
                 {"unit_id": "station-v1/audit/" + key, "stage": "audit", "key": key,
                  "depends_on": [extract], "logical_owner": workers[i % len(workers)]["username"]}]
    scripts = {}
    for row in rows:
        row["task_id"] = task_id(row["unit_id"])
        row["job_name"] = "es2_" + row["stage"] + "_" + row["task_id"]
        row["script"] = row["stage"] + "/" + row["job_name"] + ".sh"
        body = render(row, config, workers)
        if row["script"] in scripts:
            raise ValueError("task/script identity collision")
        scripts[row["script"]] = body
        row["script_sha256"] = hashlib.sha256(body.encode()).hexdigest()
    manifest = {"schema": "station-job-pack-v1", "campaign": config,
                "input_index_sha256": ct.digest(index), "code_sha": code_sha,
                "workers": workers, "aggregator": aggregator, "jobs": rows,
                "counts": dict(Counter(r["stage"] for r in rows)),
                "source_combinations": len(combos), "signal_files": sum(len(c["signals"]) for c in combos.values())}
    manifest["identity"] = ct.fingerprint(manifest)
    return manifest, scripts


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--campaign", default=str(Path(__file__).with_name("campaign.json")))
    p.add_argument("--input-index", help="Local copy for generation; scientific jobs use campaign input path")
    p.add_argument("--code-sha", required=True)
    p.add_argument("--jobs-dir", required=True)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    try:
        config = ct.read_json(a.campaign)
        manifest, scripts = build(a.campaign, a.input_index or config["input_index"], a.code_sha)
        target = Path(a.jobs_dir)
        if not a.dry_run:
            if target.exists():
                raise FileExistsError("job pack directory already exists; use a new directory")
            target.mkdir(parents=True)
            for name, body in scripts.items():
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
                path.chmod(0o750)
            ct.atomic_json(target / "manifest.json", manifest)
        print(json.dumps({"counts": manifest["counts"], "signal_files": manifest["signal_files"],
                          "total_jobs": len(scripts), "dry_run": a.dry_run}))
    except (ValueError, KeyError, OSError) as e:
        p.exit(2, f"error: {e}\n")


if __name__ == "__main__":
    main()
