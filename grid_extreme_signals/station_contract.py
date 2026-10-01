"""Standard-library contracts shared by station extraction and job tooling."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

SCHEMA = "station-extreme-v1"
DUAL_SCENARIO_SCHEMA = "station-extreme-v2"
FILL = -127
MODELS = ("CANESM5", "MPI-ESM1-2-HR", "MRI-ESM2-0", "BCC-CSM2-MR")
SCENARIOS = ("ssp126", "ssp245", "ssp585")
TECHS = ("wind", "solar")
STATUS = {"MATCHED": 0, "OUTSIDE_DOMAIN": 1, "TOO_FAR": 2, "NO_SOURCE_PATCH": 3}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def file_stat(path):
    p = Path(path).resolve(strict=True)
    s = p.stat()
    return {"path": str(p), "size": s.st_size, "mtime_ns": s.st_mtime_ns}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = temporary(path)
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def temporary(path):
    p = Path(path)
    return p.with_name(p.name + ".partial." + uuid.uuid4().hex)


@contextmanager
def lock(path):
    """Nonblocking lock; a second writer must not wait and overwrite the first."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def safe_name(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", str(value)):
        raise ValueError(f"unsafe name: {value!r}")
    return value


def years(value):
    if not re.fullmatch(r"\d{4}-\d{4}", value):
        raise ValueError(f"expected YYYY-YYYY: {value}")
    a, b = map(int, value.split("-"))
    if not 1 <= a <= b <= 9998:
        raise ValueError(f"invalid period: {value}")
    return a, b


def combo_key(c):
    if "climate_scenario" in c or "station_scenario" in c:
        climate = scenario(c["climate_scenario"])
        station = scenario(c["station_scenario"], station=True)
        return "/".join(safe_name(v) for v in
                        (c["model"], f"climate_{climate}", f"station_{station}", c["patch"], c["tech"]))
    return "/".join(safe_name(c[k]) for k in ("model", "scenario", "patch", "tech"))


def scenario(value, *, station=False):
    value = "ssp585" if station and value == "ssp560" else value
    if value not in SCENARIOS:
        raise ValueError(f"unsupported {'Station' if station else 'Climate'}: {value}")
    return value


def station_combinations(sources, station_scenarios):
    stations = [scenario(s, station=True) for s in station_scenarios]
    if not stations or len(set(stations)) != len(stations):
        raise ValueError("empty or duplicate Station scenarios")
    result = {}
    for source in sources.values():
        for station in stations:
            c = {k: v for k, v in source.items() if k != "scenario"}
            c.update(climate_scenario=scenario(source["scenario"]), station_scenario=station)
            key = combo_key(c)
            if key in result:
                raise ValueError(f"duplicate station combination: {key}")
            result[key] = c
    return dict(sorted(result.items()))


def combinations(index, models=None):
    if index.get("kind") != "grid-v2-unified-index" or index.get("schema_version") != 1:
        raise ValueError("expected grid-v2 unified index schema 1")
    selected = set(models or index["selected_models"])
    lo, hi = years(index["analysis_years"])
    result = {}
    for c in index["combinations"].values():
        if c["model"] not in selected:
            continue
        if c["scenario"] not in SCENARIOS or c["tech"] not in TECHS:
            raise ValueError("unsupported scenario/technology")
        key = combo_key(c)
        if key in result:
            raise ValueError(f"duplicate combination: {key}")
        signals = sorted((dict(a) for a in c["artifacts"] if a["stage"] == "signals"),
                         key=lambda a: years(a["years"]))
        expected = lo
        for a in signals:
            start, end = years(a["years"])
            if start != expected or end > hi:
                raise ValueError(f"overlapping/missing source periods: {key}")
            expected = end + 1
            a["path"] = a.get("unified_output", a["output"])
        if expected != hi + 1:
            raise ValueError(f"incomplete source coverage: {key}")
        result[key] = {k: c[k] for k in ("model", "scenario", "patch", "tech", "source_run_id")}
        result[key]["signals"] = signals
    if {c["model"] for c in result.values()} != selected:
        raise ValueError("selected model absent from source index")
    return dict(sorted(result.items()))


def completed(path, identity=None):
    try:
        meta = read_json(str(path) + ".json")
        if meta["status"] != "COMPLETED" or meta["artifact"] != file_stat(path):
            return None
        if identity is not None and meta["identity"] != identity:
            return None
        return meta
    except (OSError, ValueError, KeyError, TypeError):
        return None


def require_frozen(artifact):
    if file_stat(artifact["path"]) != artifact["stat"]:
        raise ValueError(f"source changed: {artifact['path']}")
    if digest(artifact["path"] + ".json") != artifact["sidecar_sha256"]:
        raise ValueError(f"source sidecar changed: {artifact['path']}")
