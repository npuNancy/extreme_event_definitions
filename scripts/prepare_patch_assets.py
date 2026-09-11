#!/usr/bin/env python3
"""Create the small, reusable station/patch catalog for one patch.

The weather arrays are never copied.  This preparation artifact is safe to
share read-only between CF, extreme-event and loss jobs.
"""
from __future__ import annotations
import argparse, json, os
from pathlib import Path
import pandas as pd
import sys
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from grid_extreme_signals.station_match import load_stations, station_id

def main(a):
    manifest=json.loads(Path(a.patch_manifest).read_text(encoding="utf-8")); bbox=manifest["patches"][a.patch]["core_bbox_360"]
    w,e,s,n=map(float,bbox); w=((w+180)%360)-180; e=((e+180)%360)-180
    raw=load_stations(a.stations_csv); raw=raw[((raw.lon>=w)&(raw.lon<e) if w<e else ((raw.lon>=w)|(raw.lon<e)))&(raw.lat>=s)&((raw.lat<n)|(raw.lat==90 if n==90 else False))].copy()
    if not raw.empty:
        raw["station_id"]=[station_id(a.scenario,t,x,y) for t,x,y in zip(raw.type,raw.lon,raw.lat)]
        raw=raw.sort_values("year").groupby(["type","lon","lat"],as_index=False).agg(activation_year=("year","min"),capacity_gw=("capacity_gw","first"),station_id=("station_id","first"))
    out=Path(a.output); out.parent.mkdir(parents=True,exist_ok=True); tmp=out.with_suffix(out.suffix+f".partial.{os.getpid()}"); raw.to_csv(tmp,index=False); tmp.replace(out)
    Path(str(out)+".json").write_text(json.dumps({"patch_id":a.patch,"scenario":a.scenario,"station_count":len(raw),"has_stations":bool(len(raw)),"source":str(Path(a.stations_csv).resolve())},ensure_ascii=False,indent=2)+"\n")
if __name__=="__main__":
 p=argparse.ArgumentParser(); p.add_argument("--patch-manifest",required=True); p.add_argument("--patch",required=True); p.add_argument("--scenario",required=True); p.add_argument("--stations-csv",required=True); p.add_argument("--output",required=True); main(p.parse_args())
