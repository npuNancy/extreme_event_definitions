"""Stream existing grid-event flags to stations; never compute weather events."""
from __future__ import annotations

import os
import hashlib
from pathlib import Path
import time
import netCDF4
import numpy as np

from . import station_contract as ct
from .station_mapping import grid_metadata


def load_mapping(path):
    with netCDF4.Dataset(path) as ds:
        ds.set_auto_maskandscale(False)
        return {name: np.asarray(v[:]) for name, v in ds.variables.items()}, dict(ds.__dict__)


def source_schema(ds, combination):
    for key, attr in (("model", "model"), ("climate_scenario", "scenario"), ("tech", "tech"), ("patch", "patch_id")):
        if getattr(ds, attr, None) != combination[key]:
            raise ValueError(f"source {attr} mismatch")
    names = sorted(n for n in ds.variables if n.startswith("signal_"))
    advertised = sorted("signal_" + n for n in ds.supported_events.split(",") if n)
    if not names or names != advertised:
        raise ValueError("source event list mismatch")
    for name in names:
        v = ds[name]
        if v.dimensions != ("time", "lat", "lon") or v.dtype != np.dtype("int8") or v._FillValue != ct.FILL:
            raise ValueError(f"invalid signal schema: {name}")
    t = ds["time"]
    values = np.asarray(t[:])
    if t.dimensions != ("time",) or not len(values) or not np.isfinite(values).all() or np.any(np.diff(values) <= 0):
        raise ValueError("invalid source time axis")
    netCDF4.num2date(values[[0, -1]], t.units, getattr(t, "calendar", "standard"))
    return names


def spatial_groups(variable, mapping):
    """Compute the occupied chunk groups once for an entire source variable."""
    iy, ix = mapping["source_iy"], mapping["source_ix"]
    valid = mapping["mapping_status"] == ct.STATUS["MATCHED"]
    chunks = variable.chunking()
    cy, cx = chunks[1:] if isinstance(chunks, list) else (32, 32)
    groups = (iy // cy) * ((variable.shape[2] + cx - 1) // cx) + ix // cx
    positions = np.flatnonzero(valid)
    positions = positions[np.argsort(groups[positions], kind="stable")]
    boundaries = np.flatnonzero(np.diff(groups[positions])) + 1
    result = []
    for positions in np.split(positions, boundaries):
        if not len(positions):
            continue
        y0, x0 = int(iy[positions[0]] // cy * cy), int(ix[positions[0]] // cx * cx)
        result.append((positions, y0, x0, cy, cx))
    return result


def _gather(variable, start, stop, mapping, groups=None):
    """Read each occupied spatial chunk once; pair indices in memory."""
    out = np.full((stop - start, len(mapping["station_id"])), ct.FILL, np.int8)
    iy, ix = mapping["source_iy"], mapping["source_ix"]
    for positions, y0, x0, cy, cx in (spatial_groups(variable, mapping) if groups is None else groups):
        block = np.asarray(variable[start:stop, y0:y0 + cy, x0:x0 + cx])
        values = block[:, iy[positions] - y0, ix[positions] - x0]
        if not np.isin(values, [ct.FILL, 0, 1]).all():
            raise ValueError("source signals contain values other than 0/1/fill")
        out[:, positions] = values
    return out


def extract_file(artifact, combination, mapping_info, mapping_identity, output,
                 release_identity, code_sha, time_chunk=240, station_chunk=256, complevel=1,
                 catalog_sha256="", max_distance_deg=0.15):
    if min(time_chunk, station_chunk) < 1 or not 0 <= complevel <= 9:
        raise ValueError("invalid chunk/compression settings")
    ct.require_frozen(artifact)
    if not mapping_info["path"]:
        raise ValueError("empty mapping must be recorded as a skipped combination")
    mapping, map_attrs = load_mapping(mapping_info["path"])
    if map_attrs["identity"] != mapping_identity:
        raise ValueError("mapping identity mismatch")
    contract = {"schema": ct.DUAL_SCENARIO_SCHEMA, "combination": ct.combo_key(combination),
                "source": artifact["stat"],
                "source_identity": artifact["source_identity"], "mapping": mapping_identity,
                "mapping_file": ct.file_stat(mapping_info["path"]), "release": release_identity,
                "code_sha": code_sha, "time_chunk": time_chunk,
                "station_chunk": station_chunk, "complevel": complevel}
    identity = ct.fingerprint(contract)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with ct.lock(str(output) + ".lock"):
        old = ct.completed(output, identity)
        if old:
            return old
        if output.exists() or Path(str(output) + ".json").exists():
            raise FileExistsError(f"incomplete or conflicting output; use a new attempt directory: {output}")
        tmp = ct.temporary(output)
        started = time.monotonic()
        try:
            if grid_metadata(artifact["path"])["fingerprint"] != mapping_info["grid_fingerprint"]:
                raise ValueError("source grid/domain differs from frozen mapping")
            with netCDF4.Dataset(artifact["path"]) as src, netCDF4.Dataset(tmp, "w") as dst:
                src.set_auto_maskandscale(False)
                names = source_schema(src, combination)
                if src.identity != artifact["source_identity"]:
                    raise ValueError("source NetCDF identity differs from its sidecar")
                n, nt = len(mapping["station_id"]), len(src.dimensions["time"])
                if n != mapping_info["count"] or n < 1 or len(set(mapping["station_id"])) != n:
                    raise ValueError("mapping station count/uniqueness mismatch")
                dst.createDimension("time", nt)
                dst.createDimension("station", n)
                t = dst.createVariable("time", src["time"].dtype, ("time",), fill_value=False)
                t.setncatts({k: v for k, v in src["time"].__dict__.items() if k != "_FillValue"})
                t[:] = src["time"][:]
                dst.createVariable("station", "i4", ("station",))[:] = np.arange(n, dtype="i4")
                for name, values in mapping.items():
                    if name in ("station_patch", "source_patch"):
                        continue
                    dtype = str if values.dtype.kind in "OUS" else values.dtype
                    v = dst.createVariable(name, dtype, ("station",))
                    v[:] = values
                dst["station_id"].cf_role = "timeseries_id"
                for name, units in (("lon", "degrees_east"), ("lat", "degrees_north")):
                    dst[name].units = units
                    dst[name].standard_name = "longitude" if name == "lon" else "latitude"
                    dst["source_grid_" + name].units = units
                dst["match_dist_deg"].units = "degrees"
                dst["match_dist_km"].units = "km"
                dst["mapping_status"].flag_values = np.array(list(ct.STATUS.values()), dtype="i1")
                dst["mapping_status"].flag_meanings = " ".join(ct.STATUS).lower()
                preserved = ("supported_events", "skipped_events", "skipped_reasons", "event_definitions",
                             "baseline_years", "analysis_years", "reference_variable", "provenance")
                attrs = {k: getattr(src, k) for k in preserved if k in src.ncattrs()}
                attrs.update(schema_version=ct.DUAL_SCENARIO_SCHEMA, Conventions="CF-1.9", featureType="timeSeries",
                             model=combination["model"], climate_scenario=combination["climate_scenario"],
                             station_scenario=combination["station_scenario"], tech=combination["tech"],
                             source_patch=combination["patch"], source_run_id=combination["source_run_id"],
                             source_file=artifact["path"], source_realpath=artifact["stat"]["path"],
                             source_signal_identity=artifact["source_identity"], source_collection_id="grid_v2",
                             source_index_sha256=artifact["index_sha256"], mapping_sha256=mapping_identity,
                             catalog_sha256=catalog_sha256, max_match_dist_deg=max_distance_deg,
                             identity=identity, code_sha=code_sha, activation_mask="off", match_method="nearest_regular")
                dst.setncatts(attrs)
                stats = {}
                timings = {"read_gather_seconds": 0., "write_seconds": 0.}
                for name in names:
                    v = dst.createVariable(name, "i1", ("time", "station"), fill_value=ct.FILL,
                                           chunksizes=(min(time_chunk, nt), min(station_chunk, n)),
                                           zlib=complevel > 0, complevel=complevel, shuffle=True)
                    v.flag_values = np.array([0, 1], "i1")
                    v.flag_meanings = "no_event event"
                    v.units = "1"
                    v.coordinates = "station_id lon lat activation_year source_grid_lon source_grid_lat mapping_status"
                    events = valid = 0
                    groups = spatial_groups(src[name], mapping)
                    for start in range(0, nt, time_chunk):
                        stop = min(start + time_chunk, nt)
                        phase = time.monotonic()
                        values = _gather(src[name], start, stop, mapping, groups)
                        timings["read_gather_seconds"] += time.monotonic() - phase
                        phase = time.monotonic()
                        v[start:stop] = values
                        timings["write_seconds"] += time.monotonic() - phase
                        events += int(np.count_nonzero(values == 1))
                        valid += int(np.count_nonzero(values != ct.FILL))
                    stats[name] = {"event_count": events, "valid_count": valid, "missing_count": nt * n - valid}
                time_identity = ct.fingerprint({"values": hashlib.sha256(np.asarray(src["time"][:]).tobytes()).hexdigest(),
                                                "units": t.units, "calendar": getattr(t, "calendar", "standard")})
            ct.require_frozen(artifact)
            os.replace(tmp, output)
            result = {"status": "COMPLETED", "identity": identity, "contract": contract,
                      "artifact": ct.file_stat(output), "source": artifact, "period": artifact["years"],
                      "station_count": n, "time_count": nt, "time_identity": time_identity,
                      "statistics": stats, "timing": timings, "wall_seconds": time.monotonic() - started}
            ct.atomic_json(str(output) + ".json", result)
            return result
        finally:
            tmp.unlink(missing_ok=True)


def audit_combination(combination, mapping, records, output, sample_stations=16):
    """Independent source comparison plus full per-shard time/coordinate checks."""
    patch_map = mapping["patches"][combination["patch"]]
    if not patch_map["count"]:
        if records:
            raise ValueError("empty station combination has unexpected outputs")
        result = {"status": "SKIPPED_NO_STATIONS", "combination": combination,
                  "mapping_identity": mapping["identity"], "outputs": [], "station_count": 0}
        ct.atomic_json(output, result)
        return result
    if [r["period"] for r in records] != [a["years"] for a in combination["signals"]]:
        raise ValueError("audit missing or reordered signal periods")
    values, _ = load_mapping(patch_map["path"])
    n = len(values["station_id"])
    rng = np.random.default_rng(47)
    positions = np.unique(np.r_[0, n - 1, rng.choice(n, min(sample_stations, n), replace=False)])
    last = None
    previous_step = None
    calendar = None
    time_units = "hours since 0001-01-01 00:00:00"
    events = None
    for artifact, record in zip(combination["signals"], records):
        ct.require_frozen(artifact)
        path = record["artifact"]["path"]
        if not ct.completed(path, record["identity"]):
            raise ValueError("audit found incomplete/changed output")
        with netCDF4.Dataset(path) as out, netCDF4.Dataset(artifact["path"]) as src:
            out.set_auto_maskandscale(False)
            src.set_auto_maskandscale(False)
            names = source_schema(src, combination)
            if sorted(n for n in out.variables if n.startswith("signal_")) != names:
                raise ValueError("output event list differs from source")
            if record["contract"].get("combination") != ct.combo_key(combination):
                raise ValueError("output combination identity mismatch")
            for key in ("model", "climate_scenario", "station_scenario", "tech"):
                if getattr(out, key, None) != combination[key]:
                    raise ValueError(f"output {key} mismatch")
            if out.source_signal_identity != artifact["source_identity"]:
                raise ValueError("output source identity mismatch")
            if events is not None and names != events:
                raise ValueError("event list changes between shards")
            events = names
            for name in ("station_id", "lon", "lat", "source_iy", "source_ix", "mapping_status"):
                np.testing.assert_array_equal(out[name][:], values[name])
            np.testing.assert_array_equal(out["time"][:], src["time"][:])
            for attr in ("units", "calendar"):
                if getattr(out["time"], attr, "standard") != getattr(src["time"], attr, "standard"):
                    raise ValueError("time metadata changed")
            cal = getattr(src["time"], "calendar", "standard")
            dates = netCDF4.num2date(src["time"][:], src["time"].units, cal)
            hours = np.asarray(netCDF4.date2num(dates, time_units, cal))
            steps = np.diff(hours)
            if len(steps) and not np.allclose(steps, steps[0], atol=1e-6, rtol=0):
                raise ValueError("nonuniform source time steps")
            if calendar is not None and cal != calendar:
                raise ValueError("calendar changes between shards")
            step = float(steps[0]) if len(steps) else previous_step
            if last is not None and (step is None or not np.isclose(hours[0] - last, step, atol=1e-6)):
                raise ValueError("source shard boundary gap or overlap")
            last, previous_step, calendar = hours[-1], step, cal
            ti = np.unique(np.r_[0, len(hours) - 1, rng.integers(len(hours), size=min(8, len(hours)))])
            for name in names:
                if out[name].dtype != np.dtype("i1") or out[name]._FillValue != ct.FILL:
                    raise ValueError("output signal dtype/fill mismatch")
                stats = record["statistics"][name]
                if not 0 <= stats["event_count"] <= stats["valid_count"] or stats["valid_count"] + stats["missing_count"] != len(hours) * n:
                    raise ValueError("invalid output event/valid/missing counts")
                for s in positions:
                    actual = np.asarray(out[name][ti, int(s)])
                    expected = (np.asarray(src[name][ti, int(values["source_iy"][s]), int(values["source_ix"][s])])
                                if values["mapping_status"][s] == ct.STATUS["MATCHED"]
                                else np.full(len(ti), ct.FILL, np.int8))
                    np.testing.assert_array_equal(actual, expected, err_msg=f"{path}/{name}/{s}")
    result = {"status": "COMPLETED", "combination": combination, "mapping_identity": mapping["identity"],
              "outputs": records, "station_count": n, "independent_audit": "seed47 sampled values; full time and station coordinates"}
    ct.atomic_json(output, result)
    return result
