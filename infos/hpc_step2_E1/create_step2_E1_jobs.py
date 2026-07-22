#!/usr/bin/env python3
"""生成 step2 E1（普通极端事件）Slurm 作业脚本。"""
from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from infos.hpc_step2_common import (  # noqa: E402
    SCENARIO_STATIONS,
    campaign_id,
    expand_path,
    expected_output,
    job_name,
    parse_csv,
    parse_years,
    render_script,
    require_dir,
    require_file,
    resolve_regions,
    validate_scenarios,
    validate_partition,
    validate_sbatch_path,
    validate_techs,
    write_manifest,
    write_text,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成 step2 E1 Slurm 作业；只生成，不提交。"
    )
    parser.add_argument("--models", required=True, help="逗号分隔的 CMIP6 模式。")
    parser.add_argument(
        "--regions", required=True, help="单个/逗号分隔区域，或 all（仍逐区域生成作业）。"
    )
    parser.add_argument("--scenarios", required=True, help="如 ssp126,ssp245,ssp585。")
    parser.add_argument("--techs", default="wind,solar", help="wind、solar 或二者。")
    parser.add_argument("--years", required=True, help="YYYY 或 YYYY-YYYY。")
    parser.add_argument("--project-dir", default="~/extreme_event_definitions")
    parser.add_argument("--data-dir", default="~/data/bcsd_outputs")
    parser.add_argument("--stations-dir", default="~/data/stations")
    parser.add_argument(
        "--shp",
        default="~/data/maps/natural_earth/ne_110m_admin_0_countries.shp",
    )
    parser.add_argument("--output-root", default=None)
    parser.add_argument("--job-root", default="~/extreme_event_jobs/step2_E1")
    parser.add_argument("--log-root", default="~/extreme_event_logs/step2_E1")
    parser.add_argument("--partition", default="wzhctest")
    parser.add_argument("--e1-cpus", type=int, default=6)
    parser.add_argument(
        "--activate-path",
        default="/work/home/acbpgywfpz/miniconda3/bin/activate",
    )
    parser.add_argument("--environment-name", default="climate")
    parser.add_argument(
        "--allow-unit-inference",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--allow-missing-optional",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--force", action="store_true", help="覆盖同名作业脚本和清单。")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件。")
    return parser


def run(args: argparse.Namespace) -> Path:
    models = parse_csv(args.models, label="--models")
    scenarios = validate_scenarios(args.scenarios)
    techs = validate_techs(args.techs)
    parse_years(args.years)
    if args.e1_cpus < 1:
        raise ValueError("--e1-cpus 必须为正整数")
    validate_partition(args.partition)

    project_dir = expand_path(args.project_dir)
    data_dir = expand_path(args.data_dir)
    stations_dir = expand_path(args.stations_dir)
    shp = expand_path(args.shp)
    output_root = (
        expand_path(args.output_root)
        if args.output_root
        else project_dir / "outputs/station_signals"
    )
    job_root = expand_path(args.job_root)
    log_root = expand_path(args.log_root)
    activate_path = expand_path(args.activate_path)
    validate_sbatch_path(log_root, label="--log-root")
    require_dir(project_dir, label="--project-dir")
    require_dir(data_dir, label="--data-dir")
    require_dir(stations_dir, label="--stations-dir")
    require_file(project_dir / "step2_split_E1_weather_extremes.py", label="E1 入口")
    require_file(activate_path, label="--activate-path")
    require_file(shp, label="--shp")
    for suffix in (".shx", ".dbf"):
        require_file(shp.with_suffix(suffix), label=f"Natural Earth {suffix}")
    for scenario in scenarios:
        require_file(
            stations_dir / SCENARIO_STATIONS[scenario],
            label=f"{scenario} 场站 CSV",
        )
    regions_by_model = resolve_regions(data_dir, models, args.regions)

    selection = {
        "models": models,
        "regions": regions_by_model,
        "scenarios": scenarios,
        "techs": techs,
        "years": args.years,
        "output_root": str(output_root),
    }
    campaign = campaign_id("E1", selection)
    prefix = f"s2e1_{campaign}_"
    units: list[dict[str, str]] = []
    planned_paths: set[Path] = set()
    scripts_to_write: list[tuple[Path, str, list[str]]] = []

    for model in models:
        for region in regions_by_model[model]:
            for scenario in scenarios:
                station_csv = stations_dir / SCENARIO_STATIONS[scenario]
                for tech in techs:
                    name = job_name(
                        "E1", campaign, model, region, scenario, tech, args.years
                    )
                    script_path = job_root / f"job_{name}.sh"
                    if script_path in planned_paths:
                        raise ValueError(f"生成脚本文件名冲突：{script_path}")
                    planned_paths.add(script_path)
                    output = expected_output(
                        output_root, model, region, scenario, tech, args.years
                    )
                    command = [
                        "python",
                        "step2_split_E1_weather_extremes.py",
                        "--source",
                        "regional_bcsd",
                        "--data_dir",
                        str(data_dir),
                        "--model",
                        model,
                        "--scenario",
                        scenario,
                        "--stations_csv",
                        str(station_csv),
                        "--region",
                        region,
                        "--years",
                        args.years,
                        "--tech",
                        tech,
                        "--shp",
                        str(shp),
                        "--output_root",
                        str(output_root),
                    ]
                    if args.allow_unit_inference:
                        command.append("--allow_unit_inference")
                    if args.allow_missing_optional:
                        command.append("--allow_missing_optional")
                    if args.overwrite:
                        command.append("--overwrite")
                    script = render_script(
                        name=name,
                        partition=args.partition,
                        cpus=args.e1_cpus,
                        log_root=log_root,
                        project_dir=project_dir,
                        activate_path=activate_path,
                        environment_name=args.environment_name,
                        command=command,
                    )
                    scripts_to_write.append((script_path, script, command))
                    units.append(
                        {
                            "unit_id": f"{model}|{region}|{scenario}|{tech}|{args.years}",
                            "model": model,
                            "region": region,
                            "scenario": scenario,
                            "tech": tech,
                            "years": args.years,
                            "job_name": name,
                            "script_path": str(script_path),
                            "expected_output": str(output),
                            "command": shlex.join(command),
                        }
                    )

    manifest_path = job_root / f"manifest_step2_E1_{campaign}.json"
    manifest = {
        "schema_version": 1,
        "stage": "E1",
        "campaign_id": campaign,
        "job_prefix": prefix,
        "project_dir": str(project_dir),
        "job_root": str(job_root),
        "log_root": str(log_root),
        "partition": args.partition,
        "cpus": args.e1_cpus,
        "overwrite": bool(args.overwrite),
        "selection": selection,
        "units": units,
    }
    if args.dry_run:
        for script_path, _, command in scripts_to_write:
            print(f"[dry-run] {script_path}: {shlex.join(command)}")
    else:
        collisions = [
            path
            for path in [*(item[0] for item in scripts_to_write), manifest_path]
            if path.exists()
        ]
        if collisions and not args.force:
            raise FileExistsError(
                f"生成物已存在；确认后传 --force：{collisions[0]}"
            )
        log_root.mkdir(parents=True, exist_ok=True)
        for script_path, script, _ in scripts_to_write:
            write_text(script_path, script, force=args.force)
            script_path.chmod(0o750)
        write_manifest(manifest_path, manifest, force=args.force)
    if args.overwrite:
        print("警告：生成的 E1 作业将覆盖所选科学输出。")
    print(f"E1 计划作业数：{len(units)}")
    print(f"E1 清单：{manifest_path}")
    return manifest_path


def main() -> None:
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
