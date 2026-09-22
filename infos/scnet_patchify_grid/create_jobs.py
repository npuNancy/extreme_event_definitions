#!/usr/bin/env python3
"""Generate identical, account-independent production-V2 grid Slurm job packs.

Pure standard library, no submission, SSH or scientific data reads.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import itertools
import json
from pathlib import Path
import re
import shlex

HERE = Path(__file__).resolve().parent
MODELS = ("CANESM5", "MPI-ESM1-2-HR", "MRI-ESM2-0", "BCC-CSM2-MR")
SCENARIOS = ("ssp126", "ssp245", "ssp585")
TECHS = ("wind", "solar")
STAGES = ("baseline", "signals", "audit")


def selections(raw, allowed, label):
    values = [v for word in raw for v in word.split(",")]
    if not values or any(not v or v not in allowed for v in values):
        raise ValueError(f"invalid {label}: {values}")
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate {label}: {values}")
    return values


def years(value):
    if not re.fullmatch(r"\d{4}-\d{4}", value):
        raise ValueError(f"expected YYYY-YYYY: {value}")
    lo, hi = map(int, value.split("-"))
    if not 1 <= lo <= hi <= 9998:
        raise ValueError(f"invalid years: {value}")
    return lo, hi


def segments(value, width):
    lo, hi = years(value)
    return [f"{y:04d}-{min(y+width-1,hi):04d}" for y in range(lo, hi+1, width)]


def unit_id(stage, model, scenario, tech, patch, period):
    return f"grid-v2/{stage}/{model}/{scenario}/{tech}/{patch}/{period}"


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--models", nargs="+", required=True, help="Explicit inventory; runtime GCM allowlist is separate")
    p.add_argument("--scenarios", nargs="+", default=list(SCENARIOS))
    p.add_argument("--techs", nargs="+", default=list(TECHS))
    p.add_argument("--patches", nargs="+", help="Omit for all 47 patches")
    p.add_argument("--stages", nargs="+", default=list(STAGES))
    p.add_argument("--analysis-years", default="2015-2060")
    p.add_argument("--baseline-years", default="2015-2024")
    p.add_argument("--years-per-file", type=int, default=5)
    p.add_argument("--jobs-dir", required=True)
    p.add_argument("--code-sha", required=True, help="Pinned full 40-character deployed Git SHA")
    p.add_argument("--partition", default="wzhctest")
    p.add_argument("--resource-profile", default="grid_v2_pilot_v1")
    p.add_argument("--tile-shape", type=int, nargs=2, default=(32,32))
    p.add_argument("--time-chunk", type=int, default=240)
    p.add_argument("--complevel", type=int, default=1)
    for stage in STAGES:
        p.add_argument(f"--{stage}-cpus", type=int, default=2 if stage == "audit" else 8)
        p.add_argument(f"--{stage}-time", default="02:00:00" if stage == "audit" else "24:00:00")
        if stage != "audit":
            p.add_argument(f"--{stage}-processes", type=int, default=4)
    p.add_argument("--dry-run", action="store_true")
    return p


def configuration(args):
    with (HERE / "作业分工/patch_assignment.csv").open(encoding="utf-8-sig") as f:
        patches = {row["patch_id"]: row for row in csv.DictReader(f)}
    with (HERE / "accounts.csv").open(encoding="utf-8-sig") as f:
        workers = [row["username"] for row in csv.DictReader(f) if row["role"] == "worker"]
    selected = {"models": selections(args.models, MODELS, "models"),
                "scenarios": selections(args.scenarios, SCENARIOS, "scenarios"),
                "techs": selections(args.techs, TECHS, "techs"),
                "patches": selections(args.patches or list(patches), patches, "patches"),
                "stages": selections(args.stages, STAGES, "stages")}
    a,b = years(args.analysis_years); c,d = years(args.baseline_years)
    if not a <= c <= d <= b:
        raise ValueError("baseline must be inside analysis years")
    if args.years_per_file < 1 or args.time_chunk < 1 or min(args.tile_shape) < 1:
        raise ValueError("year span, tile shape and time chunk must be positive")
    if not 0 <= args.complevel <= 9:
        raise ValueError("complevel must be 0..9")
    if not re.fullmatch(r"[0-9a-f]{40}", args.code_sha):
        raise ValueError("code-sha must be a full lowercase 40-character Git SHA")
    for name in ("partition", "resource_profile"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", getattr(args,name)):
            raise ValueError(f"unsafe {name}")
    for stage in STAGES:
        cpus = getattr(args,f"{stage}_cpus")
        processes = 1 if stage == "audit" else getattr(args,f"{stage}_processes")
        if not 1 <= processes <= cpus:
            raise ValueError(f"{stage}: require 1 <= processes <= cpus")
        wall = getattr(args, f"{stage}_time")
        if not re.fullmatch(r"(?:\d+-)?\d{2,3}:[0-5]\d:[0-5]\d", wall) or not any(int(v) for v in re.split("[-:]", wall)):
            raise ValueError(f"invalid {stage} wall time")
    return selected, patches, workers


def render(args, row, workers):
    stage = row["stage"]
    cpus = getattr(args, f"{stage}_cpus")
    processes = 1 if stage == "audit" else getattr(args, f"{stage}_processes")
    command = ["python", "infos/scnet_patchify_grid/run_job.py", "--stage", stage,
               "--model", row["model"], "--scenario", row["scenario"], "--patch", row["patch"],
               "--tech", row["tech"], "--years", row["years"], "--analysis-years", args.analysis_years,
               "--baseline-years", args.baseline_years, "--years-per-file", str(args.years_per_file),
               "--tile-shape", *map(str,args.tile_shape), "--time-chunk", str(args.time_chunk),
               "--complevel", str(args.complevel), "--processes", str(processes),
               "--code-sha", args.code_sha, "--resource-profile", args.resource_profile]
    return "\n".join([
        "#!/usr/bin/env bash",
        f"#SBATCH --job-name={row['job_name']}",
        f"#SBATCH --partition={args.partition}",
        "#SBATCH --nodes=1", "#SBATCH --ntasks=1",
        f"#SBATCH --cpus-per-task={cpus}",
        f"#SBATCH --time={getattr(args,f'{stage}_time')}",
        "#SBATCH --output=logs/%x-%j.out", "#SBATCH --error=logs/%x-%j.err",
        "set -eo pipefail",
        ': "${EXTREME_ENV_FILE:?set EXTREME_ENV_FILE to the external campaign environment}"',
        'source "$EXTREME_ENV_FILE"',
        "source /work/home/acbpgywfpz/miniconda3/bin/activate climate",
        "set -euo pipefail",
        'run_user="$(id -un)"',
        'case "$run_user" in',
        f"  {'|'.join(workers)}) ;;",
        '  *) echo "Account is not an authorized grid worker: $run_user" >&2; exit 2 ;;',
        "esac",
        'export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1',
        ': "${EXTREME_REPO:?set EXTREME_REPO}"',
        'cd "$EXTREME_REPO"',
        shlex.join(command), "",
    ])


def build(args):
    selected, assignments, workers = configuration(args)
    periods = segments(args.analysis_years, args.years_per_file)
    rows, scripts = [], {}
    for model,scenario,patch,tech in itertools.product(selected["models"],selected["scenarios"],selected["patches"],selected["techs"]):
        base = unit_id("baseline",model,scenario,tech,patch,args.baseline_years)
        signal_ids = [unit_id("signals",model,scenario,tech,patch,y) for y in periods]
        for stage in selected["stages"]:
            for period in periods if stage == "signals" else [args.baseline_years if stage == "baseline" else args.analysis_years]:
                uid = unit_id(stage,model,scenario,tech,patch,period)
                name = f"eg2_{stage}_{model}_{scenario}_{patch}_{tech}_{period}"
                relative = f"{stage}/{name}.sh"
                row = {"unit_id":uid,"stage":stage,"model":model,"scenario":scenario,"patch":patch,"tech":tech,
                       "years":period,"job_name":name,"script":relative,
                       "logical_owner":assignments[patch]["username"],"submit_username":None,
                       "depends_on":[] if stage == "baseline" else [base] if stage == "signals" else [base,*signal_ids],
                       "external_bc_variables":(["uas","vas"] if tech == "wind" else ["rsds"]) if stage == "baseline"
                            else ["tas","uas","vas","hurs"] + (["pr","rsds"] if tech == "solar" else []),
                       "expected_relative_output":f"{model}/{scenario}/{patch}/{tech}/" +
                            (f"audit_{period}.json" if stage == "audit" else f"{'baseline' if stage == 'baseline' else 'signals'}_{period}.nc"),
                       "resource_profile":args.resource_profile,"cpus":getattr(args,f"{stage}_cpus"),
                       "processes":1 if stage == "audit" else getattr(args,f"{stage}_processes")}
                script = render(args,row,workers)
                if relative in scripts:
                    raise ValueError(f"script collision: {relative}")
                scripts[relative] = script
                row["script_sha256"] = hashlib.sha256(script.encode()).hexdigest()
                rows.append(row)
    manifest = {"schema":"grid-v2-job-pack-v1","input_release":"production_v2","code_sha":args.code_sha,
                "selection":selected,"analysis_years":args.analysis_years,"baseline_years":args.baseline_years,
                "years_per_file":args.years_per_file,"resource_profile":args.resource_profile,
                "workers":workers,"counts":dict(Counter(row["stage"] for row in rows)),"jobs":rows}
    return manifest,scripts


def main(argv=None):
    p = parser(); args = p.parse_args(argv)
    try:
        manifest,scripts = build(args)
        target = Path(args.jobs_dir).expanduser()
        if not args.dry_run:
            # Preflight all collisions before creating any files; packs are immutable.
            for name in [*scripts,"manifest.json"]:
                path = target / name
                if path.exists() or path.is_symlink():
                    raise FileExistsError(f"refusing to overwrite: {path}; choose a new pack directory")
            for relative,script in scripts.items():
                path = target / relative
                path.parent.mkdir(parents=True,exist_ok=True)
                with path.open("x",encoding="utf-8") as stream:
                    stream.write(script)
                path.chmod(0o750)
            with (target/"manifest.json").open("x",encoding="utf-8") as stream:
                json.dump(manifest,stream,ensure_ascii=False,indent=2); stream.write("\n")
    except (ValueError,OSError) as exc:
        p.exit(2,f"error: {exc}\n")
    print(json.dumps({"dry_run":args.dry_run,"counts":manifest["counts"],"total":len(scripts)},ensure_ascii=False))


if __name__ == "__main__":
    main()
