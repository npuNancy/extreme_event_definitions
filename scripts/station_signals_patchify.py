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

class _SignalWriter:
    """Streams station signal columns to netCDF; mirrors the dataset layout of
    station_match.write_station_signals without materializing (T, all stations)."""
    def __init__(self,path,times,stations,match,a,files):
        import netCDF4
        self.path=path
        self.ds=netCDF4.Dataset(path,"w",format="NETCDF4")
        self.ds.createDimension("time",len(times)); self.ds.createDimension("station",len(stations))
        t=self.ds.createVariable("time","f8",("time",))
        from xarray.coding.times import encode_cf_datetime
        raw=np.asarray(times)
        if np.issubdtype(raw.dtype,np.datetime64):
            encoded,units,calendar=encode_cf_datetime(raw.astype("datetime64[ns]"),"hours since 1970-01-01")
        else:
            encoded,units,calendar=encode_cf_datetime(list(times),"hours since 1970-01-01")
        t.units=units; t.calendar=calendar
        t[:]=encoded
        s=self.ds.createVariable("station","i4",("station",)); s[:]=np.arange(len(stations),dtype=np.int32)
        ids=sm.station_ids(a.scenario,a.tech,stations.lon.to_numpy(float),stations.lat.to_numpy(float))
        sm.validate_station_ids(ids,a.scenario,a.tech,stations.lon.to_numpy(float),stations.lat.to_numpy(float))
        for name,values,dtype in (("station_id",ids,str),("lon",stations.lon.to_numpy(np.float32),"f4"),
                ("lat",stations.lat.to_numpy(np.float32),"f4"),("capacity_gw",stations.capacity_gw.to_numpy(np.float32),"f4"),
                ("activation_year",stations.activation_year.to_numpy(np.int16),"i2"),
                ("match_dist_deg",match.dist_deg.astype(np.float32),"f4")):
            v=self.ds.createVariable(name,dtype,("station",)); v[:]=values
        self.ds.setncatts({"source":"global_bcsd_patch","model":a.model,"patch_id":a.patch,"scenario":a.scenario,
            "tech":a.tech,"source_csv":os.path.basename(a.stations_csv),"pipeline":"patchify",
            "grid_resolution":"0.1deg","match_method":match.method,"max_match_dist_deg":str(a.max_distance_deg),
            "activation_mask":"on","skipped_events":"","patch_manifest":str(Path(a.patch_manifest).resolve()),
            "low_resource_cache":"clim288+P5 in job","bcsd_files":json.dumps({k:str(v) for k,v in files.items()})})
        self._vars={}; self._events=set(); self._skipped=set()
    def write(self,masks,k0):
        for name,values in masks.items():
            if name not in self._vars:
                v=self.ds.createVariable(name,"i1",("time","station"),zlib=True,complevel=4)
                # List the station metadata as CF auxiliary coordinates so
                # xarray/loss readers see station_id/lon/lat as coords, not
                # data_vars (mirrors the original xarray writer's dataset).
                v.coordinates="station_id lon lat capacity_gw activation_year match_dist_deg"
                v.flag_values="0, 1"; v.flag_meanings="false true"
                self._vars[name]=v
                self._events.add(name)
                self.ds.setncattr("supported_events",",".join(sorted(self._events)))
            self._vars[name][:,k0:k0+values.shape[1]]=values
    def note_skipped(self,names,reason):
        self._skipped.update(names)
        self.ds.setncattr("skipped_events",",".join(sorted(self._skipped)))
        self.ds.setncattr("skipped_reasons",json.dumps({n:reason for n in sorted(self._skipped)}))
    def close(self):
        self.ds.close()

def run(a):
    # Read patch bbox from an optional manifest; explicit bbox is required so the
    # The manifest is the only spatial ownership source.
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
        full_match=sm.match_regular_weighted(ref[ln].values,ref[on].values,stations,method=a.spatial_method,max_dist=a.max_distance_deg)
        idx=_pseudo(times); years=np.array([_year(t) for t in times]); base=(years>=2015)&(years<=2024)
        if base.sum()==0: raise ValueError("2015-2024 baseline is absent")
        steps=common.window_steps_for_hours(3.0)
        # Large patches hold tens of thousands of stations; the per-variable
        # station series alone is (T, S) float32 = tens of GB. All signals and
        # the low-resource climatology are station-independent, so stream in
        # station blocks: gather the block's grid rows/cols, run the identical
        # per-block pipeline, write netCDF columns straight to disk.
        nt=len(times); ns=len(stations)
        blk=max(256,int(1.0e9//max(1,nt*4*6)))
        blocks=[(k0,min(ns,k0+blk)) for k0 in range(0,ns,blk)]
        tmp=out.with_suffix(out.suffix+f".partial.{os.getpid()}")
        writer=_SignalWriter(tmp,times,stations,full_match,a,files)
        try:
            for k0,k1 in blocks:
                sub=stations.iloc[k0:k1].reset_index(drop=True)
                m=sm.match_regular_weighted(ref[ln].values,ref[on].values,sub,method=a.spatial_method,max_dist=a.max_distance_deg)
                used_lat=np.unique(m.idx0); used_lon=np.unique(m.idx1)
                lat_pos=np.full(m.idx0.max()+1,-1,dtype=np.int64); lat_pos[used_lat]=np.arange(len(used_lat))
                lon_pos=np.full(m.idx1.max()+1,-1,dtype=np.int64); lon_pos[used_lon]=np.arange(len(used_lon))
                m=sm.StationSpatialWeights(m.stations,lat_pos[m.idx0],lon_pos[m.idx1],m.weight,m.dist_deg,m.valid,grid_kind=m.grid_kind,method=m.method)
                weather={}
                # Gather the station block in time sub-blocks: a dense block
                # spans most of the grid, and a one-shot (T, rows, cols)
                # read would materialize tens of GB per variable. Off-axis
                # variables interpolate per sub-block padded by one source
                # step so linear edges keep their neighbours.
                tb=max(64,min(2048,int(1.5e9//max(1,len(used_lat)*len(used_lon)*4))))
                for v,ds in opened.items():
                    da=_v(ds,v); dt=_coord(ds,("time","valid_time"))
                    off_axis = not np.array_equal(ds[dt].values,times)
                    src_times=ds[dt].values
                    da=da.isel(**{ln:used_lat,on:used_lon})
                    station_series=np.empty((nt,k1-k0),dtype=np.float32)
                    for s0 in range(0,nt,tb):
                        s1=min(nt,s0+tb)
                        if off_axis:
                            pad_lo=1 if s0>0 else 0
                            pad_hi=1 if s1<len(src_times) else 0
                            chunk=da.isel(**{dt:slice(s0-pad_lo,s1+pad_hi)})
                            chunk=chunk.interp(**{dt:times[s0:s1]})
                        else:
                            chunk=da.isel(**{dt:slice(s0,s1)})
                        arr=np.asarray(chunk.transpose(dt,ln,on).values,dtype=np.float32)
                        station_series[s0:s1]=sm.gather_to_stations_weighted(arr,m)
                    arr=station_series
                    units=_v(ds,v).attrs.get("units") or {"tas":"K","uas":"m/s","vas":"m/s","hurs":"%","pr":"kg m-2 s-1","rsds":"W m-2"}[v]
                    if v in ("uas","vas"): arr=wind_to_ms(arr,units)
                    elif v=="tas": arr=tas_to_celsius(arr,units)
                    elif v=="hurs": arr=hurs_to_pct(arr,units)
                    elif v=="pr": arr=pr_to_mmh(arr,units)
                    elif v=="rsds": arr=rsds_to_wm2(arr,units)
                    weather[{"tas":"temp_C","uas":"wind_u_ms","vas":"wind_v_ms","hurs":"rh_pct","pr":"precip_mmh","rsds":"rsds"}[v]]=arr
                weather["wind_ms"]=np.hypot(weather.pop("wind_u_ms"),weather.pop("wind_v_ms")).astype(np.float32)
                # skip_missing=True: solar dust needs observed dust_aod (MERRA-2),
                # which global BCSD does not carry; such events are skipped and
                # recorded in skipped_events instead of killing the unit.
                masks=registry.simple_signals(a.tech,weather,skip_missing=True)
                supported=set(masks)|set(writer._events)
                skipped=sorted(set(registry.SIMPLE[a.tech])-supported)
                if skipped: writer.note_skipped(skipped,reason="input absent from global BCSD")
                resource=weather[registry.LOWRES_RESOURCE[a.tech]]
                roll=common.roll_centered(resource,steps); clim=common.clim288(roll,idx,base); anom=roll-clim[idx.month.to_numpy()-1,idx.hour.to_numpy()]; thr=np.nanpercentile(np.where(np.isfinite(anom[base]),anom[base],np.nan),5,axis=0)
                low_kwargs=dict(base_mask=base,clim_tbl=clim,thr=thr,window_steps=steps)
                if a.tech=="solar": low_kwargs.update(lat=sub.lat.to_numpy(float),lon=sub.lon.to_numpy(float))
                low=registry.low_resource_signal(a.tech,{registry.LOWRES_RESOURCE[a.tech]:resource},idx,**low_kwargs)
                masks["low_resource"]=low
                valid=m.valid[None,:]; act=years[:,None]>=sub.activation_year.to_numpy(int)[None,:]
                masks={f"signal_{k}":(np.asarray(v,bool)&valid&act).astype(np.int8) for k,v in masks.items()}
                writer.write(masks,k0)
        finally:
            writer.close()
        os.replace(tmp,out); Path(str(out)+".json").write_text(json.dumps({"model":a.model,"scenario":a.scenario,"patch_id":a.patch,"tech":a.tech,"station_count":len(stations),"baseline":"2015-2024","output":str(out)},indent=2)+"\n")
    finally:
        for ds in opened.values(): ds.close()

def parser():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--bcsd-root",required=True); p.add_argument("--model",required=True); p.add_argument("--scenario",required=True); p.add_argument("--patch",required=True); p.add_argument("--patch-manifest",required=True); p.add_argument("--stations-csv",required=True); p.add_argument("--tech",choices=("wind","solar"),required=True); p.add_argument("--years",default="2015-2060"); p.add_argument("--output-root",required=True); p.add_argument("--spatial-method",choices=("nearest","bilinear"),default="nearest"); p.add_argument("--max-distance-deg",type=float,default=.15); p.add_argument("--overwrite",action="store_true",help="rewrite existing output (the job generator always passes this; output writing is atomic tmp+replace anyway)"); return p
if __name__=="__main__": run(parser().parse_args())
