"""低资源基线(clim288 + 每站P5阈值) —— 复用 extreme_signals 已抽好的逐站资源(1987-2016)。

无需从原始全球文件重抽: /data1/luobaozhen/extreme_signals 下已有逐站 ERA5-Land:
  output_china_trainset/extracted/{u10,v10,ssrd}/{var}_{year}.nc   (中国 1079 站)
  output_addtion_global/extracted/{u10,v10,ssrd}/{var}_{year}.nc   (国外/全球 addition)
已验证: 这些资源与 extract_station_weather_nc 产出的 weather nc **完全一致**(wind 2024 corr=1.0)。

资源: 风=10m风速(u10,v10); 光=辐照 rsds(extracted/ssrd, 已去累积 W/m2)。
口径同 tools/common.py: roll24c -> clim288(月×时) -> 距平 P5。

用法:
  python compute_lowres_baseline.py --meta meta.csv --tech wind  --out lowres_baseline_wind.npz
  python compute_lowres_baseline.py --meta meta.csv --tech solar --out lowres_baseline_solar.npz
依赖: tools/common.py。
"""
from __future__ import annotations
import argparse
import logging
import os
import sys
import numpy as np
import pandas as pd
import xarray as xr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import common
from tools.logging_utils import setup_logging

logger = logging.getLogger(__name__)

ES = "/data1/luobaozhen/extreme_signals"
ROOTS = [f"{ES}/output_china_trainset/extracted", f"{ES}/output_addtion_global/extracted"]


def _log(message, *args):
    logger.info(message, *args)


def fill_resource(root, tech, years, want, canon, res, colmap):
    """从一个 extracted root 读 want 站资源, 填入 res(T_canon, K)(按 canon 时间 + colmap 列)。
    返回该 root 命中的 station_id 集合。"""
    need = ["u10", "v10"] if tech == "wind" else ["ssrd"]
    f0 = f"{root}/{need[0]}/{need[0]}_{years[0]}.nc"
    if not os.path.exists(f0):
        return set()
    d0 = xr.open_dataset(f0); rids = np.array([str(x) for x in d0["station_id"].values]); d0.close()
    sel = np.array([i for i, s in enumerate(rids) if s in want and s in colmap], dtype=int)
    if len(sel) == 0:
        return set()
    cols = np.array([colmap[rids[i]] for i in sel], dtype=int)
    for y in years:
        arrs = {}; ty = None; ok = True
        for v in need:
            fp = f"{root}/{v}/{v}_{y}.nc"
            if not os.path.exists(fp):
                ok = False; break
            dv = xr.open_dataset(fp); arrs[v] = dv[v].values[:, sel].astype(np.float32)
            ty = pd.to_datetime(dv["time"].values); dv.close()
        if not ok:
            continue
        r = np.sqrt(arrs["u10"]**2 + arrs["v10"]**2) if tech == "wind" else \
            np.where(arrs["ssrd"] > 1361.0, 0.0, arrs["ssrd"])
        pos = canon.get_indexer(pd.DatetimeIndex(ty)); m = pos >= 0
        res[np.ix_(pos[m], cols)] = r[m]
    return set(rids[sel])


def fill_raw(tech, miss, sub, idc, years, canon, res, colmap):
    """对 extracted 中缺失的站, 从原始全球 ERA5-Land 补抽基线资源, 填入 res。按国家分区切 bbox。"""
    import legacy_station_pipeline.weather_loaders as wl
    from legacy_station_pipeline.extract_station_weather_nc import gather_var, _align
    mm = sub[sub[idc].astype(str).isin(miss)].copy()
    mm["_sid"] = mm[idc].astype(str)
    lat = pd.to_numeric(mm["lat"], errors="coerce").to_numpy(float)
    lon = wl.to_180(pd.to_numeric(mm["lon"], errors="coerce").to_numpy(float))
    lon360 = wl.to_360(lon)
    sids = mm["_sid"].to_numpy()
    grp = (mm["country"].astype(str).to_numpy() if "country" in mm.columns
           else np.array(["ALL"] * len(mm)))
    need = ["u10", "v10"] if tech == "wind" else ["ssrd"]
    for gname in pd.unique(grp):
        gi = np.where(grp == gname)[0]
        la = lat[gi]; lo = lon360[gi]; pad = 0.5
        bbox = (min(la.max() + pad, 90), max(la.min() - pad, -90),
                max(lo.min() - pad, 0), min(lo.max() + pad, 360))
        arrs = {}
        for v in need:
            arr, t = gather_var(v, years, la, lo, bbox)
            if v == "ssrd":
                arr = wl._deaccum(arr, t) * wl.SSRD_J_TO_W
            arrs[v] = _align(arr, t, canon)
        r = (np.sqrt(arrs["u10"]**2 + arrs["v10"]**2) if tech == "wind"
             else np.where(arrs["ssrd"] > 1361.0, 0.0, arrs["ssrd"])).astype(np.float32)
        cols = np.array([colmap[sids[i]] for i in gi], dtype=int)
        res[:, cols] = r
        _log("  [兜底] %s：%d 站补抽完成", gname, len(gi))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta", required=True)
    ap.add_argument("--tech", required=True, choices=["wind", "solar"])
    ap.add_argument("--start_year", type=int, default=1987)
    ap.add_argument("--end_year", type=int, default=2016)
    ap.add_argument("--out", required=True)
    ap.add_argument("--pct", type=float, default=common.PCT)
    ap.add_argument("--fallback_raw", type=int, default=1,
                    help="1: extracted 缺的站从原始全球文件补抽(默认开)")
    a = ap.parse_args()
    setup_logging("compute_lowres_baseline")
    years = list(range(a.start_year, a.end_year + 1))

    meta = pd.read_csv(a.meta); meta.columns = [str(c).lstrip("﻿") for c in meta.columns]
    idc = next(c for c in ("station_id", "ID", "id") if c in meta.columns)
    sub = meta[meta["type"].astype(str) == a.tech]
    want = set(sub[idc].astype(str))
    _log("%s：目标 %d 站，基线 %d-%d", a.tech, len(want), years[0], years[-1])

    sids = np.array(sorted(want))                       # 统一站序(全部目标站)
    colmap = {s: i for i, s in enumerate(sids)}
    canon = pd.date_range(f"{years[0]}-01-01 00:00", f"{years[-1]}-12-31 23:00", freq="h")
    res = np.full((len(canon), len(sids)), np.nan, np.float32)
    found = set()
    for root in ROOTS:
        hit = fill_resource(root, a.tech, years, want, canon, res, colmap)
        if hit:
            _log("  %s：命中 %d 站", os.path.basename(os.path.dirname(root)), len(hit))
            found |= hit
    miss = want - found
    if miss and a.fallback_raw:
        _log("[兜底] %d 站不在 extracted，从原始全球 ERA5-Land 补抽基线 ...", len(miss))
        fill_raw(a.tech, miss, sub, idc, years, canon, res, colmap)
        found = want  # 兜底后视为已覆盖(仍可能个别 NaN)
    miss = want - found
    if miss:
        _log("[警告] %d 站仍缺基线（低资源该站全 NaN/无事件）：示例 %s", len(miss), list(miss)[:5])
    if res.shape[1] == 0 or np.isfinite(res).sum() == 0:
        logger.error("无任何站资源")
        return

    tref = canon
    _log("资源数组 (T,K)=%s；计算 roll24c + clim288 + P%s ...", res.shape, a.pct)
    roll = common.roll24c(res)
    clim = common.clim288(roll, tref, base_mask=None)
    mo = tref.month.to_numpy() - 1; hr = tref.hour.to_numpy()
    anom = roll - clim[mo, hr]
    with np.errstate(all="ignore"):
        thr = np.nanpercentile(np.where(np.isfinite(anom), anom, np.nan), a.pct, axis=0).astype(np.float32)
    np.savez(a.out, station_id=sids, clim_tbl=clim.astype(np.float32), thr=thr,
             pct=a.pct, baseline=f"{a.start_year}-{a.end_year}", tech=a.tech)
    _log("已写出 %s  clim%s thr%s K=%d 阈值[min/med/max]=%.3f/%.3f/%.3f",
         a.out, clim.shape, thr.shape, len(sids),
         np.nanmin(thr), np.nanmedian(thr), np.nanmax(thr))


if __name__ == "__main__":
    main()
