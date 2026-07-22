#!/usr/bin/env python3
"""Step2 E1/E2 Slurm 生成器共享约定。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Iterable

SCENARIO_STATIONS = {
    "ssp126": "stations_SSP1-2.6.csv",
    "ssp245": "stations_SSP2-4.5.csv",
    "ssp585": "stations_SSP5-6.0.csv",
}
TECHS = ("wind", "solar")


def expand_path(value: str | Path) -> Path:
    """展开生成器运行环境中的 ``~`` 和环境变量并返回绝对路径。"""
    return Path(os.path.expandvars(str(value))).expanduser().resolve()


def require_dir(path: Path, *, label: str) -> None:
    if not path.is_dir():
        raise FileNotFoundError(f"{label} 目录不存在：{path}")


def require_file(path: Path, *, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} 文件不存在：{path}")


def parse_csv(value: str, *, label: str) -> list[str]:
    values = [item.strip() for item in value.split(",") if item.strip()]
    if not values:
        raise ValueError(f"{label} 不能为空")
    if len(values) != len(set(values)):
        raise ValueError(f"{label} 包含重复值：{value}")
    return values


def validate_components(values: Iterable[str], *, label: str) -> None:
    for value in values:
        if value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
            raise ValueError(f"{label} 包含不安全路径分量：{value!r}")


def validate_partition(value: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise ValueError(f"--partition 包含不安全字符：{value!r}")


def validate_sbatch_path(path: Path, *, label: str) -> None:
    if any(character.isspace() for character in str(path)):
        raise ValueError(f"{label} 不能包含空白字符：{path}")


def parse_years(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{4})(?:-(\d{4}))?", value)
    if not match:
        raise ValueError("--years 必须为 YYYY 或 YYYY-YYYY")
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if start > end:
        raise ValueError("--years 起始年份不能晚于结束年份")
    return start, end


def validate_scenarios(value: str) -> list[str]:
    scenarios = parse_csv(value, label="--scenarios")
    if "ssp560" in scenarios:
        raise ValueError("不支持 ssp560；SSP5-8.5 请使用 ssp585 指定")
    invalid = [item for item in scenarios if item not in SCENARIO_STATIONS]
    if invalid:
        raise ValueError(
            f"不支持的 SSP：{','.join(invalid)}；可选值为 ssp126,ssp245,ssp585"
        )
    return scenarios


def validate_techs(value: str) -> list[str]:
    techs = parse_csv(value, label="--techs")
    invalid = [item for item in techs if item not in TECHS]
    if invalid:
        raise ValueError(f"不支持的技术类型：{','.join(invalid)}；可选值为 wind,solar")
    return techs


def discover_regions(data_dir: Path, model: str) -> list[str]:
    root = data_dir / model
    if not root.is_dir():
        raise FileNotFoundError(f"模型输入目录不存在：{root}")
    regions = sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir()
        and path.name
        and not path.name.startswith("_")
        and not path.name.endswith("_")
        and not path.name.endswith("_repeated")
    )
    if not regions:
        raise ValueError(f"模型 {model} 下未发现有效区域：{root}")
    return regions


def resolve_regions(data_dir: Path, models: list[str], selection: str) -> dict[str, list[str]]:
    requested = parse_csv(selection, label="--regions")
    validate_components(models, label="--models")
    validate_components(requested, label="--regions")
    if "all" in requested and len(requested) != 1:
        raise ValueError("--regions all 不能与具体区域同时使用")
    result: dict[str, list[str]] = {}
    for model in models:
        available = discover_regions(data_dir, model)
        if requested == ["all"]:
            result[model] = available
            continue
        missing = [region for region in requested if region not in available]
        if missing:
            raise ValueError(
                f"模型 {model} 缺少显式指定区域：{','.join(missing)}；"
                f"输入目录={data_dir / model}"
            )
        result[model] = list(requested)
    return result


def safe_token(value: str, *, max_length: int = 28) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    token = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_")
    if not token:
        token = "unit"
    return token[:max_length]


def campaign_id(stage: str, payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(stage.encode("ascii") + b"\0" + encoded).hexdigest()[:10]


def job_name(
    stage: str,
    campaign: str,
    model: str,
    region: str,
    scenario: str,
    tech: str,
    years: str,
) -> str:
    stage_token = "s2e1" if stage == "E1" else "s2e2"
    parts = (
        stage_token,
        campaign,
        safe_token(model, max_length=20),
        safe_token(region, max_length=22),
        scenario,
        tech,
        years.replace("-", "_"),
    )
    name = "_".join(parts)
    if len(name) > 120:
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
        name = f"{name[:111]}_{digest}"
    return name


def expected_output(
    output_root: Path,
    model: str,
    region: str,
    scenario: str,
    tech: str,
    years: str,
) -> Path:
    start, end = parse_years(years)
    return (
        output_root
        / "regional_bcsd"
        / model
        / region
        / scenario
        / f"station_signals_{tech}_{model}_{region}_{scenario}_{start}-{end}.nc"
    )


def render_script(
    *,
    name: str,
    partition: str,
    cpus: int,
    log_root: Path,
    project_dir: Path,
    activate_path: Path,
    environment_name: str,
    command: Iterable[str],
) -> str:
    if cpus < 1:
        raise ValueError("核心数必须为正整数")
    return f"""#!/usr/bin/env bash
#SBATCH -J {name}
#SBATCH -p {partition}
#SBATCH -N 1
#SBATCH -n {cpus}
#SBATCH -o {log_root}/{name}_%j.out
#SBATCH -e {log_root}/{name}_%j.err
set -euo pipefail

export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

cd {shlex.quote(str(project_dir))}
# conda 环境的 activate.d 脚本（如 magics）可能引用未绑定变量，与 set -u 冲突；source 期间临时关闭 -u
set +u
source {shlex.quote(str(activate_path))} {shlex.quote(environment_name)}
set -u
{shlex.join(list(command))}
"""


def write_text(path: Path, content: str, *, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"文件已存在；确认后传 --force：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_manifest(path: Path, payload: dict, *, force: bool) -> None:
    payload = dict(payload)
    payload["generated_at"] = datetime.now().astimezone().isoformat()
    write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        force=force,
    )
