"""Single-job station extreme signals for one global_bcsd patch.

--processes N runs station blocks in N spawned workers, each writing an
independent part file; the parent then streams the parts into the final
NetCDF without ever materializing (time, all stations).
"""
from __future__ import annotations
import argparse, json, os, sys, time
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
SUPPORTED_YEARS = "2015-2060"
VAR_TO_WEATHER={"tas":"temp_C","uas":"wind_u_ms","vas":"wind_v_ms","hurs":"rh_pct","pr":"precip_mmh","rsds":"rsds"}
UNIT_FALLBACK={"tas":"K","uas":"m/s","vas":"m/s","hurs":"%","pr":"kg m-2 s-1","rsds":"W m-2"}


class _Timer:
    """Phase wall-clock accumulator for the --timing-report sidecar."""
    def __init__(self): self.slots={}; self._t0=None; self._name=None
    def __call__(self,name):
        self._name=name; self._t0=time.perf_counter(); return self
    def __enter__(self): return self
    def __exit__(self,*exc):
        self.slots[self._name]=self.slots.get(self._name,0.0)+time.perf_counter()-self._t0


def _validate_years(value):
    if value != SUPPORTED_YEARS:
        raise ValueError(f"--years 目前只允许输入 {SUPPORTED_YEARS}，收到 {value!r}")
    return value


def _years_arg(value):
    try:
        return _validate_years(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc

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


def _station_block_signals(a, sub, full_match_slice, ref, opened, times, idx, years, base, steps, timer):
    """Compute all signal masks for one station block; returns int8 (T, K) dict.

    sub: station rows; full_match_slice: precomputed match (idx0/idx1/weight/
    dist/valid already aligned to the full grid axes) restricted to sub."""
    ln=_coord(ref,("lat","latitude")); on=_coord(ref,("lon","longitude"))
    used_lat=np.unique(full_match_slice.idx0); used_lon=np.unique(full_match_slice.idx1)
    lat_pos=np.full(full_match_slice.idx0.max()+1,-1,dtype=np.int64); lat_pos[used_lat]=np.arange(len(used_lat))
    lon_pos=np.full(full_match_slice.idx1.max()+1,-1,dtype=np.int64); lon_pos[used_lon]=np.arange(len(used_lon))
    m=sm.StationSpatialWeights(full_match_slice.stations,lat_pos[full_match_slice.idx0],lon_pos[full_match_slice.idx1],
                               full_match_slice.weight,full_match_slice.dist_deg,full_match_slice.valid,
                               grid_kind=full_match_slice.grid_kind,method=full_match_slice.method)
    weather={}
    # Gather the station block in time sub-blocks: a dense block
    # spans most of the grid, and a one-shot (T, rows, cols)
    # read would materialize tens of GB per variable. Off-axis
    # variables interpolate per sub-block padded by one source
    # step so linear edges keep their neighbours.
    nt=len(times); k1k0=len(sub)
    tb=max(64,min(2048,int(1.5e9//max(1,len(used_lat)*len(used_lon)*4))))
    for v,ds in opened.items():
        with timer(f"weather_{v}"):
            da=_v(ds,v); dt=_coord(ds,("time","valid_time"))
            off_axis = not np.array_equal(ds[dt].values,times)
            src_times=ds[dt].values
            da=da.isel(**{ln:used_lat,on:used_lon})
            station_series=np.empty((nt,k1k0),dtype=np.float32)
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
            units=_v(ds,v).attrs.get("units") or UNIT_FALLBACK[v]
            if v in ("uas","vas"): arr=wind_to_ms(arr,units)
            elif v=="tas": arr=tas_to_celsius(arr,units)
            elif v=="hurs": arr=hurs_to_pct(arr,units)
            elif v=="pr": arr=pr_to_mmh(arr,units)
            elif v=="rsds": arr=rsds_to_wm2(arr,units)
            weather[VAR_TO_WEATHER[v]]=arr
    weather["wind_ms"]=np.hypot(weather.pop("wind_u_ms"),weather.pop("wind_v_ms")).astype(np.float32)
    # skip_missing=True: solar dust needs observed dust_aod (MERRA-2),
    # which global BCSD does not carry; such events are skipped and
    # recorded in skipped_events instead of killing the unit.
    with timer("ordinary_events"):
        masks=registry.simple_signals(a.tech,weather,skip_missing=True)
        skipped=sorted(set(registry.SIMPLE[a.tech])-set(masks))
    with timer("low_resource"):
        resource=weather[registry.LOWRES_RESOURCE[a.tech]]
        roll=common.roll_centered(resource,steps); clim=common.clim288(roll,idx,base); anom=roll-clim[idx.month.to_numpy()-1,idx.hour.to_numpy()]; thr=np.nanpercentile(np.where(np.isfinite(anom[base]),anom[base],np.nan),5,axis=0)
        low_kwargs=dict(base_mask=base,clim_tbl=clim,thr=thr,window_steps=steps)
        if a.tech=="solar": low_kwargs.update(lat=sub.lat.to_numpy(float),lon=sub.lon.to_numpy(float))
        low=registry.low_resource_signal(a.tech,{registry.LOWRES_RESOURCE[a.tech]:resource},idx,**low_kwargs)
        masks["low_resource"]=low
    with timer("masks_apply"):
        valid=m.valid[None,:]; act=years[:,None]>=sub.activation_year.to_numpy(int)[None,:]
        masks={f"signal_{k}":(np.asarray(v,bool)&valid&act).astype(np.int8) for k,v in masks.items()}
    return masks, skipped


def _serial_blocks(a,stations,full_match,ref,opened,times,idx,years,base,steps,out,files,timer):
    """Original streaming loop: compute each station block and write columns."""
    writer=_SignalWriter(out.with_suffix(out.suffix+f".partial.{os.getpid()}"),times,stations,full_match,a,files)
    try:
        nt=len(times); ns=len(stations)
        blk=max(256,int(1.0e9//max(1,nt*4*6)))
        for k0 in range(0,ns,blk):
            k1=min(ns,k0+blk)
            sub=stations.iloc[k0:k1].reset_index(drop=True)
            with timer("station_block_total"):
                slice_match=sm.StationSpatialWeights(sub,full_match.idx0[k0:k1],full_match.idx1[k0:k1],
                                                     full_match.weight[k0:k1],full_match.dist_deg[k0:k1],
                                                     full_match.valid[k0:k1],grid_kind=full_match.grid_kind,
                                                     method=full_match.method)
                masks,skipped=_station_block_signals(a,sub,slice_match,ref,opened,times,idx,years,base,steps,timer)
                if skipped: writer.note_skipped(skipped,reason="input absent from global BCSD")
                with timer("write"):
                    writer.write(masks,k0)
    finally:
        writer.close()


def _part_worker(payload):
    """One station-block part in a spawned child process."""
    a=argparse.Namespace(**payload["args"])
    stations=payload["stations"]
    slice_match=sm.StationSpatialWeights(stations,np.asarray(payload["idx0"]),np.asarray(payload["idx1"]),
                                         np.asarray(payload["weight"],np.float32),np.asarray(payload["dist_deg"]),
                                         np.asarray(payload["valid"]),method=payload["method"])
    out=Path(payload["part_path"])
    # Part-level retry: a completed part with a valid sidecar is reused as-is;
    # anything else (missing file, missing/invalid sidecar) is recomputed.
    if out.is_file():
        side=Path(str(out)+".json")
        if side.is_file():
            try:
                if json.loads(side.read_text()).get("status")=="COMPLETED":
                    return {"part_index":payload["part_index"],"station_offset":payload["station_offset"],
                            "station_count":payload["station_count"],"output":str(out),"status":"REUSED"}
            except json.JSONDecodeError:
                pass
    files={v:final(a.bcsd_root,a.model,a.scenario,v,a.patch) for v in NEEDED[a.tech]}
    opened={v:xr.open_dataset(p) for v,p in files.items()}
    timer=_Timer()
    try:
        ref=opened["rsds" if a.tech=="solar" else "uas"]; tn=_coord(ref,("time","valid_time")); ln=_coord(ref,("lat","latitude")); on=_coord(ref,("lon","longitude"))
        raw_times=ref[tn].values
        def _year(t):
            value=getattr(t,"year",None)
            return value if value is not None else pd.Timestamp(t).year
        years_raw=np.asarray([int(_year(t)) for t in raw_times]); y0,y1=(int(x) for x in a.years.split("-",1)) if "-" in a.years else (int(a.years),int(a.years))
        selected=np.flatnonzero((years_raw>=y0)&(years_raw<=y1))
        ref=ref.isel({tn:selected}); times=ref[tn].values
        idx=_pseudo(times); years=np.array([_year(t) for t in times]); base=(years>=2015)&(years<=2024)
        steps=common.window_steps_for_hours(3.0)
        tmp=out.with_suffix(out.suffix+f".partial.{os.getpid()}")
        writer=_SignalWriter(tmp,times,stations,slice_match,a,files)
        try:
            with timer("station_block_total"):
                masks,skipped=_station_block_signals(a,stations,slice_match,ref,opened,times,idx,years,base,steps,timer)
            if skipped: writer.note_skipped(skipped,reason="input absent from global BCSD")
            with timer("write"):
                writer.write(masks,0)
        finally:
            writer.close()
        os.replace(tmp,out)
        Path(str(out)+".json").write_text(json.dumps({"status":"COMPLETED","model":a.model,"scenario":a.scenario,
            "patch_id":a.patch,"tech":a.tech,"part_index":payload["part_index"],"station_offset":payload["station_offset"],
            "station_count":len(stations),"output":str(out),"timing":{k:round(v,3) for k,v in timer.slots.items()}},indent=2)+"\n")
        return {"part_index":payload["part_index"],"station_offset":payload["station_offset"],
                "station_count":len(stations),"output":str(out),"status":"COMPLETED"}
    finally:
        for ds in opened.values(): ds.close()


def _merge_parts(parts, stations, out, a):
    """Stream event variables × station columns from part files into the final
    NetCDF; never materializes (time, all stations) in memory."""
    import netCDF4
    # Station-dim metadata comes from the parent's authoritative frame; part
    # metadata is validated against it (station_id, lon, lat, capacity,
    # activation_year, time axis) before any data is copied.
    open_parts=[netCDF4.Dataset(p["output"]) for p in parts]
    tmp=out.with_suffix(out.suffix+f".partial.{os.getpid()}")
    try:
        first=open_parts[0]
        time_var=first.variables["time"]
        events=sorted(set(first.variables)-{"time","station","station_id","lon","lat","capacity_gw","activation_year","match_dist_deg"})
        # Build final writer header from the parent frame + part 0 layout.
        w=netCDF4.Dataset(tmp,"w",format="NETCDF4")
        w.createDimension("time",len(time_var)); w.createDimension("station",len(stations))
        t=w.createVariable("time","f8",("time",)); t.units=time_var.units; t.calendar=getattr(time_var,"calendar","standard"); t[:]=time_var[:]
        s=w.createVariable("station","i4",("station",)); s[:]=np.arange(len(stations),dtype=np.int32)
        ids=sm.station_ids(a.scenario,a.tech,stations.lon.to_numpy(float),stations.lat.to_numpy(float))
        sm.validate_station_ids(ids,a.scenario,a.tech,stations.lon.to_numpy(float),stations.lat.to_numpy(float))
        # match_dist_deg comes from the parts (each worker's writer holds the
        # authoritative copy for its slice; the parent concatenates in order).
        match_dist_by_part={p["part_index"]:np.asarray(ds.variables["match_dist_deg"][:],dtype=np.float32)
                            for p,ds in zip(parts,open_parts)}
        match_dist=np.concatenate([match_dist_by_part[p["part_index"]] for p in parts])
        for name,values,dtype in (("station_id",ids,str),("lon",stations.lon.to_numpy(np.float32),"f4"),
                ("lat",stations.lat.to_numpy(np.float32),"f4"),("capacity_gw",stations.capacity_gw.to_numpy(np.float32),"f4"),
                ("activation_year",stations.activation_year.to_numpy(np.int16),"i2"),
                ("match_dist_deg",match_dist,"f4")):
            v=w.createVariable(name,dtype,("station",)); v[:]=values
        w.setncatts({k:first.getncattr(k) for k in first.ncattrs() if k not in {"supported_events","skipped_events","skipped_reasons"}})
        # Validate identity + time axis on every part before copying data.
        for p,ds in zip(parts,open_parts):
            got_ids=np.asarray(ds.variables["station_id"][:],dtype=str)
            n_sta=ds.dimensions["station"].size
            exp=ids[p["station_offset"]:p["station_offset"]+n_sta]
            if not np.array_equal(got_ids,exp): raise ValueError(f"part {p['part_index']} station_id mismatch")
            for key in ("lon","lat","capacity_gw","activation_year"):
                got=np.asarray(ds.variables[key][:])
                exp_col=_expected_meta(key,stations,p)
                if not np.array_equal(got.astype(exp_col.dtype),exp_col): raise ValueError(f"part {p['part_index']} {key} mismatch")
            if not np.array_equal(np.asarray(ds.variables["time"][:]),np.asarray(time_var[:])): raise ValueError(f"part {p['part_index']} time axis mismatch")
        # Copy each event variable column-block by column-block.
        TB=4096
        total_stations=len(stations)
        for ev in events:
            v=w.createVariable(ev,"i1",("time","station"),zlib=True,complevel=4)
            v.coordinates="station_id lon lat capacity_gw activation_year match_dist_deg"
            v.flag_values="0, 1"; v.flag_meanings="false true"
            for s0 in range(0,len(time_var),TB):
                s1=min(len(time_var),s0+TB)
                block=np.empty((s1-s0,total_stations),dtype=np.int8)
                for p,ds in zip(parts,open_parts):
                    off=p["station_offset"]; n=ds.dimensions["station"].size
                    block[:,off:off+n]=ds.variables[ev][s0:s1,:]
                v[s0:s1,:]=block
        supported=set()
        skipped_all={}
        for p,ds in zip(parts,open_parts):
            attr="supported_events"
            if attr in ds.ncattrs(): supported |= {x for x in str(ds.getncattr(attr)).split(",") if x}
            if "skipped_reasons" in ds.ncattrs():
                try: skipped_all.update(json.loads(ds.getncattr("skipped_reasons")))
                except Exception: pass
        w.setncattr("supported_events",",".join(sorted(x for x in supported if x)))
        w.setncattr("skipped_events",",".join(sorted(skipped_all)))
        if skipped_all: w.setncattr("skipped_reasons",json.dumps(skipped_all))
        w.close()
        for ds in open_parts: ds.close()
        open_parts=[]
        os.replace(tmp,out)
    finally:
        for ds in open_parts: ds.close()
    Path(str(out)+".json").write_text(json.dumps({"model":a.model,"scenario":a.scenario,"patch_id":a.patch,"tech":a.tech,
        "station_count":len(stations),"baseline":"2015-2024","processes":a.processes,"parts":[p["output"] for p in parts],
        "output":str(out)},indent=2)+"\n")


def _expected_meta(key,stations,p,match_dist_by_part=None):
    off=p["station_offset"]; n=p["station_count"]
    if key=="match_dist_deg":
        if match_dist_by_part is None or p["part_index"] not in match_dist_by_part: raise KeyError(key)
        return np.asarray(match_dist_by_part[p["part_index"]],dtype=np.float32)
    col={"lon":np.float32,"lat":np.float32,"capacity_gw":np.float32,"activation_year":np.int16}[key]
    return stations[key].iloc[off:off+n].to_numpy(col)


def run(a):
    _validate_years(a.years)
    t0=time.perf_counter()
    timer=_Timer()
    # Read patch bbox from an optional manifest; explicit bbox is required so the
    # The manifest is the only spatial ownership source.
    with timer("stations_csv"):
        manifest=json.loads(Path(a.patch_manifest).read_text())
        bbox=manifest["patches"][a.patch]["core_bbox_360"]
        stations=_stations(a.stations_csv,a.tech,bbox)
    out=Path(a.output_root)/a.model/a.scenario/a.patch/f"{a.tech}.nc"; out.parent.mkdir(parents=True,exist_ok=True)
    if stations.empty:
        marker=Path(str(out)+".SKIPPED_NO_STATIONS.json"); marker.write_text(json.dumps({"status":"SKIPPED_NO_STATIONS","patch":a.patch,"tech":a.tech},indent=2)+"\n"); return
    if out.is_file() and not a.overwrite:
        return
    with timer("input_open"):
        files={v:final(a.bcsd_root,a.model,a.scenario,v,a.patch) for v in NEEDED[a.tech]}
        opened={v:xr.open_dataset(p) for v,p in files.items()}
    try:
        with timer("time_axis_select"):
            ref=opened["rsds" if a.tech=="solar" else "uas"]; tn=_coord(ref,("time","valid_time")); ln=_coord(ref,("lat","latitude")); on=_coord(ref,("lon","longitude")); raw_times=ref[tn].values
            y0,y1=(int(x) for x in a.years.split("-",1)) if "-" in a.years else (int(a.years),int(a.years))
            def _year(t):
                value=getattr(t,"year",None)
                return value if value is not None else pd.Timestamp(t).year
            years_raw=np.asarray([int(_year(t)) for t in raw_times]); selected=np.flatnonzero((years_raw>=y0)&(years_raw<=y1))
            if selected.size==0: raise ValueError(f"no time points in --years {a.years}")
            ref=ref.isel({tn:selected}); times=ref[tn].values
        with timer("spatial_match"):
            # One full match for the whole unit: the multiprocess path slices
            # idx0/idx1/weight from it so workers never redo nearest/bilinear.
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
        processes=max(1,int(getattr(a,"processes",1) or 1))
        if processes>1:
            with timer("parallel_dispatch"):
                blk=int(getattr(a,"station_block_size",None) or 0) or max(256,int(1.0e9//max(1,nt*4*6)))
                bounds=[(k0,min(ns,k0+blk)) for k0 in range(0,ns,blk)]
                parts_root=Path(getattr(a,"parts_root",None) or (Path(a.output_root)/".extreme_parts"))/a.model/a.scenario/a.patch/a.tech
                parts_root.mkdir(parents=True,exist_ok=True)
                payload_args={k:v for k,v in vars(a).items() if not k.startswith("_")}
                payloads=[]
                for i,(k0,k1) in enumerate(bounds):
                    sub=stations.iloc[k0:k1].reset_index(drop=True)
                    payloads.append({"args":payload_args,"stations":sub,"idx0":full_match.idx0[k0:k1],"idx1":full_match.idx1[k0:k1],
                                     "weight":full_match.weight[k0:k1],"dist_deg":full_match.dist_deg[k0:k1],"valid":full_match.valid[k0:k1],
                                     "method":full_match.method,"part_index":i,"station_offset":k0,"station_count":k1-k0,
                                     "part_path":str(parts_root/f"part_{i:02d}.nc")})
                from concurrent.futures import ProcessPoolExecutor, as_completed
                import multiprocessing
                ctx=multiprocessing.get_context("spawn")
                results=[]
                with ProcessPoolExecutor(max_workers=min(processes,len(payloads)),mp_context=ctx) as pool:
                    futures=[pool.submit(_part_worker,p) for p in payloads]
                    for f in as_completed(futures): results.append(f.result())
                results.sort(key=lambda r:r["part_index"])
            with timer("merge"):
                _merge_parts(results,stations,out,a)
        else:
            with timer("whole_job_loop"):
                _serial_blocks(a,stations,full_match,ref,opened,times,idx,years,base,steps,out,files,timer)
            os.replace(out.with_suffix(out.suffix+f".partial.{os.getpid()}"),out)
            Path(str(out)+".json").write_text(json.dumps({"model":a.model,"scenario":a.scenario,"patch_id":a.patch,"tech":a.tech,
                "station_count":len(stations),"baseline":"2015-2024","processes":1,"output":str(out)},indent=2)+"\n")
    finally:
        for ds in opened.values(): ds.close()
    if getattr(a,"timing_report",False):
        report={k:round(v,3) for k,v in timer.slots.items()}
        report["total_wall"]=round(time.perf_counter()-t0,3)
        Path(str(out)+".timing.json").write_text(json.dumps(report,indent=2)+"\n")


def parser():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--bcsd-root",required=True); p.add_argument("--model",required=True); p.add_argument("--scenario",required=True); p.add_argument("--patch",required=True); p.add_argument("--patch-manifest",required=True); p.add_argument("--stations-csv",required=True); p.add_argument("--tech",choices=("wind","solar"),required=True); p.add_argument("--years",type=_years_arg,default=SUPPORTED_YEARS,help=f"固定使用 {SUPPORTED_YEARS}"); p.add_argument("--output-root",required=True); p.add_argument("--spatial-method",choices=("nearest","bilinear"),default="nearest"); p.add_argument("--max-distance-deg",type=float,default=.15); p.add_argument("--processes",type=int,default=1,help="station-block worker processes (1 = serial streaming)"); p.add_argument("--station-block-size",type=int,default=None,help="stations per worker part (default: memory-derived, same as serial)"); p.add_argument("--parts-root",default=None,help="directory for per-worker part files (default: <output-root>/.extreme_parts)"); p.add_argument("--timing-report",action="store_true",help="write <output>.timing.json phase wall-clock sidecar"); p.add_argument("--overwrite",action="store_true",help="rewrite existing output (the job generator always passes this; output writing is atomic tmp+replace anyway)"); return p
if __name__=="__main__": run(parser().parse_args())
