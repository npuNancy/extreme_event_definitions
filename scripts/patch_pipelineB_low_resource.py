#!/usr/bin/env python3
"""补写 Pipeline B 场站级低资源事件。

该脚本只计算 ``signal_low_resource``。目标 E1 文件存在时原位补写；目标区域
有场站但 E1 文件不存在时，创建一个只含低资源事件的兼容场站 NetCDF；目标区域
无对应技术场站时成功跳过。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import warnings
from pathlib import Path

os.environ.setdefault("HDF5_USE_FILE_LOCKING", "FALSE")

import h5py
import numpy as np
import pandas as pd

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import registry  # noqa: E402
from tools import common  # noqa: E402
from grid_extreme_signals import cf_low_resource  # noqa: E402
from grid_extreme_signals import station_match as sm  # noqa: E402
from tools.logging_utils import setup_logging  # noqa: E402

logger = logging.getLogger("patch_pipelineB_low_resource")


DEFAULT_OUTPUT_ROOT = (
    "outputs/station_signals/regional_bcsd/NESM3"
)
DEFAULT_CF_ROOT = "data/cfs"
DEFAULT_THRESHOLD_DIR = "outputs/low_resource_thresholds/sparse_station_ERA5Land_2015-2024"


def lon_to_180(lon):
    return ((np.asarray(lon, dtype=np.float64) + 180.0) % 360.0) - 180.0


def is_lon_360(grid_lon) -> bool:
    arr = np.asarray(grid_lon, dtype=np.float64)
    return bool(arr.size and float(np.nanmax(arr)) > 180.0)


def normalize_grid_lon(grid_lon) -> np.ndarray:
    arr = np.asarray(grid_lon, dtype=np.float64)
    return lon_to_180(arr) if is_lon_360(arr) else arr


def nearest_index_regular(grid_lat, grid_lon, sta_lat, sta_lon):
    grid_lat = np.asarray(grid_lat, dtype=np.float64)
    grid_lon = np.asarray(grid_lon, dtype=np.float64)
    sta_lat = np.asarray(sta_lat, dtype=np.float64)
    sta_lon = np.asarray(sta_lon, dtype=np.float64)
    n_sta = sta_lat.shape[0]
    lat_idx = np.empty(n_sta, dtype=np.int64)
    lon_idx = np.empty(n_sta, dtype=np.int64)
    dlat = np.empty(n_sta, dtype=np.float64)
    dlon = np.empty(n_sta, dtype=np.float64)
    for i in range(n_sta):
        lat_dist = np.abs(grid_lat - sta_lat[i])
        j_lat = int(np.argmin(lat_dist))
        lat_idx[i] = j_lat
        dlat[i] = lat_dist[j_lat]

        lon_dist = np.abs(((grid_lon - sta_lon[i] + 180.0) % 360.0) - 180.0)
        j_lon = int(np.argmin(lon_dist))
        lon_idx[i] = j_lon
        dlon[i] = lon_dist[j_lon]
    return lat_idx, lon_idx, np.maximum(dlat, dlon)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="补写 Pipeline B 文件中的 signal_low_resource。",
    )
    p.add_argument("--output_root", default=DEFAULT_OUTPUT_ROOT,
                   help="已有 Pipeline B 结果根目录，指向 source/model 层。")
    p.add_argument("--cf_root", default=DEFAULT_CF_ROOT,
                   help="CF 数据根目录，默认 data/cfs。")
    p.add_argument("--threshold_dir", default=DEFAULT_THRESHOLD_DIR,
                   help="ERA5Land SSP 场站稀疏低资源阈值目录。")
    p.add_argument("--stations_csv",
                   help="当前 SSP 的场站 CSV；逐单元运行时用于判断无场站和创建新文件。")
    p.add_argument("--shp",
                   help="Natural Earth 国家边界 .shp；逐单元运行时必需。")
    p.add_argument("--source", default="regional_bcsd")
    p.add_argument("--model", default="NESM3")
    p.add_argument("--region", default="all",
                   help="区域名或 all。")
    p.add_argument("--scenario", default="all",
                   help="ssp126/ssp245/ssp585 或 all。")
    p.add_argument("--tech", choices=["wind", "solar", "both"], default="both")
    p.add_argument("--years", default="2015-2060")
    p.add_argument("--baseline_years", default=None,
                   help=argparse.SUPPRESS)
    p.add_argument("--station_chunk", type=int, default=128,
                   help="低资源计算时每块场站数。")
    p.add_argument("--time_chunk", type=int, default=512,
                   help="从 CF 文件读取时的时间块大小。")
    p.add_argument("--compress_level", type=int, default=4)
    p.add_argument("--spatial_interp", choices=["nearest", "bilinear"],
                   default="nearest")
    p.add_argument("--max_dist", type=float, default=sm.MAX_DIST_DEG)
    p.add_argument("--overwrite", action="store_true",
                   help="若 signal_low_resource 已存在则覆盖。")
    p.add_argument("--dry_run", action="store_true",
                   help="只列出将处理的文件。")
    p.add_argument("--max_files", type=int, default=0,
                   help="最多处理多少个文件；0 表示不限制。")
    return p


def _decode_attr(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, np.ndarray) and value.shape == ():
        return _decode_attr(value.item())
    return str(value)


def _set_str_attr(obj, key: str, value: str) -> None:
    obj.attrs[key] = np.bytes_(value)


def _open_h5(path: Path, mode: str):
    try:
        return h5py.File(path, mode, locking=False)
    except (TypeError, ValueError):
        return h5py.File(path, mode)


def _parse_years(years: str) -> tuple[int, int]:
    if "-" in years:
        a, b = years.split("-", 1)
        return int(a), int(b)
    y = int(years)
    return y, y


def _decode_time(values: np.ndarray, units: str) -> pd.DatetimeIndex:
    units_l = units.strip()
    if " since " not in units_l:
        raise ValueError(f"无法解析时间单位：{units!r}")
    unit, origin = units_l.split(" since ", 1)
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


def _read_time(f: h5py.File) -> pd.DatetimeIndex:
    units = _decode_attr(f["time"].attrs["units"])
    return _decode_time(f["time"][:], units)


def _infer_timestep_hours(times: pd.DatetimeIndex) -> float:
    if len(times) < 2:
        raise ValueError("时间轴长度不足，无法推断时间步长")
    ns = times.to_numpy(dtype="datetime64[ns]").astype("int64")
    diffs = np.diff(ns).astype(np.float64) / 3.6e12
    return float(np.nanmedian(diffs))


def _window_steps_24h(times: pd.DatetimeIndex) -> int:
    dt = _infer_timestep_hours(times)
    steps = int(round(24.0 / dt))
    if steps < 1 or not np.isclose(steps * dt, 24.0, atol=1e-6):
        raise ValueError(f"时间步长 {dt}h 不能整除 24h")
    return steps


def _find_station_files(args) -> list[Path]:
    root = Path(args.output_root)
    if args.region == "all":
        region_dirs = [p for p in root.iterdir() if p.is_dir()]
    else:
        region_dirs = [root / args.region]

    techs = ["wind", "solar"] if args.tech == "both" else [args.tech]
    scenarios = None if args.scenario == "all" else {args.scenario}
    y0, y1 = _parse_years(args.years)

    files: list[Path] = []
    for region_dir in sorted(region_dirs):
        if not region_dir.exists():
            continue
        region = region_dir.name
        for scenario_dir in sorted(region_dir.iterdir()):
            if not scenario_dir.is_dir():
                continue
            scenario = scenario_dir.name
            if scenarios is not None and scenario not in scenarios:
                continue
            for tech in techs:
                p = scenario_dir / (
                    f"station_signals_{tech}_{args.model}_{region}_{scenario}_{y0}-{y1}.nc"
                )
                if p.exists():
                    files.append(p)
    if args.max_files > 0:
        files = files[:args.max_files]
    return files


def _parse_station_path(path: Path) -> tuple[str, str, str]:
    scenario = path.parent.name
    region = path.parent.parent.name
    name = path.name
    if name.startswith("station_signals_wind_"):
        tech = "wind"
    elif name.startswith("station_signals_solar_"):
        tech = "solar"
    else:
        raise ValueError(f"无法从文件名识别技术类型：{path}")
    return region, scenario, tech


def _target_path(args, region: str, scenario: str, tech: str) -> Path:
    y0, y1 = _parse_years(args.years)
    return (
        Path(args.output_root)
        / region
        / scenario
        / f"station_signals_{tech}_{args.model}_{region}_{scenario}_{y0}-{y1}.nc"
    )


def _load_unit_stations(args, region: str, tech: str) -> pd.DataFrame:
    if not args.stations_csv or not args.shp:
        raise ValueError(
            "逐单元 E2 运行需要同时提供 --stations_csv 和 --shp"
        )
    countries = sm.load_country_shapes(args.shp)
    country_name = sm.bcsd_region_to_ne_name(region)
    if country_name not in countries:
        raise KeyError(
            f"Natural Earth 中没有区域 {region!r} 对应的国家 {country_name!r}"
        )
    stations = sm.load_stations(args.stations_csv)
    return sm.filter_stations_for_country(stations, countries[country_name], tech)


def _cf_subdir(tech: str) -> str:
    return "CFs_of_solar" if tech == "solar" else "CFs_of_wind"


def _cf_var(tech: str) -> str:
    return "solar_cf" if tech == "solar" else "wind_cf"


def _find_cf_file(cf_root: Path, model: str, region: str,
                  scenario: str, tech: str) -> Path | None:
    subdir = _cf_subdir(tech)
    var_prefix = "solar_CF" if tech == "solar" else "wind_CF"
    rel = Path(subdir) / model / region / (
        f"{var_prefix}_{region}_{model}_{scenario}_2015-2060_allmonths.nc"
    )
    p = cf_root / rel
    if p.exists():
        return p

    backup = cf_root / subdir / f"{model}_backup" / region / p.name
    if backup.exists():
        return backup

    matches = sorted((cf_root / subdir).glob(
        f"*/{region}/{var_prefix}_{region}_*_{scenario}_2015-2060_allmonths.nc"
    ))
    return matches[0] if matches else None


def _match_cf_grid(cf_file: Path, station_lats: np.ndarray,
                   station_lons: np.ndarray, max_dist: float):
    with _open_h5(cf_file, "r") as f:
        lat = f["lat"][:]
        lon = f["lon"][:]
    lon_180 = normalize_grid_lon(lon)
    sta_lon = lon_to_180(station_lons.astype(np.float64))
    lat_idx, lon_idx, dist = nearest_index_regular(
        lat, lon_180, station_lats.astype(np.float64), sta_lon
    )
    return lat_idx, lon_idx, dist <= max_dist, lat.size, lon.size, is_lon_360(lon)


def _materialize_station_cf(cf_file: Path, tech: str, lat_idx: np.ndarray,
                            lon_idx: np.ndarray, time_chunk: int,
                            temp_dir: Path) -> np.memmap:
    var = _cf_var(tech)
    with _open_h5(cf_file, "r") as f:
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


def _compute_low_resource_block(cf_block: np.ndarray, times: pd.DatetimeIndex,
                                baseline_mask: np.ndarray, tech: str,
                                lats: np.ndarray, lons: np.ndarray,
                                window_steps: int) -> np.ndarray:
    roll = (
        pd.DataFrame(cf_block)
        .rolling(window_steps, center=True, min_periods=window_steps)
        .mean()
        .to_numpy(dtype=np.float32)
    )
    months = times.month.to_numpy() - 1
    hours = times.hour.to_numpy()
    n_station = cf_block.shape[1]
    clim = np.full((12, 24, n_station), np.nan, dtype=np.float32)
    for m in range(12):
        for h in range(24):
            sel = baseline_mask & (months == m) & (hours == h)
            if np.any(sel):
                with np.errstate(all="ignore"), warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    clim[m, h] = np.nanmean(roll[sel], axis=0)

    anom = roll - clim[months, hours]
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        thr = np.nanpercentile(
            np.where(np.isfinite(anom[baseline_mask]), anom[baseline_mask], np.nan),
            5.0,
            axis=0,
        )
    ev = anom <= thr[None, :]
    ev2 = ev.copy()
    ev2[1:] |= ev[:-1]
    sig = ev2
    sig[~np.isfinite(anom)] = False

    if tech == "solar":
        night = (common.solar_elevation(lats, lons, times) <= 0).T
        sig &= ~night
    return sig.astype(np.int8)


def _ensure_signal_dataset(f: h5py.File, name: str, shape: tuple[int, int],
                           compress_level: int, overwrite: bool):
    if name in f:
        if not overwrite:
            return f[name], False
        del f[name]
    chunks = (min(512, shape[0]), min(256, shape[1]))
    d = f.create_dataset(
        name,
        shape=shape,
        dtype=np.int8,
        chunks=chunks,
        compression="gzip",
        compression_opts=compress_level,
        fillvalue=np.int8(0),
    )
    d.dims[0].attach_scale(f["time"])
    d.dims[1].attach_scale(f["station"])
    d.attrs["_Netcdf4Coordinates"] = np.array([0, 1], dtype=np.int32)
    d.attrs["flag_values"] = np.bytes_("0, 1")
    d.attrs["flag_meanings"] = np.bytes_("false true")
    d.attrs["long_name"] = "极端天气信号：low_resource"
    return d, True


def _csv_list_attr(f: h5py.File, key: str) -> list[str]:
    raw = f.attrs.get(key, b"")
    text = _decode_attr(raw)
    return [x.strip() for x in text.split(",") if x.strip()]


def _set_csv_list_attr(f: h5py.File, key: str, values: list[str]) -> None:
    _set_str_attr(f, key, ",".join(values))


def _update_attrs(f: h5py.File, attrs: dict[str, str]) -> None:
    supported = _csv_list_attr(f, "supported_events")
    if "low_resource" not in supported:
        supported.append("low_resource")
    _set_csv_list_attr(f, "supported_events", sorted(supported))

    skipped = [x for x in _csv_list_attr(f, "skipped_events") if x != "low_resource"]
    _set_csv_list_attr(f, "skipped_events", skipped)

    if "low_resource_baseline_years" in f.attrs:
        del f.attrs["low_resource_baseline_years"]
    for key, value in attrs.items():
        _set_str_attr(f, key, str(value))


def _process_file(path: Path, args) -> bool:
    region, scenario, tech = _parse_station_path(path)
    cf_file = _find_cf_file(Path(args.cf_root), args.model, region, scenario, tech)
    if cf_file is None:
        raise FileNotFoundError(
            f"[{region}/{scenario}/{tech}] 未找到目标容量因子文件：{args.cf_root}"
        )
    threshold_file = cf_low_resource.sparse_threshold_file_for_scenario_tech(
        args.threshold_dir,
        scenario,
        tech,
        args.baseline_years or "2015-2024",
    )
    if not threshold_file.exists():
        threshold_file = cf_low_resource.threshold_file_for_tech(
            args.threshold_dir,
            tech,
            args.baseline_years or "2015-2024",
        )
    if not threshold_file.exists():
        raise FileNotFoundError(
            f"[{region}/{scenario}/{tech}] 未找到 ERA5Land 阈值文件：{threshold_file}"
        )

    logger.info("[%s/%s/%s] 处理 %s", region, scenario, tech, path)
    with _open_h5(path, "r+") as out:
        if "signal_low_resource" in out and not args.overwrite:
            logger.info("[%s/%s/%s] 已存在 signal_low_resource，跳过", region, scenario, tech)
            return False

        out_times = _read_time(out)
        station_lats = out["station_lat"][:].astype(np.float64)
        station_lons = out["station_lon"][:].astype(np.float64)
        activation_years = out["activation_year"][:].astype(np.int64)
        match_dist = out["match_dist_deg"][:].astype(np.float64)
        max_dist = float(_decode_attr(out.attrs.get("max_match_dist_deg", b"0.15")))
        spatial_interp = _decode_attr(out.attrs.get("match_method", b"nearest")).lower()
        if spatial_interp not in {"nearest", "bilinear"}:
            raise ValueError(
                f"{path}: match_method={spatial_interp!r} 不支持；"
                "当前只支持 nearest 或 bilinear"
            )
        n_time = out["time"].shape[0]
        n_station = out["station"].shape[0]

        dset, created = _ensure_signal_dataset(
            out, "signal_low_resource", (n_time, n_station),
            args.compress_level, args.overwrite,
        )
        if not created:
            return False

        years = out_times.year.to_numpy()
        result = cf_low_resource.compute_station_low_resource(
            cf_file,
            tech,
            out_times,
            station_lats,
            station_lons,
            threshold_file=threshold_file,
            max_dist=max_dist,
            spatial_interp=spatial_interp,
            station_chunk=args.station_chunk,
            time_chunk=args.time_chunk,
        )
        valid = (match_dist <= max_dist) & result.valid
        active = years[:, None] >= activation_years[None, :]
        sig = result.mask.astype(bool)
        sig &= active
        sig &= valid[None, :]
        dset[:, :] = sig.astype(np.int8)

        _update_attrs(out, cf_low_resource.attrs(result))
        frac = float(np.mean(dset[:])) if n_time and n_station else 0.0
        logger.info(
            "[%s/%s/%s] 完成 low_resource，事件比例 %.6f，CF文件=%s 阈值=%s",
            region, scenario, tech, frac, cf_file, threshold_file,
        )
        return True


def _create_low_resource_file(
    path: Path,
    args,
    *,
    region: str,
    scenario: str,
    tech: str,
    stations: pd.DataFrame,
) -> bool:
    """E1 文件缺失时创建只含 ``signal_low_resource`` 的兼容文件。"""
    cf_file = cf_low_resource.find_cf_file(
        args.cf_root,
        args.source,
        args.model,
        scenario,
        tech,
        region=region,
        years=args.years,
    )
    if cf_file is None:
        raise FileNotFoundError(
            f"[{region}/{scenario}/{tech}] 未找到目标容量因子文件：{args.cf_root}"
        )
    threshold_file = cf_low_resource.sparse_threshold_file_for_scenario_tech(
        args.threshold_dir,
        scenario,
        tech,
        args.baseline_years or "2015-2024",
    )
    if not threshold_file.exists():
        threshold_file = cf_low_resource.threshold_file_for_tech(
            args.threshold_dir,
            tech,
            args.baseline_years or "2015-2024",
        )
    if not threshold_file.exists():
        raise FileNotFoundError(
            f"[{region}/{scenario}/{tech}] 未找到 ERA5Land 阈值文件：{threshold_file}"
        )

    y0, y1 = _parse_years(args.years)
    with cf_low_resource.open_h5(cf_file, "r") as cf:
        cf_times = cf_low_resource.read_time(cf)
        lat = cf["lat"][:]
        lon = cf["lon"][:]
    year_mask = (cf_times.year >= y0) & (cf_times.year <= y1)
    out_times = cf_times[year_mask]
    if out_times.empty or int(out_times.year.min()) != y0 or int(out_times.year.max()) != y1:
        raise ValueError(
            f"[{region}/{scenario}/{tech}] CF 时间轴未完整覆盖请求边界 {y0}-{y1}"
        )

    match = sm.match_regular_weighted(
        lat,
        lon,
        stations,
        method=args.spatial_interp,
        max_dist=args.max_dist,
    )
    result = cf_low_resource.compute_station_low_resource(
        cf_file,
        tech,
        out_times,
        stations["lat"].to_numpy(np.float64),
        stations["lon"].to_numpy(np.float64),
        threshold_file=threshold_file,
        max_dist=args.max_dist,
        spatial_interp=args.spatial_interp,
        station_chunk=args.station_chunk,
        time_chunk=args.time_chunk,
    )
    active = (
        out_times.year.to_numpy()[:, None]
        >= stations["activation_year"].to_numpy(np.int64)[None, :]
    )
    valid = match.valid.copy()
    if result.valid is not None:
        valid &= result.valid
    signal = result.mask.astype(bool) & active & valid[None, :]

    skipped = sorted(registry.SIMPLE[tech].keys())
    reason = "E1 输出不存在；E2 创建仅含 low_resource 的兼容文件"
    attrs_extra = {
        **cf_low_resource.attrs(result),
        "e2_created_without_e1": "true",
        "e2_creation_reason": reason,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_handle = tempfile.NamedTemporaryFile(
        prefix=path.name + ".tmp.", suffix=".nc", dir=path.parent, delete=False
    )
    temp_path = Path(temp_handle.name)
    temp_handle.close()
    try:
        sm.write_station_signals(
            temp_path,
            {"signal_low_resource": signal.astype(np.int8)},
            out_times.to_numpy(),
            match,
            tech,
            source=args.source,
            model=args.model,
            region=region,
            scenario=scenario,
            source_csv=os.path.basename(args.stations_csv),
            pipeline="B",
            supported=["low_resource"],
            skipped=skipped,
            skipped_reasons={event: reason for event in skipped},
            max_dist=args.max_dist,
            activation_mask_on=True,
            compress_level=args.compress_level,
            attrs_extra=attrs_extra,
        )
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    logger.info(
        "[%s/%s/%s] E1 输出不存在，已新建 low_resource 文件 %s；场站=%d",
        region,
        scenario,
        tech,
        path,
        len(stations),
    )
    return True


def _process_unit(args) -> bool:
    region = args.region
    scenario = args.scenario
    tech = args.tech
    stations = _load_unit_stations(args, region, tech)
    if stations.empty:
        logger.info(
            "[%s/%s/%s] 国家内无场站，跳过",
            region,
            scenario,
            tech,
        )
        return False
    path = _target_path(args, region, scenario, tech)
    if path.exists():
        return _process_file(path, args)
    return _create_low_resource_file(
        path,
        args,
        region=region,
        scenario=scenario,
        tech=tech,
        stations=stations,
    )


def main() -> None:
    args = build_parser().parse_args()
    setup_logging("patch_pipelineB_low_resource")
    explicit_unit = (
        args.region != "all"
        and args.scenario != "all"
        and args.tech != "both"
    )
    if explicit_unit:
        if args.dry_run:
            logger.info("逐单元 dry-run：%s", _target_path(
                args, args.region, args.scenario, args.tech
            ))
            return
        try:
            _process_unit(args)
        except Exception:
            logger.exception(
                "[%s/%s/%s] 处理失败",
                args.region,
                args.scenario,
                args.tech,
            )
            raise SystemExit(1)
        return

    files = _find_station_files(args)
    logger.info("待处理文件数：%d", len(files))
    for p in files:
        logger.info("  %s", p)
    if args.dry_run:
        return
    if not files:
        raise FileNotFoundError(
            "未找到待补写的 E1 场站信号文件；请确认 E1 已完成及筛选参数一致"
        )

    done = 0
    failed = 0
    for p in files:
        try:
            if _process_file(p, args):
                done += 1
        except Exception:
            failed += 1
            logger.exception("[%s] 处理失败，继续下一个文件", p)
    logger.info("完成写入文件数：%d/%d，失败文件数：%d", done, len(files), failed)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
