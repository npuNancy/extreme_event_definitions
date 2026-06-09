"""ERA5-Land + MERRA-2 数据调用 —— 生成事件判定所需的 weather 字典。

经度约定: 站点经度内部统一到 **canonical [-180, 180)**(本模块对外固定范围)。
         ERA5-Land 网格 0..360、MERRA-2 网格 -180..180, 采样时各自转换(to_360/to_180)。

三种入口(按你已有数据选其一):

(A) 已有 covariates_{year}.nc (本项目产物, 最省事):
        from weather_loaders import load_covariates
        weather, time, lat, lon = load_covariates("covariates_2024.nc")

(B) 已有 ERA5-Land 站点抽取 extracted/{var}/{var}_{year}.nc + MERRA-2 沙尘:
        from weather_loaders import build_from_extracted
        weather, time, sid, lat, lon = build_from_extracted(root, year)

(C) ★从零: 场站信息文件 + 原始 ERA5-Land 全球月文件 + MERRA-2 (直接出极端天气):
        from weather_loaders import load_stations, build_from_raw_era5land
        from registry import simple_signals
        st = load_stations("/data1/luobaozhen/extreme_signals/inputs/meta_data.csv",
                           id_col="ID", lat_col="lat", lon_col="lon")
        weather, time, lat, lon = build_from_raw_era5land(st, 2024)
        masks = simple_signals("solar", weather)        # 各事件 (T,K) bool

------------------------------------------------------------------------------
数据源路径 (Spark02)
------------------------------------------------------------------------------
  ERA5-Land 全球月文件: /data1/luobaozhen/era5_download/ERA5_land/global/{var}/{var}_{YYYY}_{MM}.nc
      var: u10 v10 t2m tp ssrd d2m   (lon 0-360, lat 90..-90 0.1°, 时间坐标 valid_time)
  MERRA-2 沙尘:        /data3/luobaozhen/MERRA2/M2T1NXAER/DUEXTTAU/{YYYY}/{MM}/MERRA2.tavg1_2d_aer_Nx.{YYYYMMDD}.nc4
      变量 DUEXTTAU, 逐小时(24步/日), lon -180..180, 0.5°x0.625°

权威脚本(已验证, 直接复用，勿重写):
  ERA5-Land 原始->站点(含 tp/ssrd 去累积): /data1/luobaozhen/extreme_signals/extreme_signals_pipeline/pipeline/extract.py
                                            + utils/era5_io.py (accum_to_hourly_amount: hour==1 取原值,其余逐时差分)
  6 协变量组装(含 RH/沙尘):                /data1/luobaozhen/extreme_signals/extreme_signals_pipeline/build_covariates.py
  配置(站点表/年份/路径):                  config_china_trainset.yaml
"""
from __future__ import annotations
import glob
import numpy as np
import pandas as pd
import xarray as xr

DUST_DIR = "/data3/luobaozhen/MERRA2/M2T1NXAER/DUEXTTAU"
ERA5_ROOT = "/data1/luobaozhen/era5_download/ERA5_land/global"   # {prefix}/{prefix}_{YYYY}_{MM}.nc
ERA5_PREFIX_ROOTS = {
    "d2m": "/data1/luobaozhen/global_wind_pv/era5_download/ERA5_land/global/d2m",
}
# 逻辑变量名 -> 盘上"文件夹/文件前缀"。露点统一为 d2m；NetCDF 内部变量名也是 d2m。
ERA5_VARS = {"u10": "u10", "v10": "v10", "t2m": "t2m", "tp": "tp", "ssrd": "ssrd", "d2m": "d2m"}
ACCUM_VARS = {"tp", "ssrd"}
TP_M_TO_MM = 1000.0          # tp: m -> mm
SSRD_J_TO_W = 1.0 / 3600.0   # ssrd: J/m^2 -> W/m^2 (1h)
SOLAR_MAX = 1361.0   # 辐照上限(W/m^2); 超过判为 ssrd 去累积伪峰, 置 0

# weather 字典需要的键(事件判定输入)
WEATHER_KEYS = ["temp_C", "wind_ms", "precip_mmh", "irradiance_wm2", "rh_pct", "dust_aod"]


def era5_file(prefix, year, month):
    """Return the ERA5-Land monthly file path for a variable prefix."""
    root = ERA5_PREFIX_ROOTS.get(prefix, f"{ERA5_ROOT}/{prefix}")
    return f"{root}/{prefix}_{year}_{month:02d}.nc"


def magnus_rh(t2m_k, d2m_k):
    """2m 相对湿度(%) = 100*e(Td)/es(T), Magnus 公式; 输入开尔文。"""
    t = t2m_k - 273.15; td = d2m_k - 273.15
    e = 6.112 * np.exp((17.67 * td) / (td + 243.5))
    es = 6.112 * np.exp((17.67 * t) / (t + 243.5))
    return np.clip(100.0 * e / es, 0.0, 100.0).astype(np.float32)


# ---- 经度统一约定 ----------------------------------------------------------
# 站点经度内部统一为 **canonical [-180, 180)** (本模块对外的固定范围)。
# 采样时各数据源再各自转换: ERA5-Land 网格是 0..360 用 to_360; MERRA-2 是 -180..180 用 to_180。
def to_180(lon):
    """经度 -> [-180, 180)。"""
    return ((np.asarray(lon, float) + 180.0) % 360.0) - 180.0


def to_360(lon):
    """经度 -> [0, 360)。"""
    return np.mod(np.asarray(lon, float), 360.0)


# ---- 场站信息输入 ----------------------------------------------------------
def load_stations(path, id_col="station_id", lat_col="lat", lon_col="lon",
                  tech=None, tech_col=None):
    """读场站信息文件 -> DataFrame[station_id, lat, lon] (lon 已统一到 [-180,180))。
    支持 .csv 与 .gpkg。可用 tech/tech_col 按技术筛选(如 type 列)。

    常用场站文件 (Spark02 / LM1):
      - /data1/luobaozhen/extreme_signals/inputs/meta_data.csv   (流水线站表; 列 ID,lon,lat)
      - /data7/luobaozhen/global_wind_pv/data/RSW/{wind,solar}_all_2024q2_final.gpkg
            (全量电站; 列 id,lon,lat,construction_year,COUNTRY)
      - /data6/luobaozhen/china_grid_task1/metadata/china_sample_{wind,solar}_n10000_seed20260603.csv
      - /data6/luobaozhen/us_grid_task4/metadata/us_sample_{wind,solar}.csv
    """
    if str(path).endswith(".gpkg"):
        import geopandas as gpd
        df = gpd.read_file(path)
    else:
        df = pd.read_csv(path)
    df.columns = [str(c).lstrip("﻿") for c in df.columns]
    # 列名兜底: id 可能叫 id/ID/station_id; 经纬度可能叫 lon/longitude, lat/latitude
    def pick(want, cands):
        for c in [want] + cands:
            if c in df.columns:
                return c
        raise KeyError(f"场站文件缺少列 {want}; 现有列: {list(df.columns)}")
    ic = pick(id_col, ["id", "ID", "station_id", "name"])
    lac = pick(lat_col, ["lat", "latitude", "Lat"])
    loc = pick(lon_col, ["lon", "longitude", "Lon", "long"])
    if tech and tech_col and tech_col in df.columns:
        df = df[df[tech_col].astype(str) == tech]
    out = pd.DataFrame({
        "station_id": df[ic].astype(str).to_numpy(),
        "lat": pd.to_numeric(df[lac], errors="coerce").to_numpy(float),
        "lon": to_180(pd.to_numeric(df[loc], errors="coerce").to_numpy(float)),
    }).dropna(subset=["lat", "lon"]).reset_index(drop=True)
    return out


# ---- 入口 A: 读现成 covariates_{year}.nc ------------------------------------
def load_covariates(nc_path):
    """读本项目 covariates_*.nc -> (weather, time, lat, lon)。
    weather 含 temp_C/wind_ms/precip_mmh/irradiance_wm2/rh_pct/dust_aod (T,K)。"""
    d = xr.open_dataset(nc_path)
    weather = {k: d[k].values.astype(np.float32) for k in WEATHER_KEYS if k in d.data_vars}
    time = pd.DatetimeIndex(pd.to_datetime(d["time"].values))
    lat = np.asarray(d["lat"].values, float); lon = to_180(d["lon"].values)  # 统一 [-180,180)
    d.close()
    return weather, time, lat, lon


# ---- 入口 B: 从 ERA5-Land 站点抽取 extracted/ 组装 (含 MERRA-2 沙尘) --------
def _load_ext(root, var, year):
    d = xr.open_dataset(f"{root}/extracted/{var}/{var}_{year}.nc")
    arr = d[var].values.astype(np.float32)
    time = pd.to_datetime(d["time"].values)
    sid = np.array(d["station_id"].values); lat = np.array(d["lat"].values, float); lon = to_180(d["lon"].values)
    d.close()
    return arr, time, sid, lat, lon


def build_from_extracted(root, year, with_dust=True):
    """从 ERA5-Land 站点抽取(extract.py 产物)组装 weather; tp/ssrd 须已去累积。
    返回 (weather, time, sid, lat, lon)。"""
    t2m, time, sid, lat, lon = _load_ext(root, "t2m", year)
    u10, *_ = _load_ext(root, "u10", year)
    v10, *_ = _load_ext(root, "v10", year)
    tp, *_ = _load_ext(root, "tp", year)
    ssrd, *_ = _load_ext(root, "ssrd", year)
    d2m, *_ = _load_ext(root, "d2m", year)
    weather = {
        "temp_C": (t2m - 273.15).astype(np.float32),
        "wind_ms": np.sqrt(u10 * u10 + v10 * v10).astype(np.float32),
        "precip_mmh": tp.astype(np.float32),
        "irradiance_wm2": np.where(ssrd > SOLAR_MAX, 0.0, ssrd).astype(np.float32),
        "rh_pct": magnus_rh(t2m, d2m),
    }
    if with_dust:
        weather["dust_aod"] = sample_dust(year, lat, lon, time)
    return weather, time, sid, lat, lon


# ---- 入口 C: 直接从原始 ERA5-Land 全球月文件抽取 (含去累积) + MERRA-2 沙尘 ----
def _deaccum(accum, time_index):
    """ERA5-Land 累积量去累积为逐小时增量 (复刻 era5_io.accum_to_hourly_amount):
    hour==1(00UTC后第1步)取原值; 其余逐时差分; 首步/NaN->0; 负跳变clip到0。"""
    accum = accum.astype(np.float32)
    inc = np.empty_like(accum)
    inc[0] = np.nan
    if accum.shape[0] > 1:
        inc[1:] = accum[1:] - accum[:-1]
    reset = np.asarray(pd.DatetimeIndex(time_index).hour) == 1
    inc[reset] = accum[reset]
    inc = np.where(np.isfinite(inc), inc, 0.0)
    return np.maximum(inc, 0.0)


def _align_time(arr, t, canon):
    """把 (T,K) 按时间 t 对齐到规范时间轴 canon; 缺失时刻 -> NaN。"""
    out = np.full((len(canon), arr.shape[1]), np.nan, np.float32)
    pos = pd.DatetimeIndex(canon).get_indexer(pd.DatetimeIndex(t))
    ok = pos >= 0
    out[pos[ok]] = arr[ok]
    return out


def _sample_era5_var(prefix, year, slat, slon360, bbox):
    """读某变量 12 个月文件, 在 bbox 内最近邻采样到站点。返回 (arr(T,K), time)。
    prefix=盘上文件夹/文件前缀; bbox=(lat_n, lat_s, lon_w, lon_e) (lat 降序; lon 0-360)。
    缺月直接跳过(时间轴留洞), 由上层 _align_time 对齐到规范轴并填 NaN。"""
    import glob as _g
    pieces = []; times = []
    for m in range(1, 13):
        fp = era5_file(prefix, year, m)
        fs = _g.glob(fp)
        if not fs:
            continue
        d = xr.open_dataset(fs[0])
        latn = "latitude" if "latitude" in d.coords else "lat"
        lonn = "longitude" if "longitude" in d.coords else "lon"
        tn = "valid_time" if "valid_time" in d.coords else "time"
        # 文件名前缀(如 d2t)可能与内部变量名(如 d2m)不同 -> 取文件里唯一的数据变量
        vname = prefix if prefix in d.data_vars else list(d.data_vars)[0]
        sub = d[vname].sel({latn: slice(bbox[0], bbox[1]), lonn: slice(bbox[2], bbox[3])}).load()
        gla = sub[latn].values; glo = sub[lonn].values
        tt = pd.to_datetime(sub[tn].values)
        arr = sub.values.astype(np.float32)            # (Tm, nlat, nlon)
        ila = np.abs(gla[None, :] - np.asarray(slat)[:, None]).argmin(axis=1)
        ilo = np.abs(glo[None, :] - np.asarray(slon360)[:, None]).argmin(axis=1)
        pieces.append(arr[:, ila, ilo]); times.append(tt)
        d.close()
    if not pieces:
        raise FileNotFoundError(f"no ERA5-Land {prefix} files for {year} under {ERA5_ROOT}")
    return np.concatenate(pieces, 0), pd.DatetimeIndex(np.concatenate([t.values for t in times]))


def build_from_raw_era5land(stations, year, bbox=None, with_dust=True):
    """★ 直接从原始 ERA5-Land 全球月文件 + MERRA-2 沙尘 出 weather 字典。
    stations: DataFrame 或 dict, 需含 'lat','lon' (lon 任意制式, 内部转0-360); 行序=站序。
    bbox: (lat_n, lat_s, lon_w, lon_e); None 则按站点范围自动(留0.5°余量)。
    返回 (weather, time, lat, lon)。tp/ssrd 自动去累积+单位换算; 不插补。"""
    lat = np.asarray(stations["lat"], float)
    lon = to_180(stations["lon"])                 # 统一到 canonical [-180,180)
    lon360 = to_360(lon)                            # ERA5-Land 网格用 0..360
    if bbox is None:
        pad = 0.5
        bbox = (min(lat.max() + pad, 90), max(lat.min() - pad, -90),
                max(lon360.min() - pad, 0), min(lon360.max() + pad, 360))
    canon = pd.date_range(f"{year}-01-01 00:00", f"{year}-12-31 23:00", freq="h")
    raw = {}
    for key, prefix in ERA5_VARS.items():
        arr, t = _sample_era5_var(prefix, year, lat, lon360, bbox)
        if key in ACCUM_VARS:
            arr = _deaccum(arr, t)                  # 去累积须在原生连续序列上做
            arr = arr * (TP_M_TO_MM if key == "tp" else SSRD_J_TO_W)
        raw[key] = _align_time(arr, t, canon)       # 各变量统一到规范小时轴(缺->NaN)
    ref_time = canon
    weather = {
        "temp_C": (raw["t2m"] - 273.15).astype(np.float32),
        "wind_ms": np.sqrt(raw["u10"]**2 + raw["v10"]**2).astype(np.float32),
        "precip_mmh": raw["tp"].astype(np.float32),
        "irradiance_wm2": np.where(raw["ssrd"] > SOLAR_MAX, 0.0, raw["ssrd"]).astype(np.float32),
        "rh_pct": magnus_rh(raw["t2m"], raw["d2m"]),
    }
    if with_dust:
        weather["dust_aod"] = sample_dust(year, lat, lon, ref_time)
    return weather, ref_time, lat, lon


# ---- MERRA-2 沙尘最近邻采样 -------------------------------------------------
def sample_dust(year, lat, lon, time):
    """DUEXTTAU 最近邻采样到 (lat,lon), 对齐到整点小时。返回 (T,K)。"""
    files = sorted(glob.glob(f"{DUST_DIR}/{year}/*/MERRA2.tavg1_2d_aer_Nx.{year}*.nc4"))
    if not files:
        raise FileNotFoundError(f"no MERRA-2 dust for {year} under {DUST_DIR}")
    d0 = xr.open_dataset(files[0]); glat = d0["lat"].values; glon = d0["lon"].values; d0.close()
    lon180 = to_180(lon)                              # MERRA-2 网格是 -180..180
    ilat = np.abs(glat[None, :] - np.asarray(lat)[:, None]).argmin(axis=1)
    ilon = np.abs(glon[None, :] - np.asarray(lon180)[:, None]).argmin(axis=1)
    time = pd.DatetimeIndex(time); S, T = len(lat), len(time)
    by_hour = {}
    for f in files:
        d = xr.open_dataset(f); a = d["DUEXTTAU"].values
        tt = pd.to_datetime(d["time"].values).floor("h")
        samp = a[:, ilat, ilon].astype(np.float32); d.close()
        for i, ts in enumerate(tt):
            by_hour[ts] = samp[i]
    dust = np.full((T, S), np.nan, np.float32)
    for i, ts in enumerate(time):
        v = by_hour.get(ts)
        if v is not None:
            dust[i] = v
    return dust
