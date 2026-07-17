#!/usr/bin/env python3
"""生成昆山超算 step1 E2a/E2b/E3 Slurm 作业脚本。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.hpc_step1_common import CHUNK_PLAN_NAME, read_chunk_plan

SERVER_CONFIG = {
    "scnet-kunshan-185": {
        "tech": "wind",
        "user": "acbw9wpn5k",
        "project_dir": "/public/home/acbw9wpn5k/extreme_event_definitions",
        "activate": "/public/home/acbw9wpn5k/.venv/bin/activate",
    },
    "scnet-kunshan-199": {
        "tech": "solar",
        "user": "aclym5felp",
        "project_dir": "/public/home/aclym5felp/extreme_event_definitions",
        "activate": "/public/home/acbw9wpn5k/.venv/bin/activate",
    },
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成昆山 step1 Slurm 作业。")
    parser.add_argument("--server", choices=sorted(SERVER_CONFIG), required=True)
    parser.add_argument("--partition", default=None)
    parser.add_argument("--history-start", default=None)
    parser.add_argument("--kernel-num", type=int, default=4)
    parser.add_argument("--e2a-time", default="24:00:00")
    parser.add_argument("--e2b-time", default="08:00:00")
    parser.add_argument("--e3-time", default="08:00:00")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _write(path: Path, content: str, *, force: bool, dry_run: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"作业脚本已存在；确认后传 --force：{path}")
    if dry_run:
        print(f"[dry-run] {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o750)


def _slurm_script(
    *,
    job_name: str,
    command: str,
    project_dir: str,
    activate: str,
    kernel_num: int,
    time_limit: str,
    partition: str | None,
) -> str:
    partition_line = f"#SBATCH --partition={partition}\n" if partition else ""
    return f"""#!/usr/bin/env bash
#SBATCH -N 1
#SBATCH -n {kernel_num}
#SBATCH --time={time_limit}
#SBATCH --job-name={job_name}
#SBATCH --output={project_dir}/logs/slurm/{job_name}_%j.out
{partition_line}set -euo pipefail
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
cd {project_dir}
source {activate}
{command}
"""


def run(args: argparse.Namespace) -> Path:
    if args.kernel_num < 1:
        raise ValueError("--kernel-num 必须为正整数")
    config = SERVER_CONFIG[args.server]
    tech = config["tech"]
    project = Path(config["project_dir"])
    jobs_dir = project / "jobs/step1_low_resource"
    union_dir = project / "outputs/cache/era5land_union_station_cf/union_stations"
    plan_path = union_dir / CHUNK_PLAN_NAME
    plan = read_chunk_plan(plan_path)
    common = {
        "project_dir": str(project),
        "activate": config["activate"],
        "kernel_num": args.kernel_num,
        "partition": args.partition,
    }
    e2a_scripts: list[tuple[bool, Path]] = []
    for row in plan.itertuples(index=False):
        job_name = row.job_name.replace("{tech}", tech)
        script = jobs_dir / f"job_{job_name}.sh"
        command = (
            "python step1_split_E2a_extract_union_station_cf_monthly.py "
            '--cf_root "$HOME/data/cfs" '
            f"--union_stations_csv {union_dir}/stations_union_ssp126_ssp245_ssp585.csv "
            f"--tech {tech} "
            f"--year_month_start {row.year_month_start} "
            f"--year_month_end {row.year_month_end} "
            "--threshold_interp nearest_valid "
            f"--output_dir {project}/outputs/cache/era5land_union_station_cf/time_chunks"
        )
        content = _slurm_script(
            job_name=job_name, command=command, time_limit=args.e2a_time, **common
        )
        _write(script, content, force=args.force, dry_run=args.dry_run)
        e2a_scripts.append((bool(row.is_probe), script))

    e2b_name = f"step1_E2b_{tech}"
    e2b_script = jobs_dir / f"job_{e2b_name}.sh"
    e2b_command = (
        "python step1_split_E2b_merge_union_station_cf_cache.py "
        f"--time_chunk_dir {project}/outputs/cache/era5land_union_station_cf/time_chunks "
        f"--chunk_plan_csv {plan_path} --tech {tech} --baseline_years 2015-2024 "
        "--threshold_interp nearest_valid "
        f"--output_cache {project}/outputs/cache/era5land_union_station_cf/"
        f"merged_cache/station_cf_union_{tech}_ERA5Land_2015-2024_nearest_valid.nc"
    )
    _write(
        e2b_script,
        _slurm_script(
            job_name=e2b_name, command=e2b_command, time_limit=args.e2b_time, **common
        ),
        force=args.force,
        dry_run=args.dry_run,
    )

    e3_name = f"step1_E3_{tech}"
    e3_script = jobs_dir / f"job_{e3_name}.sh"
    merged = (
        f"{project}/outputs/cache/era5land_union_station_cf/merged_cache/"
        f"station_cf_union_{tech}_ERA5Land_2015-2024_nearest_valid.nc"
    )
    e3_command = (
        "python step1_split_E3_thresholds_from_union_cache.py "
        f"--union_station_cf_cache {merged} "
        f"--union_stations_csv {union_dir}/stations_union_ssp126_ssp245_ssp585.csv "
        f"--index_map_ssp126 {union_dir}/station_index_map_ssp126.csv "
        f"--index_map_ssp245 {union_dir}/station_index_map_ssp245.csv "
        f"--index_map_ssp585 {union_dir}/station_index_map_ssp585.csv "
        f"--tech {tech} --baseline_years 2015-2024 "
        f"--output_dir {project}/outputs/low_resource_thresholds/"
        "sparse_station_ERA5Land_2015-2024"
    )
    _write(
        e3_script,
        _slurm_script(
            job_name=e3_name, command=e3_command, time_limit=args.e3_time, **common
        ),
        force=args.force,
        dry_run=args.dry_run,
    )

    probe_scripts = [path for is_probe, path in e2a_scripts if is_probe]
    formal_scripts = [path for is_probe, path in e2a_scripts if not is_probe]
    initial = "#!/usr/bin/env bash\nset -euo pipefail\n" + "\n".join(
        f"sbatch --parsable {path}" for path in probe_scripts
    ) + "\n"
    batches = "#!/usr/bin/env bash\nset -euo pipefail\n" + "\n".join(
        f"sbatch --parsable {path}" for path in formal_scripts
    ) + "\n"
    _write(
        jobs_dir / f"submit_step1_split_E2a_initial_{tech}.sh",
        initial,
        force=args.force,
        dry_run=args.dry_run,
    )
    _write(
        jobs_dir / f"submit_step1_split_E2a_batches_{tech}.sh",
        batches,
        force=args.force,
        dry_run=args.dry_run,
    )
    print(f"已生成 {tech}：1 个 E2a 测试作业、20 个正式作业、E2b 和 E3")
    return jobs_dir


def main() -> None:
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
