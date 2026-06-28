#!/usr/bin/env python3
"""Unified CLI entry point for multi-source grid extreme weather signal generation.

Supports four data sources:
  - ``regional_bcsd``  — CMIP6–ERA5Land BCSD (regular lat/lon, 3-hourly)
  - ``china_cmfd_bcsd`` — CMIP6–CMFD BCSD for China (sfcWind, 3-hourly)
  - ``cordex_nam12``   — CMIP6–CORDEX NAM-12 (rotated pole, hourly)
  - ``era5land_raw``   — Raw ERA5-Land global (hourly, requires deaccumulation)

Examples::

    # Regional BCSD
    python scripts/generate_multi_source_grid_signals.py \\
        --source regional_bcsd \\
        --data_dir data/bcsd_outputs \\
        --model MIROC-ES2H \\
        --region Austria \\
        --scenario ssp126 \\
        --years 2015-2060

    # China CMFD BCSD
    python scripts/generate_multi_source_grid_signals.py \\
        --source china_cmfd_bcsd \\
        --data_dir data/cmip6_downscaling_3hr \\
        --model MIROC-ES2H \\
        --scenario ssp126 \\
        --years 2015-2100

    # CORDEX NAM-12
    python scripts/generate_multi_source_grid_signals.py \\
        --source cordex_nam12 \\
        --data_dir data/CORDEX-CMIP6/NAM-12/1hr \\
        --gcm_model MPI-ESM1-2-LR \\
        --realization r1i1p1f1 \\
        --rcm_model CRCM5 \\
        --scenario ssp126 \\
        --years 2020-2060

    # ERA5-Land
    python scripts/generate_multi_source_grid_signals.py \\
        --source era5land_raw \\
        --data_dir data \\
        --years 2024 \\
        --months 1,2
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

# Ensure project root is importable
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from grid_extreme_signals.adapters.regional_bcsd import RegionalBcsdAdapter
from grid_extreme_signals.adapters.china_cmfd_bcsd import ChinaCmfdBcsdAdapter
from grid_extreme_signals.adapters.cordex_nam12 import CordexNam12Adapter
from grid_extreme_signals.adapters.era5land_raw import Era5LandRawAdapter
from grid_extreme_signals.signal_runner import run_signal_pipeline

logger = logging.getLogger("grid_extreme_signals")

# Default chunk sizes per source
_DEFAULT_CHUNK_TIME = {
    "regional_bcsd": 512,
    "china_cmfd_bcsd": 512,
    "cordex_nam12": 24,
    "era5land_raw": 24,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate extreme weather signals from multi-source grid climate data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # ---- Common arguments ----
    p.add_argument(
        "--source", required=True,
        choices=["regional_bcsd", "china_cmfd_bcsd", "cordex_nam12", "era5land_raw"],
        help="Data source type.",
    )
    p.add_argument("--data_dir", required=True, help="Input data root directory.")
    p.add_argument(
        "--output_root", default="outputs/grid_extreme_signals",
        help="Output root directory (default: outputs/grid_extreme_signals).",
    )
    p.add_argument(
        "--years", required=True,
        help="Year range: 'YYYY' or 'YYYY-YYYY'.",
    )
    p.add_argument(
        "--months", default="",
        help="Month filter: '1,2,3' or empty for all months (default: all).",
    )
    p.add_argument(
        "--stations_dir", default=None,
        help="Station selection directory (RESERVED — raises NotImplementedError in Phase 1).",
    )
    p.add_argument(
        "--require_events", nargs="*", default=[],
        help="Events that must be computed; error if input is missing.",
    )
    p.add_argument(
        "--allow_missing_optional", action="store_true",
        help="Skip gracefully when optional inputs are missing.",
    )
    p.add_argument(
        "--allow_unit_inference", action="store_true",
        help="Allow heuristic unit inference when units attribute is missing.",
    )
    p.add_argument(
        "--chunk_time", type=int, default=None,
        help="Time steps per processing chunk (default varies by source).",
    )
    p.add_argument(
        "--compress_level", type=int, default=4,
        help="NetCDF zlib compression level (default: 4).",
    )
    p.add_argument(
        "--overwrite", action="store_true",
        help="Recompute and overwrite existing output files.",
    )
    p.add_argument(
        "--dry_run", action="store_true",
        help="Print task plan without computing.",
    )
    p.add_argument(
        "--save_weather", action="store_true",
        help="Save standardised weather NetCDF files in addition to signals.",
    )

    # ---- Source-specific arguments ----
    # regional_bcsd / china_cmfd_bcsd
    p.add_argument("--model", default=None, help="Climate model name.")
    p.add_argument("--region", default=None, help="Region name (regional_bcsd only). Use 'all' to process all regions.")
    p.add_argument("--scenario", default=None, help="Scenario (e.g., ssp126, ssp245, ssp585).")

    # cordex_nam12
    p.add_argument("--gcm_model", default=None, help="GCM model (CORDEX).")
    p.add_argument("--realization", default=None, help="Realization ID, e.g. r1i1p1f1 (CORDEX).")
    p.add_argument("--rcm_model", default=None, help="RCM model, e.g. CRCM5 (CORDEX).")

    # era5land_raw
    p.add_argument(
        "--era5land_d2m_root", default=None,
        help="Alternative root directory for d2m variable (ERA5-Land).",
    )
    p.add_argument(
        "--dust_dir", default=None,
        help="MERRA-2 dust data root directory (ERA5-Land, optional).",
    )

    return p


def validate_args(args: argparse.Namespace) -> None:
    """Check that required source-specific arguments are present."""
    src = args.source

    if src == "regional_bcsd":
        for name in ("model", "region", "scenario"):
            if getattr(args, name) is None:
                raise ValueError(f"--{name} is required for --source regional_bcsd")

    elif src == "china_cmfd_bcsd":
        for name in ("model", "scenario"):
            if getattr(args, name) is None:
                raise ValueError(f"--{name} is required for --source china_cmfd_bcsd")

    elif src == "cordex_nam12":
        for name in ("gcm_model", "realization", "rcm_model", "scenario"):
            if getattr(args, name) is None:
                raise ValueError(f"--{name} is required for --source cordex_nam12")

    # era5land_raw needs no extra args


def create_adapter(args: argparse.Namespace):
    """Instantiate the correct adapter for the chosen source."""
    if args.source == "regional_bcsd":
        return RegionalBcsdAdapter(args)
    elif args.source == "china_cmfd_bcsd":
        return ChinaCmfdBcsdAdapter(args)
    elif args.source == "cordex_nam12":
        return CordexNam12Adapter(args)
    elif args.source == "era5land_raw":
        return Era5LandRawAdapter(args)
    else:
        raise ValueError(f"Unknown source: {args.source}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = build_parser()
    args = parser.parse_args()

    # Set default chunk_time per source
    if args.chunk_time is None:
        args.chunk_time = _DEFAULT_CHUNK_TIME[args.source]

    validate_args(args)

    logger.info("Source: %s", args.source)
    adapter = create_adapter(args)
    run_signal_pipeline(adapter, args)


if __name__ == "__main__":
    main()
