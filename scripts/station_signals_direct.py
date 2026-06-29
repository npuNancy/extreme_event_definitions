#!/usr/bin/env python3
"""Pipeline B — station-direct extreme weather signal generation.

For every region that has weather data on disk, this pipeline:

  1. loads + standardises the weather via the existing multi-source adapter
     (``RegionalBcsdAdapter`` etc.) — the same path Phase 1 uses;
  2. selects only the stations that fall inside that region's country polygon
     (point-in-polygon on Natural Earth) and deduplicates them with
     ``activation_year = min(year)``;
  3. matches each station to the nearest grid cell (longitude normalised to
     ``[-180, 180)`` per file — BCSD's convention is region-dependent);
  4. gathers the standardised weather variables to the matched cells into
     ``(time, n_stations)`` arrays and runs ``registry.simple_signals``;
  5. applies the activation-year mask (signal is 0 before the station exists)
     and the distance-tolerance mask, then writes one station-level NetCDF per
     ``(region, tech, scenario)``.

It never writes full-grid signal files — only per-station output.  Stations in
countries without weather data are simply never processed.

Currently supports ``--source regional_bcsd``.  ``china_cmfd_bcsd`` /
``cordex_nam12`` are wired through the shared matching layer but data is not
staged yet; they will be enabled when their NetCDFs arrive.

Example::

    python scripts/station_signals_direct.py \\
        --source regional_bcsd \\
        --data_dir data/bcsd_outputs \\
        --model MIROC-ES2H \\
        --scenario ssp126 \\
        --stations_csv data/stations/stations_SSP1-2.6.csv \\
        --region Germany \\
        --years 2015-2050
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

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import registry  # noqa: E402
from grid_extreme_signals.adapters.regional_bcsd import RegionalBcsdAdapter  # noqa: E402
from grid_extreme_signals import station_match as sm  # noqa: E402

logger = logging.getLogger("station_signals_direct")

DEFAULT_SHP = "/data6/yanxiaokai/project_climate/data/maps/natural_earth/ne_110m_admin_0_countries.shp"


# =====================================================================
# CLI
# =====================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Pipeline B: generate per-station extreme-weather signals directly (no grid output).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--source", default="regional_bcsd",
                   choices=["regional_bcsd", "china_cmfd_bcsd", "cordex_nam12"],
                   help="Data source (regional_bcsd supported now; others deferred).")
    p.add_argument("--data_dir", required=True, help="Input data root directory.")
    p.add_argument("--model", required=True, help="Climate model name.")
    p.add_argument("--scenario", default=None,
                   help="Scenario code (e.g. ssp126). Inferred from --stations_csv if omitted.")
    p.add_argument("--stations_csv", required=True,
                   help="Station siting CSV (e.g. data/stations/stations_SSP1-2.6.csv).")
    p.add_argument("--region", default="all",
                   help="Region name or 'all' (default: all regions with data).")
    p.add_argument("--years", required=True, help="Year range: 'YYYY' or 'YYYY-YYYY'.")
    p.add_argument("--tech", choices=["wind", "solar", "both"], default="both")
    p.add_argument("--shp", default=DEFAULT_SHP, help="Natural Earth countries shapefile.")
    p.add_argument("--max_dist", type=float, default=sm.MAX_DIST_DEG,
                   help="Nearest-cell distance tolerance in degrees (default %(default)s).")
    p.add_argument("--output_root", default="outputs/station_signals")
    p.add_argument("--compress_level", type=int, default=4)
    p.add_argument("--no_activation_mask", action="store_true",
                   help="Keep signals for all years (do not zero pre-activation years).")
    p.add_argument("--allow_unit_inference", action="store_true")
    p.add_argument("--allow_missing_optional", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--dry_run", action="store_true")
    return p


# =====================================================================
# Helpers
# =====================================================================

def _bundle_spatial_axes(bundle) -> tuple[str, str, str]:
    """Return (time_name, lat_name, lon_name) for a regular-latlon bundle."""
    spatial = set(bundle.spatial_dims)
    sample = next(iter(bundle.dataset.data_vars.values()))
    time_name = next(d for d in sample.dims if d not in spatial)
    lat_name, lon_name = bundle.spatial_dims
    return time_name, lat_name, lon_name


def _gather_weather(bundle, match: sm.StationMatch) -> dict[str, np.ndarray]:
    """Gather standardised weather vars from the bundle grid to stations.

    Returns ``{var_name: (time, n_stations) float32}``.
    """
    out: dict[str, np.ndarray] = {}
    for var in ("temp_C", "wind_ms", "precip_mmh", "rsds", "rh_pct", "dust_aod"):
        if var in bundle.dataset.data_vars:
            out[var] = sm.gather_to_stations(
                bundle.dataset[var].values.astype(np.float32), match
            )
    return out


def _activation_time_mask(times: np.ndarray, activation_years: np.ndarray,
                          enabled: bool) -> np.ndarray:
    """(T, n_sta) bool: True where the station is active at that time."""
    if not enabled:
        return np.ones((times.shape[0], activation_years.shape[0]), dtype=bool)
    years = pd.DatetimeIndex(times).year.to_numpy(np.int64)[:, None]
    return years >= activation_years[None, :]


def _skip(path: str, overwrite: bool) -> bool:
    if overwrite:
        return False
    return os.path.exists(path)


# =====================================================================
# Per-tech processing
# =====================================================================

def _process_tech(adapter, args, country_stations: dict[str, pd.DataFrame],
                  region: str, scenario: str, tech: str,
                  country_shapes: dict) -> str | None:
    """Process one (region, tech); return the output path or None if skipped."""
    stations = country_stations.get(tech)
    if stations is None or len(stations) == 0:
        logger.info("[%s/%s] no stations in country — skip", region, tech)
        return None

    y0, y1 = (int(x) for x in args.years.split("-")) if "-" in args.years else (int(args.years),) * 2
    out_path = str(
        Path(args.output_root) / args.source / args.model / region / scenario /
        f"station_signals_{tech}_{args.model}_{region}_{scenario}_{y0}-{y1}.nc"
    )
    if _skip(out_path, args.overwrite):
        logger.info("[%s/%s] exists — skip (--overwrite to recompute)", region, tech)
        return out_path
    if args.dry_run:
        logger.info("[%s/%s] [dry_run] would write %s", region, tech, out_path)
        return out_path

    match: sm.StationMatch | None = None
    masks_acc: dict[str, list[np.ndarray]] = {}
    times_acc: list[np.ndarray] = []
    skipped_inputs: dict[str, str] = {}

    for year in range(y0, y1 + 1):
        task = {"data_dir": args.data_dir, "model": args.model, "region": region,
                "scenario": scenario, "year": year}
        try:
            bundle = (adapter.load_wind_weather(task) if tech == "wind"
                      else adapter.load_solar_weather(task))
        except (FileNotFoundError, ValueError) as e:
            logger.warning("[%s/%s/%d] missing input: %s — stop year loop", region, tech, year, e)
            break

        time_name, lat_name, lon_name = _bundle_spatial_axes(bundle)
        times = bundle.dataset[time_name].values
        if times.size == 0:
            logger.warning("[%s/%d] empty time axis — skip year", region, year)
            continue

        # Match once per region/tech (grid is constant across years)
        if match is None:
            grid_lat = bundle.dataset[lat_name].values
            grid_lon = bundle.dataset[lon_name].values
            match = sm.match_regular(grid_lat, grid_lon, stations, max_dist=args.max_dist)
            n_bad = int((~match.valid).sum())
            if n_bad:
                logger.warning("[%s/%s] %d/%d stations exceed max_dist=%.2f° (will be zeroed)",
                               region, tech, n_bad, len(match), args.max_dist)
            skipped_inputs = dict(bundle.skipped_inputs)
            logger.info("[%s/%s] matched %d stations (grid %dx%d, lon360=%s)",
                        region, tech, len(match),
                        grid_lat.size, grid_lon.size, sm.is_lon_360(grid_lon))

        weather = _gather_weather(bundle, match)
        if not weather:
            logger.warning("[%s/%s/%d] no weather vars gathered — skip year", region, tech, year)
            continue
        masks = registry.simple_signals(tech, weather, skip_missing=True)
        for name, arr in masks.items():
            masks_acc.setdefault(name, []).append(arr.astype(bool))
        times_acc.append(times)
        del bundle, weather, masks

    if match is None or not masks_acc:
        logger.warning("[%s/%s] nothing produced — skip", region, tech)
        return None

    # Concatenate across years
    times_all = np.concatenate(times_acc)
    masks_all = {name: np.concatenate(parts, axis=0) for name, parts in masks_acc.items()}

    # Activation + distance masks
    act = _activation_time_mask(
        times_all, match.stations["activation_year"].to_numpy(np.int64),
        enabled=not args.no_activation_mask,
    )
    valid = match.valid[None, :]  # (1, n_sta)

    supported = sorted(masks_all.keys())
    all_simple = set(registry.SIMPLE[tech].keys())
    skipped = sorted(all_simple - set(supported))
    skipped_reasons = {ev: skipped_inputs.get(_first_req_var(tech, ev), f"missing input for {ev}")
                       for ev in skipped}

    out_masks: dict[str, np.ndarray] = {}
    for name, arr in masks_all.items():
        arr = arr & act & valid  # zero pre-activation and out-of-tolerance stations
        out_masks[f"signal_{name}"] = arr.astype(np.int8)

    sm.write_station_signals(
        out_path, out_masks, times_all, match, tech,
        source=args.source, model=args.model, region=region, scenario=scenario,
        source_csv=os.path.basename(args.stations_csv), pipeline="B",
        supported=supported, skipped=skipped, skipped_reasons=skipped_reasons,
        max_dist=args.max_dist, activation_mask_on=not args.no_activation_mask,
        compress_level=args.compress_level,
    )
    logger.info("[%s/%s] wrote %s  events=%s  stations=%d  steps=%d",
                region, tech, out_path, supported, len(match), times_all.size)
    return out_path


def _first_req_var(tech: str, event_name: str) -> str:
    mapping = {
        "high_temp": "temp_C", "high_wind": "wind_ms", "icing": "rh_pct",
        "hot_humid": "rh_pct", "freezing_rain": "precip_mmh", "rainstorm": "precip_mmh",
        "cold_highwind": "temp_C", "high_humidity": "rh_pct", "dust": "dust_aod",
    }
    return mapping.get(event_name, "unknown")


# =====================================================================
# Main
# =====================================================================

def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    args = build_parser().parse_args()

    if args.source != "regional_bcsd":
        raise NotImplementedError(
            f"--source {args.source!r} not enabled yet in Pipeline B "
            "(data not staged). Only 'regional_bcsd' is supported currently."
        )

    scenario = args.scenario or sm.infer_scenario_from_csv(args.stations_csv)
    logger.info("Loading stations %s (scenario=%s)", args.stations_csv, scenario)
    stations_df = sm.load_stations(args.stations_csv)
    logger.info("Loading country shapes %s", args.shp)
    country_shapes = sm.load_country_shapes(args.shp)

    adapter = RegionalBcsdAdapter(SimpleNamespace(
        data_dir=args.data_dir, model=args.model, region=args.region,
        scenario=scenario, years=args.years, output_root=args.output_root,
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
        country_stations = {
            tech: sm.filter_stations_for_country(stations_df, geom, tech) for tech in techs
        }
        n_total = sum(len(v) for v in country_stations.values())
        if n_total == 0:
            logger.info("[%s] no stations in country — skip", region)
            continue
        logger.info("[%s] country=%s stations=%s",
                    region, country_name, {t: len(v) for t, v in country_stations.items()})
        for tech in techs:
            _process_tech(adapter, args, country_stations, region, scenario, tech, country_shapes)

    logger.info("Pipeline B complete.")


if __name__ == "__main__":
    main()
