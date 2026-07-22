#!/usr/bin/env python3
"""预计算 SSP 场站稀疏 ERA5Land 低资源阈值。"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("HDF5_USE_FILE_LOCKING", "FALSE")

import netCDF4
import numpy as np
import pandas as pd

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from grid_extreme_signals import cf_low_resource  # noqa: E402
from grid_extreme_signals import station_match as sm  # noqa: E402
from scripts import precompute_low_resource_thresholds as full_precompute  # noqa: E402
from tools.logging_utils import setup_logging  # noqa: E402

logger = logging.getLogger("precompute_station_low_resource_thresholds")

DEFAULT_OUTPUT_DIR = "outputs/low_resource_thresholds/sparse_station_ERA5Land_2015-2024"
DEFAULT_STATION_CF_CACHE_DIR = "outputs/cache/era5land_station_cf"
STATION_KEY_DECIMALS = 5


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="基于 ERA5Land CF 预计算 SSP 场站稀疏低资源 clim/threshold。",
    )
    p.add_argument("--cf_root", default="data/cfs")
    p.add_argument("--stations_csv", required=True)
    p.add_argument("--scenario", default=None,
                   help="ssp126/ssp245/ssp585；省略时从 stations_csv 文件名推断。")
    p.add_argument("--output_dir", default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--baseline_years", default="2015-2024")
    p.add_argument("--tech", choices=["wind", "solar", "both"], default="both")
    p.add_argument(
        "--threshold_interp",
        choices=["nearest_valid", "bilinear"],
        default="nearest_valid",
        help="ERA5Land CF 抽取到场站的方式：nearest_valid=最近有效格点（默认），bilinear=四点双线性。",
    )
    p.add_argument("--station_chunk", type=int, default=128)
    p.add_argument("--compress_level", type=int, default=4)
    p.add_argument(
        "--station_cf_cache_dir",
        default=DEFAULT_STATION_CF_CACHE_DIR,
        help="ERA5Land 场站 CF 缓存目录。",
    )
    p.add_argument(
        "--station_cf_cache_compress_level",
        type=int,
        default=1,
        help="场站 CF 缓存压缩等级；建议 1-2，避免阈值阶段读取过慢。",
    )
    p.add_argument(
        "--cache_time_chunk",
        type=int,
        default=None,
        help="生成场站 CF 缓存时的时间读取块；默认使用 HDF5 原生时间 chunk。",
    )
    p.add_argument(
        "--overwrite_station_cf_cache",
        action="store_true",
        help="即使场站 CF 缓存已存在且完整，也重新生成。",
    )
    p.add_argument(
        "--no_station_cf_cache",
        action="store_true",
        help="关闭场站 CF 缓存，退回旧的直接读取 ERA5Land 月文件路径，仅用于调试。",
    )
    p.add_argument(
        "--reuse_from_scenario",
        default="ssp126",
        help="计算非基准 SSP 时尝试复用的源情景阈值文件；默认 ssp126。",
    )
    p.add_argument("--no_reuse_thresholds", action="store_true",
                   help="关闭从已有 SSP 阈值文件增量复用，始终完整计算当前 SSP。")
    p.add_argument("--allow_incomplete", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--dry_run", action="store_true")
    return p


@dataclass
class ReuseSourceThreshold:
    """可复用的源 SSP 稀疏阈值数据。"""

    path: Path
    key_to_index: dict[tuple[float, float, int], int]
    era5_lat_idx: np.ndarray
    era5_lon_idx: np.ndarray
    era5_lat: np.ndarray
    era5_lon: np.ndarray
    weight: np.ndarray
    clim: np.ndarray
    threshold: np.ndarray
    valid_count: np.ndarray


@dataclass
class StationCfCache:
    """已就绪的 ERA5Land 场站 CF 缓存。"""

    path: Path
    times: pd.DatetimeIndex


def _station_type_code(tech: str) -> int:
    return 1 if tech == "wind" else 0


def _interpolation_metadata(threshold_interp: str) -> tuple[str, str]:
    """返回阈值侧插值方法写入文件的属性值。"""
    if threshold_interp == "bilinear":
        return "bilinear_4point", "southwest,southeast,northwest,northeast"
    return "nearest_valid", "nearest_valid,unused,unused,unused"


def _prepare_stations(stations_csv: str | Path, tech: str) -> pd.DataFrame:
    df = sm.load_stations(stations_csv)
    df = df[df["type"] == tech].copy()
    if df.empty:
        return pd.DataFrame(columns=["lon", "lat", "type", "activation_year", "capacity_gw"])
    return (
        df.sort_values("year")
        .groupby(["lon", "lat", "type"], as_index=False)
        .agg({"year": "min", "capacity_gw": "first"})
        .rename(columns={"year": "activation_year"})
        .reset_index(drop=True)
    )


def _station_keys_from_arrays(
    lon: np.ndarray,
    lat: np.ndarray,
    station_type: np.ndarray,
) -> list[tuple[float, float, int]]:
    """按严格 ``lon, lat, type`` 构造可复用场站 key。"""
    lon_norm = np.round(sm.lon_to_180(lon), STATION_KEY_DECIMALS)
    lat_round = np.round(np.asarray(lat, dtype=np.float64), STATION_KEY_DECIMALS)
    type_arr = np.asarray(station_type, dtype=np.int16)
    return [
        (float(lon_norm[i]), float(lat_round[i]), int(type_arr[i]))
        for i in range(lat_round.size)
    ]


def _station_keys_from_frame(stations: pd.DataFrame, tech: str) -> list[tuple[float, float, int]]:
    """从目标场站表构造复用 key。"""
    type_code = np.full(len(stations), _station_type_code(tech), dtype=np.int16)
    return _station_keys_from_arrays(
        stations["lon"].to_numpy(np.float64),
        stations["lat"].to_numpy(np.float64),
        type_code,
    )


def _decode_attrs(h5) -> dict[str, str]:
    return {k: cf_low_resource.decode_attr(v) for k, v in h5.attrs.items()}


def _require_dataset(h5, name: str):
    if name not in h5:
        raise ValueError(f"缺少变量 {name}")
    return h5[name]


def _load_reuse_source_threshold(
    path: Path,
    *,
    reuse_from_scenario: str,
    tech: str,
    baseline_years: str,
    baseline_effective: str,
    interpolation_method: str,
) -> ReuseSourceThreshold:
    """读取并校验可复用的源 SSP 稀疏阈值文件。"""
    if not path.exists():
        raise FileNotFoundError(f"源阈值文件不存在：{path}")

    with cf_low_resource.open_h5(path, "r") as f:
        attrs = _decode_attrs(f)
        expected_attrs = {
            "threshold_kind": "sparse_station",
            "threshold_source": "ERA5Land",
            "scenario": reuse_from_scenario,
            "tech": tech,
            "baseline_years_requested": baseline_years,
            "baseline_years_effective": baseline_effective,
            "interpolation_method": interpolation_method,
            "resource_variable": full_precompute.era5land_cf_var(tech),
            "window_hours": "24",
            "percentile": "5",
        }
        for key, expected in expected_attrs.items():
            got = attrs.get(key)
            if got != expected:
                raise ValueError(f"属性 {key} 不兼容：期望 {expected!r}，实际 {got!r}")

        station = _require_dataset(f, "station")
        corner = _require_dataset(f, "corner")
        month = _require_dataset(f, "month")
        hour = _require_dataset(f, "hour")
        n_station = int(station.shape[0])
        if int(corner.shape[0]) != 4:
            raise ValueError(f"corner 维度不是 4：{corner.shape}")
        if int(month.shape[0]) != 12:
            raise ValueError(f"month 维度不是 12：{month.shape}")
        if int(hour.shape[0]) != 24:
            raise ValueError(f"hour 维度不是 24：{hour.shape}")

        required_shapes = {
            "station_lon": (n_station,),
            "station_lat": (n_station,),
            "station_type": (n_station,),
            "capacity_gw": (n_station,),
            "activation_year": (n_station,),
            "era5_lat_idx": (n_station, 4),
            "era5_lon_idx": (n_station, 4),
            "era5_lat": (n_station, 4),
            "era5_lon": (n_station, 4),
            "weight": (n_station, 4),
            "clim": (12, 24, n_station),
            "threshold": (n_station,),
            "valid_count": (n_station,),
        }
        arrays = {}
        for name, expected_shape in required_shapes.items():
            data = _require_dataset(f, name)
            if tuple(data.shape) != expected_shape:
                raise ValueError(
                    f"变量 {name} 形状不兼容：期望 {expected_shape}，实际 {tuple(data.shape)}"
                )
            arrays[name] = data[:]

    threshold = arrays["threshold"].astype(np.float32)
    valid_count = arrays["valid_count"].astype(np.int32)
    weight = arrays["weight"].astype(np.float32)
    if not np.all(np.isfinite(threshold)):
        raise ValueError("源阈值文件 threshold 含 NaN 或 Inf")
    if not np.all(valid_count > 0):
        raise ValueError("源阈值文件 valid_count 存在非正值")
    if not np.allclose(weight.sum(axis=1), 1.0, atol=1e-5):
        raise ValueError("源阈值文件 weight 权重和不等于 1")

    keys = _station_keys_from_arrays(
        arrays["station_lon"], arrays["station_lat"], arrays["station_type"]
    )
    key_to_index: dict[tuple[float, float, int], int] = {}
    for idx, key in enumerate(keys):
        if key in key_to_index:
            raise ValueError(f"源阈值文件存在重复场站 key：{key}")
        key_to_index[key] = idx

    return ReuseSourceThreshold(
        path=path,
        key_to_index=key_to_index,
        era5_lat_idx=arrays["era5_lat_idx"].astype(np.int64),
        era5_lon_idx=arrays["era5_lon_idx"].astype(np.int64),
        era5_lat=arrays["era5_lat"].astype(np.float32),
        era5_lon=arrays["era5_lon"].astype(np.float32),
        weight=weight,
        clim=arrays["clim"].astype(np.float32),
        threshold=threshold,
        valid_count=valid_count,
    )


def _try_load_reuse_source(
    args,
    *,
    scenario: str,
    tech: str,
    baseline_effective: str,
    interpolation_method: str,
) -> ReuseSourceThreshold | None:
    """按参数决定是否加载复用源；失败时返回 None 并记录原因。"""
    reuse_from = getattr(args, "reuse_from_scenario", "ssp126")
    no_reuse = bool(getattr(args, "no_reuse_thresholds", False))
    if no_reuse:
        logger.info("[%s/%s] 已关闭阈值复用，完整计算", scenario, tech)
        return None
    if not reuse_from or scenario == reuse_from:
        return None

    source_path = cf_low_resource.sparse_threshold_file_for_scenario_tech(
        args.output_dir, reuse_from, tech, args.baseline_years
    )
    try:
        return _load_reuse_source_threshold(
            source_path,
            reuse_from_scenario=reuse_from,
            tech=tech,
            baseline_years=args.baseline_years,
            baseline_effective=baseline_effective,
            interpolation_method=interpolation_method,
        )
    except Exception as exc:
        logger.warning(
            "[%s/%s] 不使用复用源 %s：%s；改为完整计算",
            scenario, tech, source_path, exc,
        )
        return None


def _empty_match(n_station: int) -> cf_low_resource.FourPointMatch:
    """创建待填充的四点匹配结果。"""
    return cf_low_resource.FourPointMatch(
        lat_idx=np.empty((n_station, 4), dtype=np.int64),
        lon_idx=np.empty((n_station, 4), dtype=np.int64),
        weights=np.empty((n_station, 4), dtype=np.float32),
        corner_lat=np.empty((n_station, 4), dtype=np.float32),
        corner_lon=np.empty((n_station, 4), dtype=np.float32),
    )


def _regular_land_mask(lat: np.ndarray, lon: np.ndarray, chunk_rows: int = 128) -> np.ndarray | None:
    """用可选陆地掩膜识别 ERA5Land 陆地点；缺少依赖时返回 None。"""
    try:
        from global_land_mask import globe
    except ImportError:
        return None

    lat = np.asarray(lat, dtype=np.float64)
    lon_180 = sm.lon_to_180(lon)
    mask = np.zeros((lat.size, lon_180.size), dtype=bool)
    lon_row = lon_180[None, :]
    for r0 in range(0, lat.size, chunk_rows):
        r1 = min(r0 + chunk_rows, lat.size)
        lat_block = np.repeat(lat[r0:r1, None], lon_180.size, axis=1)
        lon_block = np.repeat(lon_row, r1 - r0, axis=0)
        mask[r0:r1, :] = globe.is_land(lat_block, lon_block)
    return mask


def _read_valid_cf_mask(
    path: Path,
    tech: str,
    lat: np.ndarray,
    lon: np.ndarray,
    probe_steps: int = 24,
) -> np.ndarray:
    """读取 ERA5Land CF 有效格点掩膜，用于避开海上缺测或填 0 格点。"""
    var_name = full_precompute.era5land_cf_var(tech)
    with cf_low_resource.open_h5(path, "r") as f:
        d = f[var_name]
        n_probe = min(int(probe_steps), int(d.shape[0]))
        valid = np.zeros(tuple(int(x) for x in d.shape[1:]), dtype=bool)
        positive = np.zeros_like(valid)
        for t in range(n_probe):
            arr = d[t, :, :]
            finite = np.isfinite(arr)
            valid |= finite
            positive |= finite & (arr > 0.0)
    land = _regular_land_mask(lat, lon)
    if land is not None:
        valid &= (land | positive)
    else:
        logger.warning(
            "未找到 global_land_mask，使用前 %d 个时次内出现正 CF 的格点估计 ERA5Land 有效格点",
            n_probe,
        )
        valid = positive
    if not np.any(valid):
        raise ValueError(f"{path}: {var_name} 前 {n_probe} 个时次均无有效格点")
    return valid


def _read_month_station_cf(
    path: Path,
    tech: str,
    lat_idx: np.ndarray,
    lon_idx: np.ndarray,
    weights: np.ndarray,
) -> tuple[pd.DatetimeIndex, np.ndarray]:
    """读取一个 ERA5Land 月文件的加权场站 CF。"""
    var_name = full_precompute.era5land_cf_var(tech)
    n_station = lat_idx.shape[0]
    with cf_low_resource.open_h5(path, "r") as f:
        times = cf_low_resource.read_time(f)
        d = f[var_name]
        out = np.zeros((d.shape[0], n_station), dtype=np.float32)
        for corner in range(4):
            corner_weight = weights[:, corner]
            if not np.any(corner_weight != 0.0):
                continue
            corner_values = np.empty((d.shape[0], n_station), dtype=np.float32)
            for lat_i in np.unique(lat_idx[:, corner]):
                pos = np.where(lat_idx[:, corner] == lat_i)[0]
                unique_lon, inv = np.unique(lon_idx[pos, corner], return_inverse=True)
                slab = d[:, int(lat_i), unique_lon].astype(np.float32)
                corner_values[:, pos] = slab[:, inv]
            out += corner_values * corner_weight[None, :]
    return times, out


def load_station_block(
    files: list[Path],
    tech: str,
    match: cf_low_resource.FourPointMatch,
    station_slice: slice,
) -> tuple[pd.DatetimeIndex, np.ndarray]:
    """读取一组场站在所有 ERA5Land 月文件上的加权 CF。"""
    times: list[pd.DatetimeIndex] = []
    blocks: list[np.ndarray] = []
    lat_idx = match.lat_idx[station_slice]
    lon_idx = match.lon_idx[station_slice]
    weights = match.weights[station_slice].astype(np.float32)
    for path in files:
        t, arr = _read_month_station_cf(path, tech, lat_idx, lon_idx, weights)
        times.append(t)
        blocks.append(arr)
    time = pd.DatetimeIndex(np.concatenate([t.values for t in times]))
    data = np.concatenate(blocks, axis=0)
    return time, data


def _compute_station_match(
    args,
    *,
    scenario: str,
    tech: str,
    lat: np.ndarray,
    lon: np.ndarray,
    stations: pd.DataFrame,
    files: list[Path],
) -> cf_low_resource.FourPointMatch:
    """按当前阈值插值方法计算一组场站的 ERA5Land 匹配。"""
    station_lats = stations["lat"].to_numpy(np.float64)
    station_lons = stations["lon"].to_numpy(np.float64)
    if args.threshold_interp == "bilinear":
        return cf_low_resource.bilinear_four_point_regular(
            lat,
            lon,
            station_lats,
            station_lons,
        )

    valid_mask = _read_valid_cf_mask(files[0], tech, lat, lon)
    nearest_lat, nearest_lon, _ = sm.nearest_index_regular(
        lat,
        sm.normalize_grid_lon(lon),
        station_lats,
        sm.lon_to_180(station_lons),
    )
    match = cf_low_resource.nearest_valid_point_regular(
        lat,
        lon,
        valid_mask,
        station_lats,
        station_lons,
    )
    replaced = np.count_nonzero(
        (match.lat_idx[:, 0] != nearest_lat) | (match.lon_idx[:, 0] != nearest_lon)
    )
    logger.info(
        "[%s/%s] 最近有效格点匹配：待计算场站=%d，有效网格=%d/%d，替换最近邻无效格点=%d",
        scenario, tech, len(stations), int(valid_mask.sum()), int(valid_mask.size), int(replaced),
    )
    return match


def _compose_match(
    n_station: int,
    *,
    reuse_source: ReuseSourceThreshold | None,
    reuse_target_idx: np.ndarray,
    reuse_source_idx: np.ndarray,
    compute_idx: np.ndarray,
    compute_match: cf_low_resource.FourPointMatch | None,
) -> cf_low_resource.FourPointMatch:
    """把复用匹配和新计算匹配组装为目标 SSP 的完整匹配结果。"""
    if reuse_source is None or reuse_target_idx.size == 0:
        if compute_match is None:
            raise ValueError("没有可用的场站匹配结果")
        return compute_match

    match = _empty_match(n_station)
    match.lat_idx[reuse_target_idx] = reuse_source.era5_lat_idx[reuse_source_idx]
    match.lon_idx[reuse_target_idx] = reuse_source.era5_lon_idx[reuse_source_idx]
    match.corner_lat[reuse_target_idx] = reuse_source.era5_lat[reuse_source_idx]
    match.corner_lon[reuse_target_idx] = reuse_source.era5_lon[reuse_source_idx]
    match.weights[reuse_target_idx] = reuse_source.weight[reuse_source_idx]
    if compute_idx.size:
        if compute_match is None:
            raise ValueError("存在非复用场站但缺少新计算匹配结果")
        match.lat_idx[compute_idx] = compute_match.lat_idx
        match.lon_idx[compute_idx] = compute_match.lon_idx
        match.corner_lat[compute_idx] = compute_match.corner_lat
        match.corner_lon[compute_idx] = compute_match.corner_lon
        match.weights[compute_idx] = compute_match.weights
    return match


def _write_reused_values(
    ds: netCDF4.Dataset,
    source: ReuseSourceThreshold,
    target_idx: np.ndarray,
    source_idx: np.ndarray,
) -> None:
    """把源 SSP 中交集场站的阈值变量写入目标 SSP 输出文件。"""
    if target_idx.size == 0:
        return
    order = np.argsort(target_idx)
    target_idx = target_idx[order]
    source_idx = source_idx[order]
    run_start = 0
    while run_start < target_idx.size:
        run_end = run_start + 1
        while run_end < target_idx.size and target_idx[run_end] == target_idx[run_end - 1] + 1:
            run_end += 1
        target_slice = slice(int(target_idx[run_start]), int(target_idx[run_end - 1]) + 1)
        source_sel = source_idx[run_start:run_end]
        ds["clim"][:, :, target_slice] = source.clim[:, :, source_sel]
        ds["threshold"][target_slice] = source.threshold[source_sel]
        ds["valid_count"][target_slice] = source.valid_count[source_sel]
        run_start = run_end


def _write_computed_values(
    ds: netCDF4.Dataset,
    target_idx: np.ndarray,
    clim: np.ndarray,
    threshold: np.ndarray,
    valid_count: np.ndarray,
) -> None:
    """把新计算的阈值变量写入目标 SSP 的原始 station 位置。"""
    if target_idx.size == 0:
        return
    order = np.argsort(target_idx)
    target_idx = target_idx[order]
    clim = clim[:, :, order]
    threshold = threshold[order]
    valid_count = valid_count[order]
    run_start = 0
    while run_start < target_idx.size:
        run_end = run_start + 1
        while run_end < target_idx.size and target_idx[run_end] == target_idx[run_end - 1] + 1:
            run_end += 1
        target_slice = slice(int(target_idx[run_start]), int(target_idx[run_end - 1]) + 1)
        data_slice = slice(run_start, run_end)
        ds["clim"][:, :, target_slice] = clim[:, :, data_slice]
        ds["threshold"][target_slice] = threshold[data_slice]
        ds["valid_count"][target_slice] = valid_count[data_slice]
        run_start = run_end


def _temporary_output_path(path: Path) -> Path:
    """返回当前进程专用临时输出路径。"""
    return path.with_name(f"{path.name}.tmp.{os.getpid()}")


def _close_dataset(ds: netCDF4.Dataset | None) -> None:
    """关闭 NetCDF 文件；允许重复关闭。"""
    if ds is None:
        return
    try:
        ds.close()
    except RuntimeError:
        pass


def _station_cf_cache_dir(args) -> Path:
    """返回场站 CF 缓存目录；兼容测试中手工构造的旧参数对象。"""
    value = getattr(args, "station_cf_cache_dir", None)
    if value is None:
        return Path(args.output_dir).parent / "cache" / "era5land_station_cf"
    return Path(value)


def _station_cf_cache_path(
    args,
    *,
    scenario: str,
    tech: str,
    threshold_interp: str,
) -> Path:
    """返回当前场站集合对应的 ERA5Land 场站 CF 缓存路径。"""
    name = (
        f"station_cf_{scenario}_{tech}_ERA5Land_"
        f"{args.baseline_years}_{threshold_interp}.nc"
    )
    return _station_cf_cache_dir(args) / name


def _read_all_times(files: list[Path]) -> pd.DatetimeIndex:
    """读取并拼接 ERA5Land 月文件时间轴。"""
    times = [full_precompute.read_month_time(path) for path in files]
    return pd.DatetimeIndex(np.concatenate([t.values for t in times]))


def _time_seconds(times: pd.DatetimeIndex) -> np.ndarray:
    """把时间轴转成 Unix 秒，便于写入 NetCDF/HDF5。"""
    return times.to_numpy(dtype="datetime64[s]").astype(np.int64)


def _station_cf_cache_compress_level(args) -> int:
    return int(getattr(args, "station_cf_cache_compress_level", 1))


def _cache_time_chunk_arg(args) -> int | None:
    value = getattr(args, "cache_time_chunk", None)
    if value is None:
        return None
    return int(value)


def _overwrite_station_cf_cache(args) -> bool:
    return bool(getattr(args, "overwrite_station_cf_cache", False))


def _no_station_cf_cache(args) -> bool:
    return bool(getattr(args, "no_station_cf_cache", False))


def _source_files_attr(files: list[Path]) -> str:
    return ",".join(str(p) for p in files)


def _validate_station_cf_cache(
    path: Path,
    *,
    scenario: str,
    tech: str,
    stations: pd.DataFrame,
    match: cf_low_resource.FourPointMatch,
    times: pd.DatetimeIndex,
    files: list[Path],
    baseline_years: str,
    baseline_effective: str,
    threshold_interp: str,
    interpolation_method: str,
) -> None:
    """校验场站 CF 缓存是否可直接复用。"""
    expected_attrs = {
        "cache_kind": "era5land_station_cf",
        "scenario": scenario,
        "tech": tech,
        "baseline_years": baseline_years,
        "baseline_years_effective": baseline_effective,
        "threshold_interp": threshold_interp,
        "interpolation_method": interpolation_method,
        "resource_variable": full_precompute.era5land_cf_var(tech),
        "source_files": _source_files_attr(files),
    }
    n_station = len(stations)
    with cf_low_resource.open_h5(path, "r") as f:
        attrs = _decode_attrs(f)
        for key, expected in expected_attrs.items():
            got = attrs.get(key)
            if got != expected:
                raise ValueError(f"属性 {key} 不兼容：期望 {expected!r}，实际 {got!r}")

        required_shapes = {
            "time": (len(times),),
            "station": (n_station,),
            "corner": (4,),
            "station_lon": (n_station,),
            "station_lat": (n_station,),
            "station_type": (n_station,),
            "capacity_gw": (n_station,),
            "activation_year": (n_station,),
            "era5_lat_idx": (n_station, 4),
            "era5_lon_idx": (n_station, 4),
            "era5_lat": (n_station, 4),
            "era5_lon": (n_station, 4),
            "weight": (n_station, 4),
            "cf": (len(times), n_station),
        }
        for name, expected_shape in required_shapes.items():
            data = _require_dataset(f, name)
            if tuple(data.shape) != expected_shape:
                raise ValueError(
                    f"变量 {name} 形状不兼容：期望 {expected_shape}，实际 {tuple(data.shape)}"
                )

        if not np.array_equal(f["time"][:].astype(np.int64), _time_seconds(times)):
            raise ValueError("time 坐标与当前 ERA5Land 月文件不一致")
        np.testing.assert_allclose(
            f["station_lon"][:].astype(np.float64),
            stations["lon"].to_numpy(np.float64),
            atol=1e-6,
        )
        np.testing.assert_allclose(
            f["station_lat"][:].astype(np.float64),
            stations["lat"].to_numpy(np.float64),
            atol=1e-6,
        )
        expected_type = np.full(n_station, _station_type_code(tech), dtype=np.int8)
        if not np.array_equal(f["station_type"][:].astype(np.int8), expected_type):
            raise ValueError("station_type 与当前场站不一致")
        if not np.array_equal(f["era5_lat_idx"][:].astype(np.int64), match.lat_idx):
            raise ValueError("era5_lat_idx 与当前场站匹配不一致")
        if not np.array_equal(f["era5_lon_idx"][:].astype(np.int64), match.lon_idx):
            raise ValueError("era5_lon_idx 与当前场站匹配不一致")
        np.testing.assert_allclose(
            f["weight"][:].astype(np.float32),
            match.weights.astype(np.float32),
            atol=1e-6,
        )
        weight_sum = f["weight"][:].astype(np.float32).sum(axis=1)
        if not np.allclose(weight_sum, 1.0, atol=1e-5):
            raise ValueError("缓存 weight 权重和不等于 1")


def _create_station_cf_cache_output(
    path: Path,
    *,
    scenario: str,
    tech: str,
    stations: pd.DataFrame,
    match: cf_low_resource.FourPointMatch,
    times: pd.DatetimeIndex,
    files: list[Path],
    baseline_years: str,
    baseline_effective: str,
    threshold_interp: str,
    interpolation_method: str,
    corner_order: str,
    compress_level: int,
) -> netCDF4.Dataset:
    """创建场站 CF 缓存文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = netCDF4.Dataset(path, "w", format="NETCDF4")
    n_time = len(times)
    n_station = len(stations)
    ds.createDimension("time", n_time)
    ds.createDimension("station", n_station)
    ds.createDimension("corner", 4)

    time_v = ds.createVariable("time", "i8", ("time",))
    station_v = ds.createVariable("station", "i4", ("station",))
    corner_v = ds.createVariable("corner", "i1", ("corner",))
    time_v[:] = _time_seconds(times)
    time_v.units = "seconds since 1970-01-01"
    station_v[:] = np.arange(n_station, dtype=np.int32)
    corner_v[:] = np.arange(4, dtype=np.int8)

    lon = ds.createVariable("station_lon", "f4", ("station",), zlib=True,
                            complevel=compress_level)
    lat = ds.createVariable("station_lat", "f4", ("station",), zlib=True,
                            complevel=compress_level)
    stype = ds.createVariable("station_type", "i1", ("station",), zlib=True,
                              complevel=compress_level)
    capacity = ds.createVariable("capacity_gw", "f4", ("station",), zlib=True,
                                 complevel=compress_level)
    activation = ds.createVariable("activation_year", "i2", ("station",), zlib=True,
                                   complevel=compress_level)
    era5_lat_idx = ds.createVariable("era5_lat_idx", "i4", ("station", "corner"),
                                     zlib=True, complevel=compress_level)
    era5_lon_idx = ds.createVariable("era5_lon_idx", "i4", ("station", "corner"),
                                     zlib=True, complevel=compress_level)
    era5_lat = ds.createVariable("era5_lat", "f4", ("station", "corner"),
                                 zlib=True, complevel=compress_level)
    era5_lon = ds.createVariable("era5_lon", "f4", ("station", "corner"),
                                 zlib=True, complevel=compress_level)
    weight = ds.createVariable("weight", "f4", ("station", "corner"),
                               zlib=True, complevel=compress_level)

    cache_time_chunk = min(744, max(1, n_time))
    cache_station_chunk = min(1024, max(1, n_station))
    cf = ds.createVariable(
        "cf", "f4", ("time", "station"),
        zlib=True, complevel=compress_level, fill_value=np.float32(np.nan),
        chunksizes=(cache_time_chunk, cache_station_chunk),
    )
    cf.units = "1"
    cf.long_name = "ERA5Land 场站容量因子缓存"

    lon[:] = stations["lon"].to_numpy(np.float32)
    lat[:] = stations["lat"].to_numpy(np.float32)
    stype[:] = np.full(n_station, _station_type_code(tech), dtype=np.int8)
    capacity[:] = stations["capacity_gw"].to_numpy(np.float32)
    activation[:] = stations["activation_year"].to_numpy(np.int16)
    era5_lat_idx[:, :] = match.lat_idx.astype(np.int32)
    era5_lon_idx[:, :] = match.lon_idx.astype(np.int32)
    era5_lat[:, :] = match.corner_lat.astype(np.float32)
    era5_lon[:, :] = match.corner_lon.astype(np.float32)
    weight[:, :] = match.weights.astype(np.float32)

    ds.cache_kind = "era5land_station_cf"
    ds.scenario = scenario
    ds.tech = tech
    ds.baseline_years = baseline_years
    ds.baseline_years_effective = baseline_effective
    ds.threshold_interp = threshold_interp
    ds.interpolation_method = interpolation_method
    ds.corner_order = corner_order
    ds.resource_variable = full_precompute.era5land_cf_var(tech)
    ds.source_files = _source_files_attr(files)
    ds.created_by = "scripts/precompute_station_low_resource_thresholds.py"
    return ds


def _gather_station_cf_from_slab(
    slab: np.ndarray,
    match: cf_low_resource.FourPointMatch,
) -> np.ndarray:
    """从一个原生时间 chunk 的全球 CF 中抽取场站 CF。"""
    n_station = match.lat_idx.shape[0]
    out = np.zeros((slab.shape[0], n_station), dtype=np.float32)
    for corner in range(4):
        corner_weight = match.weights[:, corner].astype(np.float32)
        if not np.any(corner_weight != 0.0):
            continue
        values = slab[:, match.lat_idx[:, corner], match.lon_idx[:, corner]]
        out += values.astype(np.float32, copy=False) * corner_weight[None, :]
    return out


def _build_station_cf_cache(
    path: Path,
    *,
    args,
    scenario: str,
    tech: str,
    stations: pd.DataFrame,
    match: cf_low_resource.FourPointMatch,
    files: list[Path],
    lat: np.ndarray,
    lon: np.ndarray,
    times: pd.DatetimeIndex,
    baseline_effective: str,
    interpolation_method: str,
    corner_order: str,
) -> None:
    """按 ERA5Land HDF5 原生 chunk 生成场站 CF 缓存。"""
    tmp_path = _temporary_output_path(path)
    if tmp_path.exists():
        tmp_path.unlink()
    ds: netCDF4.Dataset | None = None
    try:
        ds = _create_station_cf_cache_output(
            tmp_path,
            scenario=scenario,
            tech=tech,
            stations=stations,
            match=match,
            times=times,
            files=files,
            baseline_years=args.baseline_years,
            baseline_effective=baseline_effective,
            threshold_interp=args.threshold_interp,
            interpolation_method=interpolation_method,
            corner_order=corner_order,
            compress_level=_station_cf_cache_compress_level(args),
        )
        out = ds["cf"]
        var_name = full_precompute.era5land_cf_var(tech)
        global_t0 = 0
        requested_chunk = _cache_time_chunk_arg(args)
        for month_idx, month_path in enumerate(files, start=1):
            with cf_low_resource.open_h5(month_path, "r") as f:
                month_times = cf_low_resource.read_time(f)
                d = f[var_name]
                if d.shape[1] != lat.size or d.shape[2] != lon.size:
                    raise ValueError(
                        f"{month_path}: CF 变量形状与 lat/lon 不一致：{tuple(d.shape)}"
                    )
                native_chunk = int(d.chunks[0]) if d.chunks else int(d.shape[0])
                time_chunk = requested_chunk or native_chunk
                if time_chunk < 1:
                    raise ValueError("--cache_time_chunk 必须为正整数")
                if requested_chunk is not None and requested_chunk < native_chunk:
                    logger.warning(
                        "[%s/%s] cache_time_chunk=%d 小于原生 chunk=%d，可能重复解压：%s",
                        scenario, tech, requested_chunk, native_chunk, month_path,
                    )
                for t0 in range(0, int(d.shape[0]), time_chunk):
                    t1 = min(t0 + time_chunk, int(d.shape[0]))
                    slab = d[t0:t1, :, :].astype(np.float32, copy=False)
                    out[global_t0 + t0:global_t0 + t1, :] = _gather_station_cf_from_slab(
                        slab,
                        match,
                    )
            logger.info(
                "[%s/%s] 场站 CF 缓存写入月文件 %d/%d：%s，time=%d:%d",
                scenario, tech, month_idx, len(files), month_path,
                global_t0, global_t0 + len(month_times),
            )
            global_t0 += len(month_times)
        if global_t0 != len(times):
            raise ValueError(f"缓存写入时间长度不一致：写入 {global_t0}，期望 {len(times)}")
    except Exception:
        _close_dataset(ds)
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    finally:
        _close_dataset(ds)
    try:
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def load_or_build_station_cf_cache(
    args,
    *,
    scenario: str,
    tech: str,
    stations: pd.DataFrame,
    match: cf_low_resource.FourPointMatch,
    files: list[Path],
    lat: np.ndarray,
    lon: np.ndarray,
    baseline_effective: str,
    interpolation_method: str,
    corner_order: str,
) -> StationCfCache:
    """读取可用场站 CF 缓存；不存在或不兼容时重新生成。"""
    path = _station_cf_cache_path(
        args,
        scenario=scenario,
        tech=tech,
        threshold_interp=args.threshold_interp,
    )
    times = _read_all_times(files)
    full_precompute.validate_time_axis(times, args.allow_incomplete)
    if path.exists() and not _overwrite_station_cf_cache(args):
        try:
            _validate_station_cf_cache(
                path,
                scenario=scenario,
                tech=tech,
                stations=stations,
                match=match,
                times=times,
                files=files,
                baseline_years=args.baseline_years,
                baseline_effective=baseline_effective,
                threshold_interp=args.threshold_interp,
                interpolation_method=interpolation_method,
            )
            logger.info("[%s/%s] 复用场站 CF 缓存：%s", scenario, tech, path)
            return StationCfCache(path=path, times=times)
        except Exception as exc:
            logger.warning("[%s/%s] 场站 CF 缓存不可复用，将重建：%s；原因：%s",
                           scenario, tech, path, exc)
    elif path.exists():
        logger.info("[%s/%s] 按参数重新生成场站 CF 缓存：%s", scenario, tech, path)

    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(
        "[%s/%s] 开始生成场站 CF 缓存：场站=%d，月文件=%d，输出=%s",
        scenario, tech, len(stations), len(files), path,
    )
    _build_station_cf_cache(
        path,
        args=args,
        scenario=scenario,
        tech=tech,
        stations=stations,
        match=match,
        files=files,
        lat=lat,
        lon=lon,
        times=times,
        baseline_effective=baseline_effective,
        interpolation_method=interpolation_method,
        corner_order=corner_order,
    )
    _validate_station_cf_cache(
        path,
        scenario=scenario,
        tech=tech,
        stations=stations,
        match=match,
        times=times,
        files=files,
        baseline_years=args.baseline_years,
        baseline_effective=baseline_effective,
        threshold_interp=args.threshold_interp,
        interpolation_method=interpolation_method,
    )
    logger.info("[%s/%s] 场站 CF 缓存生成完成：%s", scenario, tech, path)
    return StationCfCache(path=path, times=times)


def iter_thresholds_from_station_cf_cache(
    cache: StationCfCache,
    *,
    station_chunk: int,
    allow_incomplete: bool,
):
    """从场站 CF 缓存按 station chunk 计算阈值。"""
    with cf_low_resource.open_h5(cache.path, "r") as f:
        times = cf_low_resource.read_time(f)
        full_precompute.validate_time_axis(times, allow_incomplete)
        cf = f["cf"]
        n_station = int(cf.shape[1])
        for c0 in range(0, n_station, station_chunk):
            c1 = min(c0 + station_chunk, n_station)
            block = cf[:, c0:c1].astype(np.float32)
            clim, threshold, valid_count = full_precompute.compute_threshold_block(block, times)
            yield c0, c1, clim, threshold, valid_count


def create_sparse_output(
    path: Path,
    *,
    scenario: str,
    tech: str,
    stations: pd.DataFrame,
    match: cf_low_resource.FourPointMatch,
    files: list[Path],
    baseline_years: str,
    baseline_effective: str,
    interpolation_method: str,
    corner_order: str,
    reuse_enabled: bool,
    reuse_from_scenario: str | None,
    reuse_source_file: Path | None,
    reuse_station_count: int,
    computed_station_count: int,
    station_cf_cache_enabled: bool,
    station_cf_cache_file: Path | None,
    compress_level: int,
) -> netCDF4.Dataset:
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = netCDF4.Dataset(path, "w", format="NETCDF4")
    n_station = len(stations)
    ds.createDimension("station", n_station)
    ds.createDimension("corner", 4)
    ds.createDimension("month", 12)
    ds.createDimension("hour", 24)

    ds.createVariable("station", "i4", ("station",))[:] = np.arange(n_station, dtype=np.int32)
    ds.createVariable("corner", "i1", ("corner",))[:] = np.arange(4, dtype=np.int8)
    ds.createVariable("month", "i2", ("month",))[:] = np.arange(1, 13, dtype=np.int16)
    ds.createVariable("hour", "i2", ("hour",))[:] = np.arange(24, dtype=np.int16)

    lon = ds.createVariable("station_lon", "f4", ("station",), zlib=True,
                            complevel=compress_level)
    lat = ds.createVariable("station_lat", "f4", ("station",), zlib=True,
                            complevel=compress_level)
    stype = ds.createVariable("station_type", "i1", ("station",), zlib=True,
                              complevel=compress_level)
    capacity = ds.createVariable("capacity_gw", "f4", ("station",), zlib=True,
                                 complevel=compress_level)
    activation = ds.createVariable("activation_year", "i2", ("station",), zlib=True,
                                   complevel=compress_level)
    era5_lat_idx = ds.createVariable("era5_lat_idx", "i4", ("station", "corner"),
                                     zlib=True, complevel=compress_level)
    era5_lon_idx = ds.createVariable("era5_lon_idx", "i4", ("station", "corner"),
                                     zlib=True, complevel=compress_level)
    era5_lat = ds.createVariable("era5_lat", "f4", ("station", "corner"),
                                 zlib=True, complevel=compress_level)
    era5_lon = ds.createVariable("era5_lon", "f4", ("station", "corner"),
                                 zlib=True, complevel=compress_level)
    weight = ds.createVariable("weight", "f4", ("station", "corner"),
                               zlib=True, complevel=compress_level)

    lon[:] = stations["lon"].to_numpy(np.float32)
    lat[:] = stations["lat"].to_numpy(np.float32)
    stype[:] = np.full(n_station, _station_type_code(tech), dtype=np.int8)
    capacity[:] = stations["capacity_gw"].to_numpy(np.float32)
    activation[:] = stations["activation_year"].to_numpy(np.int16)
    era5_lat_idx[:, :] = match.lat_idx.astype(np.int32)
    era5_lon_idx[:, :] = match.lon_idx.astype(np.int32)
    era5_lat[:, :] = match.corner_lat.astype(np.float32)
    era5_lon[:, :] = match.corner_lon.astype(np.float32)
    weight[:, :] = match.weights.astype(np.float32)

    fill = np.float32(np.nan)
    clim = ds.createVariable(
        "clim", "f4", ("month", "hour", "station"),
        zlib=True, complevel=compress_level, fill_value=fill,
        chunksizes=(1, 24, min(256, max(1, n_station))),
    )
    threshold = ds.createVariable(
        "threshold", "f4", ("station",),
        zlib=True, complevel=compress_level, fill_value=fill,
        chunksizes=(min(1024, max(1, n_station)),),
    )
    valid_count = ds.createVariable(
        "valid_count", "i4", ("station",),
        zlib=True, complevel=compress_level,
        chunksizes=(min(1024, max(1, n_station)),),
    )
    clim.long_name = "ERA5Land station-level month-hour capacity factor rolling-mean climatology"
    threshold.long_name = "ERA5Land station-level low-resource anomaly percentile threshold"
    valid_count.long_name = "Finite anomaly count used for threshold"

    ds.threshold_kind = "sparse_station"
    ds.threshold_source = "ERA5Land"
    ds.threshold_source_files = ",".join(str(p) for p in files)
    ds.scenario = scenario
    ds.tech = tech
    ds.baseline_years_requested = baseline_years
    ds.baseline_years_effective = baseline_effective
    ds.resource_variable = full_precompute.era5land_cf_var(tech)
    ds.resource_units = "1"
    ds.interpolation_method = interpolation_method
    ds.corner_order = corner_order
    ds.window_hours = "24"
    ds.window_steps = "24"
    ds.timestep_hours = "1"
    ds.percentile = "5"
    ds.climatology = "month_hour"
    ds.reuse_enabled = "true" if reuse_enabled else "false"
    ds.reuse_from_scenario = reuse_from_scenario or ""
    ds.reuse_source_file = "" if reuse_source_file is None else str(reuse_source_file)
    ds.reuse_station_count = str(int(reuse_station_count))
    ds.computed_station_count = str(int(computed_station_count))
    ds.reuse_key = "lon,lat,type"
    ds.station_cf_cache_enabled = "true" if station_cf_cache_enabled else "false"
    ds.station_cf_cache_file = "" if station_cf_cache_file is None else str(station_cf_cache_file)
    ds.created_by = "scripts/precompute_station_low_resource_thresholds.py"
    return ds


def process_tech(args, scenario: str, tech: str) -> Path | None:
    stations = _prepare_stations(args.stations_csv, tech)
    if stations.empty:
        logger.info("[%s/%s] 无场站，跳过", scenario, tech)
        return None

    files, missing = full_precompute.find_monthly_files(
        args.cf_root, tech, args.baseline_years
    )
    if missing:
        msg = "缺少 ERA5Land CF 月文件：" + ", ".join(f"{y}-{m:02d}" for y, m in missing)
        if not args.allow_incomplete:
            raise FileNotFoundError(f"{tech}: {msg}")
        logger.warning("[%s/%s] %s；按 --allow_incomplete 继续", scenario, tech, msg)
    if not files:
        raise FileNotFoundError(f"{tech}: 未找到 ERA5Land CF 文件")

    lat, lon, shape = full_precompute.read_static_grid(files[0])
    out_path = cf_low_resource.sparse_threshold_file_for_scenario_tech(
        args.output_dir, scenario, tech, args.baseline_years
    )
    logger.info(
        "[%s/%s] 场站=%d 月文件=%d ERA5Land网格=%dx%d 输出=%s",
        scenario, tech, len(stations), len(files), lat.size, lon.size, out_path,
    )
    if args.dry_run:
        return out_path
    if out_path.exists() and not args.overwrite:
        logger.info("[%s/%s] 输出已存在，跳过：%s", scenario, tech, out_path)
        return out_path
    if shape[1] != lat.size or shape[2] != lon.size:
        raise ValueError(f"{files[0]}: CF 变量形状与 lat/lon 不一致：{shape}")

    baseline_effective = full_precompute.effective_years(files)
    interpolation_method, corner_order = _interpolation_metadata(args.threshold_interp)
    reuse_source = _try_load_reuse_source(
        args,
        scenario=scenario,
        tech=tech,
        baseline_effective=baseline_effective,
        interpolation_method=interpolation_method,
    )

    n_station = len(stations)
    source_idx_for_target = np.full(n_station, -1, dtype=np.int64)
    if reuse_source is not None:
        for i, key in enumerate(_station_keys_from_frame(stations, tech)):
            source_idx = reuse_source.key_to_index.get(key)
            if source_idx is not None:
                source_idx_for_target[i] = int(source_idx)
    reuse_target_idx = np.where(source_idx_for_target >= 0)[0]
    reuse_source_idx = source_idx_for_target[reuse_target_idx]
    compute_idx = np.where(source_idx_for_target < 0)[0]
    if reuse_source is not None:
        logger.info(
            "[%s/%s] 复用源=%s，目标场站=%d，复用=%d，新计算=%d，复用比例=%.2f%%",
            scenario,
            tech,
            reuse_source.path,
            n_station,
            reuse_target_idx.size,
            compute_idx.size,
            100.0 * reuse_target_idx.size / max(1, n_station),
        )

    compute_match = None
    if compute_idx.size:
        compute_match = _compute_station_match(
            args,
            scenario=scenario,
            tech=tech,
            lat=lat,
            lon=lon,
            stations=stations.iloc[compute_idx].reset_index(drop=True),
            files=files,
        )
    match = _compose_match(
        n_station,
        reuse_source=reuse_source,
        reuse_target_idx=reuse_target_idx,
        reuse_source_idx=reuse_source_idx,
        compute_idx=compute_idx,
        compute_match=compute_match,
    )
    weight_sum = match.weights.sum(axis=1)
    if not np.allclose(weight_sum, 1.0, atol=1e-5):
        raise ValueError("场站匹配权重和不等于 1")

    compute_cache: StationCfCache | None = None
    if compute_idx.size and compute_match is not None:
        if _no_station_cf_cache(args):
            logger.info("[%s/%s] 已关闭场站 CF 缓存，使用旧的直接读取路径", scenario, tech)
        else:
            compute_cache = load_or_build_station_cf_cache(
                args,
                scenario=scenario,
                tech=tech,
                stations=stations.iloc[compute_idx].reset_index(drop=True),
                match=compute_match,
                files=files,
                lat=lat,
                lon=lon,
                baseline_effective=baseline_effective,
                interpolation_method=interpolation_method,
                corner_order=corner_order,
            )

    tmp_path = _temporary_output_path(out_path)
    if tmp_path.exists():
        tmp_path.unlink()
    ds: netCDF4.Dataset | None = None
    try:
        ds = create_sparse_output(
            tmp_path,
            scenario=scenario,
            tech=tech,
            stations=stations,
            match=match,
            files=files,
            baseline_years=args.baseline_years,
            baseline_effective=baseline_effective,
            interpolation_method=interpolation_method,
            corner_order=corner_order,
            reuse_enabled=bool(reuse_target_idx.size),
            reuse_from_scenario=getattr(args, "reuse_from_scenario", "ssp126")
            if reuse_target_idx.size else None,
            reuse_source_file=reuse_source.path
            if reuse_source is not None and reuse_target_idx.size else None,
            reuse_station_count=int(reuse_target_idx.size),
            computed_station_count=int(compute_idx.size),
            station_cf_cache_enabled=compute_cache is not None,
            station_cf_cache_file=compute_cache.path if compute_cache is not None else None,
            compress_level=args.compress_level,
        )
        if reuse_source is not None and reuse_target_idx.size:
            _write_reused_values(ds, reuse_source, reuse_target_idx, reuse_source_idx)
            logger.info(
                "[%s/%s] 已复用写入 station 数=%d",
                scenario, tech, reuse_target_idx.size,
            )

        if compute_idx.size and compute_match is not None:
            if compute_cache is not None:
                for c0, c1, clim, threshold, valid_count in iter_thresholds_from_station_cf_cache(
                    compute_cache,
                    station_chunk=args.station_chunk,
                    allow_incomplete=args.allow_incomplete,
                ):
                    target_idx = compute_idx[c0:c1]
                    _write_computed_values(ds, target_idx, clim, threshold, valid_count)
                    logger.info(
                        "[%s/%s] 从场站 CF 缓存写入 station %d:%d",
                        scenario, tech, int(target_idx[0]), int(target_idx[-1]) + 1,
                    )
            else:
                first_time: pd.DatetimeIndex | None = None
                for c0 in range(0, compute_idx.size, args.station_chunk):
                    c1 = min(c0 + args.station_chunk, compute_idx.size)
                    times, block = load_station_block(files, tech, compute_match, slice(c0, c1))
                    if first_time is None:
                        full_precompute.validate_time_axis(times, args.allow_incomplete)
                        first_time = times
                    clim, threshold, valid_count = full_precompute.compute_threshold_block(
                        block,
                        times,
                    )
                    target_idx = compute_idx[c0:c1]
                    _write_computed_values(ds, target_idx, clim, threshold, valid_count)
                    logger.info(
                        "[%s/%s] 直接读取 ERA5Land 写入 station %d:%d",
                        scenario, tech, int(target_idx[0]), int(target_idx[-1]) + 1,
                    )
    except Exception:
        _close_dataset(ds)
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    finally:
        _close_dataset(ds)
    try:
        os.replace(tmp_path, out_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    return out_path


def main() -> None:
    args = build_parser().parse_args()
    setup_logging("precompute_station_low_resource_thresholds")
    if args.station_chunk < 1:
        raise ValueError("--station_chunk 必须为正整数")
    if args.cache_time_chunk is not None and args.cache_time_chunk < 1:
        raise ValueError("--cache_time_chunk 必须为正整数")
    if args.station_cf_cache_compress_level < 0:
        raise ValueError("--station_cf_cache_compress_level 不能为负数")
    scenario = args.scenario or sm.infer_scenario_from_csv(args.stations_csv)
    techs = ["wind", "solar"] if args.tech == "both" else [args.tech]
    for tech in techs:
        process_tech(args, scenario, tech)


if __name__ == "__main__":
    main()
