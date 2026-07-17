"""超算 step1 拆分流程的公共工具。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import netCDF4
import numpy as np
import pandas as pd

from grid_extreme_signals import cf_low_resource

BASELINE_YEARS = "2015-2024"
CHUNK_PLAN_NAME = "e2a_chunk_plan_2015-2024.csv"
UNION_FILE_NAME = "stations_union_ssp126_ssp245_ssp585.csv"
SCENARIOS = ("ssp126", "ssp245", "ssp585")
STATION_KEY_DECIMALS = 5


def atomic_path(path: Path) -> Path:
    """返回当前进程专用的临时输出路径。"""
    return path.with_name(f"{path.name}.tmp.{os.getpid()}")


def sha256_file(path: str | Path) -> str:
    """计算文件 SHA256。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json_atomic(path: Path, value: dict) -> None:
    """原子写出 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = atomic_path(path)
    try:
        tmp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_csv_atomic(frame: pd.DataFrame, path: Path) -> None:
    """原子写出 CSV。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = atomic_path(path)
    try:
        frame.to_csv(tmp, index=False)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def normalize_station_key(lon: float, lat: float, tech: str) -> tuple[float, float, str]:
    """生成稳定的场站去重键。"""
    lon180 = float(((float(lon) + 180.0) % 360.0) - 180.0)
    return (
        round(lon180, STATION_KEY_DECIMALS),
        round(float(lat), STATION_KEY_DECIMALS),
        str(tech),
    )


def build_chunk_plan() -> pd.DataFrame:
    """生成固定覆盖 2015-2024 的 21 个 E2a 时间缓存块。"""
    rows: list[dict[str, object]] = [
        {
            "chunk_id": "2015_01",
            "year_month_start": "2015-01",
            "year_month_end": "2015-01",
            "is_probe": True,
            "expected_month_count": 1,
        },
        {
            "chunk_id": "2015_02_2015_06",
            "year_month_start": "2015-02",
            "year_month_end": "2015-06",
            "is_probe": False,
            "expected_month_count": 5,
        },
    ]
    for year in range(2015, 2025):
        if year != 2015:
            rows.append({
                "chunk_id": f"{year}_01_{year}_06",
                "year_month_start": f"{year}-01",
                "year_month_end": f"{year}-06",
                "is_probe": False,
                "expected_month_count": 6,
            })
        rows.append({
            "chunk_id": f"{year}_07_{year}_12",
            "year_month_start": f"{year}-07",
            "year_month_end": f"{year}-12",
            "is_probe": False,
            "expected_month_count": 6,
        })
    frame = pd.DataFrame(rows)
    frame["job_name"] = "step1_E2a_{tech}_" + frame["chunk_id"]
    frame["cache_file"] = "station_cf_union_{tech}_" + frame["chunk_id"] + ".nc"
    validate_chunk_plan(frame)
    return frame


def validate_chunk_plan(frame: pd.DataFrame) -> None:
    """校验 E2a 作业清单严格覆盖120个月。"""
    required = {
        "chunk_id", "year_month_start", "year_month_end", "is_probe",
        "expected_month_count", "job_name", "cache_file",
    }
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"E2a 作业清单缺少列：{sorted(missing)}")
    if len(frame) != 21:
        raise ValueError(f"E2a 作业清单必须有21行，实际为 {len(frame)}")
    if frame["chunk_id"].duplicated().any() or frame["cache_file"].duplicated().any():
        raise ValueError("E2a 作业清单的 chunk_id/cache_file 必须唯一")
    covered: list[str] = []
    for row in frame.itertuples(index=False):
        months = pd.period_range(row.year_month_start, row.year_month_end, freq="M")
        if len(months) != int(row.expected_month_count):
            raise ValueError(f"{row.chunk_id}: expected_month_count 不匹配")
        covered.extend(str(month) for month in months)
    expected = [str(p) for p in pd.period_range("2015-01", "2024-12", freq="M")]
    if covered != expected:
        raise ValueError("E2a 作业清单未按顺序完整覆盖 2015-01 至 2024-12")


def read_chunk_plan(path: str | Path) -> pd.DataFrame:
    """读取并校验 E2a 作业清单。"""
    frame = pd.read_csv(path, dtype={"chunk_id": str})
    validate_chunk_plan(frame)
    return frame


def decode_attrs(handle) -> dict[str, str]:
    """读取 HDF5 属性并转换为字符串。"""
    return {key: cf_low_resource.decode_attr(value) for key, value in handle.attrs.items()}


def require_output_available(path: Path, overwrite: bool) -> None:
    """生产默认拒绝覆盖已有输出。"""
    if path.exists() and not overwrite:
        raise FileExistsError(f"输出已存在；如确认需要重跑，请显式传 --overwrite：{path}")


def create_station_cache(
    path: Path,
    *,
    n_time: int,
    n_station: int,
    compress_level: int,
) -> netCDF4.Dataset:
    """创建统一的 E2a/E2b 场站 CF 缓存结构。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = netCDF4.Dataset(path, "w", format="NETCDF4")
    ds.createDimension("time", n_time)
    ds.createDimension("station", n_station)
    ds.createDimension("corner", 4)
    ds.createVariable("time", "i8", ("time",))
    ds["time"].units = "seconds since 1970-01-01"
    ds.createVariable("station", "i4", ("station",))[:] = np.arange(n_station)
    ds.createVariable("corner", "i1", ("corner",))[:] = np.arange(4)
    for name, dtype, dims in (
        ("union_station_index", "i4", ("station",)),
        ("station_lon", "f4", ("station",)),
        ("station_lat", "f4", ("station",)),
        ("station_type", "i1", ("station",)),
        ("era5_lat_idx", "i4", ("station", "corner")),
        ("era5_lon_idx", "i4", ("station", "corner")),
        ("era5_lat", "f4", ("station", "corner")),
        ("era5_lon", "f4", ("station", "corner")),
        ("weight", "f4", ("station", "corner")),
    ):
        ds.createVariable(name, dtype, dims, zlib=True, complevel=compress_level)
    ds.createVariable(
        "cf", "f4", ("time", "station"), zlib=True, complevel=compress_level,
        fill_value=np.float32(np.nan),
        chunksizes=(min(744, max(1, n_time)), min(1024, max(1, n_station))),
    )
    return ds


def copy_station_metadata(source, target: netCDF4.Dataset) -> None:
    """复制缓存中的静态场站匹配变量。"""
    for name in (
        "union_station_index", "station_lon", "station_lat", "station_type",
        "era5_lat_idx", "era5_lon_idx", "era5_lat", "era5_lon", "weight",
    ):
        target[name][:] = source[name][:]

