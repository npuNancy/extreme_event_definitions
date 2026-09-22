"""Native BCSD grid reads, CF time alignment and atomic grid artifacts."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

import netCDF4
import numpy as np
import pandas as pd
import xarray as xr

from grid_extreme_signals import unit_conversion as units

SCHEMA = "grid-extreme-v1"
FILL = np.int8(-127)
TIME_UNITS = "hours since 1970-01-01 00:00:00"
RESOURCE_VARS = {"wind": ("uas", "vas"), "solar": ("rsds",)}
WEATHER_VARS = {"temp_C": ("tas",), "wind_ms": ("uas", "vas"),
                "rh_pct": ("hurs",), "precip_mmh": ("pr",), "rsds": ("rsds",)}
FALLBACK_UNITS = {"tas": "K", "uas": "m/s", "vas": "m/s", "hurs": "%",
                  "pr": "kg m-2 s-1", "rsds": "W m-2"}
CALENDARS = {"gregorian": "standard", "standard": "standard",
             "proleptic_gregorian": "proleptic_gregorian",
             "noleap": "365_day", "365_day": "365_day"}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def array_fingerprint(*arrays):
    digest = hashlib.sha256()
    for array in arrays:
        a = np.ascontiguousarray(array)
        digest.update(str(a.dtype).encode())
        digest.update(str(a.shape).encode())
        digest.update(a.tobytes())
    return digest.hexdigest()


def file_identity(path):
    path = Path(path).expanduser().resolve()
    stat = path.stat()
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def sidecar(path):
    return Path(str(path) + ".json")


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = partial_path(path)
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def partial_path(path):
    path = Path(path)
    return path.with_name(path.name + f".partial.{os.getpid()}.{uuid.uuid4().hex}")


@contextmanager
def output_lock(path):
    """Serialize writers of this artifact (also works across local processes)."""
    import fcntl
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def year_range(value):
    if not re.fullmatch(r"\d{4}(?:-\d{4})?", value):
        raise ValueError(f"expected YYYY or YYYY-YYYY, got {value!r}")
    bounds = [int(x) for x in value.split("-")]
    start, end = bounds[0], bounds[-1]
    if not 1 <= start <= end <= 9998:
        raise ValueError(f"invalid year range: {value}")
    return start, end


def year_label(value):
    start, end = year_range(value)
    return f"{start:04d}-{end:04d}"


def year_segments(value, width=5):
    start, end = year_range(value)
    return [f"{y:04d}-{min(y + width - 1, end):04d}" for y in range(start, end + 1, width)]


def final_file(root, model, scenario, variable, patch):
    base = Path(root).expanduser() / "outputs" / model / scenario / variable
    candidates = [base / f"{patch}.nc", base / f"{variable}_{model}_{scenario}_{patch}.nc"]
    candidates += sorted(base.glob(f"*{patch}*.nc"))
    paths = list(dict.fromkeys(p.resolve() for p in candidates if p.is_file()))
    if len(paths) != 1:
        raise FileNotFoundError(f"expected one {variable} final for {model}/{scenario}/{patch}: {paths}")
    path = paths[0]
    meta = json.loads(sidecar(path).read_text())
    expected = {"patch_id": patch, "variable": variable}
    expected.update({k: v for k, v in (("model", model), ("scenario", scenario)) if k in meta})
    if any(meta.get(k) != v for k, v in expected.items()):
        raise ValueError(f"input identity mismatch: {sidecar(path)}")
    return path, {**file_identity(path), "sidecar": meta}


def coord(ds, choices):
    for name in choices:
        if name in ds.coords and ds[name].dims == (name,):
            return name
    raise ValueError(f"missing one-dimensional coordinate: {choices}")


def variable(ds, name):
    for candidate in (name, f"{name}_bcsd"):
        if candidate in ds.data_vars:
            return ds[candidate]
    if len(ds.data_vars) == 1:
        return ds[next(iter(ds.data_vars))]
    raise ValueError(f"cannot identify variable {name}")


def decoded_time(axis):
    calendar = str(axis.attrs.get("calendar", "standard"))
    if calendar not in CALENDARS:
        raise ValueError(f"unsupported calendar {calendar!r}; supported: {sorted(CALENDARS)}")
    raw = np.asarray(axis.values)
    dates = netCDF4.num2date(raw, axis.attrs["units"], calendar,
                            only_use_cftime_datetimes=True)
    hours = np.asarray(netCDF4.date2num(dates, TIME_UNITS, calendar), dtype=np.float64)
    if len(hours) < 2 or not np.all(np.isfinite(hours)) or not np.allclose(
            np.diff(hours), 3.0, rtol=0, atol=1e-8):
        raise ValueError("BCSD time must be strictly increasing, continuous, and spaced by 3 hours")
    return raw, np.asarray(dates), hours, calendar


def pseudo_time(dates):
    # Keep the station algorithm's leap-capable reference year for solar geometry.
    return pd.DatetimeIndex([pd.Timestamp(2000, t.month, t.day, t.hour, t.minute, t.second)
                             for t in dates])


def select_years(dates, value):
    start, end = year_range(value)
    selected = np.flatnonzero([(start <= t.year <= end) for t in dates])
    if not len(selected):
        raise ValueError(f"no reference times in {value}")
    if set(t.year for t in dates[selected]) != set(range(start, end + 1)):
        raise ValueError(f"reference time axis does not cover every year in {value}")
    first, last = dates[selected[0]], dates[selected[-1]]
    # Permit the source's sub-three-hour phase (e.g. 01:30) without accepting
    # an incomplete year as a complete climatological baseline.
    if (first.month, first.day) != (1, 1) or first.hour >= 3 or (
            last.month, last.day) != (12, 31) or last.hour < 21:
        raise ValueError(f"reference time axis has incomplete year coverage for {value}")
    return int(selected[0]), int(selected[-1] + 1)


def tiles(shape, tile_shape):
    ny, nx = shape
    dy, dx = tile_shape
    for y in range(0, ny, dy):
        for x in range(0, nx, dx):
            yield y, min(y + dy, ny), x, min(x + dx, nx)


def tile_slices(bounds):
    y0, y1, x0, x1 = bounds
    return slice(y0, y1), slice(x0, x1)


class GridReader:
    """Open variables once; read rectangular slabs using precomputed time brackets."""
    def __init__(self, args, variables):
        self.stack = ExitStack()
        try:
            self._open(args, variables)
        except BaseException:
            self.close()
            raise

    def _open(self, args, variables):
        self.args = args
        self.files, self.input_identity, self.datasets = {}, {}, {}
        self.axes, self.maps = {}, {}
        for name in variables:
            path, identity = final_file(args.bcsd_root, args.model, args.scenario, name, args.patch)
            self.files[name], self.input_identity[name] = str(path), identity
            self.datasets[name] = self.stack.enter_context(xr.open_dataset(path, decode_times=False))
        self.reference = "uas" if args.tech == "wind" else "rsds"
        ref = self.datasets[self.reference]
        tn = coord(ref, ("time", "valid_time"))
        yn, xn = coord(ref, ("lat", "latitude")), coord(ref, ("lon", "longitude"))
        self.lat, self.lon = np.asarray(ref[yn].values), np.asarray(ref[xn].values)
        for name, values in (("lat", self.lat), ("lon", self.lon)):
            diff = np.diff(values.astype(float))
            if not len(values) or not np.isfinite(values).all() or (len(diff) and not (
                    np.all(diff > 0) or np.all(diff < 0))):
                raise ValueError(f"{name} must be finite and strictly monotone")
        self.lat_attrs, self.lon_attrs = dict(ref[yn].attrs), dict(ref[xn].attrs)
        raw, dates, hours, self.calendar = decoded_time(ref[tn])
        a, b = select_years(dates, args.analysis_years)
        self.times, self.dates, self.hours = raw[a:b], dates[a:b], hours[a:b]
        self.time_attrs = dict(ref[tn].attrs)
        self.time_attrs.setdefault("calendar", self.calendar)
        self.pseudo = pseudo_time(self.dates)
        self.shape = (len(self.lat), len(self.lon))
        self.domain = self._domain(args)
        for name, ds in self.datasets.items():
            t = coord(ds, ("time", "valid_time"))
            y, x = coord(ds, ("lat", "latitude")), coord(ds, ("lon", "longitude"))
            if not np.array_equal(ds[y].values, self.lat) or not np.array_equal(ds[x].values, self.lon):
                raise ValueError(f"grid coordinate mismatch: {name}")
            da = variable(ds, name)
            if set(da.dims) != {t, y, x}:
                raise ValueError(f"{name}: expected time/lat/lon dimensions, got {da.dims}")
            _, _, source, calendar = decoded_time(ds[t])
            if CALENDARS[calendar] != CALENDARS[self.calendar]:
                raise ValueError(f"calendar mismatch: {name}")
            # Strictly bounded source brackets; exact samples do not require a neighbour.
            right = np.searchsorted(source, self.hours, side="left")
            right = np.clip(right, 0, len(source) - 1)
            exact = source[right] == self.hours
            left = np.where(exact, right, np.maximum(right - 1, 0))
            valid = (self.hours >= source[0]) & (self.hours <= source[-1])
            denominator = source[right] - source[left]
            weight = np.zeros(len(self.hours), dtype=np.float64)
            np.divide(self.hours - source[left], denominator, out=weight, where=denominator != 0)
            self.maps[name] = left, right, weight, valid
            self.axes[name] = t, y, x
        self.grid_fingerprint = array_fingerprint(self.lat, self.lon)
        self.mask_fingerprint = array_fingerprint(self.domain)
        self.time_fingerprint = fingerprint({"raw": array_fingerprint(self.times),
                                            "units": self.time_attrs["units"], "calendar": self.calendar})

    def _domain(self, args):
        manifest = json.loads(Path(args.patch_manifest).read_text())
        bbox = manifest["patches"][args.patch]["core_bbox_360"]
        west, east, south, north = map(float, bbox)
        x = np.mod(self.lon - west, 360) + west
        if not ((x >= west) & (x < east)).all() or not (
                (self.lat >= south) & ((self.lat <= north) if north == 90 else (self.lat < north))).all():
            raise ValueError("input grid extends beyond patch core_bbox_360")
        with xr.open_dataset(args.land_plan, decode_times=False) as plan:
            py, px = coord(plan, ("lat", "latitude")), coord(plan, ("lon", "longitude"))
            indexes = []
            for source, target in ((plan[py].values, self.lat), (plan[px].values, self.lon)):
                if len(np.unique(source)) != len(source):
                    raise ValueError("land plan coordinates contain duplicates")
                lookup = {v: i for i, v in enumerate(source)}
                try:
                    indexes.append(np.array([lookup[v] for v in target]))
                except KeyError as exc:
                    raise ValueError("land plan grid does not contain exact BCSD coordinates") from exc
            # land_mask is a small two-dimensional metadata array, not weather data.
            values = plan["land_mask"].transpose(py, px).values[np.ix_(*indexes)]
            if not np.isin(values, [0, 1]).all():
                raise ValueError("land_mask must contain only 0 and 1")
            domain = values.astype(np.int8)
        self.spatial_identity = {"patch_manifest": fingerprint(manifest),
                                 "land_plan": file_identity(args.land_plan)}
        return domain

    def read(self, name, start, stop, bounds):
        left, right, weights, valid = (a[start:stop] for a in self.maps[name])
        sy, sx = tile_slices(bounds)
        k = (sy.stop - sy.start) * (sx.stop - sx.start)
        out = np.full((stop - start, k), np.nan, np.float32)
        if not valid.any():
            return out
        lo, hi = int(left[valid].min()), int(right[valid].max()) + 1
        t, y, x = self.axes[name]
        da = variable(self.datasets[name], name)
        slab = np.asarray(da.isel({t: slice(lo, hi), y: sy, x: sx})
                          .transpose(t, y, x).values, dtype=np.float32).reshape(hi - lo, k)
        positions = np.flatnonzero(valid)
        l, r, w = left[valid] - lo, right[valid] - lo, weights[valid, None]
        # Match float32 xarray/scipy linear interpolation before unit conversion.
        with np.errstate(invalid="ignore"):
            values = slab[r] * w + slab[l] * (1.0 - w)
        exact = l == r
        values[exact] = slab[l[exact]]
        out[positions] = values.astype(np.float32)
        # In particular, humidity clipping must not turn infinity into valid 100%.
        out[~np.isfinite(out)] = np.nan
        unit = da.attrs.get("units") or FALLBACK_UNITS[name]
        converters = {"tas": units.tas_to_celsius, "uas": units.wind_to_ms,
                      "vas": units.wind_to_ms, "hurs": units.hurs_to_pct,
                      "pr": units.pr_to_mmh, "rsds": units.rsds_to_wm2}
        if name == "pr":
            return converters[name](out, unit, timestep_hours=3.0)
        if name == "rsds":
            return converters[name](out, unit, timestep_seconds=10800.0)
        return converters[name](out, unit)

    def weather(self, start, stop, bounds):
        raw = {name: self.read(name, start, stop, bounds) for name in self.datasets}
        result = {}
        for key, variables in WEATHER_VARS.items():
            if all(name in raw for name in variables):
                result[key] = (np.hypot(raw["uas"], raw["vas"]).astype(np.float32)
                               if key == "wind_ms" else raw[variables[0]])
        return result

    def close(self):
        self.stack.close()

    def check_unchanged(self):
        for name, path in self.files.items():
            current = {**file_identity(path), "sidecar": json.loads(sidecar(path).read_text())}
            if current != self.input_identity[name]:
                raise ValueError(f"input changed during calculation: {path}")
        if file_identity(self.args.land_plan) != self.spatial_identity["land_plan"] or fingerprint(
                json.loads(Path(self.args.patch_manifest).read_text())) != self.spatial_identity["patch_manifest"]:
            raise ValueError("spatial inputs changed during calculation")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def create_grid_file(path, reader, bounds, attrs, stage, events=(), interval=None,
                     time_chunk=240, tile_shape=(32, 32), complevel=1):
    """Create a sparse-fill file; callers own and close the returned Dataset."""
    ds = netCDF4.Dataset(path, "w", format="NETCDF4")
    try:
        sy, sx = tile_slices(bounds)
        lat, lon = reader.lat[sy], reader.lon[sx]
        ds.createDimension("lat", len(lat))
        ds.createDimension("lon", len(lon))
        for name, values, metadata in (("lat", lat, reader.lat_attrs), ("lon", lon, reader.lon_attrs)):
            v = ds.createVariable(name, values.dtype, (name,), fill_value=False)
            v[:] = values
            v.setncatts({k: val for k, val in metadata.items() if k != "_FillValue"})
            v.units = "degrees_north" if name == "lat" else "degrees_east"
            v.standard_name = "latitude" if name == "lat" else "longitude"
        spatial_chunk = (min(tile_shape[0], len(lat)), min(tile_shape[1], len(lon)))
        encoding = dict(zlib=complevel > 0, complevel=complevel, shuffle=True)
        mask = ds.createVariable("domain_mask", "i1", ("lat", "lon"), fill_value=False,
                                 chunksizes=spatial_chunk, **encoding)
        mask[:] = reader.domain[sy, sx]
        mask.flag_values = np.array([0, 1], dtype=np.int8)
        mask.flag_meanings = "outside_domain inside_domain"
        if stage == "baseline":
            for name, n in (("month", 12), ("hour", 24)):
                ds.createDimension(name, n)
                v = ds.createVariable(name, "i2", (name,))
                v[:] = np.arange(n) + (1 if name == "month" else 0)
            ds.createVariable("clim288", "f4", ("month", "hour", "lat", "lon"),
                              fill_value=np.nan, chunksizes=(1, 24, *spatial_chunk), **encoding)
            ds.createVariable("p5", "f8", ("lat", "lon"), fill_value=np.nan,
                              chunksizes=spatial_chunk, **encoding)
            ds.createVariable("baseline_valid_count", "i4", ("lat", "lon"), fill_value=-1,
                              chunksizes=spatial_chunk, **encoding)
            resource_units = "m s-1" if reader.args.tech == "wind" else "W m-2"
            ds["clim288"].units = resource_units
            ds["p5"].units = resource_units
        else:
            a, b = interval
            ds.createDimension("time", b - a)
            v = ds.createVariable("time", reader.times.dtype, ("time",), fill_value=False)
            v[:] = reader.times[a:b]
            v.setncatts({k: val for k, val in reader.time_attrs.items() if k != "_FillValue"})
            for event in events:
                v = ds.createVariable("signal_" + event, "i1", ("time", "lat", "lon"),
                                      fill_value=FILL, chunksizes=(min(time_chunk, b - a), *spatial_chunk),
                                      **encoding)
                v.flag_values = np.array([0, 1], dtype=np.int8)
                v.flag_meanings = "no_event event"
        ds.setncatts(attrs)
        return ds
    except BaseException:
        ds.close()
        raise


def complete(path, identity=None):
    """Cheap reuse check: exact identity, file stat and readable native header."""
    path = Path(path)
    try:
        meta = json.loads(sidecar(path).read_text())
        if not isinstance(meta, dict) or not isinstance(meta.get("context"), dict):
            return None
        if meta["status"] != "COMPLETED" or (identity is not None and meta["identity"] != identity):
            return None
        if meta["artifact"] != file_identity(path):
            return None
        with netCDF4.Dataset(path) as ds:
            if ds.getncattr("identity") != meta["identity"]:
                return None
            if {k: len(v) for k, v in ds.dimensions.items()} != meta["dimensions"]:
                return None
            if set(ds.variables) != set(meta["variables"]):
                return None
        return meta
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        return None


def publish(tmp, path, metadata):
    with netCDF4.Dataset(tmp) as ds:
        if ds.getncattr("identity") != metadata["identity"]:
            raise ValueError("artifact identity mismatch before publication")
        metadata = {**metadata, "dimensions": {k: len(v) for k, v in ds.dimensions.items()},
                    "variables": sorted(ds.variables)}
    os.replace(tmp, path)
    atomic_json(sidecar(path), {**metadata, "status": "COMPLETED", "artifact": file_identity(path)})
