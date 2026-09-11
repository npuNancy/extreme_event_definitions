from __future__ import annotations

import json
import subprocess
from pathlib import Path

from infos.hpc_step2 import create_step2_jobs
from infos.hpc_step2_monitor_common import classify_unit


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("test\n", encoding="utf-8")
    return path


def test_unified_generator_emits_one_bcsd_job_per_unit(tmp_path: Path, monkeypatch) -> None:
    project = Path(__file__).resolve().parents[1]
    data_dir = tmp_path / "data/bcsd_outputs"
    (data_dir / "NESM3/Germany/NESM3").mkdir(parents=True)
    stations_dir = tmp_path / "data/stations"
    for name in ("stations_SSP1-2.6.csv", "stations_SSP2-4.5.csv"):
        _touch(stations_dir / name)
    shp = tmp_path / "maps/ne.shp"
    for suffix in (".shp", ".shx", ".dbf"):
        _touch(shp.with_suffix(suffix))
    activate = _touch(tmp_path / "activate")
    monkeypatch.setattr(
        create_step2_jobs,
        "_build_station_inventory",
        lambda *args: {
            ("Germany", scenario, tech): 1
            for scenario in ("ssp126", "ssp245")
            for tech in ("wind", "solar")
        },
    )
    args = create_step2_jobs.build_parser().parse_args([
        "--models", "NESM3",
        "--regions", "Germany",
        "--scenarios", "ssp126,ssp245",
        "--techs", "wind,solar",
        "--years", "2015-2060",
        "--project-dir", str(project),
        "--data-dir", str(data_dir),
        "--stations-dir", str(stations_dir),
        "--shp", str(shp),
        "--activate-path", str(activate),
        "--job-root", str(tmp_path / "jobs"),
        "--log-root", str(tmp_path / "logs"),
    ])
    manifest_path = create_step2_jobs.run(args)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    scripts = sorted((tmp_path / "jobs").glob("job_*.sh"))
    assert manifest["stage"] == "STEP2"
    assert len(manifest["units"]) == 4
    assert all(unit["has_stations"] for unit in manifest["units"])
    assert all("step2_complete_extreme_events.py" in unit["command"] for unit in manifest["units"])
    assert all("cf_root" not in unit["command"] for unit in manifest["units"])
    assert all("threshold_dir" not in unit["command"] for unit in manifest["units"])
    subprocess.run(["bash", "-n", *map(str, scripts)], check=True)


def test_unified_monitor_accepts_completed_no_station_unit() -> None:
    classification, reason = classify_unit(
        stage="STEP2",
        output_exists=False,
        queue_record=None,
        accounting_record={"state": "COMPLETED"},
        previous=None,
        has_stations=False,
    )
    assert classification == "SKIPPED_NO_STATIONS"
    assert "no stations" in reason
