#!/usr/bin/env python3
"""生成统一 step2（普通事件 + BCSD 低资源事件）Slurm 作业。"""
from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from grid_extreme_signals import station_match as sm  # noqa: E402
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
    validate_components,
    validate_scenarios,
    validate_partition,
    validate_sbatch_path,
    validate_techs,
    write_manifest,
    write_text,
)


def _build_station_inventory(
    stations_dir: Path,
    shp: Path,
    scenarios: list[str],
    regions_by_model: dict[str, list[str]],
    techs: list[str],
) -> dict[tuple[str, str, str], int]:
    """按区域、情景和技术统计场站数，用于无场站单元的完成契约。"""
    countries = sm.load_country_shapes(shp)
    station_tables = {
        scenario: sm.load_stations(stations_dir / SCENARIO_STATIONS[scenario])
        for scenario in scenarios
    }
    regions = sorted({region for values in regions_by_model.values() for region in values})
    inventory: dict[tuple[str, str, str], int] = {}
    for region in regions:
        country_name = sm.bcsd_region_to_ne_name(region)
        if country_name not in countries:
            raise KeyError(f"Natural Earth 中没有区域 {region!r} 对应的国家 {country_name!r}")
        for scenario in scenarios:
            for tech in techs:
                inventory[(region, scenario, tech)] = len(
                    sm.filter_stations_for_country(
                        station_tables[scenario], countries[country_name], tech
                    )
                )
    return inventory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成统一 step2 Slurm 作业；普通事件和 BCSD 低资源事件一次完成。"
    )
    parser.add_argument("--models", required=True, help="逗号分隔的 CMIP6 模式。")
    parser.add_argument("--regions", required=True, help="区域名或 all。")
    parser.add_argument("--scenarios", required=True, help="如 ssp126,ssp245,ssp585。")
    parser.add_argument("--techs", default="wind,solar", help="wind、solar 或二者。")
    parser.add_argument("--years", required=True, help="连续年份范围，须覆盖 2015-2024。")
    parser.add_argument("--project-dir", default="~/extreme_event_definitions")
    parser.add_argument("--data-dir", default="~/data/bcsd_outputs")
    parser.add_argument("--stations-dir", default="~/data/stations")
    parser.add_argument(
        "--shp", default="~/data/maps/natural_earth/ne_110m_admin_0_countries.shp"
    )
    parser.add_argument("--output-root", default=None)
    parser.add_argument("--job-root", default="~/extreme_event_jobs/step2")
    parser.add_argument("--log-root", default="~/extreme_event_logs/step2")
    parser.add_argument("--partition", default="wzhctest")
    parser.add_argument("--cpus", type=int, default=6)
    parser.add_argument(
        "--activate-path", default="/work/home/acbpgywfpz/miniconda3/bin/activate"
    )
    parser.add_argument("--environment-name", default="climate")
    parser.add_argument(
        "--allow-unit-inference", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument(
        "--allow-missing-optional", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--force", action="store_true", help="覆盖同名作业脚本和清单。")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件。")
    return parser


def run(args: argparse.Namespace) -> Path:
    models = parse_csv(args.models, label="--models")
    validate_components(models, label="--models")
    scenarios = validate_scenarios(args.scenarios)
    techs = validate_techs(args.techs)
    start, end = parse_years(args.years)
    if start > 2015 or end < 2024:
        raise ValueError("--years 必须覆盖完整低资源基线期 2015-2024")
    if args.cpus < 1:
        raise ValueError("--cpus 必须为正整数")
    validate_partition(args.partition)

    project_dir = expand_path(args.project_dir)
    data_dir = expand_path(args.data_dir)
    stations_dir = expand_path(args.stations_dir)
    shp = expand_path(args.shp)
    output_root = expand_path(args.output_root) if args.output_root else project_dir / "outputs/station_signals"
    job_root = expand_path(args.job_root)
    log_root = expand_path(args.log_root)
    activate_path = expand_path(args.activate_path)
    validate_sbatch_path(log_root, label="--log-root")
    require_dir(project_dir, label="--project-dir")
    require_dir(data_dir, label="--data-dir")
    require_dir(stations_dir, label="--stations-dir")
    require_file(project_dir / "step2_complete_extreme_events.py", label="统一 step2 入口")
    require_file(activate_path, label="--activate-path")
    require_file(shp, label="--shp")
    for suffix in (".shx", ".dbf"):
        require_file(shp.with_suffix(suffix), label=f"Natural Earth {suffix}")
    for scenario in scenarios:
        require_file(stations_dir / SCENARIO_STATIONS[scenario], label=f"{scenario} 场站 CSV")

    regions_by_model = resolve_regions(data_dir, models, args.regions)
    station_inventory = _build_station_inventory(
        stations_dir, shp, scenarios, regions_by_model, techs
    )
    selection = {
        "models": models,
        "regions": regions_by_model,
        "scenarios": scenarios,
        "techs": techs,
        "years": args.years,
        "output_root": str(output_root),
    }
    campaign = campaign_id("STEP2", selection)
    prefix = f"s2_{campaign}_"
    units: list[dict[str, object]] = []
    scripts_to_write: list[tuple[Path, str, list[str]]] = []
    planned_paths: set[Path] = set()
    for model in models:
        for region in regions_by_model[model]:
            for scenario in scenarios:
                station_csv = stations_dir / SCENARIO_STATIONS[scenario]
                for tech in techs:
                    name = job_name("STEP2", campaign, model, region, scenario, tech, args.years)
                    script_path = job_root / f"job_{name}.sh"
                    if script_path in planned_paths:
                        raise ValueError(f"生成脚本文件名冲突：{script_path}")
                    planned_paths.add(script_path)
                    output = expected_output(output_root, model, region, scenario, tech, args.years)
                    command = [
                        "python", "step2_complete_extreme_events.py",
                        "--source", "regional_bcsd", "--data_dir", str(data_dir),
                        "--model", model, "--scenario", scenario,
                        "--stations_csv", str(station_csv), "--region", region,
                        "--years", args.years, "--tech", tech, "--shp", str(shp),
                        "--output_root", str(output_root),
                    ]
                    if args.allow_unit_inference:
                        command.append("--allow_unit_inference")
                    if args.allow_missing_optional:
                        command.append("--allow_missing_optional")
                    if args.overwrite:
                        command.append("--overwrite")
                    script = render_script(
                        name=name, partition=args.partition, cpus=args.cpus,
                        log_root=log_root, project_dir=project_dir,
                        activate_path=activate_path, environment_name=args.environment_name,
                        command=command,
                    )
                    scripts_to_write.append((script_path, script, command))
                    station_count = station_inventory[(region, scenario, tech)]
                    units.append({
                        "unit_id": f"{model}|{region}|{scenario}|{tech}|{args.years}",
                        "model": model, "region": region, "scenario": scenario,
                        "tech": tech, "years": args.years, "job_name": name,
                        "script_path": str(script_path), "expected_output": str(output),
                        "station_count": station_count, "has_stations": station_count > 0,
                        "command": shlex.join(command),
                    })
    manifest_path = job_root / f"manifest_step2_{campaign}.json"
    manifest = {
        "schema_version": 2, "stage": "STEP2", "campaign_id": campaign,
        "job_prefix": prefix, "project_dir": str(project_dir),
        "job_root": str(job_root), "log_root": str(log_root),
        "partition": args.partition, "cpus": args.cpus,
        "overwrite": bool(args.overwrite), "selection": selection, "units": units,
    }
    if args.dry_run:
        for script_path, _, command in scripts_to_write:
            print(f"[dry-run] {script_path}: {shlex.join(command)}")
    else:
        collisions = [path for path in [*(item[0] for item in scripts_to_write), manifest_path] if path.exists()]
        if collisions and not args.force:
            raise FileExistsError(f"生成物已存在；确认后传 --force：{collisions[0]}")
        log_root.mkdir(parents=True, exist_ok=True)
        for script_path, script, _ in scripts_to_write:
            write_text(script_path, script, force=args.force)
            script_path.chmod(0o750)
        write_manifest(manifest_path, manifest, force=args.force)
    print(f"STEP2 计划作业数：{len(units)}")
    print(f"STEP2 清单：{manifest_path}")
    return manifest_path


def main() -> None:
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
