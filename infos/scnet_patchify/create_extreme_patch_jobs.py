#!/usr/bin/env python3
"""Generate one Slurm script per patchify station-signal unit.

This generator only writes scripts and a manifest. It never calls ``sbatch``.
The job runs the station-block multiprocessing implementation in
``scripts/station_signals_patchify.py``.
"""
from __future__ import annotations

import argparse
import itertools
import json
import re
import shlex
from pathlib import Path

MODELS = ("CANESM5", "MPI-ESM1-2-HR", "MRI-ESM2-0", "BCC-CSM2-MR")
SCENARIOS = ("ssp126", "ssp245", "ssp585")
TECHS = ("wind", "solar")
SUPPORTED_YEARS = "2015-2060"
DEFAULT_CPUS = 16
DEFAULT_PROCESSES = 16
DEFAULT_RESOURCE_PROFILE = "resource_v3_extreme_multiprocess"
USERNAME = re.compile(r"^[a-z][a-z0-9]{3,31}$")


def _years_arg(value: str) -> str:
    if value != SUPPORTED_YEARS:
        raise argparse.ArgumentTypeError(
            f"--years currently accepts only {SUPPORTED_YEARS!r}; got {value!r}"
        )
    return value


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--models", nargs="+", choices=MODELS, default=list(MODELS))
    p.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    p.add_argument("--patches", nargs="+", required=True, metavar="PATCH")
    p.add_argument("--techs", nargs="+", choices=TECHS, default=list(TECHS))
    p.add_argument("--years", type=_years_arg, default=SUPPORTED_YEARS)
    p.add_argument("--bcsd-root", required=True)
    p.add_argument("--patch-manifest", required=True)
    p.add_argument("--stations-csv", required=True)
    p.add_argument("--output-root", required=True)
    p.add_argument("--project-dir", default=str(Path(__file__).resolve().parents[2]))
    p.add_argument("--jobs-dir", required=True)
    p.add_argument("--logs-dir", required=True)
    p.add_argument("--partition", default="wzhctest")
    p.add_argument("--submit-username", help="actual Slurm username; billing IDs are not accepted")
    p.add_argument("--cpus-per-task", type=int, default=DEFAULT_CPUS)
    p.add_argument("--processes", type=int, default=DEFAULT_PROCESSES,
                   help="station-block workers per job")
    p.add_argument("--station-block-size", type=int)
    p.add_argument("--parts-root")
    p.add_argument("--time")
    p.add_argument("--resource-profile", default=DEFAULT_RESOURCE_PROFILE)
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    return p


def _validate(args: argparse.Namespace) -> None:
    if args.cpus_per_task < 1:
        raise SystemExit("--cpus-per-task must be >= 1")
    if not 1 <= args.processes <= args.cpus_per_task:
        raise SystemExit("--processes must be between 1 and --cpus-per-task")
    if args.station_block_size is not None and args.station_block_size < 1:
        raise SystemExit("--station-block-size must be >= 1")
    if args.submit_username and not USERNAME.fullmatch(args.submit_username):
        raise SystemExit("--submit-username must be a Slurm username, not a billing account")
    if args.submit_username and args.submit_username.isdigit():
        raise SystemExit("billing account numbers must not be passed as --submit-username")


def main(argv=None) -> None:
    args = parser().parse_args(argv)
    _validate(args)
    jobs_dir = Path(args.jobs_dir).expanduser()
    logs_dir = Path(args.logs_dir).expanduser()
    if not args.dry_run:
        jobs_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for model, scenario, patch, tech in itertools.product(
        args.models, args.scenarios, args.patches, args.techs
    ):
        job_name = f"extp_{model}_{scenario}_{patch}_{tech}"
        quote = shlex.quote
        lines = [
            "#!/bin/bash",
            f"#SBATCH --job-name={job_name}",
            f"#SBATCH --partition={args.partition}",
            f"#SBATCH --cpus-per-task={args.cpus_per_task}",
            f"#SBATCH --output={logs_dir}/{job_name}_%j.out",
            f"#SBATCH --error={logs_dir}/{job_name}_%j.out",
        ]
        if args.time:
            lines.append(f"#SBATCH --time={args.time}")
        command = [
            "python", str(Path(args.project_dir) / "scripts/station_signals_patchify.py"),
            "--bcsd-root", args.bcsd_root, "--model", model,
            "--scenario", scenario, "--patch", patch,
            "--patch-manifest", args.patch_manifest, "--stations-csv", args.stations_csv,
            "--tech", tech, "--years", args.years, "--output-root", args.output_root,
            "--processes", str(args.processes), "--timing-report",
        ]
        if args.station_block_size is not None:
            command += ["--station-block-size", str(args.station_block_size)]
        if args.parts_root:
            command += ["--parts-root", args.parts_root]
        if args.overwrite:
            command.append("--overwrite")
        lines += [
            # Activation must precede nounset because SCNet hooks may inspect unset variables.
            "source /work/home/acbpgywfpz/miniconda3/bin/activate climate",
            "set -euo pipefail",
            "export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1",
            f"mkdir -p {quote(str(logs_dir))} {quote(args.output_root)}",
            f"cd {quote(args.project_dir)}",
            " ".join(quote(part) for part in command),
        ]
        row = {
            "unit_id": f"extreme/{model}/{scenario}/{tech}/{patch}",
            "job_name": job_name, "model": model, "scenario": scenario,
            "patch_id": patch, "technology": tech,
            "submit_username": args.submit_username or "",
            "resource_profile": args.resource_profile,
            "cpus_per_task": args.cpus_per_task, "processes": args.processes,
        }
        if args.dry_run:
            rows.append(row)
            continue
        script = jobs_dir / f"{job_name}.sh"
        if script.exists():
            raise FileExistsError(f"refusing to overwrite existing script: {script}")
        script.write_text("\n".join(lines) + "\n", encoding="utf-8")
        script.chmod(0o750)
        rows.append({**row, "script": str(script)})

    if not args.dry_run:
        (jobs_dir / "manifest.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(f"generated {len(rows)} jobs in {jobs_dir} (dry-run={args.dry_run})")


if __name__ == "__main__":
    main()
