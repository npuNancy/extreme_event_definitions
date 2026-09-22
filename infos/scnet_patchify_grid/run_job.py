#!/usr/bin/env python3
"""Compute-node adapter: V2 preflight, one grid stage, and a completion receipt."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from create_jobs import MODELS, SCENARIOS, TECHS, segments, unit_id, years


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial." + uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def within(path, root):
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_relative_to(Path(root).resolve(strict=True)):
        raise ValueError(f"path escapes expected root: {path} -> {resolved}; expected {root}")
    return resolved


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"missing environment variable {name}")
    return value


def preflight(args):
    user = pwd.getpwuid(os.getuid()).pw_name
    with (HERE / "accounts.csv").open() as stream:
        workers = {r["username"] for r in csv.DictReader(stream) if r["role"] == "worker"}
    if user not in workers:
        raise ValueError(f"not an authorized worker: {user}")
    job_id = require_env("SLURM_JOB_ID")
    if not re.fullmatch(r"\d+", job_id):
        raise ValueError("expected a normal Slurm job ID (no arrays)")
    run_id = require_env("EXTREME_RUN_ID")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", run_id):
        raise ValueError("unsafe EXTREME_RUN_ID")
    selected = require_env("EXTREME_SELECTED_MODELS").split()
    if args.model not in selected or any(v not in MODELS for v in selected):
        raise ValueError("model not in the user-confirmed runtime allowlist")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
    if head != args.code_sha or branch != "develop-patch-grid" or dirty:
        raise ValueError("checkout must be clean develop-patch-grid at the pinned code SHA")
    release_path = Path(require_env("EXTREME_V2_RELEASE")).resolve(strict=True)
    release = json.loads(release_path.read_text())
    if release["release"] != "production_v2" or release["run_id"] != run_id or release["code_sha"] != head:
        raise ValueError("V2 release/run/code identity mismatch")
    if sorted(release["selected_models"]) != sorted(selected):
        raise ValueError("release model selection differs from confirmed allowlist")
    if (release["analysis_years"], release["baseline_years"]) != (args.analysis_years,args.baseline_years):
        raise ValueError("release analysis/baseline years mismatch")
    model = release["models"][args.model]
    if model["status"] != "ready" or digest(model["evidence"]) != model["evidence_sha256"]:
        raise ValueError("V2 model readiness evidence is absent or changed")
    bcsd = Path(require_env("EXTREME_BCSD_ROOT")).expanduser().absolute()
    if bcsd != Path(release["bcsd_root"]).expanduser().absolute():
        raise ValueError("BCSD root differs from frozen input release")
    patch_manifest = Path(require_env("EXTREME_PATCH_MANIFEST")).resolve(strict=True)
    land = Path(require_env("EXTREME_LAND_PLAN")).resolve(strict=True)
    if str(patch_manifest) != release["patch_manifest"]["path"] or digest(patch_manifest) != release["patch_manifest"]["sha256"]:
        raise ValueError("patch manifest differs from frozen input release")
    ls = land.stat()
    if {"path":str(land),"size":ls.st_size,"mtime_ns":ls.st_mtime_ns} != release["land_plan"]:
        raise ValueError("land plan differs from frozen input release")
    patch_data = json.loads(patch_manifest.read_text())
    if args.patch not in patch_data["patches"]:
        raise ValueError("patch absent from frozen patch manifest")
    # The model evidence is established in preparation; daily checks only inspect
    # current-unit final paths and their lightweight sidecars, not upstream arrays.
    from grid_extreme_signals.grid_io import final_file
    variables = (("uas","vas") if args.tech == "wind" else ("rsds",)) if args.stage == "baseline" else (
        ("tas","uas","vas","hurs") + (("pr","rsds") if args.tech == "solar" else ()))
    for variable in variables:
        path, _ = final_file(bcsd,args.model,args.scenario,variable,args.patch)
        within(path, release["outputs_real_root"])
        within(str(path)+".json", release["outputs_real_root"])
    share = Path("/work/share") / user
    run_root = share / "extreme_grid" / run_id
    run_root.mkdir(parents=True,exist_ok=True)
    within(run_root, share)
    return {"user":user,"job_id":job_id,"run_id":run_id,"run_root":run_root,
            "release_sha256":digest(release_path),"release_file":str(release_path),
            "bcsd":str(bcsd),"patch_manifest":str(patch_manifest),"land_plan":str(land)}


def compute_command(args, runtime):
    output_root = runtime["run_root"] / "outputs"
    name = "prepare_grid_baseline.py" if args.stage == "baseline" else "grid_signals_patchify.py"
    command = [sys.executable,str(ROOT/"scripts"/name),"--bcsd-root",runtime["bcsd"],
               "--model",args.model,"--scenario",args.scenario,"--patch",args.patch,"--tech",args.tech,
               "--patch-manifest",runtime["patch_manifest"],"--land-plan",runtime["land_plan"],
               "--analysis-years",args.analysis_years,"--baseline-years",args.baseline_years,
               "--tile-shape",*map(str,args.tile_shape),"--time-chunk",str(args.time_chunk),
               "--complevel",str(args.complevel),"--processes",str(args.processes),
               "--output-root",str(output_root),"--parts-root",str(runtime["run_root"]/"parts"),"--timing-report"]
    if args.stage == "signals":
        baseline = Path(require_env("EXTREME_BASELINE_FILE")).resolve(strict=True)
        command += ["--baseline-file",str(baseline),"--years",args.years,"--years-per-file",str(args.years_per_file)]
    return command


def dependency(row, expected_id, runtime):
    from grid_extreme_signals import grid_io as io
    if row["unit_id"] != expected_id or row["slurm_state"] != "COMPLETED" or row["exit_code"] != "0:0":
        raise ValueError(f"dependency not successful: {expected_id}")
    receipt = json.loads(Path(row["receipt"]).read_text())
    if (receipt["status"],receipt["run_id"],receipt["unit_id"],receipt["slurm_job_id"]) != (
            "COMPLETED",runtime["run_id"],expected_id,str(row["job_id"])):
        raise ValueError(f"dependency receipt mismatch: {expected_id}")
    output = Path(row["output"]).resolve(strict=True)
    if receipt["output"] != str(output) or receipt["artifact"] != io.file_identity(output):
        raise ValueError(f"dependency artifact changed: {output}")
    if receipt["input_release_sha256"] != runtime["release_sha256"]:
        raise ValueError("dependency was produced for a different V2 release")
    meta = io.complete(output)
    if not meta:
        raise ValueError(f"invalid dependency NetCDF/sidecar: {output}")
    if receipt["code_sha"] != meta["context"]["baseline_contract"]["implementation"]["code_sha"]:
        raise ValueError("dependency receipt code SHA mismatch")
    return output, meta


def audit(args, runtime):
    """Check a distributed combination on a compute node; publish an index only."""
    import netCDF4
    import numpy as np
    from grid_extreme_signals import grid_io as io
    index = json.loads(Path(require_env("EXTREME_COMBINATION_INDEX")).read_text())
    for key in ("model","scenario","patch","tech"):
        if index[key] != getattr(args,key):
            raise ValueError(f"combination index {key} mismatch")
    if index["run_id"] != runtime["run_id"]:
        raise ValueError("combination run_id mismatch")
    make_id = lambda stage, period: unit_id(stage,args.model,args.scenario,args.tech,args.patch,period)
    base_path, base = dependency(index["baseline"],make_id("baseline",args.baseline_years),runtime)
    if base["context"]["stage"] != "baseline" or base["context"]["years"] != args.baseline_years:
        raise ValueError("baseline stage/year mismatch")
    contract = base["context"]["baseline_contract"]
    for key, value in (("model",args.model),("scenario",args.scenario),("patch_id",args.patch),("tech",args.tech),
                       ("analysis_years",args.analysis_years),("baseline_years",args.baseline_years)):
        if contract[key] != value:
            raise ValueError(f"baseline contract mismatch: {key}")
    if contract["implementation"]["code_sha"] != args.code_sha:
        raise ValueError("baseline code SHA mismatch")
    with netCDF4.Dataset(base_path) as ds:
        lat,lon,domain = ds["lat"][:],ds["lon"][:],ds["domain_mask"][:]
    expected = segments(args.analysis_years,args.years_per_file)
    rows = sorted(index["signals"],key=lambda r:r["years"])
    if [r["years"] for r in rows] != expected:
        raise ValueError("signals do not cover each expected year segment exactly once")
    end_index, previous_hour, summaries = 0,None,[]
    for row in rows:
        path, meta = dependency(row,make_id("signals",row["years"]),runtime)
        context = meta["context"]
        if (context["stage"] != "signals" or context["years"] != row["years"]
                or context["baseline_contract"] != contract or context["events"] != base["context"]["events"]):
            raise ValueError("signal context mismatch")
        if context["baseline_identity"] != base["identity"] or context["baseline_artifact"] != base["artifact"]:
            raise ValueError("signal uses a different baseline artifact")
        start,stop = context["interval"]
        if start != end_index:
            raise ValueError("overlap or gap in distributed time intervals")
        end_index = stop
        with netCDF4.Dataset(path) as ds:
            if not np.array_equal(ds["lat"][:],lat) or not np.array_equal(ds["lon"][:],lon) or not np.array_equal(ds["domain_mask"][:],domain):
                raise ValueError("distributed grid/domain mismatch")
            t = ds["time"]
            dates = netCDF4.num2date(t[:],t.units,t.calendar,only_use_cftime_datetimes=True)
            hours = np.asarray(netCDF4.date2num(dates,io.TIME_UNITS,t.calendar))
            if len(hours) != stop-start or not np.allclose(np.diff(hours),3,rtol=0,atol=1e-8):
                raise ValueError("invalid segment time count/step")
            if io.select_years(np.asarray(dates),row["years"]) != (0,len(hours)):
                raise ValueError("incorrect segment year coverage")
            if previous_hour is not None and not np.isclose(hours[0]-previous_hour,3,rtol=0,atol=1e-8):
                raise ValueError("time gap at a file boundary")
            previous_hour = hours[-1]
            for event in base["context"]["events"]:
                v = ds["signal_"+event]
                if v.dimensions != ("time","lat","lon") or v.dtype != np.dtype("int8") or v._FillValue != -127:
                    raise ValueError("signal schema mismatch")
        summaries.append({"unit_id":row["unit_id"],"output":str(path),"identity":meta["identity"],
                          "artifact":meta["artifact"],"years":row["years"],"time_count":stop-start})
    output = runtime["run_root"] / "outputs" / args.model / args.scenario / args.patch / args.tech / f"audit_{args.analysis_years}.json"
    atomic_json(output,{"status":"COMPLETED","kind":"grid-v2-distributed-index","run_id":runtime["run_id"],
                        "baseline":str(base_path),"baseline_identity":base["identity"],"contract":contract,
                        "input_release_sha256":runtime["release_sha256"],"signals":summaries,"time_count":end_index})
    return output


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stage",choices=("baseline","signals","audit"),required=True)
    p.add_argument("--model",choices=MODELS,required=True)
    p.add_argument("--scenario",choices=SCENARIOS,required=True)
    p.add_argument("--tech",choices=TECHS,required=True)
    for name in ("patch","years","analysis-years","baseline-years","code-sha","resource-profile"):
        p.add_argument("--"+name,required=True)
    p.add_argument("--years-per-file",type=int,default=5)
    p.add_argument("--processes",type=int,default=4)
    p.add_argument("--tile-shape",type=int,nargs=2,default=(32,32))
    p.add_argument("--time-chunk",type=int,default=240)
    p.add_argument("--complevel",type=int,default=1)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    years(args.years)
    a,b = years(args.analysis_years); c,d = years(args.baseline_years)
    if not a <= c <= d <= b or args.years_per_file < 1:
        raise ValueError("invalid analysis/baseline years or segment width")
    expected = ([args.baseline_years] if args.stage == "baseline" else
                [args.analysis_years] if args.stage == "audit" else segments(args.analysis_years,args.years_per_file))
    if args.years not in expected:
        raise ValueError("stage years do not match the campaign segmentation")
    if not re.fullmatch(r"R\d{2}C\d{2}",args.patch):
        raise ValueError("invalid patch")
    runtime = preflight(args)
    start = time.monotonic()
    uid = unit_id(args.stage,args.model,args.scenario,args.tech,args.patch,args.years)
    if args.stage == "audit":
        output = audit(args,runtime)
    else:
        subprocess.run(compute_command(args,runtime),check=True)
        name = ("baseline_" if args.stage == "baseline" else "signals_") + args.years + ".nc"
        output = runtime["run_root"]/"outputs"/args.model/args.scenario/args.patch/args.tech/name
        from grid_extreme_signals import grid_io as io
        if not io.complete(output):
            raise ValueError("scientific command exited without a complete artifact")
    from grid_extreme_signals.grid_io import file_identity
    receipt = runtime["run_root"] / "runtime/receipts" / uid / f"{runtime['job_id']}.json"
    atomic_json(receipt,{"status":"COMPLETED","unit_id":uid,"run_id":runtime["run_id"],
                         "submit_username":runtime["user"],"slurm_job_id":runtime["job_id"],
                         "code_sha":args.code_sha,"resource_profile":args.resource_profile,
                         "input_release_sha256":runtime["release_sha256"],"output":str(output.resolve()),
                         "artifact":file_identity(output),"elapsed_seconds":time.monotonic()-start})
    print(json.dumps({"unit_id":uid,"output":str(output),"receipt":str(receipt)}))


if __name__ == "__main__":
    main()
