"""把合并 weather nc 按技术(wind/solar)拆成各自 station_id 唯一的 nc。

背景: meta 里有风光同场电站共用 station_id(跨技术重复), 合并 nc 的 station 索引因此有重复,
无法用 station_id reindex。按技术拆开后, 每个 tech 内 station_id 唯一 -> 全程可用 station_id。

拆分依据: 合并 nc 列序 = meta 行序(extract 时按 meta 抽), 故按 meta.type 取列;
拆完后**用 station_id + 经纬度对账**确认正确(不盲信位置)。

用法:
  python split_weather_by_tech.py --weather_nc all.nc --meta meta.csv --out_dir weather_nc
"""
from __future__ import annotations
import argparse
import logging
import os
import sys
import numpy as np
import pandas as pd
import xarray as xr

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.logging_utils import setup_logging

logger = logging.getLogger(__name__)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weather_nc", required=True)
    ap.add_argument("--meta", required=True)
    ap.add_argument("--out_dir", required=True)
    a = ap.parse_args()
    setup_logging("split_weather_by_tech")

    meta = pd.read_csv(a.meta); meta.columns = [str(c).lstrip("﻿") for c in meta.columns]
    idc = next(c for c in ("station_id", "ID", "id") if c in meta.columns)
    meta = meta.reset_index(drop=True)
    ds = xr.open_dataset(a.weather_nc)
    nc_ids = np.array([str(x) for x in ds["station"].values])
    if len(nc_ids) != len(meta):
        raise SystemExit(f"nc 场站数 {len(nc_ids)} != meta 行数 {len(meta)}（列序须对应）")
    os.makedirs(a.out_dir, exist_ok=True)

    enc_vars = list(ds.data_vars)
    for tech in ("wind", "solar"):
        cols = np.where(meta["type"].astype(str).to_numpy() == tech)[0]
        if len(cols) == 0:
            continue
        sub = ds.isel(station=cols)            # 一次性按技术取列(构建期)
        sub_ids = np.array([str(x) for x in sub["station"].values])
        # 对账: 拆出的 station_id 应与 meta 该技术 id 一致, 且唯一
        meta_ids = meta.loc[cols, idc].astype(str).to_numpy()
        assert (sub_ids == meta_ids).all(), f"{tech}: station_id 与 meta 不对应"
        assert len(set(sub_ids)) == len(sub_ids), f"{tech}: station_id 仍有重复"
        # 经纬度对账
        mlat = pd.to_numeric(meta.loc[cols, "lat"], errors="coerce").to_numpy(float)
        if "lat" in sub.coords and not np.allclose(np.asarray(sub["lat"].values), mlat, atol=1e-3, equal_nan=True):
            raise SystemExit(f"{tech}: lat 对账失败")
        enc = {v: {"zlib": True, "complevel": 4, "dtype": "float32"} for v in enc_vars}
        fp = os.path.join(a.out_dir, f"weather_{tech}.nc")
        sub.to_netcdf(fp, encoding=enc)
        logger.info("已写出 %s  场站=%d（唯一）变量=%s", fp, len(sub_ids), enc_vars)
    ds.close()


if __name__ == "__main__":
    main()
