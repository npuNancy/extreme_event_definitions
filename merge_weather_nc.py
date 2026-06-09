"""合并多个场站气象 nc(extract_station_weather_nc.py 产出)成一个。

增量工作流:每个场站集合单独抽取成各自 nc,新增场站只抽新集合,再用本脚本合并 ——
不必重抽已有数据。
  python extract_station_weather_nc.py --meta origin.csv  ... --out weather_nc/origin.nc
  python extract_station_weather_nc.py --meta foreign.csv ... --out weather_nc/foreign.nc
  python merge_weather_nc.py --inputs weather_nc/origin.nc weather_nc/foreign.nc --out weather_nc/all.nc
  # 以后新增一批:
  python extract_station_weather_nc.py --meta new.csv ... --out weather_nc/new.nc
  python merge_weather_nc.py --inputs weather_nc/all.nc weather_nc/new.nc --out weather_nc/all.nc

规则:
  - 沿 **station** 维拼接;station_id **去重**(默认保留先出现的,--prefer last 改为后者)
  - 时间取 **并集**(各 nc 年份/范围可不同),缺失时刻 -> NaN
  - 变量取各 nc **交集**(并提示被丢弃的变量)
"""
from __future__ import annotations
import argparse
import functools
import numpy as np
import pandas as pd
import xarray as xr


def merge(paths, out, prefer="first"):
    dss = [xr.open_dataset(p) for p in paths]
    # 变量交集
    varsets = [set(d.data_vars) for d in dss]
    common = sorted(functools.reduce(lambda a, b: a & b, varsets))
    dropped = sorted(functools.reduce(lambda a, b: a | b, varsets) - set(common))
    if dropped:
        print(f"[warn] 变量不一致, 仅保留交集; 丢弃: {dropped}")
    # 时间并集
    union_time = functools.reduce(
        lambda a, b: a.union(b),
        [pd.DatetimeIndex(pd.to_datetime(d["time"].values)) for d in dss],
    ).sort_values()
    print(f"[merge] union time: {union_time[0]} -> {union_time[-1]} ({len(union_time)} steps)")

    order = list(dss) if prefer == "first" else list(reversed(dss))
    parts, seen = [], set()
    for d in order:
        d = d[common].reindex(time=union_time)
        ids = [str(s) for s in d["station"].values]
        keep = [i for i, s in enumerate(ids) if s not in seen]
        seen.update(ids[i] for i in keep)
        if keep:
            parts.append(d.isel(station=keep))
    merged = xr.concat(parts, dim="station")
    if prefer == "last":      # 恢复输入顺序(可选, 不强求)
        pass
    enc = {v: {"zlib": True, "complevel": 4, "dtype": "float32"} for v in common}
    merged.to_netcdf(out, encoding=enc)
    nstat = merged.sizes["station"]; ntime = merged.sizes["time"]
    print(f"[ok] wrote {out}  time={ntime} station={nstat} vars={common}")
    for d in dss:
        d.close()
    merged.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", nargs="+", required=True, help="多个 nc 路径")
    ap.add_argument("--out", required=True)
    ap.add_argument("--prefer", default="first", choices=["first", "last"],
                    help="station_id 重复时保留哪个 nc 的值")
    a = ap.parse_args()
    merge(a.inputs, a.out, a.prefer)


if __name__ == "__main__":
    main()
