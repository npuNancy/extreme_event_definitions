#!/usr/bin/env python3
"""Pipeline A — grid-then-station extreme weather signal generation.

Two-step approach:

  1. (Phase 1) generate full-grid extreme-weather signals for the requested
     regions/years via the existing Phase-1 runner — exactly what
     ``scripts/generate_multi_source_grid_signals.py`` produces.  If the grid
     signal files already exist, this step is skipped (reuse via
     ``--grid_signals_dir``).
  2. (Phase 2) read each grid signal NetCDF, match the in-country stations to
     the grid (longitude normalised per file), gather each ``signal_<event>``
     mask to ``[(time, n_stations)]``, apply the activation-year and
     distance-tolerance masks, and write one station-level NetCDF per
     ``(region, tech, scenario)`` — identical schema to Pipeline B.

Trade-off (vs Pipeline B): this pipeline also computes every grid cell, so it
is slower, but it additionally leaves the full-grid signal files on disk for
other uses, and lets you derive station signals from a grid run that may
already exist.

Consistency: for the same inputs, Pipeline A and Pipeline B must yield
identical per-station masks (A gathers already-computed masks, B gathers
weather then computes) — see ``tests/test_pipeline_consistency.py``.

Example::

    # reuse an existing Phase-1 grid run
    python scripts/station_signals_from_grid.py \\
        --source regional_bcsd --data_dir data/bcsd_outputs \\
        --model MIROC-ES2H --scenario ssp126 \\
        --stations_csv data/stations/stations_SSP1-2.6.csv \\
        --region Germany --years 2030 \\
        --grid_signals_dir outputs/grid_extreme_signals

    # or generate grid signals first, then extract
    python scripts/station_signals_from_grid.py ... --run_phase1
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import xarray as xr

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import registry  # noqa: E402
from grid_extreme_signals.adapters.regional_bcsd import RegionalBcsdAdapter  # noqa: E402
from grid_extreme_signals import station_match as sm  # noqa: E402

logger = logging.getLogger("station_signals_from_grid")

DEFAULT_SHP = "/data6/yanxiaokai/project_climate/data/maps/natural_earth/ne_110m_admin_0_countries.shp"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Pipeline A: derive per-station signals from full-grid Phase-1 signals.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--source", default="regional_bcsd",
                   choices=["regional_bcsd", "china_cmfd_bcsd", "cordex_nam12"])
    p.add_argument("--data_dir", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--scenario", default=None, help="Inferred from --stations_csv if omitted.")
    p.add_argument("--stations_csv", required=True)
    p.add_argument("--region", default="all")
    p.add_argument("--years", required=True)
    p.add_argument("--tech", choices=["wind", "solar", "both"], default="both")
    p.add_argument("--shp", default=DEFAULT_SHP)
    p.add_argument("--max_dist", type=float, default=sm.MAX_DIST_DEG)
    p.add_argument("--grid_signals_dir", default="outputs/grid_extreme_signals",
                   help="Phase-1 grid signal root (read here).")
    p.add_argument("--output_root", default="outputs/station_signals")
    p.add_argument("--compress_level", type=int, default=4)
    p.add_argument("--no_activation_mask", action="store_true")
    p.add_argument("--allow_unit_inference", action="store_true")
    p.add_argument("--allow_missing_optional", action="store_true")
    p.add_argument("--run_phase1", action="store_true",
                   help="Generate missing grid signals via Phase-1 runner before extracting.")
    p.add_argument("--overwrite_grid", action="store_true",
                   help="With --run_phase1, recompute grid signals even if present.")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--dry_run", action="store_true")
    return p


# ---------------------------------------------------------------------
# Phase-1 grid signal generation (reuses the existing runner)
# ---------------------------------------------------------------------

def _grid_signal_path(grid_root, source, model, region, scenario, tech, year) -> str:
    return str(
        Path(grid_root) / source / model / region / scenario / "signals" /
        f"extreme_signals_{tech}_{model}_{region}_{scenario}_{year}.nc"
    )


def _ensure_grid_signals(adapter, args, region, scenario, techs) -> None:
    """Run Phase 1 for any missing grid signal files (only if --run_phase1)."""
    from grid_extreme_signals.signal_runner import run_signal_pipeline
    y0, y1 = (int(x) for x in args.years.split("-")) if "-" in args.years else (int(args.years),) * 2
    missing = []
    for tech in techs:
        for year in range(y0, y1 + 1):
            p = _grid_signal_path(args.grid_signals_dir, args.source, args.model,
                                  region, scenario, tech, year)
            if args.overwrite_grid or not os.path.exists(p):
                missing.append((tech, year))
    if not missing:
        return
    logger.info("[%s] running Phase-1 for %d missing grid files: %s",
                region, len(missing), missing)
    phase_args = SimpleNamespace(**vars(args))
    phase_args.output_root = args.grid_signals_dir
    phase_args.region = region
    phase_args.overwrite = args.overwrite_grid
    phase_args.save_weather = False
    phase_args.stations_dir = None
    phase_args.require_events = []
    phase_args.dry_run = False
    phase_args.compress_level = args.compress_level
    # Restrict to this region; run_signal_pipeline iterates the adapter's tasks.
    run_signal_pipeline(adapter, phase_args)


# ---------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------

def _bundle_axes_from_grid(ds) -> tuple[str, str, str]:
    """Discover (time, lat, lon) dim names from a grid signal dataset."""
    dims = list(ds.dims)
    time_name = next((d for d in dims if d.lower().startswith("time")), dims[0])
    lat_candidates = [d for d in dims if d.lower() in ("lat", "latitude", "rlat")]
    lon_candidates = [d for d in dims if d.lower() in ("lon", "longitude", "rlon")]
    lat_name = lat_candidates[0] if lat_candidates else dims[1]
    lon_name = lon_candidates[0] if lon_candidates else dims[2]
    return time_name, lat_name, lon_name


def _process_region(adapter, args, country_stations, region, scenario, techs) -> None:
    y0, y1 = (int(x) for x in args.years.split("-")) if "-" in args.years else (int(args.years),) * 2

    for tech in techs:
        stations = country_stations.get(tech)
        if stations is None or len(stations) == 0:
            logger.info("[%s/%s] no stations — skip", region, tech)
            continue

        out_path = str(
            Path(args.output_root) / args.source / args.model / region / scenario /
            f"station_signals_{tech}_{args.model}_{region}_{scenario}_{y0}-{y1}.nc"
        )
        if not args.overwrite and os.path.exists(out_path):
            logger.info("[%s/%s] exists — skip", region, tech)
            continue
        if args.dry_run:
            logger.info("[%s/%s] [dry_run] would write %s", region, tech, out_path)
            continue

        match: sm.StationMatch | None = None
        masks_acc: dict[str, list[np.ndarray]] = {}
        times_acc: list[np.ndarray] = []
        skipped_reasons: dict[str, str] = {}

        for year in range(y0, y1 + 1):
            gp = _grid_signal_path(args.grid_signals_dir, args.source, args.model,
                                   region, scenario, tech, year)
            if not os.path.exists(gp):
                logger.warning("[%s/%s/%d] grid signal missing: %s — skip year",
                               region, tech, year, gp)
                continue
            ds = xr.open_dataset(gp)
            time_name, lat_name, lon_name = _bundle_axes_from_grid(ds)
            sig_vars = [v for v in ds.data_vars if v.startswith("signal_")]
            if not sig_vars:
                ds.close()
                logger.warning("[%s/%s/%d] no signal vars in grid file — skip", region, tech, year)
                continue

            times = ds[time_name].values
            if times.size == 0:
                ds.close()
                continue

            if match is None:
                grid_lat = ds[lat_name].values
                grid_lon = ds[lon_name].values
                match = sm.match_regular(grid_lat, grid_lon, stations, max_dist=args.max_dist)
                n_bad = int((~match.valid).sum())
                if n_bad:
                    logger.warning("[%s/%s] %d/%d stations exceed max_dist=%.2f°",
                                   region, tech, n_bad, len(match), args.max_dist)
                # skipped-event reasons from grid file attrs if present
                for ev in ds.attrs.get("skipped_events", "").split(","):
                    ev = ev.strip()
                    if ev:
                        skipped_reasons[ev] = ds.attrs.get("skipped_event_reasons", f"missing input for {ev}")
                logger.info("[%s/%s] matched %d stations (grid %dx%d, lon360=%s)",
                            region, tech, len(match), grid_lat.size, grid_lon.size,
                            sm.is_lon_360(grid_lon))

            for v in sig_vars:
                gathered = sm.gather_to_stations(ds[v].values.astype(bool), match)
                masks_acc.setdefault(v, []).append(gathered)
            times_acc.append(times)
            ds.close()

        if match is None or not masks_acc:
            logger.warning("[%s/%s] nothing produced — skip", region, tech)
            continue

        times_all = np.concatenate(times_acc)
        masks_all = {v: np.concatenate(parts, axis=0) for v, parts in masks_acc.items()}

        act = _activation_time_mask(
            times_all, match.stations["activation_year"].to_numpy(np.int64),
            enabled=not args.no_activation_mask)
        valid = match.valid[None, :]

        supported = sorted(v[len("signal_"):] for v in masks_all)
        all_simple = set(registry.SIMPLE[tech].keys())
        skipped = sorted(all_simple - set(supported))

        out_masks = {v: (arr & act & valid).astype(np.int8) for v, arr in masks_all.items()}
        sm.write_station_signals(
            out_path, out_masks, times_all, match, tech,
            source=args.source, model=args.model, region=region, scenario=scenario,
            source_csv=os.path.basename(args.stations_csv), pipeline="A",
            supported=supported, skipped=skipped, skipped_reasons=skipped_reasons,
            max_dist=args.max_dist, activation_mask_on=not args.no_activation_mask,
            compress_level=args.compress_level,
        )
        logger.info("[%s/%s] wrote %s  events=%s  stations=%d",
                    region, tech, out_path, supported, len(match))


def _activation_time_mask(times, activation_years, enabled):
    if not enabled:
        return np.ones((times.shape[0], activation_years.shape[0]), dtype=bool)
    years = pd.DatetimeIndex(times).year.to_numpy(np.int64)[:, None]
    return years >= activation_years[None, :]


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    args = build_parser().parse_args()

    if args.source != "regional_bcsd":
        raise NotImplementedError(
            f"--source {args.source!r} not enabled yet in Pipeline A "
            "(data not staged). Only 'regional_bcsd' is supported currently."
        )

    scenario = args.scenario or sm.infer_scenario_from_csv(args.stations_csv)
    logger.info("Loading stations %s (scenario=%s)", args.stations_csv, scenario)
    stations_df = sm.load_stations(args.stations_csv)
    country_shapes = sm.load_country_shapes(args.shp)

    adapter = RegionalBcsdAdapter(SimpleNamespace(
        data_dir=args.data_dir, model=args.model, region=args.region,
        scenario=scenario, years=args.years, output_root=args.grid_signals_dir,
        allow_unit_inference=args.allow_unit_inference,
        allow_missing_optional=args.allow_missing_optional,
    ))
    regions = adapter._resolve_regions()
    techs = ["wind", "solar"] if args.tech == "both" else [args.tech]
    logger.info("Regions: %d | techs: %s", len(regions), techs)

    for region in regions:
        country_name = sm.bcsd_region_to_ne_name(region)
        geom = country_shapes.get(country_name)
        if geom is None:
            logger.warning("[%s] no shapefile match for '%s' — skip", region, country_name)
            continue
        country_stations = {tech: sm.filter_stations_for_country(stations_df, geom, tech)
                            for tech in techs}
        if sum(len(v) for v in country_stations.values()) == 0:
            logger.info("[%s] no stations — skip", region)
            continue
        if args.run_phase1:
            _ensure_grid_signals(adapter, args, region, scenario, techs)
        _process_region(adapter, args, country_stations, region, scenario, techs)

    logger.info("Pipeline A complete.")


if __name__ == "__main__":
    main()
