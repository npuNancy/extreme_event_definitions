"""生成场站极端天气信号 nc —— 直接用 extreme_event_definitions.py 的定义。

输入: extract_station_weather_nc.py 产出的 weather nc(已抽好的 2020-2024, 含 6 变量)
      + meta(含 country, type)。
事件定义全部来自本目录 extreme_event_definitions 包(registry + events), 不重写阈值。

  简单阈值事件: 从 weather nc 直接算(registry.simple_signals)
  低资源 low_resource: **不能只用 2020-2024**, 需基线 clim288+每站P5。
      由 compute_lowres_baseline.py 预先抽 1987-2016 资源算出 clim/thr(存 npz),
      本脚本载入后套到 nc 资源上。无 baseline_npz 时跳过低资源(给提示)。

输出: 每 (tech, region) 一个 nc, 每事件一个 bool 变量, dims=(time, station),
      station_id 索引 + name/lat/lon/country 坐标。region: china / foreign。

用法:
  python generate_extreme_signals.py --weather_nc weather_nc/all.nc --meta meta_outcountry.csv \
         --tech wind --region china --out_dir extreme_signals_out \
         [--baseline_npz lowres_baseline_wind.npz]
"""
from __future__ import annotations
import argparse
import os
import sys
import numpy as np
import pandas as pd
import xarray as xr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import registry
from events import wind_low_resource, solar_low_resource
import common

RESOURCE_KEY = {"wind": "wind_ms", "solar": "rsds"}   # 低资源资源变量(nc 中的名字)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weather_nc", required=True)
    ap.add_argument("--meta", required=True, help="含 country, type 的 meta")
    ap.add_argument("--tech", required=True, choices=["wind", "solar"])
    ap.add_argument("--region", required=True, choices=["china", "foreign", "all"])
    ap.add_argument("--self_baseline", type=int, default=0, help="1: 低资源用本nc期(如2年)自算clim/thr")
    ap.add_argument("--out_dir", required=True)
    ap.add_argument("--baseline_npz", default="", help="低资源基线(clim_tbl/thr/station_id); 缺则跳过低资源")
    a = ap.parse_args()

    meta = pd.read_csv(a.meta); meta.columns = [str(c).lstrip("﻿") for c in meta.columns]
    idc = next(c for c in ("station_id", "ID", "id") if c in meta.columns)
    is_cn = meta["country"].astype(str) == "China"
    sub = (meta[meta["type"].astype(str)==a.tech] if a.region=="all" else meta[(meta["type"].astype(str)==a.tech) & (is_cn if a.region=="china" else ~is_cn)])
    ids = sub[idc].astype(str).to_numpy()
    if len(ids) == 0:
        print(f"[skip] no {a.tech}/{a.region} stations"); return
    print(f"[{a.tech}/{a.region}] {len(ids)} stations", flush=True)

    ds = xr.open_dataset(a.weather_nc).reindex(station=ids)   # 按该集合选站
    time = pd.DatetimeIndex(pd.to_datetime(ds["time"].values))
    lat = np.asarray(ds["lat"].values, float); lon = np.asarray(ds["lon"].values, float)
    name = np.asarray(ds["name"].values) if "name" in ds.coords else ids
    weather = {k: ds[k].values.astype(np.float32) for k in
               ["temp_C", "wind_ms", "precip_mmh", "rh_pct", "dust_aod"] if k in ds.data_vars}

    # 1) 简单阈值事件(extreme_event_definitions 定义)
    masks = registry.simple_signals(a.tech, weather)

    # 2) 低资源(需基线)
    if a.baseline_npz and os.path.exists(a.baseline_npz):
        bl = np.load(a.baseline_npz, allow_pickle=True)
        bsid = np.array([str(s) for s in bl["station_id"]])
        pos = pd.Index(bsid).get_indexer(ids)          # 对齐基线到当前站序
        if (pos < 0).any():
            print(f"[warn] {int((pos<0).sum())} 站无基线, 低资源该站置 False")
        clim = bl["clim_tbl"]; thr = bl["thr"]
        clim_a = np.where(pos[None, None, :] >= 0, clim[:, :, np.clip(pos, 0, clim.shape[2]-1)], np.nan)
        thr_a = np.where(pos >= 0, thr[np.clip(pos, 0, len(thr)-1)], np.nan)
        res = ds[RESOURCE_KEY[a.tech]].values.astype(np.float32)
        if a.tech == "solar":
            lr = solar_low_resource.signal(res, time, lat, lon, clim_tbl=clim_a, thr=thr_a)
        else:
            lr = wind_low_resource.signal(res, time, clim_tbl=clim_a, thr=thr_a)
        masks["low_resource"] = lr
        print(f"  low_resource frac {float(lr.mean()):.4f}")
    elif a.self_baseline:
        res = ds[RESOURCE_KEY[a.tech]].values.astype(np.float32)
        lr = solar_low_resource.signal(res, time, lat, lon) if a.tech=="solar" else wind_low_resource.signal(res, time)
        masks["low_resource"] = lr
        print(f"  low_resource(self-baseline) frac {float(lr.mean()):.4f}")
    else:
        print("[warn] 无 baseline_npz -> 跳过 low_resource")

    # 3) 写 nc(每事件一个 int8 bool 变量)
    os.makedirs(a.out_dir, exist_ok=True)
    out = xr.Dataset(
        {ev: (("time", "station"), m.astype(np.int8)) for ev, m in masks.items()},
        coords={"time": ds["time"].values, "station": ids,
                "name": ("station", name), "lat": ("station", lat), "lon": ("station", lon),
                "country": ("station", sub["country"].astype(str).to_numpy())},
    )
    out["station"].attrs["long_name"] = "station_id"
    out.attrs.update(source="extreme_event_definitions.py", tech=a.tech, region=a.region,
                     events=",".join(masks.keys()))
    enc = {ev: {"zlib": True, "complevel": 4, "dtype": "int8"} for ev in masks}
    fp = os.path.join(a.out_dir, f"extreme_signals_{a.tech}_{a.region}.nc")
    out.to_netcdf(fp, encoding=enc)
    ds.close()
    print(f"[ok] wrote {fp}  events={list(masks.keys())}", flush=True)


if __name__ == "__main__":
    main()
