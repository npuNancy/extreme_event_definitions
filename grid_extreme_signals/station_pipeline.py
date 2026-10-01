"""Preparation, resumable combination extraction and index publication."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from . import station_contract as ct
from .station_catalog import build_catalog, write_csv
from .station_mapping import grid_metadata, build_mapping
from .station_extract import extract_file, audit_combination


def prepare(campaign, root, code_sha):
    root = Path(root).resolve()
    index = ct.read_json(campaign["input_index"])
    manifest = ct.read_json(campaign["patch_manifest"])
    index_sha = ct.digest(campaign["input_index"])
    sources = ct.combinations(index, campaign["models"])
    station_files = {}
    for label, path in campaign["stations"].items():
        station = ct.scenario(label, station=True)
        if station in station_files:
            raise ValueError("duplicate Station file alias")
        station_files[station] = path
    if not station_files:
        raise ValueError("empty Station scenarios")
    if index["analysis_years"] != campaign["analysis_years"]:
        raise ValueError("campaign and input analysis years differ")
    root.mkdir(parents=True, exist_ok=True)
    with ct.lock(root / ".prepare.lock"):
        catalogs = {ssp: build_catalog(path, ssp, root / "catalogs")
                    for ssp, path in sorted(station_files.items())}
        groups = defaultdict(dict)
        for c in sources.values():
            frozen = []
            for a in c["signals"]:
                path = a["path"]
                meta = ct.read_json(path + ".json")
                if meta.get("status") != "COMPLETED" or meta.get("context", {}).get("stage") != "signals":
                    raise ValueError(f"source is not a completed signals artifact: {path}")
                frozen.append({"path": path, "years": a["years"], "stat": ct.file_stat(path),
                               "sidecar_sha256": ct.digest(path + ".json"), "source_identity": meta["identity"],
                               "index_sha256": index_sha})
            c["signals"] = frozen
            group = (c["model"], c["scenario"], c["tech"])
            groups[group][c["patch"]] = grid_metadata(frozen[0]["path"])
        combos = ct.station_combinations(sources, station_files)
        mappings = {}
        for (model, climate, tech), grids in sorted(groups.items()):
            for station in catalogs:
                m = build_mapping(catalogs[station]["catalogs"][tech], grids, manifest,
                                  root / "mappings", campaign["max_distance_deg"])
                mappings[m["identity"]] = m
                for patch in grids:
                    key = ct.combo_key(dict(model=model, climate_scenario=climate,
                                            station_scenario=station, patch=patch, tech=tech))
                    combos[key]["mapping_identity"] = m["identity"]
        release = {"schema": ct.DUAL_SCENARIO_SCHEMA, "code_sha": code_sha,
                   "index_sha256": index_sha, "patch_manifest_sha256": ct.digest(campaign["patch_manifest"]),
                   "catalog_identities": {s: c["identity"] for s, c in catalogs.items()},
                   "mapping_identities": sorted(mappings),
                   "campaign": campaign}
        prepared = {"identity": ct.fingerprint(release), "release": release, "catalogs": catalogs,
                    "mappings": mappings, "combinations": combos}
        if ct.digest(campaign["input_index"]) != index_sha:
            raise ValueError("input index changed during preparation")
        ct.atomic_json(root / "input_index.json", index)
        ct.atomic_json(root / "patch_manifest.json", manifest)
        ct.atomic_json(root / "prepared.json", prepared)
        return prepared


def verify_mapping(mapping):
    for path, stat in mapping["files"].items():
        if ct.file_stat(path) != stat:
            raise ValueError(f"mapping/coverage changed: {path}")


def extract_combination(prepared, key, output_root, code_sha, prior=None, years=None):
    if code_sha != prepared["release"]["code_sha"]:
        raise ValueError("extract code SHA differs from prepared release")
    c = prepared["combinations"][key]
    mapping = prepared["mappings"][c["mapping_identity"]]
    verify_mapping(mapping)
    info = mapping["patches"][c["patch"]]
    config = prepared["release"]["campaign"]["extraction"]
    records = []
    prior = {r["period"]: r for r in (prior or [])}
    artifacts = [a for a in c["signals"] if years is None or a["years"] == years]
    if not artifacts:
        raise ValueError("requested period absent from input index")
    if not info["count"]:
        return {"status": "SKIPPED_NO_STATIONS", "outputs": [], "mapping_identity": mapping["identity"], "key": key}
    for a in artifacts:
        old = prior.get(a["years"])
        if old:
            # Re-enter the extractor to check exact contract, source and completion.
            path = old["artifact"]["path"]
        else:
            path = Path(output_root) / key / f"signals_{a['years']}.nc"
        record = extract_file(a, c, info, mapping["identity"], path,
                              prepared["identity"], code_sha, **config,
                              catalog_sha256=mapping["catalog"]["sha256"],
                              max_distance_deg=mapping["contract"]["max_distance_deg"])
        records.append(record)
        ct.atomic_json(Path(output_root) / key / "extraction.json",
                       {"status": "PARTIAL", "key": key, "outputs": records,
                        "release_identity": prepared["identity"]})
    result = {"status": "COMPLETED", "outputs": records, "key": key,
              "mapping_identity": mapping["identity"], "release_identity": prepared["identity"]}
    ct.atomic_json(Path(output_root) / key / "extraction.json", result)
    return result


def audit(prepared, key, extraction, output):
    c = prepared["combinations"][key]
    mapping = prepared["mappings"][c["mapping_identity"]]
    verify_mapping(mapping)
    if extraction["key"] != key or extraction["mapping_identity"] != mapping["identity"]:
        raise ValueError("audit extraction dependency identity mismatch")
    return audit_combination(c, mapping, extraction["outputs"], output)


def publish(prepared, audits, root):
    """Publish only small indexes; scientific audit must already have succeeded."""
    expected = set(prepared["combinations"])
    if set(audits) != expected:
        raise ValueError("publication requires every expected combination audit")
    index = {"schema": ct.DUAL_SCENARIO_SCHEMA, "kind": "station-event-index", "identity": prepared["identity"],
             "release": prepared["release"], "catalogs": prepared["catalogs"],
             "mappings": prepared["mappings"], "combinations": {}}
    coverage = []
    group_totals = defaultdict(lambda: {"completed_combinations": 0, "empty_combinations": 0, "events": {}})
    for key, audit_path in sorted(audits.items()):
        result = ct.read_json(audit_path)
        c = prepared["combinations"][key]
        if result["status"] not in ("COMPLETED", "SKIPPED_NO_STATIONS") or ct.combo_key(result["combination"]) != key:
            raise ValueError("invalid audit identity/status")
        if result["mapping_identity"] != c["mapping_identity"]:
            raise ValueError("audit mapping identity mismatch")
        totals = group_totals[(c["model"], c["climate_scenario"], c["station_scenario"], c["tech"])]
        totals["completed_combinations"] += 1
        totals["empty_combinations"] += result["status"] == "SKIPPED_NO_STATIONS"
        for r in result["outputs"]:
            if not ct.completed(r["artifact"]["path"], r["identity"]):
                raise ValueError("published output changed after audit")
            for event, stats in r["statistics"].items():
                target = totals["events"].setdefault(event, dict(event_count=0, valid_count=0, missing_count=0))
                for field in target:
                    target[field] += stats[field]
        index["combinations"][key] = {**{k: c[k] for k in ("model", "climate_scenario", "station_scenario", "patch", "tech")},
                                      "audit": ct.file_stat(audit_path), "status": result["status"],
                                      "outputs": result["outputs"], "mapping_identity": c["mapping_identity"]}
    seen = set()
    for c in prepared["combinations"].values():
        group = (c["model"], c["climate_scenario"], c["station_scenario"], c["tech"])
        if group in seen:
            continue
        seen.add(group)
        m = prepared["mappings"][c["mapping_identity"]]
        verify_mapping(m)
        if sum(m["counts"].values()) != m["catalog"]["count"]:
            raise ValueError("station conservation failed")
        totals = group_totals[group]
        distances = m["distance_counts"]
        summary = dict(zip(("model", "climate_scenario", "station_scenario", "tech"), group), **m["counts"],
                       station_count=m["catalog"]["count"], cross_patch_count=m["cross_patch_count"],
                       ssp_source_rows=prepared["catalogs"][c["station_scenario"]]["raw_rows"],
                       completed_combinations=totals["completed_combinations"], empty_combinations=totals["empty_combinations"],
                       distance_exact=distances["exact"],
                       distance_within_half_cell_nonexact=distances["within_half_cell"] - distances["exact"],
                       distance_beyond_half_cell=distances["beyond_half_cell"],
                       matched_station_fraction=m["counts"]["MATCHED"] / m["catalog"]["count"] if m["catalog"]["count"] else None)
        for event, stats in sorted(totals["events"].items()):
            summary.update({event + "_" + field: count for field, count in stats.items()})
            total = stats["valid_count"] + stats["missing_count"]
            summary[event + "_valid_fraction_in_outputs"] = stats["valid_count"] / total if total else None
            summary[event + "_missing_fraction_in_outputs"] = stats["missing_count"] / total if total else None
        coverage.append(summary)
    import pandas as pd
    root = Path(root)
    write_csv(pd.DataFrame(coverage), root / "runtime/coverage_summary.csv.gz")
    ct.atomic_json(root / "runtime/authoritative_index.json", index)
    return index
