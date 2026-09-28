"""Subset station shards by stable IDs while retaining fill and native calendars."""
from __future__ import annotations

from pathlib import Path
import numpy as np
import xarray as xr

from . import station_contract as ct
from .station_catalog import load_catalog


def open_station_signals(index, model, scenario, tech, station_ids=None, years=None):
    """Return a subset Dataset; caller must close it. Does not require Dask.

    Select station_ids/years for bounded memory. Concatenation may materialize
    selected arrays; this convenience reader is not a global streaming reducer.
    """
    index = ct.read_json(index) if isinstance(index, (str, Path)) else index
    if index.get("kind") != "station-event-index":
        raise ValueError("expected station-event-index")
    catalog_info = index["catalogs"][scenario]["catalogs"][tech]
    if ct.digest(catalog_info["path"]) != catalog_info["sha256"]:
        raise ValueError("catalog identity changed")
    catalog = load_catalog(catalog_info["path"]).set_index("station_id")
    ids = list(catalog.index) if station_ids is None else list(station_ids)
    if len(set(ids)) != len(ids) or set(ids) - set(catalog.index):
        raise ValueError("duplicate or unknown station IDs")
    period = ct.years(years) if years else None
    selected = [(key, c) for key, c in sorted(index["combinations"].items())
                if tuple(key.split("/")[i] for i in (0, 1, 3)) == (model, scenario, tech)]
    mapping_ids = {c["mapping_identity"] for _, c in selected}
    if len(mapping_ids) != 1:
        raise ValueError("missing or inconsistent mappings within model/scenario/tech")
    mapping = index["mappings"][next(iter(mapping_ids))]
    if ct.file_stat(mapping["coverage"]) != mapping["files"][mapping["coverage"]]:
        raise ValueError("mapping coverage changed")
    coverage = load_catalog(mapping["coverage"]).set_index("station_id").loc[ids]
    wanted_patches = set(coverage.source_patch) - {""}
    if wanted_patches:
        selected = [(key, c) for key, c in selected if key.split("/")[2] in wanted_patches]
    else:
        selected = next(([(key, c)] for key, c in selected if c["outputs"]), [])
    opened, patches = [], []
    try:
        for key, c in selected:
            shards = []
            for record in c["outputs"]:
                a, b = ct.years(record["period"])
                if period and (b < period[0] or a > period[1]):
                    continue
                path = record["artifact"]["path"]
                if not ct.completed(path, record["identity"]):
                    raise ValueError("station shard changed")
                ds = xr.open_dataset(path)
                opened.append(ds)
                if period:
                    ds = ds.isel(time=(ds.time.dt.year >= period[0]) & (ds.time.dt.year <= period[1]))
                if not ds.sizes["time"]:
                    continue
                positions = np.flatnonzero(np.isin(ds.station_id.values, ids))
                if len(positions):
                    sub = ds.isel(station=positions).swap_dims({"station": "station_id"}).drop_vars("station")
                else:
                    # netCDF4 empty fancy indexing can collapse the time axis;
                    # build the empty subset from metadata instead of reading it.
                    sub = xr.Dataset(coords={"time": ds.time.load(), "station_id": np.array([], dtype=str)})
                    for name in ds.data_vars:
                        if name.startswith("signal_"):
                            sub[name] = (("time", "station_id"), np.empty((ds.sizes["time"], 0), np.float32), ds[name].attrs)
                # Keep empty station shards to recover the full common native time axis.
                shards.append(sub.load())
                ds.close()
            if shards:
                one = xr.concat(shards, dim="time", data_vars="minimal", coords="minimal", compat="equals", join="exact")
                if patches and not np.array_equal(patches[0].time.values, one.time.values):
                    raise ValueError("patch time axes differ")
                patches.append(one)
        if not patches:
            raise ValueError("no event data for selected model/scenario/tech/years")
        nonempty = [p for p in patches if p.sizes["station_id"]]
        result = (xr.concat(nonempty, dim="station_id", data_vars="minimal", coords="minimal", compat="equals", join="exact")
                  if nonempty else patches[0])
        if len(set(result.station_id.values)) != result.sizes["station_id"]:
            raise ValueError("station duplicated across patches")
        result = result.reindex(station_id=ids)
        for name in ("lon", "lat", "activation_year", "mapping_status", "source_patch", "station_patch"):
            result = result.assign_coords({name: ("station_id", coverage[name].to_numpy())})
        result = result.rename({"station_id": "station"}).assign_coords(station_id=("station", ids), station=np.arange(len(ids)))
        result.attrs.update(model=model, scenario=scenario, tech=tech, activation_mask="off")
        for stale in ("source_patch", "identity", "source_file", "source_realpath", "source_signal_identity"):
            result.attrs.pop(stale, None)
        result.set_close(lambda: [ds.close() for ds in opened])
        return result
    except BaseException:
        for ds in opened:
            ds.close()
        raise
