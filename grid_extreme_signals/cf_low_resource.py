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
    target_spatial_interp: str = "nearest"


@dataclass
class ThresholdGrid:
    """ERA5Land 低资源阈值网格元数据。"""

    path: Path
    lat: np.ndarray
    lon: np.ndarray
    attrs: dict[str, str]
    clim: np.ndarray | None = None
    threshold: np.ndarray | None = None


@dataclass
class SparseThreshold:
    """ERA5Land 场站稀疏低资源阈值元数据。"""

    path: Path
    station_lat: np.ndarray
    station_lon: np.ndarray
    station_type: np.ndarray
    attrs: dict[str, str]


@dataclass
class FourPointMatch:
    """规则网格四点双线性匹配结果。"""

    lat_idx: np.ndarray
    lon_idx: np.ndarray
    weights: np.ndarray
    corner_lat: np.ndarray
    corner_lon: np.ndarray


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
    return Path("outputs/low_resource_thresholds/sparse_station_ERA5Land_2015-2025")


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


def sparse_threshold_file_for_scenario_tech(
    threshold_dir: str | Path,
    scenario: str,
    tech: str,
    baseline_years: str = "2015-2025",
) -> Path:
    """返回 SSP 场站稀疏 ERA5Land 阈值文件路径。"""
    return (
        Path(threshold_dir) /
        f"low_resource_threshold_sparse_{scenario}_{tech}_ERA5Land_{baseline_years}.nc"
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


def load_sparse_low_resource_threshold(threshold_file: str | Path) -> SparseThreshold:
    """读取 SSP 场站稀疏 ERA5Land 阈值文件元数据。"""
    path = Path(threshold_file)
    with open_h5(path, "r") as f:
        attrs = {k: decode_attr(v) for k, v in f.attrs.items()}
        station_lat = f["station_lat"][:].astype(np.float64)
        station_lon = f["station_lon"][:].astype(np.float64)
        station_type = f["station_type"][:].astype(np.int8)
    return SparseThreshold(
        path=path,
        station_lat=station_lat,
        station_lon=station_lon,
        station_type=station_type,
        attrs=attrs,
    )


def is_sparse_threshold_file(threshold_file: str | Path) -> bool:
    """判断阈值文件是否为场站稀疏阈值。"""
    with open_h5(threshold_file, "r") as f:
        kind = decode_attr(f.attrs.get("threshold_kind", ""))
        return kind == "sparse_station" or (
            "station" in f and "station_lat" in f and "threshold" in f
        )


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


def _nearest_value_idx(values: np.ndarray, target: float) -> int:
    return int(np.argmin(np.abs(np.asarray(values, dtype=np.float64) - target)))


def bilinear_four_point_regular(
    grid_lat: np.ndarray,
    grid_lon: np.ndarray,
    station_lats: np.ndarray,
    station_lons: np.ndarray,
) -> FourPointMatch:
    """在规则经纬度网格上为场站选择四周点并计算双线性权重。

    corner 顺序固定为 southwest, southeast, northwest, northeast。
    经度按 360 度环形处理；返回的索引用于原始 ``grid_lat/grid_lon`` 读取。
    """
    lat = np.asarray(grid_lat, dtype=np.float64)
    lon_raw = np.asarray(grid_lon, dtype=np.float64)
    lon360 = sm.lon_to_360(lon_raw)
    sta_lat = np.asarray(station_lats, dtype=np.float64)
    sta_lon360 = sm.lon_to_360(station_lons)

    lat_order = np.argsort(lat)
    lat_sorted = lat[lat_order]
    lon_order = np.argsort(lon360)
    lon_sorted = lon360[lon_order]

    n_sta = sta_lat.shape[0]
    lat_idx = np.empty((n_sta, 4), dtype=np.int64)
    lon_idx = np.empty((n_sta, 4), dtype=np.int64)
    weights = np.empty((n_sta, 4), dtype=np.float32)
    corner_lat = np.empty((n_sta, 4), dtype=np.float32)
    corner_lon = np.empty((n_sta, 4), dtype=np.float32)

    for i in range(n_sta):
        y = float(sta_lat[i])
        pos_y = int(np.searchsorted(lat_sorted, y, side="right"))
        if pos_y <= 0:
            south_pos = north_pos = 0
        elif pos_y >= lat_sorted.size:
            south_pos = north_pos = lat_sorted.size - 1
        else:
            south_pos = pos_y - 1
            north_pos = pos_y
        south_idx = int(lat_order[south_pos])
        north_idx = int(lat_order[north_pos])
        south_lat = float(lat[south_idx])
        north_lat = float(lat[north_idx])
        if np.isclose(north_lat, south_lat):
            wy_north = 0.0
        else:
            wy_north = (y - south_lat) / (north_lat - south_lat)
            wy_north = float(np.clip(wy_north, 0.0, 1.0))
        wy_south = 1.0 - wy_north

        x = float(sta_lon360[i])
        pos_x = int(np.searchsorted(lon_sorted, x, side="right"))
        west_pos = (pos_x - 1) % lon_sorted.size
        east_pos = pos_x % lon_sorted.size
        west_idx = int(lon_order[west_pos])
        east_idx = int(lon_order[east_pos])
        west_lon = float(lon360[west_idx])
        east_lon = float(lon360[east_idx])
        dx = (east_lon - west_lon) % 360.0
        if np.isclose(dx, 0.0):
            wx_east = 0.0
        else:
            wx_east = ((x - west_lon) % 360.0) / dx
            wx_east = float(np.clip(wx_east, 0.0, 1.0))
        wx_west = 1.0 - wx_east

        lat_idx[i] = [south_idx, south_idx, north_idx, north_idx]
        lon_idx[i] = [west_idx, east_idx, west_idx, east_idx]
        weights[i] = [
            wy_south * wx_west,
            wy_south * wx_east,
            wy_north * wx_west,
            wy_north * wx_east,
        ]
        corner_lat[i] = lat[lat_idx[i]].astype(np.float32)
        corner_lon[i] = lon_raw[lon_idx[i]].astype(np.float32)

    return FourPointMatch(
        lat_idx=lat_idx,
        lon_idx=lon_idx,
        weights=weights,
        corner_lat=corner_lat,
        corner_lon=corner_lon,
    )


def match_sparse_threshold_stations(
    threshold: SparseThreshold,
    station_lats: np.ndarray,
    station_lons: np.ndarray,
    tech: str,
    *,
    tol: float = 1e-5,
) -> tuple[np.ndarray, np.ndarray]:
    """把输出场站匹配到稀疏阈值文件中的 station 维度。"""
    target_lat = np.asarray(station_lats, dtype=np.float64)
    target_lon = sm.lon_to_180(station_lons)
    th_lat = np.asarray(threshold.station_lat, dtype=np.float64)
    th_lon = sm.lon_to_180(threshold.station_lon)
    type_code = 1 if tech == "wind" else 0
    candidates = np.where(threshold.station_type == type_code)[0]

    out_idx = np.full(target_lat.shape[0], -1, dtype=np.int64)
    valid = np.zeros(target_lat.shape[0], dtype=bool)
    for i, (lat_i, lon_i) in enumerate(zip(target_lat, target_lon)):
        if candidates.size == 0:
            break
        dlat = np.abs(th_lat[candidates] - lat_i)
        dlon = np.abs(((th_lon[candidates] - lon_i + 180.0) % 360.0) - 180.0)
        dist = np.maximum(dlat, dlon)
        j = _nearest_value_idx(dist, 0.0)
        if dist[j] <= tol:
            out_idx[i] = int(candidates[j])
            valid[i] = True
    return out_idx, valid


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


def _read_sparse_threshold_points(
    threshold_file: str | Path,
    station_idx: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """按稀疏 station 索引读取 ``clim`` 和 ``threshold``。"""
    station_idx = np.asarray(station_idx, dtype=np.int64)
    read_idx = np.where(station_idx >= 0, station_idx, 0)
    unique_idx, inv = np.unique(read_idx, return_inverse=True)
    with open_h5(threshold_file, "r") as f:
        clim_unique = f["clim"][:, :, unique_idx].astype(np.float32)
        threshold_unique = f["threshold"][unique_idx].astype(np.float32)
    return clim_unique[:, :, inv], threshold_unique[inv]


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


def _materialize_station_cf_weighted(
    cf_file: Path,
    tech: str,
    match: sm.StationSpatialWeights,
    time_chunk: int,
    temp_dir: Path,
) -> np.memmap:
    """把场站加权抽取后的 CF 时间序列落到临时 memmap。"""
    var = cf_var(tech)
    with open_h5(cf_file, "r") as f:
        d = f[var]
        n_time = d.shape[0]
        n_station = len(match)
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
                slab = d[t0:t1, :, :].astype(np.float32)
                arr[t0:t1, :] = sm.gather_to_stations_weighted(slab, match)
            arr.flush()
        except Exception:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise
        arr._mmap.close()
    return np.memmap(tmp_path, dtype=np.float32, mode="r",
                     shape=(n_time, len(match)))


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
    spatial_interp: str = "nearest",
    station_chunk: int = 128,
    time_chunk: int = 512,
    temp_dir: str | Path | None = None,
) -> LowResourceResult:
    """计算场站级 ``signal_low_resource``。"""
    spatial_interp = spatial_interp.lower()
    if spatial_interp not in {"nearest", "bilinear"}:
        raise ValueError(
            f"当前只支持 spatial_interp='nearest' 或 'bilinear'，收到 {spatial_interp!r}"
        )
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

    stations_df = pd.DataFrame({
        "lat": np.asarray(station_lats, dtype=np.float64),
        "lon": np.asarray(station_lons, dtype=np.float64),
    })
    cf_match = sm.match_regular_weighted(
        lat,
        lon,
        stations_df,
        method=spatial_interp,
        max_dist=max_dist,
    )
    valid = cf_match.valid.copy()
    threshold = None
    threshold_lat_idx = threshold_lon_idx = None
    sparse_station_idx = None
    threshold_is_sparse = False
    if threshold_file is not None:
        threshold_is_sparse = is_sparse_threshold_file(threshold_file)
        if threshold_is_sparse:
            threshold = load_sparse_low_resource_threshold(threshold_file)
            sparse_station_idx, sparse_valid = match_sparse_threshold_stations(
                threshold,
                station_lats.astype(np.float64),
                station_lons.astype(np.float64),
                tech,
            )
            valid = valid & sparse_valid
        else:
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
    if spatial_interp == "nearest":
        cf_mm = _materialize_station_cf(
            cf_path,
            tech,
            cf_match.idx0[:, 0],
            cf_match.idx1[:, 0],
            time_chunk,
            tmp_dir,
        )
    else:
        cf_mm = _materialize_station_cf_weighted(
            cf_path,
            tech,
            cf_match,
            time_chunk,
            tmp_dir,
        )
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
                if threshold_is_sparse:
                    clim_tbl, thr = _read_sparse_threshold_points(
                        threshold_file,
                        sparse_station_idx[c0:c1],
                    )
                else:
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
        target_spatial_interp=spatial_interp,
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
        "low_resource_target_spatial_interp": result.target_spatial_interp,
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
