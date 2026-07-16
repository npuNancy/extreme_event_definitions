"""场站/网格气象抽取 -> NetCDF (Spark02 ERA5-Land 全球月文件 + MERRA-2 沙尘)。

替代 CoST_model/generate_weather_cache_linearv3.py 的 .npy 缓存:
  - 输出 **NetCDF**(自描述: 命名变量 + 单位 + station_id 索引), 比 [T,K,4] npy 好用
  - 在原 4 变量基础上 **新增湿度 rh_pct + 沙尘 dust_aod**
  - 输入: 场站 meta(station_id/lon/lat[/name/country]) 或 网格点; 数据全部从 **Spark02** 抽

提速要点(相比初版):
  - 用 **numpy 最近邻 gather**(非 xarray.interp), 单变量月文件读一次即取全部站
  - **沙尘全局只读一遍**(每日 MERRA-2 文件读 1 次采样所有站, 不按组重复)
  - 按 country 分区切 bbox 控内存; 每变量去累积一次性在 (T,K) 上做

口径:
  - 时间 **UTC**(ERA5-Land 原生); tp/ssrd 去累积(hour==1) + 单位换算(tp m->mm, ssrd J->W)
  - 经度内部统一 [-180,180); ERA5-Land 网格用 0-360, MERRA-2 用 -180..180

输出(dims=(time, station); station 以 station_id 为索引):
  temp_C[°C] wind_ms[m/s,10m] rsds[W/m^2] precip_mmh[mm/h] rh_pct[%] dust_aod[1]
  + 坐标 name/lat/lon

用法:
  python extract_station_weather_nc.py --meta stations.csv --start_year 2015 --end_year 2024 \
         --out era5land_dust_rh_2015_2024.nc
依赖: 与 weather_loaders.py 同目录; numpy/pandas/xarray。
"""
from __future__ import annotations
import argparse
import glob as _g
import logging
import os
import sys
import time
import numpy as np
import pandas as pd
import xarray as xr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import legacy_station_pipeline.weather_loaders as wl
from tools.logging_utils import setup_logging

logger = logging.getLogger(__name__)

ACCUM = {"tp", "ssrd"}


def _log(message, *args):
    logger.info(message, *args)


def gather_var(prefix, years, slat, slon360, bbox):
    """读某变量(全球月文件)所有 year×month, bbox 切片, 最近邻 gather 到站点。返回 (arr(T,K), time)。"""
    pieces, times = [], []
    for y in years:
        for m in range(1, 13):
            fs = _g.glob(wl.era5_file(prefix, y, m))
            if not fs:
                continue
            d = xr.open_dataset(fs[0])
            latn = "latitude" if "latitude" in d.coords else "lat"
            lonn = "longitude" if "longitude" in d.coords else "lon"
            tn = "valid_time" if "valid_time" in d.coords else "time"
            vname = prefix if prefix in d.data_vars else list(d.data_vars)[0]
            sub = d[vname].sel({latn: slice(bbox[0], bbox[1]), lonn: slice(bbox[2], bbox[3])}).load()
            gla = sub[latn].values; glo = sub[lonn].values
            ila = np.abs(gla[None, :] - np.asarray(slat)[:, None]).argmin(axis=1)
            ilo = np.abs(glo[None, :] - np.asarray(slon360)[:, None]).argmin(axis=1)
            arr = sub.transpose(tn, latn, lonn).values
            pieces.append(arr[:, ila, ilo].astype(np.float32))
            times.append(pd.to_datetime(sub[tn].values))
            d.close()
    if not pieces:
        raise FileNotFoundError(f"在 {wl.ERA5_ROOT} 下未找到 {years} 的 ERA5-Land {prefix}")
    return np.concatenate(pieces, 0), pd.DatetimeIndex(np.concatenate([t.values for t in times]))


def _align(arr, t, canon):
    out = np.full((len(canon), arr.shape[1]), np.nan, np.float32)
    pos = canon.get_indexer(pd.DatetimeIndex(t)); ok = pos >= 0
    out[pos[ok]] = arr[ok]
    return out


def _extract_one_var(payload):
    """单变量全流程(所有组/月 + 去累积 + 对齐)。供进程池并行调用。返回 (key, (T,K))。"""
    key, prefix, years, lat, lon360, groups, canon = payload
    t0 = time.time()
    out = np.full((len(canon), len(lat)), np.nan, np.float32)
    for gname, idx in groups.items():
        la = np.asarray(lat)[idx]; lo = np.asarray(lon360)[idx]
        pad = 0.5
        bbox = (min(la.max() + pad, 90), max(la.min() - pad, -90),
                max(lo.min() - pad, 0), min(lo.max() + pad, 360))
        arr, t = gather_var(prefix, years, la, lo, bbox)
        if key in ACCUM:
            arr = wl._deaccum(arr, t)
            arr = arr * (wl.TP_M_TO_MM if key == "tp" else wl.SSRD_J_TO_W)
        out[:, idx] = _align(arr, t, canon)
    _log("  ERA5 %-5s 完成（%.0f 秒）", key, time.time() - t0)
    return key, out


def extract_era5(lat, lon, years, canon, groups, workers=6):
    """ERA5-Land 6 变量 -> raw dict, 每个 (T_canon, K)。6 变量进程池并行(独立文件集)。"""
    lon360 = wl.to_360(lon)
    payloads = [(key, prefix, years, lat, lon360, groups, canon) for key, prefix in wl.ERA5_VARS.items()]
    raw = {}
    if workers and workers > 1:
        from concurrent.futures import ProcessPoolExecutor
        try:
            with ProcessPoolExecutor(max_workers=min(workers, len(payloads))) as ex:
                for key, arr in ex.map(_extract_one_var, payloads):
                    raw[key] = arr
            return raw
        except Exception as e:
            _log("  并行失败（%s），回退串行：%s", type(e).__name__, e)
    for p in payloads:
        key, arr = _extract_one_var(p); raw[key] = arr
    return raw


def dust_global(lat, lon, years, canon):
    """MERRA-2 沙尘: 每日文件读一次, 采样所有站。返回 (T_canon, K)。"""
    out = np.full((len(canon), len(lat)), np.nan, np.float32)
    for y in years:
        t0 = time.time()
        sel = canon.year == y
        sub_t = canon[sel]
        arr = wl.sample_dust(y, lat, lon, sub_t)     # (Ty, K), 单年内部已读每日文件一次
        out[np.where(sel)[0]] = arr
        _log("  沙尘 %d 完成（%.0f 秒）", y, time.time() - t0)
    return out


UNITS = {"temp_C": ("degC", "2m air temperature"), "wind_ms": ("m s-1", "10m wind speed"),
         "rsds": ("W m-2", "surface downward shortwave"), "precip_mmh": ("mm h-1", "precipitation"),
         "rh_pct": ("%", "2m relative humidity (Magnus)"), "dust_aod": ("1", "MERRA-2 DUEXTTAU dust AOD")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta", required=True, help="csv: 需含 lon,lat (推荐 station_id/ID, 可选 name, country)")
    ap.add_argument("--start_year", type=int, required=True)
    ap.add_argument("--end_year", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--no_dust", action="store_true")
    ap.add_argument("--group_by_country", type=int, default=1)
    a = ap.parse_args()
    setup_logging("extract_station_weather_nc")
    years = list(range(a.start_year, a.end_year + 1))

    meta = pd.read_csv(a.meta)
    meta.columns = [str(c).lstrip("﻿") for c in meta.columns]
    idc = next((c for c in ("station_id", "id", "ID") if c in meta.columns), None)
    namec = next((c for c in ("name", "station_name", "plant") if c in meta.columns), None)
    lac = next(c for c in ("lat", "latitude", "Lat") if c in meta.columns)
    loc = next(c for c in ("lon", "longitude", "Lon") if c in meta.columns)
    sid = meta[idc].astype(str).to_numpy() if idc else np.array([f"s{i}" for i in range(len(meta))])
    name = meta[namec].astype(str).to_numpy() if namec else sid.copy()
    lat = pd.to_numeric(meta[lac], errors="coerce").to_numpy(float)
    lon = wl.to_180(pd.to_numeric(meta[loc], errors="coerce").to_numpy(float))
    _log("meta：%d 个场站，年份 %d-%d", len(meta), years[0], years[-1])

    if a.group_by_country and "country" in meta.columns:
        groups = {c: np.where(meta["country"].astype(str).to_numpy() == c)[0]
                  for c in meta["country"].astype(str).unique()}
    else:
        groups = {"ALL": np.arange(len(meta))}
    _log("分组：%s", [(g, len(i)) for g, i in groups.items()])

    canon = pd.date_range(f"{years[0]}-01-01 00:00", f"{years[-1]}-12-31 23:00", freq="h")
    raw = extract_era5(lat, lon, years, canon, groups)
    weather = {
        "temp_C": (raw["t2m"] - 273.15).astype(np.float32),
        "wind_ms": np.sqrt(raw["u10"] ** 2 + raw["v10"] ** 2).astype(np.float32),
        "rsds": np.where(raw["ssrd"] > wl.SOLAR_MAX, 0.0, raw["ssrd"]).astype(np.float32),
        "precip_mmh": raw["tp"].astype(np.float32),
        "rh_pct": wl.magnus_rh(raw["t2m"], raw["d2m"]),
    }
    vars_out = ["temp_C", "wind_ms", "rsds", "precip_mmh", "rh_pct"]
    if not a.no_dust:
        weather["dust_aod"] = dust_global(lat, lon, years, canon); vars_out.append("dust_aod")

    ds = xr.Dataset(
        {v: (("time", "station"), weather[v]) for v in vars_out},
        coords={"time": canon.values, "station": sid,
                "name": ("station", name), "lat": ("station", lat), "lon": ("station", lon)},
    )
    ds = ds.set_index(station="station")          # station_id 作索引
    ds["station"].attrs["long_name"] = "station_id"
    for v in vars_out:
        ds[v].attrs["units"], ds[v].attrs["long_name"] = UNITS[v]
    ds.attrs.update(source="ERA5-Land(global)+MERRA2 DUEXTTAU via Spark02", time="UTC",
                    interp="nearest", years=f"{a.start_year}-{a.end_year}")
    enc = {v: {"zlib": True, "complevel": 4, "dtype": "float32"} for v in vars_out}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    ds.to_netcdf(a.out, encoding=enc)
    _log("已写出 %s  时间步=%d 场站=%d 变量=%s", a.out, len(canon), len(sid), vars_out)


if __name__ == "__main__":
    main()
