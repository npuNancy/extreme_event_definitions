"""场站到网格的匹配工具（Pipeline A 与 Pipeline B 共用）。

本模块移植了
``ref_code/calculate_wind_solar_out/station_output_calculator_0p1deg.py`` 中
已验证的场站/网格匹配逻辑，并适配到本项目的多数据源适配器层。

职责
----
- 经度归一化。**经度约定并不统一**：场站输入为 ``[-180, 180)``；BCSD 气象网格
  的经度约定随区域变化（例如 Germany 存为 ``5..15``，Portugal 存为
  ``328.7..353.7``，即 ``[0, 360)``）。因此匹配前会按文件把每个网格经度归一化
  到 ``[-180, 180)``。
- 最近网格查找：规则经纬度网格使用可分离的一维 argmin（经度为环形距离），
  旋转极点 NAM-12 网格使用二维 cKDTree。
- 通过 Natural Earth 国家边界做场站归属（点在多边形内），并以
  ``activation_year = min(year)`` 去重场站。
- 将 ``(time, *spatial)`` 数组向量化抽取为 ``(time, n_stations)``。

两个 pipeline 都只导入这些基础函数；pipeline 之间不互相调用，以保持解耦。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from scipy.spatial import cKDTree
    _HAS_SCIPY = True
except ImportError:  # pragma: no cover
    _HAS_SCIPY = False

import shapefile  # pyshp
from shapely.geometry import Point, shape
from shapely.prepared import prep


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 默认最近邻距离容差（单位：度）。
#: BCSD 通常约为 0（场站和网格都是 0.1° 整数偏移）；China/NAM-12 通常约为
#: 0.05–0.11。0.15° 可以覆盖两者，同时不会误拒绝有效的网格内场站。
MAX_DIST_DEG: float = 0.15

#: 场站 CSV 文件名 → SSP 情景代码。
SSP_MAP = {
    "SSP1-2.6": "ssp126",
    "SSP2-4.5": "ssp245",
    "SSP5-6.0": "ssp585",
    "SSP5-8.5": "ssp585",
}

#: 与 Natural Earth ``NAME`` 字段不同的 BCSD 区域目录名。
BCSD_REGION_TO_NAME = {
    "South-Africa": "South Africa",
    "South-Korea": "South Korea",
    "United-Kingdom": "United Kingdom",
    "México": "Mexico",
}


# ---------------------------------------------------------------------------
# 经度归一化
# ---------------------------------------------------------------------------

def lon_to_360(lon):
    """经度 ``[-180, 180]`` → ``[0, 360)``。"""
    return np.asarray(lon, dtype=np.float64) % 360.0


def lon_to_180(lon):
    """经度 ``[0, 360]`` → ``[-180, 180)``。"""
    return ((np.asarray(lon, dtype=np.float64) + 180.0) % 360.0) - 180.0


def is_lon_360(grid_lon) -> bool:
    """若网格经度轴看起来像 ``[0, 360)``，返回 True。

    判断规则：任意值 > 180 即认为约定为 ``[0, 360)``。真实经度全为正的区域
    （如 Germany 5..15）存在歧义，但归一化对它是 no-op；真实经度含负值的区域
    （如 Portugal、Ireland、Chile）在 ``[0, 360)`` 中会存为 > 180，因此可被正确识别。
    """
    arr = np.asarray(grid_lon, dtype=np.float64)
    return bool(arr.size and float(np.nanmax(arr)) > 180.0)


def normalize_grid_lon(grid_lon) -> np.ndarray:
    """将网格经度轴归一化到 ``[-180, 180)``（按文件检测）。

    若输入已经是 ``[-180, 180)`` 则不变；若检测到 ``[0, 360)``（任意值 > 180），
    则转换到 ``[-180, 180)``。
    """
    arr = np.asarray(grid_lon, dtype=np.float64)
    return lon_to_180(arr) if is_lon_360(arr) else arr


# ---------------------------------------------------------------------------
# 最近网格查找
# ---------------------------------------------------------------------------

def nearest_index_regular(grid_lat, grid_lon_180, sta_lat, sta_lon_180):
    """规则一维经纬度网格上的最近网格单元。

    经度使用环形距离，因此可以正确处理 ±180° 接缝附近的区域（如 Spain、Ireland、Alaska）。

    参数
    ----
    grid_lat, grid_lon_180 : (n_lat,) (n_lon,)  grid coordinates; longitude
        网格坐标；经度已归一化到 ``[-180, 180)``。
    sta_lat, sta_lon_180 : (n_sta,)  场站坐标（经度为 ``[-180, 180)``）。

    返回
    ----
    lat_idx, lon_idx : (n_sta,) int64  一维网格轴索引。
    dist : (n_sta,) float64  最近邻距离（单位：度），即
        ``max(|dlat|, |dlon_circular|)``。
    """
    grid_lat = np.asarray(grid_lat, dtype=np.float64)
    grid_lon = np.asarray(grid_lon_180, dtype=np.float64)
    sta_lat = np.asarray(sta_lat, dtype=np.float64)
    sta_lon = np.asarray(sta_lon_180, dtype=np.float64)
    n_sta = sta_lat.shape[0]

    lat_idx = np.empty(n_sta, dtype=np.int64)
    lon_idx = np.empty(n_sta, dtype=np.int64)
    dlat = np.empty(n_sta, dtype=np.float64)
    dlon = np.empty(n_sta, dtype=np.float64)

    for i in range(n_sta):
        d_lat = np.abs(grid_lat - sta_lat[i])
        j_lat = int(np.argmin(d_lat))
        lat_idx[i] = j_lat
        dlat[i] = d_lat[j_lat]

        d_lon = np.abs(((grid_lon - sta_lon[i] + 180.0) % 360.0) - 180.0)
        j_lon = int(np.argmin(d_lon))
        lon_idx[i] = j_lon
        dlon[i] = d_lon[j_lon]

    dist = np.maximum(dlat, dlon)
    return lat_idx, lon_idx, dist


def nearest_index_2d(lat2d, lon2d_180, sta_lat, sta_lon_180):
    """旋转极点二维网格（NAM-12）上的最近网格单元。

    将二维地理辅助坐标 ``lat``/``lon`` 展平后查询 cKDTree；如果 scipy 不可用，
    则退回到暴力 argmin。

    返回
    ----
    idx0, idx1 : (n_sta,) int64  指向 (rlat, rlon) 网格的索引。
    dist : (n_sta,) float64  平面距离（单位：度）。
    """
    lat2d = np.asarray(lat2d, dtype=np.float64)
    lon2d = np.asarray(lon2d_180, dtype=np.float64)
    n0, n1 = lat2d.shape
    lat_flat = lat2d.ravel()
    lon_flat = lon2d.ravel()
    pts = np.column_stack([lon_flat, lat_flat])
    queries = np.column_stack([np.asarray(sta_lon_180, dtype=np.float64),
                               np.asarray(sta_lat, dtype=np.float64)])

    if _HAS_SCIPY:
        tree = cKDTree(pts)
        dist, flat_idx = tree.query(queries, k=1)
        flat_idx = np.asarray(flat_idx, dtype=np.int64)
    else:  # pragma: no cover
        flat_idx = np.empty(len(queries), dtype=np.int64)
        dist = np.empty(len(queries), dtype=np.float64)
        for i, q in enumerate(queries):
            d = (lon_flat - q[0]) ** 2 + (lat_flat - q[1]) ** 2
            j = int(np.argmin(d))
            flat_idx[i] = j
            dist[i] = np.sqrt(d[j])

    return flat_idx // n1, flat_idx % n1, np.asarray(dist, dtype=np.float64)


# ---------------------------------------------------------------------------
# 国家边界与场站筛选
# ---------------------------------------------------------------------------

def load_country_shapes(shp_path):
    """读取 Natural Earth admin-0 国家边界 shapefile → ``{NAME: geometry}``。

    使用 pyshp（``shapefile``）+ shapely，与已验证的参考实现一致。
    """
    sf = shapefile.Reader(shp_path)
    fields = [f[0] for f in sf.fields[1:]]
    name_idx = fields.index("NAME")
    countries = {}
    for i, rec in enumerate(sf.records()):
        name = rec[name_idx]
        countries[name] = shape(sf.shape(i).__geo_interface__)
    return countries


def bcsd_region_to_ne_name(region_dir: str) -> str:
    """将 BCSD 区域目录名映射到 Natural Earth ``NAME``。"""
    return BCSD_REGION_TO_NAME.get(region_dir, region_dir)


def infer_scenario_from_csv(csv_path: str) -> str:
    """从场站 CSV 文件名推断 SSP 情景代码（``ssp126``/…）。"""
    basename = os.path.basename(csv_path)
    for ssp_name, ssp_code in SSP_MAP.items():
        if ssp_name in basename:
            return ssp_code
    raise ValueError(
        f"无法从文件名 '{basename}' 推断 SSP 情景，支持: {list(SSP_MAP.keys())}"
    )


def load_stations(csv_path) -> pd.DataFrame:
    """读取场站选址 CSV。

    期望列：``year, type, lon, lat, capacity_gw``。
    返回的 DataFrame 中 ``lon`` 已归一化到 ``[-180, 180)``，并转换为数值类型。
    缺少 lon/lat 的行会被删除。
    """
    df = pd.read_csv(csv_path)
    df["lon"] = lon_to_180(pd.to_numeric(df["lon"], errors="coerce"))
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")
    df["capacity_gw"] = pd.to_numeric(df["capacity_gw"], errors="coerce")
    df = df.dropna(subset=["lon", "lat", "year"]).reset_index(drop=True)
    return df


def filter_stations_for_country(stations_df: pd.DataFrame, country_geom,
                                stype: str) -> pd.DataFrame:
    """选择 ``country_geom`` 内类型为 ``stype`` 的场站，并去重。

    针对 2030/2040/2050 的嵌套关系，去重时保留唯一 ``(lon, lat)``，并取
    ``activation_year = min(year)``（较早投产的场站在后续年份也存在）。

    返回列：``lon, lat, type, activation_year, capacity_gw``。
    """
    if stations_df.empty:
        return stations_df.assign(activation_year=pd.Series(dtype="int64"))
    df_typed = stations_df[stations_df["type"] == stype].copy()
    if df_typed.empty:
        return df_typed.assign(activation_year=pd.Series(dtype="int64"))

    prepared = prep(country_geom)
    keep = np.fromiter(
        (prepared.contains(Point(float(row.lon), float(row.lat)))
         for row in df_typed.itertuples()),
        dtype=bool, count=len(df_typed),
    )
    result = df_typed[keep].copy()
    if result.empty:
        return result.assign(activation_year=pd.Series(dtype="int64"))

    result = (
        result.sort_values("year")
        .groupby(["lon", "lat"], as_index=False)
        .agg({"year": "min", "type": "first", "capacity_gw": "first"})
        .rename(columns={"year": "activation_year"})
        .reset_index(drop=True)
    )
    return result


# ---------------------------------------------------------------------------
# 匹配结果与抽取
# ---------------------------------------------------------------------------

@dataclass
class StationMatch:
    """一组场站匹配到一个网格后的结果。

    属性
    ----
    stations : DataFrame  筛选后的场站（lon/lat 为 [-180,180)，含 type、
        activation_year、capacity_gw）。
    idx0 : (n_sta,) int64  规则网格的 lat 索引，或旋转网格的 rlat 索引。
    idx1 : (n_sta,) int64  规则网格的 lon 索引，或旋转网格的 rlon 索引。
    dist_deg : (n_sta,) float64  最近网格距离。
    valid : (n_sta,) bool  ``dist_deg <= max_dist``。
    grid_kind : str  ``"regular_latlon"`` 或 ``"rotated_pole"``。
    """

    stations: pd.DataFrame
    idx0: np.ndarray
    idx1: np.ndarray
    dist_deg: np.ndarray
    valid: np.ndarray
    grid_kind: str

    def __len__(self) -> int:
        return len(self.stations)


def match_regular(grid_lat, grid_lon, stations_df: pd.DataFrame,
                  max_dist: float = MAX_DIST_DEG) -> StationMatch:
    """将场站匹配到规则经纬度网格（按文件做经度归一化）。"""
    grid_lon_180 = normalize_grid_lon(grid_lon)
    lat_idx, lon_idx, dist = nearest_index_regular(
        grid_lat, grid_lon_180,
        stations_df["lat"].to_numpy(np.float64),
        stations_df["lon"].to_numpy(np.float64),
    )
    return StationMatch(
        stations=stations_df.reset_index(drop=True),
        idx0=lat_idx, idx1=lon_idx, dist_deg=dist,
        valid=dist <= max_dist, grid_kind="regular_latlon",
    )


def match_2d(lat2d, lon2d, stations_df: pd.DataFrame,
             max_dist: float = MAX_DIST_DEG) -> StationMatch:
    """将场站匹配到旋转极点二维网格（NAM-12）。"""
    lon2d_180 = normalize_grid_lon(np.asarray(lon2d))
    idx0, idx1, dist = nearest_index_2d(
        lat2d, lon2d_180,
        stations_df["lat"].to_numpy(np.float64),
        stations_df["lon"].to_numpy(np.float64),
    )
    return StationMatch(
        stations=stations_df.reset_index(drop=True),
        idx0=idx0, idx1=idx1, dist_deg=dist,
        valid=dist <= max_dist, grid_kind="rotated_pole",
    )


def gather_to_stations(arr3d, match: StationMatch) -> np.ndarray:
    """将 ``(time, idx0, idx1)`` 数组抽取为 ``(time, n_sta)``。

    使用高级索引抽取已匹配网格单元。``valid=False``（``dist > max_dist``）的场站
    仍会被抽取，但应在后续流程中被掩膜。
    """
    arr3d = np.asarray(arr3d)
    return arr3d[:, match.idx0, match.idx1]


# ---------------------------------------------------------------------------
# 场站级信号写出（Pipeline A 与 Pipeline B 共用）
# ---------------------------------------------------------------------------

def write_station_signals(
    out_path,
    masks: dict,
    times: np.ndarray,
    match: "StationMatch",
    tech: str,
    *,
    source: str,
    model: str,
    region: str,
    scenario: str,
    source_csv: str,
    pipeline: str,
    supported,
    skipped,
    skipped_reasons: dict,
    max_dist: float,
    activation_mask_on: bool,
    compress_level: int = 4,
) -> None:
    """写出场站级极端天气信号 NetCDF。

    布局：维度为 ``(time, station)``；每个事件对应一个
    ``signal_<event>(time, station)`` ``int8`` 变量，并包含场站元数据
    （lon/lat/type/capacity_gw/activation_year/match_dist_deg）。两个 pipeline
    写出的文件结构一致，因此输出可以直接比较。
    """
    import xarray as xr  # local import: xarray only needed for writing

    p = Path(out_path) if not isinstance(out_path, Path) else out_path
    p.parent.mkdir(parents=True, exist_ok=True)
    n_sta = len(match)
    sta = match.stations

    data_vars = {}
    for name, arr in masks.items():
        ev = name[len("signal_"):] if name.startswith("signal_") else name
        data_vars[name] = xr.DataArray(
            np.asarray(arr, dtype=np.int8), dims=("time", "station"),
            attrs={"flag_values": "0, 1", "flag_meanings": "false true",
                   "long_name": f"Extreme weather signal: {ev}"})
    data_vars["station_lon"] = xr.DataArray(sta["lon"].to_numpy(np.float32), dims=("station",))
    data_vars["station_lat"] = xr.DataArray(sta["lat"].to_numpy(np.float32), dims=("station",))
    data_vars["station_type"] = xr.DataArray(
        np.full(n_sta, 1 if tech == "wind" else 0, dtype=np.int8), dims=("station",),
        attrs={"flag_values": "0, 1", "flag_meanings": "solar wind"})
    data_vars["capacity_gw"] = xr.DataArray(sta["capacity_gw"].to_numpy(np.float32), dims=("station",))
    data_vars["activation_year"] = xr.DataArray(sta["activation_year"].to_numpy(np.int16), dims=("station",))
    data_vars["match_dist_deg"] = xr.DataArray(match.dist_deg.astype(np.float32), dims=("station",))

    ds = xr.Dataset(data_vars, coords={
        "time": times, "station": np.arange(n_sta, dtype=np.int32)})
    ds.attrs.update({
        "source": source, "model": model, "region": region, "scenario": scenario,
        "source_csv": source_csv, "pipeline": pipeline,
        "grid_resolution": "0.1deg", "match_method": "nearest",
        "max_match_dist_deg": str(max_dist),
        "activation_mask": "on" if activation_mask_on else "off",
        "supported_events": ",".join(supported),
        "skipped_events": ",".join(skipped),
        "skipped_event_reasons": "; ".join(f"{k}: {v}" for k, v in skipped_reasons.items()),
        "n_stations": str(n_sta),
        "threshold_source": "extreme_event_definitions/events",
    })
    encoding = {name: {"zlib": True, "complevel": compress_level, "dtype": "int8"}
                for name in masks}
    ds.to_netcdf(str(p), encoding=encoding)
    ds.close()
