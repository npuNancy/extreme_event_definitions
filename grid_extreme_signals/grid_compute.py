"""Two-stage grid extreme-event computation with resumable spatial parts."""
from __future__ import annotations

import argparse
import atexit
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import platform
import resource
import subprocess
import time
import warnings

import netCDF4
import numpy as np
import pandas as pd
import xarray as xr

import registry
from tools import common
from grid_extreme_signals import grid_io as io

ROOT = Path(__file__).resolve().parents[1]
ALGORITHM = "clim288-pseudo2000-roll8-p5-linear-nextstep-v1"
_WORKER = None


class Timer:
    def __init__(self):
        self.values = {}

    @contextmanager
    def phase(self, name):
        start = time.perf_counter()
        try:
            yield
        finally:
            self.values[name] = self.values.get(name, 0.0) + time.perf_counter() - start


def implementation_identity():
    paths = [ROOT / "registry.py", ROOT / "tools/common.py"]
    paths += sorted((ROOT / "events").glob("*.py"))
    paths += [ROOT / "grid_extreme_signals" / name for name in
              ("grid_io.py", "grid_compute.py", "unit_conversion.py")]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                      stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        sha = "unknown"
    return {"source_sha256": digest.hexdigest(), "code_sha": sha,
            "python": platform.python_version(), "numpy": np.__version__,
            "pandas": pd.__version__, "xarray": xr.__version__, "netCDF4": netCDF4.__version__}


def supported(tech):
    events = [name for name, keys in registry.REQUIRED[tech].items()
              if all(key in io.WEATHER_VARS for key in keys)]
    fields = {key for event in events for key in registry.REQUIRED[tech][event]}
    fields.add(registry.LOWRES_RESOURCE[tech])
    variables = sorted({v for field in fields for v in io.WEATHER_VARS[field]})
    skipped = sorted(set(registry.SIMPLE[tech]) - set(events))
    return sorted(events + ["low_resource"]), variables, skipped


def baseline_contract(reader, args, implementation):
    return {"schema_version": io.SCHEMA, "algorithm": ALGORITHM,
            "implementation": implementation, "model": args.model, "scenario": args.scenario,
            "patch_id": args.patch, "tech": args.tech,
            "baseline_years": args.baseline_years, "analysis_years": args.analysis_years,
            "percentile": float(registry.LOWRES[args.tech].PCT), "percentile_method": "linear",
            "reference_variable": reader.reference, "calendar": reader.calendar,
            "grid_fingerprint": reader.grid_fingerprint, "mask_fingerprint": reader.mask_fingerprint,
            "time_fingerprint": reader.time_fingerprint, "spatial_inputs": reader.spatial_identity,
            "resource_inputs": {v: reader.input_identity[v] for v in io.RESOURCE_VARS[args.tech]}}


def attributes(context, identity, bounds):
    contract = context["baseline_contract"]
    return {"schema_version": io.SCHEMA, "identity": identity,
            "model": contract["model"], "scenario": contract["scenario"],
            "patch_id": contract["patch_id"], "tech": contract["tech"],
            "stage": context["stage"], "code_sha": contract["implementation"]["code_sha"],
            "source_sha256": contract["implementation"]["source_sha256"],
            "grid_fingerprint": contract["grid_fingerprint"],
            "mask_fingerprint": contract["mask_fingerprint"],
            "time_fingerprint": contract["time_fingerprint"],
            "reference_variable": contract["reference_variable"],
            "baseline_years": contract["baseline_years"], "analysis_years": contract["analysis_years"],
            "window_steps": 8, "mark_next_step": 1, "pseudo_time": "month/day/time mapped to 2000",
            "percentile_method": "linear", "percentile": contract["percentile"],
            "missing_policy": "event-specific invalid inputs/windows/baseline are fill",
            "supported_events": ",".join(context["events"]),
            "skipped_events": ",".join(context["skipped"]),
            "skipped_reasons": json.dumps({name: "input absent from global BCSD" for name in context["skipped"]}),
            "event_definitions": json.dumps({**{name: mod.EXPR for name, mod in registry.SIMPLE[contract["tech"]].items()},
                                              "low_resource": registry.LOWRES[contract["tech"]].EXPR}),
            "tile_bounds": json.dumps(list(bounds)), "provenance": json.dumps(context, sort_keys=True)}


def _baseline(reader, args, bounds, timer):
    a, b = io.select_years(reader.dates, args.baseline_years)
    sy, sx = io.tile_slices(bounds)
    domain = reader.domain[sy, sx].reshape(-1).astype(bool)
    k = len(domain)
    rolling = np.empty((b - a, k), np.float32)
    # Only one resource's baseline is retained. Weather slabs remain time-bounded.
    for start in range(a, b, args.time_chunk):
        stop = min(b, start + args.time_chunk)
        lo, hi = max(0, start - 4), min(len(reader.times), stop + 3)
        with timer.phase("weather_read"):
            weather = reader.weather(lo, hi, bounds)
        with timer.phase("rolling"):
            roll = common.roll_centered(weather[registry.LOWRES_RESOURCE[args.tech]], 8)
            rolling[start - a:stop - a] = roll[start - lo:stop - lo]
    rolling[:, ~domain] = np.nan
    idx = reader.pseudo[a:b]
    with timer.phase("climatology_percentile"), warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Mean of empty slice")
        warnings.filterwarnings("ignore", message="All-NaN slice encountered")
        clim = common.clim288(rolling, idx)
        anomaly = rolling - clim[idx.month.to_numpy() - 1, idx.hour.to_numpy()]
        anomaly[~np.isfinite(anomaly)] = np.nan
        count = np.sum(np.isfinite(anomaly), axis=0, dtype=np.int32)
        threshold = np.nanpercentile(anomaly, registry.LOWRES[args.tech].PCT, axis=0, method="linear")
    count[~domain] = -1
    return clim, threshold, count


def _signals(reader, args, context, bounds, writer, timer):
    sy, sx = io.tile_slices(bounds)
    shape = (sy.stop - sy.start, sx.stop - sx.start)
    k = shape[0] * shape[1]
    domain = reader.domain[sy, sx].reshape(-1).astype(bool)
    with netCDF4.Dataset(args.baseline_file) as baseline:
        baseline.set_auto_mask(False)
        clim = np.asarray(baseline["clim288"][:, :, sy, sx]).reshape(12, 24, k)
        threshold = np.asarray(baseline["p5"][sy, sx]).reshape(k)
    lat, lon = np.meshgrid(reader.lat[sy], reader.lon[sx], indexing="ij")
    a, b = context["interval"]
    for start in range(a, b, args.time_chunk):
        stop = min(b, start + args.time_chunk)
        lo, hi = max(0, start - 5), min(len(reader.times), stop + 3)
        center = slice(start - lo, stop - lo)
        with timer.phase("weather_read"):
            weather = reader.weather(lo, hi, bounds)
        with timer.phase("ordinary_events"):
            masks = registry.simple_signals(args.tech, weather, skip_missing=True)
            valid = {name: domain[None, :] & np.logical_and.reduce(
                [np.isfinite(weather[key]) for key in registry.REQUIRED[args.tech][name]]) for name in masks}
        with timer.phase("low_resource"):
            roll = common.roll_centered(weather[registry.LOWRES_RESOURCE[args.tech]], 8)
            idx = reader.pseudo[lo:hi]
            anomaly = roll - clim[idx.month.to_numpy() - 1, idx.hour.to_numpy()]
            night = ((common.solar_elevation(lat.ravel(), lon.ravel(), idx) <= 0).T
                     if args.tech == "solar" else None)
            masks["low_resource"] = common.low_resource_from_anomaly(anomaly, threshold, night=night)
            valid["low_resource"] = domain[None, :] & np.isfinite(anomaly) & np.isfinite(threshold)[None, :]
        with timer.phase("write"):
            for name, mask in masks.items():
                encoded = np.where(valid[name][center], mask[center], io.FILL).astype(np.int8)
                writer["signal_" + name][start - a:stop - a] = encoded.reshape(stop - start, *shape)


def _init_worker(args_dict, context):
    global _WORKER
    args = argparse.Namespace(**args_dict)
    reader = io.GridReader(args, context["variables"])
    if reader.input_identity != context["inputs"] or baseline_contract(
            reader, args, context["baseline_contract"]["implementation"]) != context["baseline_contract"]:
        reader.close()
        raise ValueError("worker inputs differ from the parent input snapshot")
    _WORKER = (args, context, reader)
    atexit.register(reader.close)


def _tile_worker(bounds):
    args, context, reader = _WORKER
    sy, sx = io.tile_slices(bounds)
    if not reader.domain[sy, sx].any():
        return {"bounds": list(bounds), "status": "DOMAIN_EMPTY"}
    identity = io.fingerprint({"context": context, "bounds": list(bounds)})
    part = Path(context["parts_dir"]) / ("tile_" + "_".join(map(str, bounds)) + ".nc")
    metadata = {"identity": identity, "context": context, "bounds": list(bounds)}
    with io.output_lock(part):
        cached = io.complete(part, identity)
        if cached:
            return {"bounds": list(bounds), "status": "REUSED", "path": str(part), "identity": identity,
                    "timing": cached.get("timing", {})}
        tmp = io.partial_path(part)
        timer = Timer()
        start_time = time.perf_counter()
        try:
            with io.create_grid_file(tmp, reader, bounds, attributes(context, identity, bounds), context["stage"],
                                     context["events"], context["interval"], args.time_chunk,
                                     args.tile_shape, args.complevel) as writer:
                if context["stage"] == "baseline":
                    clim, threshold, count = _baseline(reader, args, bounds, timer)
                    shape = (sy.stop - sy.start, sx.stop - sx.start)
                    with timer.phase("write"):
                        writer["clim288"][:] = clim.reshape(12, 24, *shape)
                        writer["p5"][:] = threshold.reshape(shape)
                        writer["baseline_valid_count"][:] = count.reshape(shape)
                else:
                    _signals(reader, args, context, bounds, writer, timer)
            timer.values["total_wall"] = time.perf_counter() - start_time
            timer.values["process_peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            io.publish(tmp, part, {**metadata, "timing": timer.values})
        finally:
            tmp.unlink(missing_ok=True)
    return {"bounds": list(bounds), "status": "COMPLETED", "path": str(part), "identity": identity,
            "timing": timer.values}


def _merge(reader, args, context, parts, output, identity):
    expected = list(io.tiles(reader.shape, args.tile_shape))
    if sorted(tuple(p["bounds"]) for p in parts) != sorted(expected):
        raise ValueError("parts do not cover the grid exactly once")
    bounds = (0, reader.shape[0], 0, reader.shape[1])
    tmp = io.partial_path(output)
    try:
        with io.create_grid_file(tmp, reader, bounds, attributes(context, identity, bounds), context["stage"],
                                 context["events"], context["interval"], args.time_chunk,
                                 args.tile_shape, args.complevel) as target:
            for part in parts:
                sy, sx = io.tile_slices(part["bounds"])
                if part["status"] == "DOMAIN_EMPTY":
                    if reader.domain[sy, sx].any():
                        raise ValueError("unexpected empty-domain tile")
                    continue
                if not io.complete(part["path"], part["identity"]):
                    raise ValueError(f"part changed before merge: {part['path']}")
                with netCDF4.Dataset(part["path"]) as source:
                    source.set_auto_mask(False)
                    if not np.array_equal(source["lat"][:], reader.lat[sy]) or not np.array_equal(
                            source["lon"][:], reader.lon[sx]) or not np.array_equal(
                            source["domain_mask"][:], reader.domain[sy, sx]):
                        raise ValueError("part grid/domain mismatch")
                    if context["stage"] == "baseline":
                        # Bound the climatology copy by month as well as tile.
                        for month in range(12):
                            target["clim288"][month, :, sy, sx] = source["clim288"][month]
                        for name in ("p5", "baseline_valid_count"):
                            target[name][sy, sx] = source[name][:]
                    else:
                        a, b = context["interval"]
                        if not np.array_equal(source["time"][:], reader.times[a:b]):
                            raise ValueError("part time mismatch")
                        for start in range(0, b - a, args.time_chunk):
                            stop = min(b - a, start + args.time_chunk)
                            for event in context["events"]:
                                name = "signal_" + event
                                target[name][start:stop, sy, sx] = source[name][start:stop]
        # Merge timing is added before the final publication by the caller.
        return tmp
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def validate_baseline(path, expected):
    metadata = io.complete(path)
    if not metadata or metadata["context"]["stage"] != "baseline":
        raise ValueError(f"baseline is incomplete or invalid: {path}")
    if metadata["context"]["baseline_contract"] != expected:
        raise ValueError("baseline identity mismatch (inputs, grid, calendar, years or implementation)")
    with netCDF4.Dataset(path) as ds:
        if ds["clim288"].dimensions != ("month", "hour", "lat", "lon") or ds["p5"].dtype != np.dtype("float64"):
            raise ValueError("invalid baseline schema")
    return metadata


def update_manifest(output_dir, reader, args, contract):
    """Aggregate completed time segments under a short, separate manifest lock."""
    path = output_dir / "manifest.json"
    with io.output_lock(path):
        completed = []
        for candidate in sorted(output_dir.glob("signals_*.nc")):
            meta = io.complete(candidate)
            if meta and meta["context"]["baseline_contract"] == contract:
                completed.append({"path": str(candidate), "years": meta["context"]["years"],
                                  "interval": meta["context"]["interval"], "identity": meta["identity"],
                                  "time_count": meta["dimensions"]["time"],
                                  "events": meta["context"]["events"]})
        completed.sort(key=lambda row: row["interval"])
        for first, second in zip(completed, completed[1:]):
            if first["interval"][1] > second["interval"][0]:
                raise ValueError("overlapping signal files; use a separate output root for a different partition")
        io.atomic_json(path, {"schema_version": io.SCHEMA, "baseline_contract": contract,
                              "expected_years": io.year_segments(args.analysis_years, args.years_per_file),
                              "grid_fingerprint": reader.grid_fingerprint, "completed": completed})


def check_overlap(output_dir, years):
    start, end = io.year_range(years)
    expected = output_dir / f"signals_{years}.nc"
    for candidate in output_dir.glob("signals_*.nc"):
        if candidate == expected:
            continue
        meta = io.complete(candidate)
        if meta:
            lo, hi = io.year_range(meta["context"]["years"])
            if max(start, lo) <= min(end, hi):
                raise ValueError("overlapping signal files; use a separate output root for a different partition")


def _run_segment(reader, args, stage, years, implementation, output_dir):
    global _WORKER
    contract = baseline_contract(reader, args, implementation)
    events, _, skipped = supported(args.tech)
    interval = list(io.select_years(reader.dates, years)) if stage == "signals" else None
    context = {"stage": stage, "years": years, "baseline_contract": contract,
               "variables": list(reader.datasets), "inputs": reader.input_identity,
               "events": events, "skipped": skipped, "interval": interval,
               "tile_shape": list(args.tile_shape), "time_chunk": args.time_chunk,
               "complevel": args.complevel}
    if stage == "signals":
        baseline = validate_baseline(args.baseline_file, contract)
        context["baseline_identity"] = baseline["identity"]
        context["baseline_artifact"] = baseline["artifact"]
    # Parts depend on data/configuration, not on worker count or final directory.
    config_id = io.fingerprint(context)
    parts_root = Path(args.parts_root or (Path(args.output_root) / ".grid_parts")).expanduser().resolve()
    context["parts_dir"] = str(parts_root / args.model / args.scenario / args.patch / args.tech /
                               stage / years / config_id)
    identity = io.fingerprint(context)
    output = output_dir / f"{'baseline' if stage == 'baseline' else 'signals'}_{years}.nc"
    with io.output_lock(output):
        cached = io.complete(output, identity)
        if cached and not args.overwrite:
            if args.timing_report:
                io.atomic_json(str(output) + ".timing.json", cached["timing"])
            return output
        if output.exists() and not args.overwrite:
            old = io.complete(output)
            if old:
                raise FileExistsError(f"output has a different configuration: {output}; use a new root or --overwrite")
        Path(context["parts_dir"]).mkdir(parents=True, exist_ok=True)
        timer = Timer()
        start_time = time.perf_counter()
        bounds = list(io.tiles(reader.shape, args.tile_shape))
        with timer.phase("compute_parts"):
            if args.processes == 1:
                _WORKER = (args, context, reader)
                try:
                    parts = [_tile_worker(tile) for tile in bounds]
                finally:
                    _WORKER = None
            else:
                # Limit nested native thread pools before spawned workers import NumPy.
                names = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS")
                previous = {name: os.environ.get(name) for name in names}
                try:
                    os.environ.update({name: "1" for name in names})
                    with ProcessPoolExecutor(max_workers=min(args.processes, len(bounds)),
                                             mp_context=multiprocessing.get_context("spawn"),
                                             initializer=_init_worker, initargs=(vars(args), context)) as pool:
                        parts = list(pool.map(_tile_worker, bounds))
                finally:
                    for name, value in previous.items():
                        if value is None:
                            os.environ.pop(name, None)
                        else:
                            os.environ[name] = value
        with timer.phase("merge"):
            tmp = _merge(reader, args, context, parts, output, identity)
        timer.values["total_wall"] = time.perf_counter() - start_time
        timer.values["parent_peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        timer.values["processes"] = args.processes
        timer.values["parts"] = parts
        try:
            reader.check_unchanged()
            if stage == "signals":
                current = validate_baseline(args.baseline_file, contract)
                if current["artifact"] != context["baseline_artifact"]:
                    raise ValueError("baseline changed during calculation")
            io.publish(tmp, output, {"identity": identity, "context": context,
                                     "timing": timer.values, "tiles": [p["bounds"] for p in parts]})
        finally:
            tmp.unlink(missing_ok=True)
        if args.timing_report:
            io.atomic_json(str(output) + ".timing.json", timer.values)
    return output


def validate_args(args, stage):
    for name in ("model", "scenario", "patch"):
        if not re_safe(getattr(args, name)):
            raise ValueError(f"unsafe {name}: {getattr(args, name)!r}")
    if args.processes < 1 or args.time_chunk < 1 or any(n < 1 for n in args.tile_shape):
        raise ValueError("processes, time-chunk and tile-shape must be positive")
    if not 0 <= args.complevel <= 9:
        raise ValueError("complevel must be between 0 and 9")
    args.analysis_years, args.baseline_years = map(io.year_label, (args.analysis_years, args.baseline_years))
    a, b = io.year_range(args.analysis_years)
    lo, hi = io.year_range(args.baseline_years)
    if not a <= lo <= hi <= b:
        raise ValueError("baseline-years must be within analysis-years")
    if stage == "signals":
        if args.years_per_file < 1:
            raise ValueError("years-per-file must be positive")
        if args.years:
            args.years = io.year_label(args.years)
            lo, hi = io.year_range(args.years)
            if not a <= lo <= hi <= b:
                raise ValueError("years must be within analysis-years")
        args.baseline_file = str(Path(args.baseline_file).expanduser().resolve())
    for name in ("bcsd_root", "patch_manifest", "land_plan", "output_root"):
        setattr(args, name, str(Path(getattr(args, name)).expanduser().resolve()))


def re_safe(value):
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value))


def run(args, stage):
    validate_args(args, stage)
    implementation = implementation_identity()
    variables = io.RESOURCE_VARS[args.tech] if stage == "baseline" else supported(args.tech)[1]
    output_dir = Path(args.output_root) / args.model / args.scenario / args.patch / args.tech
    output_dir.mkdir(parents=True, exist_ok=True)
    with io.GridReader(args, variables) as reader:
        io.select_years(reader.dates, args.baseline_years)
        years = ([args.baseline_years] if stage == "baseline" else
                 [args.years] if args.years else io.year_segments(args.analysis_years, args.years_per_file))
        outputs = []
        for segment in years:
            if stage == "signals":
                check_overlap(output_dir, segment)
            outputs.append(_run_segment(reader, args, stage, segment, implementation, output_dir))
        if stage == "signals":
            update_manifest(output_dir, reader, args, baseline_contract(reader, args, implementation))
    return outputs


def parser(stage):
    p = argparse.ArgumentParser(description=("Prepare per-grid-cell resource climatology and P5" if stage == "baseline"
                                            else "Compute native BCSD grid extreme-weather signals"))
    for name in ("bcsd-root", "model", "scenario", "patch", "patch-manifest", "land-plan", "output-root"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--tech", required=True, choices=("wind", "solar"))
    p.add_argument("--analysis-years", default="2015-2060")
    p.add_argument("--baseline-years", default="2015-2024")
    p.add_argument("--tile-shape", type=int, nargs=2, default=(32, 32), metavar=("NY", "NX"))
    p.add_argument("--time-chunk", type=int, default=240)
    p.add_argument("--processes", type=int, default=1)
    p.add_argument("--parts-root")
    p.add_argument("--complevel", type=int, default=1)
    p.add_argument("--timing-report", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    if stage == "signals":
        p.add_argument("--baseline-file", required=True)
        p.add_argument("--years", help="One output segment; omit to process analysis-years in five-year segments")
        p.add_argument("--years-per-file", type=int, default=5)
    return p


def main(stage, argv=None):
    p = parser(stage)
    args = p.parse_args(argv)
    try:
        outputs = run(args, stage)
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        p.exit(2, f"error: {exc}\n")
    for path in outputs:
        print(path)
