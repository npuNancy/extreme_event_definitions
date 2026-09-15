"""Multiprocess station-block equivalence and part/merge contract tests.

Runs the same synthetic patch through --processes 1 and --processes N and
compares every signal bit; also checks part sidecars, retry semantics, and
that the serial output contract (attrs, coords promotion in the loss reader
sense) survives the streaming merge.
"""
import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_streaming_signals_equivalence import build  # noqa: E402


def _run(root: Path, tech: str, processes: int, extra_out: str = "out",
         overwrite: bool = False, reuse_parts_root: Path | None = None,
         block_size: int | None = None):
    import station_signals_patchify as ssp
    tech_csv = root / f"stations_{tech}.csv"
    rows = pd.read_csv(root / "stations.csv"); rows["type"] = tech
    rows.to_csv(tech_csv, index=False)
    out_root = root / extra_out
    args = type("A", (), {})()
    args.bcsd_root = str(root / "bcsd"); args.model = "M"; args.scenario = "ssp126"
    args.patch = "P1"; args.patch_manifest = str(root / "patch_manifest.json")
    args.stations_csv = str(tech_csv); args.tech = tech
    args.years = "2015-2060"; args.output_root = str(out_root)
    args.spatial_method = "nearest"; args.max_distance_deg = 0.15
    args.processes = processes; args.station_block_size = block_size
    args.parts_root = str(reuse_parts_root) if reuse_parts_root else None
    args.overwrite = overwrite; args.timing_report = False
    ssp.run(args)
    return out_root / "M" / "ssp126" / "P1" / f"{tech}.nc"


def _open(ds_path: Path):
    return xr.open_dataset(ds_path)


@pytest.mark.parametrize("tech", ["wind", "solar"])
@pytest.mark.parametrize("processes", [2, 4])
def test_multiprocess_bitwise_equal(tech, processes):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        build(root)
        serial = _run(root, tech, 1, "out_serial")
        par = _run(root, tech, processes, "out_par")
        with _open(serial) as a, _open(par) as b:
            assert set(a.variables) == set(b.variables), f"variable sets differ: {set(a.variables) ^ set(b.variables)}"
            assert np.array_equal(a.time.values, b.time.values)
            for name in sorted(set(a.variables) & {"station_id", "lon", "lat", "capacity_gw", "activation_year", "match_dist_deg"}):
                got_a = a[name].values
                got_b = b[name].values
                if name == "station_id":
                    np.testing.assert_array_equal(np.asarray(got_a, str), np.asarray(got_b, str), err_msg=name)
                else:
                    np.testing.assert_array_equal(got_a, got_b, err_msg=name)
            events = [n for n in a.variables if n.startswith("signal_")]
            assert events, "no signal variables found"
            for name in events:
                np.testing.assert_array_equal(a[name].values, b[name].values, err_msg=f"{name} differs")
            assert sorted(str(a.attrs["supported_events"]).split(",")) == sorted(str(b.attrs["supported_events"]).split(","))
            assert str(a.attrs.get("skipped_events", "")) == str(b.attrs.get("skipped_events", ""))


def test_part_layout_and_sidecars():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        build(root)
        # Small synthetic patch (40 stations) needs an explicit block size to
        # split across workers; production uses the memory-derived default.
        out = _run(root, "wind", 3, "out_par", block_size=14)
        parts_root = root / "out_par" / ".extreme_parts" / "M" / "ssp126" / "P1" / "wind"
        parts = sorted(parts_root.glob("part_*.nc"))
        assert len(parts) == 3, f"expected 3 parts, got {len(parts)}"
        for p in parts:
            side = Path(str(p) + ".json")
            assert side.is_file(), f"missing part sidecar {side}"
            payload = json.loads(side.read_text())
            assert payload["status"] == "COMPLETED"
            assert payload["patch_id"] == "P1" and payload["tech"] == "wind"
            assert payload["station_count"] > 0
            assert "timing" in payload
        final_side = json.loads(Path(str(out) + ".json").read_text())
        assert final_side["station_count"] == sum(json.loads(Path(str(p) + ".json").read_text())["station_count"] for p in parts)
        assert len(final_side["parts"]) == 3
        assert final_side["processes"] == 3


def test_part_retry_semantics():
    """Completed parts with valid sidecars are reused; a stale part without a
    sidecar is recomputed (delete one part + sidecar, rerun, output equal)."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        build(root)
        first = _run(root, "wind", 2, "out_a", overwrite=True, block_size=20)
        parts_root = root / "out_a" / ".extreme_parts" / "M" / "ssp126" / "P1" / "wind"
        parts = sorted(parts_root.glob("part_*.nc"))
        assert len(parts) == 2
        # Corrupt one part: drop the sidecar so it must be recomputed.
        parts[0].unlink()
        Path(str(parts[0]) + ".json").unlink()
        second = _run(root, "wind", 2, "out_a", overwrite=True, reuse_parts_root=parts_root, block_size=20)
        with _open(first) as a, _open(second) as b:
            for name in [n for n in a.variables if n.startswith("signal_")]:
                np.testing.assert_array_equal(a[name].values, b[name].values, err_msg=name)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
