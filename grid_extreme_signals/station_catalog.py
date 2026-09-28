"""Validated station locations and construction-year capacity records."""
from __future__ import annotations

from pathlib import Path
import gzip
import io
import os
import numpy as np
import pandas as pd

from . import station_contract as ct
from .station_match import lon_to_180, station_ids


def write_csv(frame, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = ct.temporary(path)
    try:
        with tmp.open("wb") as raw:
            with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
                with io.TextIOWrapper(compressed, encoding="utf-8") as stream:
                    frame.to_csv(stream, index=False, float_format="%.17g")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load_catalog(path):
    return pd.read_csv(path, keep_default_na=False, dtype={"station_id": str}, float_precision="round_trip")


def build_catalog(csv_path, scenario, root):
    root = Path(root)
    source_sha = ct.digest(csv_path)
    identity = ct.fingerprint({"schema": ct.SCHEMA, "csv": source_sha, "scenario": scenario,
                               "implementation": ct.digest(__file__),
                               "station_ids": ct.digest(Path(__file__).with_name("station_match.py"))})
    directory = root / scenario / identity
    done = directory / "catalog.json"
    if done.exists():
        result = ct.read_json(done)
        if all(ct.digest(p) == sha for p, sha in result["files"].items()):
            return result
        raise ValueError("catalog changed after publication")
    frame = pd.read_csv(csv_path)
    required = {"year", "type", "lon", "lat", "capacity_gw"}
    if not required <= set(frame):
        raise ValueError(f"station CSV missing columns: {required - set(frame)}")
    frame = frame[list(sorted(required))].copy()
    frame["type"] = frame.type.astype(str).str.strip().str.lower()
    for name in ("year", "lon", "lat", "capacity_gw"):
        frame[name] = pd.to_numeric(frame[name], errors="coerce")
    invalid = (~np.isfinite(frame[["year", "lon", "lat", "capacity_gw"]])).any(axis=1)
    invalid |= (~frame.type.isin(ct.TECHS) | ~frame.lat.between(-90, 90)
                | (frame.capacity_gw < 0) | (frame.year % 1 != 0)
                | ~frame.year.between(1, 9998))
    if invalid.any():
        write_csv(frame.loc[invalid].assign(source_row=np.flatnonzero(invalid) + 2), directory / "invalid_rows.csv.gz")
        raise ValueError(f"invalid station rows: {int(invalid.sum())}; see {directory}")
    frame["lon"] = lon_to_180(frame.lon)
    frame["year"] = frame.year.astype("int16")
    frame["source_row"] = np.arange(len(frame), dtype="i8") + 2
    duplicate = frame.duplicated(["year", "type", "lon", "lat"], keep=False)
    if duplicate.any():
        write_csv(frame.loc[duplicate], directory / "duplicate_rows.csv.gz")
        raise ValueError("duplicate construction-year station rows")
    result = {"identity": identity, "scenario": scenario, "source": ct.file_stat(csv_path),
              "source_sha256": source_sha, "raw_rows": len(frame), "catalogs": {}, "files": {}}
    capacity_path = directory / "capacity_rows.csv.gz"
    capacity = []
    for tech in ct.TECHS:
        raw = frame.loc[frame.type == tech]
        sites = raw.groupby(["lon", "lat"], sort=True, as_index=False).agg(activation_year=("year", "min"))
        sites["station_id"] = station_ids(scenario, tech, sites.lon, sites.lat)
        sites = sites.sort_values("station_id").reset_index(drop=True)
        capacity.append(raw.merge(sites[["lon", "lat", "station_id"]], on=["lon", "lat"],
                                  how="left", validate="many_to_one"))
        path = directory / tech / "stations.csv.gz"
        write_csv(sites, path)
        sha = ct.digest(path)
        result["catalogs"][tech] = {"path": str(path.resolve()), "sha256": sha, "count": len(sites)}
        result["files"][str(path.resolve())] = sha
    write_csv(pd.concat(capacity).sort_values("source_row"), capacity_path)
    result["files"][str(capacity_path.resolve())] = ct.digest(capacity_path)
    if ct.digest(csv_path) != source_sha:
        raise ValueError("station CSV changed while constructing catalog")
    ct.atomic_json(done, result)
    return result
