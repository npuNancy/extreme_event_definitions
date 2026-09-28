"""Periodic regular-grid nearest mapping, independent of event values."""
from __future__ import annotations

import os
from pathlib import Path
import netCDF4
import numpy as np

from . import station_contract as ct
from .grid_io import array_fingerprint
from .station_catalog import load_catalog, write_csv
from .station_match import lon_to_180


def grid_metadata(path):
    with netCDF4.Dataset(path) as ds:
        ds.set_auto_maskandscale(False)
        lat, lon = np.asarray(ds["lat"][:]), np.asarray(ds["lon"][:])
        mask = np.asarray(ds["domain_mask"][:])
        for axis in (lat, lon):
            delta = np.diff(axis)
            if (axis.ndim != 1 or not len(axis) or not np.isfinite(axis).all()
                    or (len(delta) and not (np.all(delta > 0) or np.all(delta < 0)))):
                raise ValueError("grid axes must be finite, unique and monotone")
        if mask.shape != (len(lat), len(lon)) or not np.isin(mask, [0, 1]).all():
            raise ValueError("invalid domain mask")
        return {"lat": lat, "lon": lon, "mask": mask,
                "fingerprint": array_fingerprint(lat, lon, mask)}


def _lattice(grids, name):
    values = np.unique(np.concatenate([g[name] for g in grids.values()]).astype(float))
    if name == "lon":
        values = np.unique(lon_to_180(values))
    steps = np.diff(values)
    if not len(steps):
        raise ValueError("need at least two coordinates to infer regular grid spacing")
    step = float(steps.min())
    if step <= 0 or not np.allclose(steps / step, np.rint(steps / step), atol=1e-5, rtol=0):
        raise ValueError("grid is not a common regular lattice")
    if name == "lon" and not np.isclose(360 / step, round(360 / step), atol=1e-5):
        raise ValueError("longitude lattice does not close periodically")
    lower, upper = (-180., 180.) if name == "lon" else (-90., 90.)
    first = values[0] + np.ceil((lower - values[0] - 1e-8) / step) * step
    count = int(np.floor((upper - first + 1e-8) / step)) + 1
    axis = first + np.arange(count) * step
    return axis[axis < 180 - 1e-8] if name == "lon" else axis


def nearest_axis(axis, positions, periodic=False):
    """Search two bracketing cells; ties choose smaller normalized coordinate."""
    axis = np.asarray(axis, float)
    pos = np.asarray(positions, float)
    k = np.searchsorted(axis, pos)
    lo = (k - 1) % len(axis) if periodic else np.maximum(k - 1, 0)
    hi = k % len(axis) if periodic else np.minimum(k, len(axis) - 1)
    a, b = axis[lo], axis[hi]
    da, db = abs(a - pos), abs(b - pos)
    if periodic:
        da, db = abs(lon_to_180(a - pos)), abs(lon_to_180(b - pos))
    use_a = (da < db - 1e-8) | ((abs(da - db) <= 1e-8) & (a < b))
    return np.where(use_a, a, b)


def _local(axis, target, periodic=False):
    axis = lon_to_180(axis) if periodic else np.asarray(axis, float)
    order = np.argsort(axis)
    sorted_axis = axis[order]
    k = np.clip(np.searchsorted(sorted_axis, target), 0, len(axis) - 1)
    prev = np.maximum(k - 1, 0)
    k = np.where(abs(sorted_axis[prev] - target) < abs(sorted_axis[k] - target), prev, k)
    return order[k], np.isclose(sorted_axis[k], target, atol=1e-6, rtol=0)


def make_mapping(sites, grids, manifest, max_distance=0.15):
    if not np.isfinite(max_distance) or max_distance < 0:
        raise ValueError("invalid maximum matching distance")
    out = sites.copy()
    sy, sx = sites.lat.to_numpy(float), sites.lon.to_numpy(float)
    y = nearest_axis(_lattice(grids, "lat"), sy)
    x = nearest_axis(_lattice(grids, "lon"), sx, periodic=True)
    dy, dx = abs(y - sy), abs(lon_to_180(x - sx))
    dist = np.maximum(dy, dx)
    angle = (np.sin(np.deg2rad(y - sy) / 2) ** 2
             + np.cos(np.deg2rad(y)) * np.cos(np.deg2rad(sy)) * np.sin(np.deg2rad(dx) / 2) ** 2)
    out["source_grid_lat"], out["source_grid_lon"] = y, x
    out["match_dist_deg"] = dist
    out["match_dist_km"] = 6371.0088 * 2 * np.arcsin(np.sqrt(np.clip(angle, 0, 1)))
    out["source_iy"], out["source_ix"] = -1, -1
    out["domain_mask"] = -1
    out["source_patch"], out["station_patch"] = "", ""
    out["mapping_status"] = ct.STATUS["NO_SOURCE_PATCH"]
    for patch, g in sorted(grids.items()):
        w, e, s, n = manifest["patches"][patch]["core_bbox_360"]
        grid_x = (g["lon"] - w) % 360 + w
        if not ((grid_x >= w - 1e-8) & (grid_x < e - 1e-8)).all() or not (
                (g["lat"] >= s - 1e-8) & ((g["lat"] <= n + 1e-8) if n == 90 else (g["lat"] < n - 1e-8))).all():
            raise ValueError(f"grid outside patch core: {patch}")
        iy, valid_y = _local(g["lat"], y)
        ix, valid_x = _local(g["lon"], x, periodic=True)
        keep = valid_y & valid_x
        if (out.loc[keep, "source_patch"] != "").any():
            raise ValueError("duplicate grid coordinates in patch cores")
        out.loc[keep, "source_patch"] = patch
        out.loc[keep, "source_iy"] = iy[keep]
        out.loc[keep, "source_ix"] = ix[keep]
        domain = g["mask"][iy[keep], ix[keep]]
        out.loc[keep, "domain_mask"] = domain
        out.loc[keep, "mapping_status"] = np.where(domain == 1, ct.STATUS["MATCHED"], ct.STATUS["OUTSIDE_DOMAIN"])
    out.loc[dist > max_distance, "mapping_status"] = ct.STATUS["TOO_FAR"]
    out.loc[dist > max_distance, "source_patch"] = ""
    for patch, spec in manifest["patches"].items():
        w, e, s, n = spec["core_bbox_360"]
        longitude = (sx - w) % 360 + w
        keep = (longitude >= w) & (longitude < e) & (sy >= s) & ((sy <= n) if n == 90 else (sy < n))
        if (out.loc[keep, "station_patch"] != "").any():
            raise ValueError("overlapping patch core bboxes")
        out.loc[keep, "station_patch"] = patch
    return out


def write_mapping(frame, path, identity, grid_hash):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = ct.temporary(path)
    strings = {"station_id", "station_patch", "source_patch"}
    integers = {"activation_year": "i2", "source_iy": "i4", "source_ix": "i4",
                "mapping_status": "i1", "domain_mask": "i1"}
    try:
        with netCDF4.Dataset(tmp, "w") as ds:
            ds.createDimension("station", len(frame))
            for name in frame:
                dtype = str if name in strings else integers.get(name, "f8")
                var = ds.createVariable(name, dtype, ("station",))
                var[:] = frame[name].to_numpy(dtype=str if dtype is str else dtype)
            ds.setncatts({"identity": identity, "grid_fingerprint": grid_hash})
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def build_mapping(catalog, grids, manifest, root, max_distance=0.15):
    contract = {"schema": ct.SCHEMA, "catalog": catalog["sha256"], "implementation": ct.digest(__file__),
                "grids": {p: g["fingerprint"] for p, g in sorted(grids.items())},
                "manifest": ct.fingerprint(manifest), "method": "nearest_regular",
                "max_distance_deg": max_distance}
    identity = ct.fingerprint(contract)
    directory = Path(root) / identity
    done = directory / "mapping.json"
    if done.exists():
        result = ct.read_json(done)
        if all(ct.file_stat(p) == stat for p, stat in result["files"].items()):
            return result
        raise ValueError("published mapping changed")
    if ct.digest(catalog["path"]) != catalog["sha256"]:
        raise ValueError("catalog changed")
    frame = make_mapping(load_catalog(catalog["path"]), grids, manifest, max_distance)
    coverage = directory / "coverage.csv.gz"
    write_csv(frame, coverage)
    counts = {name: int((frame.mapping_status == code).sum()) for name, code in ct.STATUS.items()}
    result = {"identity": identity, "contract": contract, "catalog": catalog,
              "counts": counts, "coverage": str(coverage.resolve()), "patches": {}, "files": {}}
    for patch, g in grids.items():
        sub = frame.loc[frame.source_patch == patch].sort_values(["source_iy", "source_ix", "station_id"])
        record = {"count": len(sub), "grid_fingerprint": g["fingerprint"], "path": None}
        if len(sub):
            path = directory / f"{patch}.nc"
            write_mapping(sub, path, identity, g["fingerprint"])
            record["path"] = str(path.resolve())
            result["files"][str(path.resolve())] = ct.file_stat(path)
        result["patches"][patch] = record
    result["files"][str(coverage.resolve())] = ct.file_stat(coverage)
    result["cross_patch_count"] = int(((frame.source_patch != "") & (frame.source_patch != frame.station_patch)).sum())
    result["distance_counts"] = {"exact": int((frame.match_dist_deg <= 1e-6).sum()),
                                 "within_half_cell": int((frame.match_dist_deg <= 0.050001).sum()),
                                 "beyond_half_cell": int((frame.match_dist_deg > 0.050001).sum())}
    ct.atomic_json(done, result)
    return result
