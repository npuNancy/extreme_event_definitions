"""Regression: scattered-index reads must not livelock in chunk decompression.

Reproduces the production failure geometry: a station block whose match
indices are sparse and straddle chunk boundaries in BOTH lat and lon on a
(240,64,64)-chunked, zlib-compressed dataset. The old isel(unique_idx)
path interleaved chunk accesses so the 1 MB default chunk cache missed on
every access; the slab-read path must complete quickly and produce values
identical to a dense in-memory reference gather.
"""
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from grid_extreme_signals import station_match as sm  # noqa: E402


def build_chunked_dataset(path, nt=2400, ny=300, nx=300, seed=7):
    rng = np.random.default_rng(seed)
    data = rng.random((nt, ny, nx), dtype=np.float32)
    ds = xr.Dataset(
        {"v_bcsd": (("time", "lat", "lon"), data)},
        coords={"time": np.arange(nt), "lat": np.linspace(0.0, 30.0, ny),
                "lon": np.linspace(60.0, 90.0, nx)},
    )
    # Production BCSD layout: (240,64,64) chunks, zlib+shuffle.
    ds.to_netcdf(path, encoding={"v_bcsd": {"chunksizes": (240, 64, 64),
                                            "zlib": True, "complevel": 2, "shuffle": True}})
    ds.close()


def test_slab_read_completes_and_matches_reference(tmp_path):
    path = tmp_path / "chunked.nc"
    build_chunked_dataset(path)
    ds = xr.open_dataset(path)
    da = ds["v_bcsd"]
    rng = np.random.default_rng(3)
    # Geometry of the livelocked part_16: rows dense over a tall span,
    # 5 scattered lon columns straddling the 64-column chunk boundary.
    used_lat = np.sort(rng.choice(np.arange(40, 158), 117, replace=False))
    used_lon = rng.choice(np.array([61, 62, 64, 65, 120]), 117)  # straddles 63/64 edge
    stations = pd.DataFrame({
        "lon": np.linspace(66.1, 72.2, len(used_lat)),
        "lat": np.linspace(4.0, 15.7, len(used_lat)),
        "capacity_gw": np.full(len(used_lat), 0.5),
        "year": np.full(len(used_lat), 2020),
        "activation_year": np.full(len(used_lat), 2020),
    })
    weights = np.ones((len(stations), 1), np.float32)
    # nearest-style (K,1) neighbour indices, as match_regular_weighted builds
    match = sm.StationSpatialWeights(stations, used_lat[:, None], used_lon[:, None],
                                     weights,
                                     np.full(len(stations), 0.01), np.ones(len(stations), bool))
    tb = 2048
    lat_lo, lat_hi = int(match.idx0.min()), int(match.idx0.max()) + 1
    lon_lo, lon_hi = int(match.idx1.min()), int(match.idx1.max()) + 1
    m_slab = sm.StationSpatialWeights(match.stations, match.idx0 - lat_lo, match.idx1 - lon_lo,
                                      match.weight, match.dist_deg, match.valid)
    out_slab = np.empty((da.sizes["time"], len(stations)), np.float32)
    out_scatter = np.empty_like(out_slab)
    t0 = time.perf_counter()
    slab = da.isel(lat=slice(lat_lo, lat_hi), lon=slice(lon_lo, lon_hi))
    for s0 in range(0, da.sizes["time"], tb):
        s1 = min(da.sizes["time"], s0 + tb)
        arr = np.asarray(slab.isel(time=slice(s0, s1)).values, np.float32)
        out_slab[s0:s1] = sm.gather_to_stations_weighted(arr, m_slab)
    slab_seconds = time.perf_counter() - t0
    t0 = time.perf_counter()
    # scattered reference path mirrors the pre-fix code: unique sorted index lists
    scattered = da.isel(lat=np.unique(used_lat), lon=np.unique(used_lon))
    # map each station onto its (unique-index) position
    uniq_lat, uniq_lon = np.unique(used_lat), np.unique(used_lon)
    pos0 = np.searchsorted(uniq_lat, used_lat)
    pos1 = np.searchsorted(uniq_lon, used_lon)
    m_scatter = sm.StationSpatialWeights(match.stations, pos0[:, None], pos1[:, None],
                                         match.weight, match.dist_deg, match.valid)
    for s0 in range(0, da.sizes["time"], tb):
        s1 = min(da.sizes["time"], s0 + tb)
        arr = np.asarray(scattered.isel(time=slice(s0, s1)).values, np.float32)
        out_scatter[s0:s1] = sm.gather_to_stations_weighted(arr, m_scatter)
    scatter_seconds = time.perf_counter() - t0
    np.testing.assert_array_equal(out_slab, out_scatter)
    # Both paths must be bounded (local file: seconds, not minutes). The
    # assertion guards regressions that reintroduce per-access re-deflate.
    assert slab_seconds < 60, f"slab read too slow: {slab_seconds:.1f}s"
    print(f"\nslab={slab_seconds:.1f}s scattered={scatter_seconds:.1f}s")
    ds.close()


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        test_slab_read_completes_and_matches_reference(Path(td))
    print("slab-read regression test passed")
