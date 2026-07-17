from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import h5py
import numpy as np
import pandas as pd

import step1_split_E1_union_stations as e1
import step1_split_E2a_extract_union_station_cf_monthly as e2a
import step1_split_E2b_merge_union_station_cf_cache as e2b
import step1_split_E3_thresholds_from_union_cache as e3
from grid_extreme_signals import cf_low_resource
from infos.hpc_step1 import create_jobs_kunshan
from scripts.hpc_step1_common import (
    CHUNK_PLAN_NAME,
    build_chunk_plan,
    create_station_cache,
)


def _write_stations(path: Path, rows: list[str]) -> None:
    path.write_text(
        "year,type,lon,lat,capacity_gw\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )


def test_chunk_plan_has_21_chunks_covering_120_months():
    plan = build_chunk_plan()

    assert len(plan) == 21
    assert int(plan["expected_month_count"].sum()) == 120
    assert plan.iloc[0]["chunk_id"] == "2015_01"
    assert bool(plan.iloc[0]["is_probe"])
    assert int((~plan["is_probe"]).sum()) == 20


def test_e1_builds_union_maps_and_chunk_plan(tmp_path):
    csv126 = tmp_path / "stations_SSP1-2.6.csv"
    csv245 = tmp_path / "stations_SSP2-4.5.csv"
    csv585 = tmp_path / "stations_SSP5-8.5.csv"
    _write_stations(csv126, [
        "2030,wind,116.0,40.0,1.0",
        "2040,wind,116.0,40.0,2.0",
        "2030,solar,117.0,39.0,3.0",
    ])
    _write_stations(csv245, [
        "2030,wind,116.0,40.0,4.0",
        "2030,solar,118.0,38.0,5.0",
    ])
    _write_stations(csv585, ["2030,wind,119.0,37.0,6.0"])
    output = tmp_path / "union"

    e1.run(Namespace(
        stations_csv_ssp126=str(csv126),
        stations_csv_ssp245=str(csv245),
        stations_csv_ssp585=str(csv585),
        output_dir=str(output),
        key="lon,lat,type",
        overwrite=False,
    ))

    union = pd.read_csv(output / "stations_union_ssp126_ssp245_ssp585.csv")
    map126 = pd.read_csv(output / "station_index_map_ssp126.csv")
    map245 = pd.read_csv(output / "station_index_map_ssp245.csv")
    plan = pd.read_csv(output / CHUNK_PLAN_NAME)
    assert len(union) == 4
    assert len(map126) == 2
    assert len(map245) == 2
    shared126 = map126.loc[map126["type"] == "wind", "union_station_index"].item()
    shared245 = map245.loc[map245["type"] == "wind", "union_station_index"].item()
    assert shared126 == shared245
    assert len(plan) == 21


def test_e2a_extracts_union_station_cf(tmp_path):
    cf_root = tmp_path / "cfs"
    cf_dir = cf_root / "CFs_of_wind_ERA5Land"
    cf_dir.mkdir(parents=True)
    times = pd.date_range("2015-01-01", periods=48, freq="h")
    values = np.arange(48 * 4, dtype=np.float32).reshape(48, 2, 2)
    with h5py.File(cf_dir / "wind_cf_2015_01.nc", "w") as handle:
        time = handle.create_dataset(
            "time", data=times.to_numpy(dtype="datetime64[s]").astype(np.int64)
        )
        time.attrs["units"] = np.bytes_("seconds since 1970-01-01")
        handle.create_dataset("lat", data=np.array([40.0, 39.0]))
        handle.create_dataset("lon", data=np.array([116.0, 117.0]))
        handle.create_dataset("wind_cf", data=values, chunks=(24, 2, 2))
    union_csv = tmp_path / "union.csv"
    pd.DataFrame([{
        "union_station_index": 3,
        "lon": 116.25,
        "lat": 39.25,
        "type": "wind",
    }]).to_csv(union_csv, index=False)
    args = Namespace(
        cf_root=str(cf_root),
        union_stations_csv=str(union_csv),
        tech="wind",
        year_month_start="2015-01",
        year_month_end="2015-01",
        threshold_interp="bilinear",
        output_dir=str(tmp_path / "chunks"),
        compress_level=1,
        cache_time_chunk=None,
        overwrite=False,
    )

    path = e2a.run(args)

    with h5py.File(path, "r") as handle:
        assert cf_low_resource.decode_attr(handle.attrs["cache_kind"]) == (
            "era5land_union_station_cf_chunk"
        )
        assert handle["cf"].shape == (48, 1)
        assert handle["union_station_index"][:].tolist() == [3]
        np.testing.assert_allclose(handle["weight"][:].sum(axis=1), [1.0])


def test_e2b_merges_21_time_chunks(tmp_path):
    plan = build_chunk_plan()
    plan_path = tmp_path / CHUNK_PLAN_NAME
    plan.to_csv(plan_path, index=False)
    chunk_dir = tmp_path / "chunks"
    chunk_dir.mkdir()
    total_time = 0
    for row in plan.itertuples(index=False):
        start = pd.Timestamp(f"{row.year_month_start}-01")
        end = pd.Timestamp(row.year_month_end) + pd.offsets.MonthEnd(1)
        times = pd.date_range(start, end + pd.Timedelta(hours=23), freq="h")
        path = chunk_dir / row.cache_file.replace("{tech}", "wind")
        ds = create_station_cache(path, n_time=len(times), n_station=1, compress_level=1)
        ds["time"][:] = times.to_numpy(dtype="datetime64[s]").astype(np.int64)
        ds["union_station_index"][:] = [0]
        ds["station_lon"][:] = [116.0]
        ds["station_lat"][:] = [40.0]
        ds["station_type"][:] = [1]
        ds["era5_lat_idx"][:] = [[0, 0, 0, 0]]
        ds["era5_lon_idx"][:] = [[0, 0, 0, 0]]
        ds["era5_lat"][:] = [[40.0] * 4]
        ds["era5_lon"][:] = [[116.0] * 4]
        ds["weight"][:] = [[1.0, 0.0, 0.0, 0.0]]
        ds["cf"][:] = np.full((len(times), 1), 0.5, dtype=np.float32)
        ds.cache_kind = "era5land_union_station_cf_chunk"
        ds.tech = "wind"
        ds.year_month_start = row.year_month_start
        ds.year_month_end = row.year_month_end
        ds.threshold_interp = "nearest_valid"
        ds.source_files = ""
        ds.close()
        total_time += len(times)
    output = tmp_path / "merged.nc"

    e2b.run(Namespace(
        time_chunk_dir=str(chunk_dir),
        chunk_plan_csv=str(plan_path),
        tech="wind",
        baseline_years="2015-2024",
        threshold_interp="nearest_valid",
        output_cache=str(output),
        compress_level=1,
        overwrite=False,
    ))

    with h5py.File(output, "r") as handle:
        assert cf_low_resource.decode_attr(handle.attrs["chunk_count"]) == "21"
        assert handle["cf"].shape == (total_time, 1)
        times = cf_low_resource.read_time(handle)
        assert times[0] == pd.Timestamp("2015-01-01")
        assert times[-1] == pd.Timestamp("2024-12-31 23:00:00")


def test_e3_writes_three_scenario_threshold_files(tmp_path):
    cache_path = tmp_path / "merged.nc"
    times = pd.date_range("2015-01-01", periods=72, freq="h")
    ds = create_station_cache(cache_path, n_time=len(times), n_station=1, compress_level=1)
    ds["time"][:] = times.to_numpy(dtype="datetime64[s]").astype(np.int64)
    ds["union_station_index"][:] = [0]
    ds["station_lon"][:] = [116.0]
    ds["station_lat"][:] = [40.0]
    ds["station_type"][:] = [1]
    ds["era5_lat_idx"][:] = [[0, 0, 0, 0]]
    ds["era5_lon_idx"][:] = [[0, 0, 0, 0]]
    ds["era5_lat"][:] = [[40.0] * 4]
    ds["era5_lon"][:] = [[116.0] * 4]
    ds["weight"][:] = [[1.0, 0.0, 0.0, 0.0]]
    ds["cf"][:] = np.linspace(0.1, 0.9, len(times), dtype=np.float32)[:, None]
    ds.cache_kind = "era5land_union_station_cf"
    ds.tech = "wind"
    ds.baseline_years = "2015-2024"
    ds.baseline_years_effective = "2015-2024"
    ds.chunk_count = "21"
    ds.interpolation_method = "nearest_valid"
    ds.corner_order = "nearest_valid,unused,unused,unused"
    ds.source_files = ""
    ds.close()

    union_csv = tmp_path / "union.csv"
    pd.DataFrame([{
        "union_station_index": 0, "lon": 116.0, "lat": 40.0, "type": "wind",
    }]).to_csv(union_csv, index=False)
    maps = {}
    for scenario in ("ssp126", "ssp245", "ssp585"):
        path = tmp_path / f"map_{scenario}.csv"
        pd.DataFrame([{
            "scenario_station_index": 0,
            "union_station_index": 0,
            "lon": 116.0,
            "lat": 40.0,
            "type": "wind",
            "activation_year": 2030,
            "capacity_gw": 1.0,
        }]).to_csv(path, index=False)
        maps[scenario] = path
    args = Namespace(
        union_station_cf_cache=str(cache_path),
        union_stations_csv=str(union_csv),
        index_map_ssp126=str(maps["ssp126"]),
        index_map_ssp245=str(maps["ssp245"]),
        index_map_ssp585=str(maps["ssp585"]),
        tech="wind",
        baseline_years="2015-2024",
        output_dir=str(tmp_path / "thresholds"),
        station_chunk=1,
        compress_level=1,
        scenarios="ssp126,ssp245,ssp585",
        n_jobs=1,
        overwrite=False,
    )

    outputs = e3.run(args)

    assert len(outputs) == 3
    for scenario, path in zip(("ssp126", "ssp245", "ssp585"), outputs, strict=True):
        with h5py.File(path, "r") as handle:
            assert cf_low_resource.decode_attr(handle.attrs["scenario"]) == scenario
            assert handle["clim"].shape == (12, 24, 1)
            assert np.isfinite(handle["threshold"][:]).all()


def test_job_generator_uses_chunk_plan_without_overwrite(tmp_path, monkeypatch):
    project = tmp_path / "project"
    union_dir = project / "outputs/cache/era5land_union_station_cf/union_stations"
    union_dir.mkdir(parents=True)
    build_chunk_plan().to_csv(union_dir / CHUNK_PLAN_NAME, index=False)
    monkeypatch.setitem(
        create_jobs_kunshan.SERVER_CONFIG,
        "scnet-kunshan-185",
        {
            "tech": "wind",
            "user": "test",
            "project_dir": str(project),
            "activate": "/tmp/test-venv/bin/activate",
        },
    )

    jobs_dir = create_jobs_kunshan.run(Namespace(
        server="scnet-kunshan-185",
        partition=None,
        history_start=None,
        kernel_num=4,
        e2a_time="24:00:00",
        e2b_time="08:00:00",
        e3_time="08:00:00",
        force=False,
        dry_run=False,
    ))

    e2a_scripts = sorted(jobs_dir.glob("job_step1_E2a_wind_*.sh"))
    assert len(e2a_scripts) == 21
    assert "--overwrite" not in e2a_scripts[0].read_text(encoding="utf-8")
    formal_submit = (
        jobs_dir / "submit_step1_split_E2a_batches_wind.sh"
    ).read_text(encoding="utf-8")
    assert formal_submit.count("sbatch --parsable") == 20


def test_e3_handles_unsorted_positions(tmp_path):
    """ssp245/585 场站在 union 中无序时，E3 不得报 h5py indexing 顺序错误。

    回归：算完 ssp126 后进入 ssp245 时 'Indexing elements must be in increasing order'。
    h5py fancy indexing 要求索引递增，故 _match_from_cache 和 cf 块读取都需排序恢复。
    """
    cache_path = tmp_path / "merged.nc"
    n_station = 4
    times = pd.date_range("2015-01-01", periods=72, freq="h")
    ds = create_station_cache(cache_path, n_time=len(times), n_station=n_station, compress_level=1)
    ds["time"][:] = times.to_numpy(dtype="datetime64[s]").astype(np.int64)
    ds["union_station_index"][:] = np.arange(n_station)
    ds["station_lon"][:] = [116.0, 117.0, 118.0, 119.0]
    ds["station_lat"][:] = [40.0, 39.5, 39.0, 38.5]
    ds["station_type"][:] = 1
    ds["era5_lat_idx"][:] = [[0, 0, 0, 0]] * n_station
    ds["era5_lon_idx"][:] = [[0, 0, 0, 0]] * n_station
    ds["era5_lat"][:] = [[40.0] * 4, [39.5] * 4, [39.0] * 4, [38.5] * 4]
    ds["era5_lon"][:] = [[116.0] * 4, [117.0] * 4, [118.0] * 4, [119.0] * 4]
    ds["weight"][:] = [[1.0, 0.0, 0.0, 0.0]] * n_station
    # cf[:, s] 编码 station s，使各 station 阈值互不相同，便于校验读取顺序
    for s in range(n_station):
        ds["cf"][:, s] = np.linspace(0.1 + 0.2 * s, 0.9 + 0.2 * s, len(times), dtype=np.float32)
    ds.cache_kind = "era5land_union_station_cf"
    ds.tech = "wind"
    ds.baseline_years = "2015-2024"
    ds.baseline_years_effective = "2015-2024"
    ds.chunk_count = "21"
    ds.interpolation_method = "nearest_valid"
    ds.corner_order = "nearest_valid,unused,unused,unused"
    ds.source_files = ""
    ds.close()

    union_csv = tmp_path / "union.csv"
    pd.DataFrame([
        {"union_station_index": i, "lon": 116.0 + i, "lat": 40.0 - 0.5 * i, "type": "wind"}
        for i in range(n_station)
    ]).to_csv(union_csv, index=False)

    # ssp245/585 的 union_station_index 故意无序（修复前触发 h5py 顺序错误）
    order_by_scenario = {"ssp126": [0, 1, 2, 3], "ssp245": [3, 1, 0, 2], "ssp585": [2, 0, 3, 1]}
    map_paths = {}
    for scenario, order in order_by_scenario.items():
        path = tmp_path / f"map_{scenario}.csv"
        rows = []
        for sidx, uidx in enumerate(order):
            rows.append({
                "scenario_station_index": sidx,
                "union_station_index": uidx,
                "lon": 116.0 + uidx, "lat": 40.0 - 0.5 * uidx,
                "type": "wind", "activation_year": 2030, "capacity_gw": 1.0,
            })
        pd.DataFrame(rows).to_csv(path, index=False)
        map_paths[scenario] = path

    outputs = e3.run(Namespace(
        union_station_cf_cache=str(cache_path),
        union_stations_csv=str(union_csv),
        index_map_ssp126=str(map_paths["ssp126"]),
        index_map_ssp245=str(map_paths["ssp245"]),
        index_map_ssp585=str(map_paths["ssp585"]),
        tech="wind",
        baseline_years="2015-2024",
        output_dir=str(tmp_path / "thresholds"),
        station_chunk=2,
        compress_level=1,
        scenarios="ssp126,ssp245,ssp585",
        n_jobs=1,
        overwrite=False,
    ))

    assert len(outputs) == 3
    with h5py.File(outputs[0], "r") as handle:  # ssp126 有序
        thr126 = handle["threshold"][:]
    with h5py.File(outputs[1], "r") as handle:  # ssp245 无序 [3, 1, 0, 2]
        thr245 = handle["threshold"][:]
    assert np.isfinite(thr245).all()
    # ssp245 scenario station j 取自 union order[j]，应等于 ssp126（union 顺序）对应位置
    np.testing.assert_allclose(thr245, thr126[np.array([3, 1, 0, 2])])


def _build_multi_station_cache(cache_path, n_station=4, n_time=72):
    """合成多 station union cache（wind），cf 编码 station 使各站阈值不同。"""
    times = pd.date_range("2015-01-01", periods=n_time, freq="h")
    ds = create_station_cache(cache_path, n_time=n_time, n_station=n_station, compress_level=1)
    ds["time"][:] = times.to_numpy(dtype="datetime64[s]").astype(np.int64)
    ds["union_station_index"][:] = np.arange(n_station)
    ds["station_lon"][:] = [116.0 + i for i in range(n_station)]
    ds["station_lat"][:] = [40.0 - 0.5 * i for i in range(n_station)]
    ds["station_type"][:] = 1
    ds["era5_lat_idx"][:] = [[0, 0, 0, 0]] * n_station
    ds["era5_lon_idx"][:] = [[0, 0, 0, 0]] * n_station
    ds["era5_lat"][:] = [[40.0 - 0.5 * i] * 4 for i in range(n_station)]
    ds["era5_lon"][:] = [[116.0 + i] * 4 for i in range(n_station)]
    ds["weight"][:] = [[1.0, 0.0, 0.0, 0.0]] * n_station
    for s in range(n_station):
        ds["cf"][:, s] = np.linspace(0.1 + 0.2 * s, 0.9 + 0.2 * s, n_time, dtype=np.float32)
    ds.cache_kind = "era5land_union_station_cf"
    ds.tech = "wind"
    ds.baseline_years = "2015-2024"
    ds.baseline_years_effective = "2015-2024"
    ds.chunk_count = "21"
    ds.interpolation_method = "nearest_valid"
    ds.corner_order = "nearest_valid,unused,unused,unused"
    ds.source_files = ""
    ds.close()


def _build_union_and_maps(tmp_path, order_by_scenario):
    union_csv = tmp_path / "union.csv"
    pd.DataFrame([
        {"union_station_index": i, "lon": 116.0 + i, "lat": 40.0 - 0.5 * i, "type": "wind"}
        for i in range(4)
    ]).to_csv(union_csv, index=False)
    map_paths = {}
    for scenario, order in order_by_scenario.items():
        path = tmp_path / f"map_{scenario}.csv"
        rows = [
            {"scenario_station_index": s, "union_station_index": u,
             "lon": 116.0 + u, "lat": 40.0 - 0.5 * u, "type": "wind",
             "activation_year": 2030, "capacity_gw": 1.0}
            for s, u in enumerate(order)
        ]
        pd.DataFrame(rows).to_csv(path, index=False)
        map_paths[scenario] = path
    return union_csv, map_paths


def test_e3_scenarios_single_matches_full(tmp_path):
    """--scenarios 单 SSP 只输出该 SSP 文件，且数值与全量一致。"""
    cache_path = tmp_path / "merged.nc"
    _build_multi_station_cache(cache_path)
    order_by_scenario = {"ssp126": [0, 1, 2, 3], "ssp245": [3, 1, 0, 2], "ssp585": [2, 0, 3, 1]}
    union_csv, map_paths = _build_union_and_maps(tmp_path, order_by_scenario)
    base = dict(
        union_station_cf_cache=str(cache_path), union_stations_csv=str(union_csv),
        index_map_ssp126=str(map_paths["ssp126"]), index_map_ssp245=str(map_paths["ssp245"]),
        index_map_ssp585=str(map_paths["ssp585"]), tech="wind", baseline_years="2015-2024",
        compress_level=1, overwrite=False, n_jobs=1,
    )
    full_dir = tmp_path / "full"
    e3.run(Namespace(**base, scenarios="ssp126,ssp245,ssp585",
                     output_dir=str(full_dir), station_chunk=2))
    single_dir = tmp_path / "single"
    e3.run(Namespace(**base, scenarios="ssp245",
                     output_dir=str(single_dir), station_chunk=2))

    # 单 ssp245 只产 ssp245，不产 ssp126/ssp585
    assert cf_low_resource.sparse_threshold_file_for_scenario_tech(
        single_dir, "ssp245", "wind", "2015-2024").exists()
    for s in ("ssp126", "ssp585"):
        assert not cf_low_resource.sparse_threshold_file_for_scenario_tech(
            single_dir, s, "wind", "2015-2024").exists()
    # 数值与全量一致
    full_p = cf_low_resource.sparse_threshold_file_for_scenario_tech(
        full_dir, "ssp245", "wind", "2015-2024")
    single_p = cf_low_resource.sparse_threshold_file_for_scenario_tech(
        single_dir, "ssp245", "wind", "2015-2024")
    with h5py.File(full_p, "r") as fa, h5py.File(single_p, "r") as fb:
        np.testing.assert_allclose(fa["threshold"][:], fb["threshold"][:])
        np.testing.assert_allclose(fa["clim"][:], fb["clim"][:])


def test_e3_n_jobs_parallel_equivalence(tmp_path):
    """--n_jobs=1 与 --n_jobs=4 输出数值一致（并行只改速度不改结果）。"""
    cache_path = tmp_path / "merged.nc"
    _build_multi_station_cache(cache_path)
    order_by_scenario = {"ssp126": [0, 1, 2, 3], "ssp245": [3, 1, 0, 2], "ssp585": [2, 0, 3, 1]}
    union_csv, map_paths = _build_union_and_maps(tmp_path, order_by_scenario)
    base = dict(
        union_station_cf_cache=str(cache_path), union_stations_csv=str(union_csv),
        index_map_ssp126=str(map_paths["ssp126"]), index_map_ssp245=str(map_paths["ssp245"]),
        index_map_ssp585=str(map_paths["ssp585"]), tech="wind", baseline_years="2015-2024",
        compress_level=1, overwrite=False, scenarios="ssp245", station_chunk=2,
    )
    out1 = tmp_path / "j1"
    e3.run(Namespace(**base, output_dir=str(out1), n_jobs=1))
    out4 = tmp_path / "j4"
    e3.run(Namespace(**base, output_dir=str(out4), n_jobs=4))

    p1 = cf_low_resource.sparse_threshold_file_for_scenario_tech(out1, "ssp245", "wind", "2015-2024")
    p4 = cf_low_resource.sparse_threshold_file_for_scenario_tech(out4, "ssp245", "wind", "2015-2024")
    with h5py.File(p1, "r") as fa, h5py.File(p4, "r") as fb:
        np.testing.assert_allclose(fa["threshold"][:], fb["threshold"][:])
        np.testing.assert_allclose(fa["clim"][:], fb["clim"][:])
