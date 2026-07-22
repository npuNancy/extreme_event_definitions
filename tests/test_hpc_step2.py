"""SCNet step2 生成器、监控分类和失败传播测试。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np
import pandas as pd
import pytest

from infos.hpc_step2_E1 import create_step2_E1_jobs as e1_jobs
from infos.hpc_step2_E2 import create_step2_E2_jobs as e2_jobs
from infos import hpc_step2_monitor_common as monitor_common
from infos.hpc_step2_common import resolve_regions
from infos.hpc_step2_monitor_common import classify_unit
from scripts import patch_pipelineB_low_resource as patch_low_resource
from scripts import station_signals_direct

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("test\n", encoding="utf-8")
    return path


def _common_tree(tmp_path: Path) -> dict[str, Path]:
    data_dir = tmp_path / "data/bcsd_outputs"
    (data_dir / "NESM3/Germany/NESM3").mkdir(parents=True)
    stations_dir = tmp_path / "data/stations"
    for name in (
        "stations_SSP1-2.6.csv",
        "stations_SSP2-4.5.csv",
        "stations_SSP5-6.0.csv",
    ):
        _touch(stations_dir / name)
    shp = tmp_path / "data/maps/natural_earth/ne_110m_admin_0_countries.shp"
    for suffix in (".shp", ".shx", ".dbf"):
        _touch(shp.with_suffix(suffix))
    activate = _touch(tmp_path / "miniconda3/bin/activate")
    cf_root = tmp_path / "data/cfs"
    cf_root.mkdir(parents=True)
    thresholds = tmp_path / "thresholds/sparse_station_ERA5Land_2015-2024"
    for scenario in ("ssp126", "ssp245"):
        for tech in ("wind", "solar"):
            _touch(
                thresholds
                / f"low_resource_threshold_sparse_{scenario}_{tech}_ERA5Land_2015-2024.nc"
            )
    return {
        "data_dir": data_dir,
        "stations_dir": stations_dir,
        "shp": shp,
        "activate": activate,
        "cf_root": cf_root,
        "thresholds": thresholds,
    }


def test_e1_generator_tiny_matrix_and_bash_syntax(tmp_path: Path) -> None:
    tree = _common_tree(tmp_path)
    job_root = tmp_path / "jobs/E1"
    args = e1_jobs.build_parser().parse_args(
        [
            "--models", "NESM3",
            "--regions", "Germany",
            "--scenarios", "ssp126,ssp245",
            "--techs", "wind,solar",
            "--years", "2015-2060",
            "--project-dir", str(PROJECT_ROOT),
            "--data-dir", str(tree["data_dir"]),
            "--stations-dir", str(tree["stations_dir"]),
            "--shp", str(tree["shp"]),
            "--activate-path", str(tree["activate"]),
            "--job-root", str(job_root),
            "--log-root", str(tmp_path / "logs/E1"),
        ]
    )
    manifest_path = e1_jobs.run(args)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    scripts = sorted(job_root.glob("job_*.sh"))
    assert len(manifest["units"]) == 4
    assert len(scripts) == 4
    assert len({path.name for path in scripts}) == 4
    text = scripts[0].read_text(encoding="utf-8")
    assert "#SBATCH -p wzhctest" in text
    assert "#SBATCH -N 1" in text
    assert "#SBATCH -n 6" in text
    assert "#SBATCH --time" not in text
    assert "source " in text and " climate" in text
    assert "--region Germany" in text
    assert "--tech " in text
    assert "--overwrite" not in text
    assert any("stations_SSP1-2.6.csv" in unit["command"] for unit in manifest["units"])
    assert any("stations_SSP2-4.5.csv" in unit["command"] for unit in manifest["units"])
    subprocess.run(["bash", "-n", *map(str, scripts)], check=True)


def test_e2_generator_tiny_matrix_and_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _common_tree(tmp_path)
    job_root = tmp_path / "jobs/E2"
    monkeypatch.setattr(
        e2_jobs,
        "_build_station_inventory",
        lambda stations_dir, shp, scenarios, regions_by_model, techs: {
            ("Germany", scenario, tech): 2
            for scenario in scenarios
            for tech in techs
        },
    )
    args = e2_jobs.build_parser().parse_args(
        [
            "--models", "NESM3",
            "--regions", "Germany",
            "--scenarios", "ssp126,ssp245",
            "--techs", "wind,solar",
            "--years", "2015-2060",
            "--project-dir", str(PROJECT_ROOT),
            "--data-dir", str(tree["data_dir"]),
            "--cf-root", str(tree["cf_root"]),
            "--stations-dir", str(tree["stations_dir"]),
            "--shp", str(tree["shp"]),
            "--threshold-dir", str(tree["thresholds"]),
            "--activate-path", str(tree["activate"]),
            "--job-root", str(job_root),
            "--log-root", str(tmp_path / "logs/E2"),
        ]
    )
    manifest_path = e2_jobs.run(args)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    scripts = sorted(job_root.glob("job_*.sh"))
    assert len(manifest["units"]) == 4
    assert len(scripts) == 4
    for unit in manifest["units"]:
        assert unit["expected_output"] == unit["e1_expected_output"]
        assert "/outputs/station_signals/regional_bcsd/NESM3/Germany/" in unit["expected_output"]
        assert "--baseline_years 2015-2024" in unit["command"]
        assert "--stations_csv " in unit["command"]
        assert "--shp " in unit["command"]
        assert unit["station_count"] == 2
        assert unit["has_stations"] is True
    subprocess.run(["bash", "-n", *map(str, scripts)], check=True)


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    tree = _common_tree(tmp_path)
    job_root = tmp_path / "dry-run-jobs"
    args = e1_jobs.build_parser().parse_args(
        [
            "--models", "NESM3",
            "--regions", "Germany",
            "--scenarios", "ssp126",
            "--techs", "wind",
            "--years", "2030-2060",
            "--project-dir", str(PROJECT_ROOT),
            "--data-dir", str(tree["data_dir"]),
            "--stations-dir", str(tree["stations_dir"]),
            "--shp", str(tree["shp"]),
            "--activate-path", str(tree["activate"]),
            "--job-root", str(job_root),
            "--log-root", str(tmp_path / "dry-run-logs"),
            "--dry-run",
        ]
    )
    e1_jobs.run(args)
    assert not job_root.exists()
    assert not (tmp_path / "dry-run-logs").exists()


def test_ssp560_has_targeted_error() -> None:
    args = e1_jobs.build_parser().parse_args(
        [
            "--models", "NESM3",
            "--regions", "Germany",
            "--scenarios", "ssp560",
            "--years", "2015-2060",
        ]
    )
    with pytest.raises(ValueError, match="ssp585"):
        e1_jobs.run(args)


def test_regions_all_supports_canesm5(tmp_path: Path) -> None:
    for region in ("Germany", "Japan"):
        (tmp_path / "CANESM5" / region / "CANESM5").mkdir(parents=True)
    assert resolve_regions(tmp_path, ["CANESM5"], "all") == {
        "CANESM5": ["Germany", "Japan"]
    }


@pytest.mark.parametrize(
    ("stage", "output_exists", "accounting", "has_stations", "expected"),
    [
        ("E1", True, None, True, "SUCCEEDED_PREEXISTING"),
        ("E2", True, None, True, "NOT_SUBMITTED"),
        ("E2", True, {"state": "COMPLETED"}, True, "SUCCEEDED"),
        ("E2", False, {"state": "COMPLETED"}, False, "SKIPPED_NO_STATIONS"),
        ("E1", False, {"state": "COMPLETED"}, True, "INCOMPLETE_OUTPUT"),
        ("E1", True, {"state": "FAILED"}, True, "FAILED"),
    ],
)
def test_monitor_classification(
    stage: str,
    output_exists: bool,
    accounting: dict[str, str] | None,
    has_stations: bool,
    expected: str,
) -> None:
    classification, _ = classify_unit(
        stage=stage,
        output_exists=output_exists,
        queue_record=None,
        accounting_record=accounting,
        previous=None,
        has_stations=has_stations,
    )
    assert classification == expected


def test_e1_missing_requested_year_is_fatal(tmp_path: Path) -> None:
    class MissingAdapter:
        def load_wind_weather(self, task):
            raise FileNotFoundError("missing test input")

    args = SimpleNamespace(
        years="2015-2016",
        output_root=str(tmp_path),
        data_dir=str(tmp_path / "bcsd"),
        source="regional_bcsd",
        model="NESM3",
        overwrite=False,
        dry_run=False,
        spatial_interp="nearest",
    )
    stations = {"wind": pd.DataFrame({"dummy": [1]})}
    with pytest.raises(RuntimeError, match="无法加载请求年份"):
        station_signals_direct._process_tech(
            MissingAdapter(), args, stations, "Germany", "ssp126", "wind", {}
        )


def test_e2_processing_failure_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["patch_pipelineB_low_resource.py"])
    monkeypatch.setattr(patch_low_resource, "setup_logging", lambda *_: None)
    monkeypatch.setattr(
        patch_low_resource, "_find_station_files", lambda args: [Path("one.nc")]
    )

    def fail(path, args):
        raise RuntimeError("test failure")

    monkeypatch.setattr(patch_low_resource, "_process_file", fail)
    with pytest.raises(SystemExit) as exc:
        patch_low_resource.main()
    assert exc.value.code == 1


def test_e2_unit_without_stations_skips_successfully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    args = SimpleNamespace(
        region="Germany",
        scenario="ssp126",
        tech="wind",
    )
    monkeypatch.setattr(
        patch_low_resource,
        "_load_unit_stations",
        lambda args, region, tech: pd.DataFrame(),
    )
    with caplog.at_level("INFO"):
        assert patch_low_resource._process_unit(args) is False
    assert "国家内无场站，跳过" in caplog.text


def test_e2_creates_low_resource_file_when_e1_output_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cf_file = tmp_path / "solar.nc"
    with h5py.File(cf_file, "w") as handle:
        time = handle.create_dataset("time", data=np.array([0.0, 394464.0]))
        time.attrs["units"] = "hours since 2015-01-01 00:00:00"
        handle.create_dataset("lat", data=np.array([50.0]))
        handle.create_dataset("lon", data=np.array([10.0]))
    threshold_dir = tmp_path / "thresholds"
    threshold = _touch(
        threshold_dir
        / "low_resource_threshold_sparse_ssp126_solar_ERA5Land_2015-2024.nc"
    )
    stations = pd.DataFrame(
        {
            "lon": [10.0],
            "lat": [50.0],
            "type": ["solar"],
            "activation_year": [2030],
            "capacity_gw": [1.0],
        }
    )
    match = SimpleNamespace(
        stations=stations,
        valid=np.array([True]),
        dist_deg=np.array([0.0]),
        method="nearest",
    )
    result = patch_low_resource.cf_low_resource.LowResourceResult(
        mask=np.ones((2, 1), dtype=np.int8),
        cf_file=cf_file,
        timestep_hours=1.0,
        window_steps=24,
        grid_shape=(1, 1),
        lon360=False,
        threshold_file=threshold,
        valid=np.array([True]),
    )
    monkeypatch.setattr(
        patch_low_resource.cf_low_resource, "find_cf_file", lambda *a, **k: cf_file
    )
    monkeypatch.setattr(
        patch_low_resource.sm, "match_regular_weighted", lambda *a, **k: match
    )
    monkeypatch.setattr(
        patch_low_resource.cf_low_resource,
        "compute_station_low_resource",
        lambda *a, **k: result,
    )
    captured: dict = {}

    def fake_write(path, masks, times, match, tech, **kwargs):
        captured.update(masks=masks, times=times, kwargs=kwargs)
        Path(path).write_bytes(b"netcdf")

    monkeypatch.setattr(patch_low_resource.sm, "write_station_signals", fake_write)
    target = tmp_path / "out.nc"
    args = SimpleNamespace(
        cf_root=str(tmp_path),
        source="regional_bcsd",
        model="CANESM5",
        years="2015-2060",
        threshold_dir=str(threshold_dir),
        baseline_years="2015-2024",
        spatial_interp="nearest",
        max_dist=0.15,
        station_chunk=128,
        time_chunk=512,
        stations_csv=str(tmp_path / "stations.csv"),
        compress_level=4,
    )
    assert patch_low_resource._create_low_resource_file(
        target,
        args,
        region="Germany",
        scenario="ssp126",
        tech="solar",
        stations=stations,
    )
    assert target.read_bytes() == b"netcdf"
    np.testing.assert_array_equal(
        captured["masks"]["signal_low_resource"], np.array([[0], [1]], dtype=np.int8)
    )
    assert captured["kwargs"]["supported"] == ["low_resource"]
    assert captured["kwargs"]["attrs_extra"]["e2_created_without_e1"] == "true"


def _monitor_manifest(stage: str, tmp_path: Path) -> dict:
    units = []
    for index in range(2):
        output = str(tmp_path / f"output-{index}.nc")
        unit = {
            "unit_id": f"NESM3|Region{index}|ssp126|wind|2015-2060",
            "model": "NESM3",
            "region": f"Region{index}",
            "scenario": "ssp126",
            "tech": "wind",
            "years": "2015-2060",
            "job_name": f"s2{stage.lower()}_campaign_job{index}",
            "script_path": str(tmp_path / f"job-{index}.sh"),
            "expected_output": output,
            "command": "python test.py",
        }
        if stage == "E2":
            unit["e1_expected_output"] = output
        units.append(unit)
    return {
        "stage": stage,
        "campaign_id": "campaign",
        "job_prefix": f"s2{stage.lower()}_campaign_",
        "units": units,
    }


def _monitor_args() -> SimpleNamespace:
    return SimpleNamespace(
        server="test-server",
        history_start="2026-07-22",
        ssh_timeout=10,
        no_submit=False,
        max_active_jobs=20,
    )


def test_e1_monitor_submits_only_not_submitted_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _monitor_manifest("E1", tmp_path)
    monkeypatch.setattr(monitor_common, "_queue_records", lambda *a, **k: {})
    monkeypatch.setattr(monitor_common, "_accounting_records", lambda *a, **k: {})
    monkeypatch.setattr(
        monitor_common,
        "_stat_nonempty",
        lambda server, paths_by_key, **kwargs: {key: False for key in paths_by_key},
    )
    submitted: list[str] = []

    def submit_one(**kwargs):
        submitted.append(kwargs["script_path"])
        return str(100 + len(submitted)), "submitted"

    monkeypatch.setattr(monitor_common, "_submit_one", submit_one)
    snapshot = monitor_common.monitor_once(
        stage="E1",
        state_dir=tmp_path / "state",
        args=_monitor_args(),
        manifest_path="/remote/manifest.json",
        manifest=manifest,
    )
    assert len(submitted) == 2
    assert {record["classification"] for record in snapshot["units"].values()} == {"ACTIVE"}


def test_e2_monitor_missing_e1_outputs_does_not_block_submissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _monitor_manifest("E2", tmp_path)
    monkeypatch.setattr(monitor_common, "_queue_records", lambda *a, **k: {})
    monkeypatch.setattr(monitor_common, "_accounting_records", lambda *a, **k: {})

    def stat_nonempty(server, paths_by_key, **kwargs):
        keys = list(paths_by_key)
        return {key: index == 0 for index, key in enumerate(keys)}

    monkeypatch.setattr(monitor_common, "_stat_nonempty", stat_nonempty)

    submitted: list[str] = []

    def submit_one(**kwargs):
        submitted.append(kwargs["script_path"])
        return str(200 + len(submitted)), "submitted"

    monkeypatch.setattr(monitor_common, "_submit_one", submit_one)
    snapshot = monitor_common.monitor_once(
        stage="E2",
        state_dir=tmp_path / "state",
        args=_monitor_args(),
        manifest_path="/remote/manifest.json",
        manifest=manifest,
    )
    assert snapshot["e1_gate_ready"] is True
    assert snapshot["stop_reason"] == ""
    assert len(submitted) == 2
