"""基于容量因子（CF）计算风电/光伏低资源事件。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import glob
import logging
import os
import tempfile

os.environ.setdefault("HDF5_USE_FILE_LOCKING", "FALSE")

import h5py
import numpy as np
import pandas as pd

import registry
from grid_extreme_signals import station_match as sm

logger = logging.getLogger(__name__)


@dataclass
class LowResourceResult:
    """低资源计算结果及元数据。"""

    mask: np.ndarray
    cf_file: Path
    timestep_hours: float
    window_steps: int
    grid_shape: tuple[int, int]
    lon360: bool
    baseline_years: str | None = None
    threshold_file: Path | None = None
    threshold_source: str | None = None
    threshold_baseline_years: str | None = None
    threshold_match_max_dist: float | None = None
    valid: np.ndarray | None = None


@dataclass
class ThresholdGrid:
    """ERA5Land 低资源阈值网格元数据。"""

    path: Path
    lat: np.ndarray
    lon: np.ndarray
    attrs: dict[str, str]
    clim: np.ndarray | None = None
    threshold: np.ndarray | None = None


def parse_years(years: str) -> tuple[int, int]:
    """解析 ``YYYY`` 或 ``YYYY-YYYY``。"""
    if "-" in years:
        a, b = years.split("-", 1)
        return int(a), int(b)
    y = int(years)
    return y, y


def decode_attr(value) -> str:
    """把 HDF5/NetCDF 属性转成字符串。"""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, np.ndarray) and value.shape == ():
        return decode_attr(value.item())
    return str(value)


def open_h5(path: str | Path, mode: str):
    """打开 HDF5/NetCDF4 文件；读共享文件时关闭 HDF5 文件锁。"""
    try:
        return h5py.File(path, mode, locking=False)
    except (TypeError, ValueError):
        return h5py.File(path, mode)


def decode_time(values: np.ndarray, units: str) -> pd.DatetimeIndex:
    """解析 CF 文件中的数值时间轴。"""
    text = units.strip()
    if " since " not in text:
        raise ValueError(f"无法解析时间单位：{units!r}")
    unit, origin = text.split(" since ", 1)
    origin_ts = pd.Timestamp(origin)
    unit = unit.strip().lower()
    vals = np.asarray(values)
    if unit.startswith("hour"):
        return pd.DatetimeIndex(origin_ts + pd.to_timedelta(vals, unit="h"))
    if unit.startswith("day"):
        return pd.DatetimeIndex(origin_ts + pd.to_timedelta(vals, unit="D"))
    if unit.startswith("second"):
        return pd.DatetimeIndex(origin_ts + pd.to_timedelta(vals, unit="s"))
    raise ValueError(f"不支持的时间单位：{units!r}")


def read_time(h5: h5py.File) -> pd.DatetimeIndex:
    """读取 HDF5/NetCDF 文件的 time 坐标。"""
    return decode_time(h5["time"][:], decode_attr(h5["time"].attrs["units"]))


def infer_timestep_hours(times: pd.DatetimeIndex) -> float:
    """从时间轴推断小时步长。"""
    if len(times) < 2:
        raise ValueError("时间轴长度不足，无法推断时间步长")
    ns = times.to_numpy(dtype="datetime64[ns]").astype("int64")
    diffs = np.diff(ns).astype(np.float64) / 3.6e12
    return float(np.nanmedian(diffs))


def window_steps_24h(times: pd.DatetimeIndex) -> int:
    """返回 24 小时窗口对应的时间步数。"""
    dt = infer_timestep_hours(times)
    steps = int(round(24.0 / dt))
    if steps < 1 or not np.isclose(steps * dt, 24.0, atol=1e-6):
        raise ValueError(f"时间步长 {dt}h 不能整除 24h")
    return steps


def cf_subdir(source: str, tech: str) -> str:
    """返回容量因子文件所在子目录。"""
    if source == "china_cmfd_bcsd":
        return "CFs_of_solar_china" if tech == "solar" else "CFs_of_wind_china"
    if source == "cordex_nam12":
        return "CFs_of_solar_NAM-12" if tech == "solar" else "CFs_of_wind_NAM-12"
    return "CFs_of_solar" if tech == "solar" else "CFs_of_wind"


def cf_var(tech: str) -> str:
    """返回 CF 变量名。"""
    return "solar_cf" if tech == "solar" else "wind_cf"


def default_threshold_dir() -> Path:
    """返回默认 ERA5Land 低资源阈值目录。"""
    return Path("outputs/low_resource_thresholds/ERA5Land_2015-2025")


def threshold_file_for_tech(
    threshold_dir: str | Path,
    tech: str,
    baseline_years: str = "2015-2025",
) -> Path:
    """返回指定技术类型的 ERA5Land 阈值文件路径。"""
    return (
        Path(threshold_dir) /
        f"low_resource_threshold_{tech}_ERA5Land_{baseline_years}.nc"
    )


def find_cf_file(
    cf_root: str | Path,
    source: str,
    model: str,
    scenario: str,
    tech: str,
    *,
    region: str | None = None,
    years: str = "2015-2060",
) -> Path | None:
    """查找与任务对应的 CF 文件。"""
    root = Path(cf_root)
    subdir = cf_subdir(source, tech)
    var_prefix = "solar_CF" if tech == "solar" else "wind_CF"
    y0, y1 = parse_years(years)

    candidates: list[Path] = []
    if source == "regional_bcsd" and region:
        exact = (
            root / subdir / model / region /
            f"{var_prefix}_{region}_{model}_{scenario}_{y0}-{y1}_allmonths.nc"
        )
        candidates.append(exact)
        candidates.append(root / subdir / f"{model}_backup" / region / exact.name)
        patterns = [
            root / subdir / "*" / region /
            f"{var_prefix}_{region}_*_{scenario}_{y0}-{y1}_allmonths.nc",
            root / subdir / "*" / region /
            f"{var_prefix}_{region}_*_{scenario}_*_allmonths.nc",
        ]
    else:
        region_part = f"*{region}*" if region else "*"
        patterns = [
            root / subdir / "**" /
            f"{var_prefix}_{region_part}_{model}_{scenario}_{y0}-{y1}_allmonths.nc",
            root / subdir / "**" /
            f"{var_prefix}_*{model}*{scenario}*_allmonths.nc",
        ]

    for p in candidates:
        if p.exists():
            return p
    for pattern in patterns:
        matches = sorted(Path(p) for p in glob.glob(str(pattern), recursive=True))
        if matches:
            return matches[0]
    return None


def load_low_resource_threshold(
    threshold_file: str | Path,
    *,
    load_values: bool = False,
) -> ThresholdGrid:
    """读取 ERA5Land 低资源阈值文件。

    默认只读取坐标和全局属性；``load_values=True`` 主要用于小样本测试。
    正式计算按点/块读取 ``clim`` 和 ``threshold``，避免全量载入全球阈值。
    """
    path = Path(threshold_file)
    with open_h5(path, "r") as f:
        lat = f["lat"][:].astype(np.float64)
        lon = f["lon"][:].astype(np.float64)
        attrs = {k: decode_attr(v) for k, v in f.attrs.items()}
        clim = f["clim"][:].astype(np.float32) if load_values else None
        threshold = f["threshold"][:].astype(np.float32) if load_values else None
    return ThresholdGrid(path=path, lat=lat, lon=lon, attrs=attrs,
                         clim=clim, threshold=threshold)


def match_threshold_grid(
    threshold_lat: np.ndarray,
    threshold_lon: np.ndarray,
    target_lat: np.ndarray,
    target_lon: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """把目标点匹配到 ERA5Land 阈值规则经纬度网格。"""
    lon_180 = sm.normalize_grid_lon(threshold_lon)
    target_lon_180 = sm.lon_to_180(np.asarray(target_lon, dtype=np.float64))
    return sm.nearest_index_regular(
        np.asarray(threshold_lat, dtype=np.float64),
        lon_180,
        np.asarray(target_lat, dtype=np.float64),
        target_lon_180,
    )


def _read_threshold_points(
    threshold_file: str | Path,
    lat_idx: np.ndarray,
    lon_idx: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """按点读取 ``clim(12,24,K)`` 和 ``threshold(K,)``。"""
    lat_idx = np.asarray(lat_idx, dtype=np.int64)
    lon_idx = np.asarray(lon_idx, dtype=np.int64)
    n_point = lat_idx.shape[0]
    clim = np.empty((12, 24, n_point), dtype=np.float32)
    threshold = np.empty(n_point, dtype=np.float32)

    with open_h5(threshold_file, "r") as f:
        clim_d = f["clim"]
        threshold_d = f["threshold"]
        for lat_i in np.unique(lat_idx):
            pos = np.where(lat_idx == lat_i)[0]
            unique_lon, inv = np.unique(lon_idx[pos], return_inverse=True)
            clim_slice = clim_d[:, :, int(lat_i), unique_lon].astype(np.float32)
            threshold_slice = threshold_d[int(lat_i), unique_lon].astype(np.float32)
            clim[:, :, pos] = clim_slice[:, :, inv]
            threshold[pos] = threshold_slice[inv]
    return clim, threshold


def _time_indices(source_times: pd.DatetimeIndex, target_times) -> np.ndarray:
    """把目标时间轴映射到 CF 时间轴索引。"""
    target = pd.DatetimeIndex(target_times)
    pos = pd.Series(np.arange(len(source_times), dtype=np.int64),
                    index=source_times).reindex(target)
    if pos.isna().any():
        missing = target[pos.isna()]
        raise ValueError(f"CF 时间轴缺少目标时间，例如 {missing[0]}")
    return pos.to_numpy(dtype=np.int64)


def _baseline_mask(times: pd.DatetimeIndex, baseline_years: str) -> np.ndarray:
    y0, y1 = parse_years(baseline_years)
    years = times.year.to_numpy()
    mask = (years >= y0) & (years <= y1)
    if not np.any(mask):
        raise ValueError(f"基线期 {baseline_years} 与 CF 时间轴无交集")
    return mask


def _low_resource_block(
    cf_block: np.ndarray,
    times: pd.DatetimeIndex,
    baseline_mask: np.ndarray | None,
    tech: str,
    lats: np.ndarray | None,
    lons: np.ndarray | None,
    window_steps: int,
    clim_tbl: np.ndarray | None = None,
    thr: np.ndarray | None = None,
) -> np.ndarray:
    """计算一个二维块 ``(time, cell)`` 的低资源信号。"""
    mod = registry.LOWRES[tech]
    if tech == "solar":
        return mod.signal(
            cf_block, times, lats, lons,
            base_mask=baseline_mask,
            clim_tbl=clim_tbl,
            thr=thr,
            window_steps=window_steps,
            mark_next_step=True,
        ).astype(np.int8)
    return mod.signal(
        cf_block, times,
        base_mask=baseline_mask,
        clim_tbl=clim_tbl,
        thr=thr,
        window_steps=window_steps,
        mark_next_step=True,
    ).astype(np.int8)


def _materialize_station_cf(
    cf_file: Path,
    tech: str,
    lat_idx: np.ndarray,
    lon_idx: np.ndarray,
    time_chunk: int,
    temp_dir: Path,
) -> np.memmap:
    """把场站对应的 CF 时间序列落到临时 memmap。"""
    var = cf_var(tech)
    with open_h5(cf_file, "r") as f:
        d = f[var]
        n_time = d.shape[0]
        n_station = lat_idx.shape[0]
        tmp = tempfile.NamedTemporaryFile(
            prefix="lowres_cf_", suffix=".dat", dir=temp_dir, delete=False
        )
        tmp_path = Path(tmp.name)
        tmp.close()
        arr = np.memmap(tmp_path, dtype=np.float32, mode="w+",
                        shape=(n_time, n_station))
        try:
            for t0 in range(0, n_time, time_chunk):
                t1 = min(t0 + time_chunk, n_time)
                slab = d[t0:t1, :, :]
                arr[t0:t1, :] = slab[:, lat_idx, lon_idx].astype(np.float32)
            arr.flush()
        except Exception:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise
        arr._mmap.close()
    return np.memmap(tmp_path, dtype=np.float32, mode="r",
                     shape=(n_time, lat_idx.shape[0]))


def compute_station_low_resource(
    cf_file: str | Path,
    tech: str,
    output_times,
    station_lats: np.ndarray,
    station_lons: np.ndarray,
    *,
    baseline_years: str = "2015-2029",
    threshold_file: str | Path | None = None,
    threshold_match_max_dist: float | None = None,
    max_dist: float = sm.MAX_DIST_DEG,
    station_chunk: int = 128,
    time_chunk: int = 512,
    temp_dir: str | Path | None = None,
) -> LowResourceResult:
    """计算场站级 ``signal_low_resource``。"""
    cf_path = Path(cf_file)
    with open_h5(cf_path, "r") as f:
        cf_times = read_time(f)
        lat = f["lat"][:]
        lon = f["lon"][:]
        cf_shape = f[cf_var(tech)].shape

    out_idx = _time_indices(cf_times, output_times)
    baseline = None if threshold_file is not None else _baseline_mask(cf_times, baseline_years)
    timestep_hours = infer_timestep_hours(cf_times)
    window_steps = window_steps_24h(cf_times)

    lon_180 = sm.normalize_grid_lon(lon)
    sta_lon = sm.lon_to_180(station_lons.astype(np.float64))
    lat_idx, lon_idx, dist = sm.nearest_index_regular(
        lat, lon_180, station_lats.astype(np.float64), sta_lon
    )
    valid = dist <= max_dist
    threshold = None
    threshold_lat_idx = threshold_lon_idx = None
    if threshold_file is not None:
        threshold = load_low_resource_threshold(threshold_file)
        threshold_lat_idx, threshold_lon_idx, threshold_dist = match_threshold_grid(
            threshold.lat,
            threshold.lon,
            station_lats.astype(np.float64),
            station_lons.astype(np.float64),
        )
        th_max = max_dist if threshold_match_max_dist is None else threshold_match_max_dist
        valid = valid & (threshold_dist <= th_max)

    tmp_dir = Path(temp_dir) if temp_dir is not None else Path(tempfile.gettempdir())
    cf_mm = _materialize_station_cf(cf_path, tech, lat_idx, lon_idx, time_chunk, tmp_dir)
    tmp_path = Path(cf_mm.filename)
    n_out = len(out_idx)
    n_station = len(station_lats)
    out = np.zeros((n_out, n_station), dtype=np.int8)
    try:
        for c0 in range(0, n_station, station_chunk):
            c1 = min(c0 + station_chunk, n_station)
            block = np.asarray(cf_mm[:, c0:c1], dtype=np.float32)
            clim_tbl = thr = None
            if threshold_file is not None:
                clim_tbl, thr = _read_threshold_points(
                    threshold_file,
                    threshold_lat_idx[c0:c1],
                    threshold_lon_idx[c0:c1],
                )
            sig_full = _low_resource_block(
                block, cf_times, baseline, tech,
                station_lats[c0:c1], station_lons[c0:c1], window_steps,
                clim_tbl=clim_tbl, thr=thr,
            )
            out[:, c0:c1] = sig_full[out_idx, :]
    finally:
        try:
            cf_mm._mmap.close()
        except Exception:
            pass
        try:
            tmp_path.unlink()
        except OSError:
            pass

    return LowResourceResult(
        mask=out,
        cf_file=cf_path,
        baseline_years=None if threshold_file is not None else baseline_years,
        timestep_hours=timestep_hours,
        window_steps=window_steps,
        grid_shape=(int(cf_shape[1]), int(cf_shape[2])),
        lon360=sm.is_lon_360(lon),
        threshold_file=Path(threshold_file) if threshold_file is not None else None,
        threshold_source=(threshold.attrs.get("threshold_source") if threshold is not None else None),
        threshold_baseline_years=(
            threshold.attrs.get("baseline_years_effective")
            if threshold is not None else None
        ),
        threshold_match_max_dist=(
            max_dist if threshold_match_max_dist is None else threshold_match_max_dist
        ) if threshold_file is not None else None,
        valid=valid,
    )


def compute_grid_low_resource(
    cf_file: str | Path,
    tech: str,
    output_times,
    *,
    baseline_years: str = "2015-2029",
    threshold_file: str | Path | None = None,
    threshold_match_max_dist: float = sm.MAX_DIST_DEG,
    lat_chunk: int = 1,
) -> LowResourceResult:
    """计算网格级 ``signal_low_resource``，输出时间轴可为 CF 时间轴子集。"""
    cf_path = Path(cf_file)
    with open_h5(cf_path, "r") as f:
        cf_times = read_time(f)
        lat = f["lat"][:].astype(np.float64)
        lon = f["lon"][:].astype(np.float64)
        d = f[cf_var(tech)]
        n_time, n_lat, n_lon = d.shape
        out_idx = _time_indices(cf_times, output_times)
        baseline = None if threshold_file is not None else _baseline_mask(cf_times, baseline_years)
        timestep_hours = infer_timestep_hours(cf_times)
        window_steps = window_steps_24h(cf_times)
        out = np.zeros((len(out_idx), n_lat, n_lon), dtype=np.int8)
        threshold = load_low_resource_threshold(threshold_file) if threshold_file is not None else None

        for i0 in range(0, n_lat, lat_chunk):
            i1 = min(i0 + lat_chunk, n_lat)
            block = d[:, i0:i1, :].astype(np.float32).reshape(n_time, -1)
            lat2d, lon2d = np.meshgrid(lat[i0:i1], lon, indexing="ij")
            lats = lat2d.ravel()
            lons = lon2d.ravel()
            clim_tbl = thr = None
            valid_flat = None
            if threshold is not None:
                th_lat_idx, th_lon_idx, th_dist = match_threshold_grid(
                    threshold.lat, threshold.lon, lats, lons
                )
                valid_flat = th_dist <= threshold_match_max_dist
                clim_tbl, thr = _read_threshold_points(
                    threshold_file, th_lat_idx, th_lon_idx
                )
            sig_full = _low_resource_block(
                block, cf_times, baseline, tech,
                lats if tech == "solar" else None,
                lons if tech == "solar" else None,
                window_steps,
                clim_tbl=clim_tbl,
                thr=thr,
            )
            if valid_flat is not None:
                sig_full[:, ~valid_flat] = 0
            out[:, i0:i1, :] = sig_full[out_idx, :].reshape(len(out_idx), i1 - i0, n_lon)
            logger.info("低资源网格块写入 lat %d:%d", i0, i1)

    return LowResourceResult(
        mask=out,
        cf_file=cf_path,
        baseline_years=None if threshold_file is not None else baseline_years,
        timestep_hours=timestep_hours,
        window_steps=window_steps,
        grid_shape=(int(n_lat), int(n_lon)),
        lon360=sm.is_lon_360(lon),
        threshold_file=Path(threshold_file) if threshold_file is not None else None,
        threshold_source=(threshold.attrs.get("threshold_source") if threshold is not None else None),
        threshold_baseline_years=(
            threshold.attrs.get("baseline_years_effective")
            if threshold is not None else None
        ),
        threshold_match_max_dist=threshold_match_max_dist if threshold_file is not None else None,
    )


def attrs(result: LowResourceResult) -> dict[str, str]:
    """生成低资源相关 NetCDF 属性。"""
    out = {
        "low_resource_source": "capacity_factor",
        "low_resource_cf_file": str(result.cf_file),
        "low_resource_window_hours": "24",
        "low_resource_window_steps": str(result.window_steps),
        "low_resource_timestep_hours": f"{result.timestep_hours:g}",
        "low_resource_mark_next_step": "true",
    }
    if result.threshold_file is not None:
        out.update({
            "low_resource_threshold_file": str(result.threshold_file),
            "low_resource_threshold_source": result.threshold_source or "ERA5Land",
            "low_resource_threshold_baseline_years": result.threshold_baseline_years or "",
            "low_resource_threshold_match_max_dist_deg": (
                "" if result.threshold_match_max_dist is None
                else f"{result.threshold_match_max_dist:g}"
            ),
        })
    elif result.baseline_years is not None:
        out["low_resource_baseline_years"] = result.baseline_years
    return out
