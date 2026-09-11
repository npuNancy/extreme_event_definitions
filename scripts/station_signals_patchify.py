#!/usr/bin/env python3
"""Single-job station extreme signals for one global_bcsd patch."""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path
import numpy as np
import pandas as pd
import xarray as xr

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
import registry
from grid_extreme_signals import station_match as sm
from grid_extreme_signals.unit_conversion import hurs_to_pct, pr_to_mmh, rsds_to_wm2, tas_to_celsius, wind_to_ms
from tools import common

NEEDED={"wind":("tas","uas","vas","hurs","pr"),"solar":("tas","uas","vas","hurs","pr","rsds")}

def final(root, model, scenario, var, patch):
    base=Path(root).expanduser()/"outputs"/model/scenario/var
    candidates=[base/f"{patch}.nc",base/f"{var}_{model}_{scenario}_{patch}.nc"]
    candidates += sorted(base.glob(f"*{patch}*.nc")) if base.is_dir() else []
    paths=[]
    for p in candidates:
        if p.is_file() and p not in paths: paths.append(p)
    if len(paths)!=1: raise FileNotFoundError(f"expected one {var} final for {model}/{scenario}/{patch}, got {paths}")
    side=Path(str(paths[0])+".json")
    if not side.is_file(): raise FileNotFoundError(side)
    meta=json.loads(side.read_text())
    if meta.get("patch_id")!=patch or meta.get("variable")!=var: raise ValueError(f"identity mismatch in {side}")
    return paths[0]

def _v(ds,var):
    for n in (var,f"{var}_bcsd"):
        if n in ds: return ds[n]
    if len(ds.data_vars)==1:return ds[next(iter(ds.data_vars))]
    raise KeyError(var)

def _coord(ds,names):
    for n in names:
        if n in ds.coords or n in ds.dims:return n
    raise KeyError(names)

def _stations(csv,tech,patch_bbox):
    raw=sm.load_stations(csv); raw=raw[raw.type.astype(str).str.lower()==tech].copy()
    w,e,s,n=patch_bbox
    w=((float(w)+180)%360)-180; e=((float(e)+180)%360)-180
    lon=raw.lon.to_numpy(); lat=raw.lat.to_numpy()
    # A bbox crossing the antimeridian is represented as two intervals after
    # converting the BCSD [0,360) convention to [-180,180).
    lon_keep=(lon>=w)&(lon<e) if w<e else ((lon>=w)|(lon<e))
    keep=lon_keep&(lat>=s)&((lat<n)|(lat==90 if n==90 else False))
    raw=raw.loc[keep].copy()
    if raw.empty:return raw.assign(activation_year=pd.Series(dtype="int64"))
    raw=(raw.sort_values("year").groupby(["lon","lat"],as_index=False).agg(activation_year=("year","min"),capacity_gw=("capacity_gw","first"),type=("type","first")))
    return raw.reset_index(drop=True)

def _pseudo(times):
    # Keep month/day/hour semantics without Gregorian leap-day gaps in 365_day data.
    out=[]
    for t in times:
        stamp=pd.Timestamp(t) if not hasattr(t,"month") else t
        # 2000 is leap-capable, so both Gregorian leap-day and no-leap inputs
        # can be represented without an artificial February gap.
        out.append(pd.Timestamp(2000,int(stamp.month),int(stamp.day),int(getattr(stamp,"hour",0)),int(getattr(stamp,"minute",0)),int(getattr(stamp,"second",0))))
    return pd.DatetimeIndex(out)

def run(a):
    # Read patch bbox from an optional manifest; explicit bbox is required so the
    # script never silently assigns stations by country/region.
    manifest=json.loads(Path(a.patch_manifest).read_text())
    bbox=manifest["patches"][a.patch]["core_bbox_360"]
    stations=_stations(a.stations_csv,a.tech,bbox)
    out=Path(a.output_root)/a.model/a.scenario/a.patch/f"{a.tech}.nc"; out.parent.mkdir(parents=True,exist_ok=True)
    if stations.empty:
        marker=Path(str(out)+".SKIPPED_NO_STATIONS.json"); marker.write_text(json.dumps({"status":"SKIPPED_NO_STATIONS","patch":a.patch,"tech":a.tech},indent=2)+"\n"); return
    files={v:final(a.bcsd_root,a.model,a.scenario,v,a.patch) for v in NEEDED[a.tech]}
    opened={v:xr.open_dataset(p) for v,p in files.items()}
    try:
        ref=opened["rsds" if a.tech=="solar" else "uas"]; tn=_coord(ref,("time","valid_time")); ln=_coord(ref,("lat","latitude")); on=_coord(ref,("lon","longitude")); raw_times=ref[tn].values
        y0,y1=(int(x) for x in a.years.split("-",1)) if "-" in a.years else (int(a.years),int(a.years))
        def _year(t):
            value=getattr(t,"year",None)
            return value if value is not None else pd.Timestamp(t).year
        years_raw=np.asarray([int(_year(t)) for t in raw_times]); selected=np.flatnonzero((years_raw>=y0)&(years_raw<=y1))
        if selected.size==0: raise ValueError(f"no time points in --years {a.years}")
        ref=ref.isel({tn:selected}); times=ref[tn].values
        match=sm.match_regular_weighted(ref[ln].values,ref[on].values,stations,method=a.spatial_method,max_dist=a.max_distance_deg)
        weather={}
        for v,ds in opened.items():
            da=_v(ds,v); dt=_coord(ds,("time","valid_time"))
            if not np.array_equal(ds[dt].values,times): da=da.interp({dt:ref[tn]})
            arr=np.asarray(da.transpose(dt,ln,on).values,dtype=np.float32)
            arr=sm.gather_to_stations_weighted(arr,match)
            units=da.attrs.get("units") or {"tas":"K","uas":"m/s","vas":"m/s","hurs":"%","pr":"kg m-2 s-1","rsds":"W m-2"}[v]
            if v in ("uas","vas"): arr=wind_to_ms(arr,units); 
            elif v=="tas": arr=tas_to_celsius(arr,units)
            elif v=="hurs": arr=hurs_to_pct(arr,units)
            elif v=="pr": arr=pr_to_mmh(arr,units)
            elif v=="rsds": arr=rsds_to_wm2(arr,units)
            weather[{"tas":"temp_C","uas":"wind_u_ms","vas":"wind_v_ms","hurs":"rh_pct","pr":"precip_mmh","rsds":"rsds"}[v]]=arr
        weather["wind_ms"]=np.hypot(weather.pop("wind_u_ms"),weather.pop("wind_v_ms")).astype(np.float32)
        masks=registry.simple_signals(a.tech,weather,skip_missing=False)
        idx=_pseudo(times); years=np.array([_year(t) for t in times]); base=(years>=2015)&(years<=2024)
        if base.sum()==0: raise ValueError("2015-2024 baseline is absent")
        resource=weather[registry.LOWRES_RESOURCE[a.tech]]
        steps=common.window_steps_for_hours(3.0); roll=common.roll_centered(resource,steps); clim=common.clim288(roll,idx,base); anom=roll-clim[idx.month.to_numpy()-1,idx.hour.to_numpy()]; thr=np.nanpercentile(np.where(np.isfinite(anom[base]),anom[base],np.nan),5,axis=0)
        low_kwargs=dict(base_mask=base,clim_tbl=clim,thr=thr,window_steps=steps)
        if a.tech=="solar": low_kwargs.update(lat=stations.lat.to_numpy(float),lon=stations.lon.to_numpy(float))
        low=registry.low_resource_signal(a.tech,{registry.LOWRES_RESOURCE[a.tech]:resource},idx,**low_kwargs)
        masks["low_resource"]=low
        valid=match.valid[None,:]; act=years[:,None]>=stations.activation_year.to_numpy(int)[None,:]
        masks={f"signal_{k}":(np.asarray(v,bool)&valid&act).astype(np.int8) for k,v in masks.items()}
        tmp=out.with_suffix(out.suffix+f".partial.{os.getpid()}")
        sm.write_station_signals(tmp,masks,times,match,a.tech,source="global_bcsd_patch",model=a.model,region=a.patch,scenario=a.scenario,source_csv=os.path.basename(a.stations_csv),pipeline="patchify",supported=sorted(masks),skipped=[],skipped_reasons={},max_dist=a.max_distance_deg,activation_mask_on=True,attrs_extra={"patch_manifest":str(Path(a.patch_manifest).resolve()),"low_resource_cache":"clim288+P5 in job","bcsd_files":json.dumps({k:str(v) for k,v in files.items()})})
        os.replace(tmp,out); Path(str(out)+".json").write_text(json.dumps({"model":a.model,"scenario":a.scenario,"patch_id":a.patch,"tech":a.tech,"station_count":len(stations),"baseline":"2015-2024","output":str(out)},indent=2)+"\n")
    finally:
        for ds in opened.values(): ds.close()

def parser():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--bcsd-root",required=True); p.add_argument("--model",required=True); p.add_argument("--scenario",required=True); p.add_argument("--patch",required=True); p.add_argument("--patch-manifest",required=True); p.add_argument("--stations-csv",required=True); p.add_argument("--tech",choices=("wind","solar"),required=True); p.add_argument("--years",default="2015-2060"); p.add_argument("--output-root",required=True); p.add_argument("--spatial-method",choices=("nearest","bilinear"),default="nearest"); p.add_argument("--max-distance-deg",type=float,default=.15); return p
if __name__=="__main__": run(parser().parse_args())
