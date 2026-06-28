"""Signal runner: orchestrates the load → standardise → detect → write pipeline.

This module is the shared core for all four data source adapters.  It is
called from the CLI entry point and drives the per-task processing loop.
"""
from __future__ import annotations

import gc
import logging
import os
import sys
from pathlib import Path

import numpy as np

# Ensure project root is importable
_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import registry

from grid_extreme_signals.adapters.base import WeatherBundle
from grid_extreme_signals.io_utils import (
    skip_existing,
    write_signal_dataset,
    write_weather_dataset,
)

logger = logging.getLogger(__name__)


# =====================================================================
# Event → required variable mapping
# =====================================================================

# Minimal set of unified variables each event needs
_EVENT_REQUIRED_VARS: dict[str, list[str]] = {
    "high_temp": ["temp_C"],
    "high_wind": ["wind_ms"],
    "icing": ["temp_C", "rh_pct"],
    "hot_humid": ["temp_C", "rh_pct"],
    "freezing_rain": ["temp_C", "precip_mmh"],
    "rainstorm": ["precip_mmh"],
    "cold_highwind": ["temp_C", "wind_ms"],
    "high_humidity": ["rh_pct"],
    "dust": ["dust_aod"],
}


def _event_required_var(tech: str, event_name: str) -> str:
    """Return the first required variable name for an event (for logging)."""
    return _EVENT_REQUIRED_VARS.get(event_name, ["unknown"])[0]


# =====================================================================
# Main pipeline
# =====================================================================

def run_signal_pipeline(adapter, args) -> None:
    """Run the full signal generation pipeline.

    Parameters
    ----------
    adapter : WeatherAdapter
        Instantiated adapter for the chosen data source.
    args : argparse.Namespace
        Parsed CLI arguments.
    """
    # --- Station filter guard (Phase 1) ---------------------------------
    if getattr(args, "stations_dir", None) is not None:
        raise NotImplementedError(
            "Station-filter mode is reserved but not implemented yet. "
            "Omit --stations_dir to run all-grid mode."
        )

    output_root = getattr(args, "output_root", "outputs/grid_extreme_signals")
    compress_level = getattr(args, "compress_level", 4)
    overwrite = getattr(args, "overwrite", False)
    save_weather = getattr(args, "save_weather", False)
    dry_run = getattr(args, "dry_run", False)
    require_events = getattr(args, "require_events", [])

    tasks = adapter.iter_tasks(args)
    logger.info("Total tasks: %d", len(tasks))

    for task in tasks:
        _log_task(adapter, task)
        for tech in ("wind", "solar"):
            signal_path = adapter.signal_output_path(task, tech)
            if skip_existing(signal_path, overwrite):
                logger.info("[skip] %s", signal_path)
                continue

            if dry_run:
                logger.info("[dry_run] would write %s", signal_path)
                continue

            # --- Load weather ---
            try:
                if tech == "wind":
                    bundle = adapter.load_wind_weather(task)
                else:
                    bundle = adapter.load_solar_weather(task)
            except (FileNotFoundError, ValueError) as e:
                logger.warning("Missing input for %s: %s — skipping task", tech, e)
                continue

            # --- Extract numpy arrays for registry ---
            weather_dict = _bundle_to_weather_dict(bundle)

            # --- Compute signals ---
            masks = registry.simple_signals(tech, weather_dict, skip_missing=True)

            # --- Supported / skipped events ---
            supported = sorted(masks.keys())
            all_simple = set(registry.SIMPLE[tech].keys())
            skipped = sorted(all_simple - set(supported))
            skipped_reasons = {}
            for ev in skipped:
                req_var = _event_required_var(tech, ev)
                reason = bundle.skipped_inputs.get(req_var, f"missing {req_var}")
                skipped_reasons[ev] = reason

            # --- Check --require_events ---
            for req in require_events:
                if req not in masks:
                    raise RuntimeError(
                        f"Required event '{req}' could not be computed. "
                        f"Missing input: {bundle.skipped_inputs}"
                    )

            # --- Build output attributes ---
            attrs_extra = dict(bundle.attrs_extra)
            attrs_extra["supported_events"] = ",".join(supported)
            attrs_extra["skipped_events"] = ",".join(skipped)
            attrs_extra["skipped_event_reasons"] = "; ".join(
                f"{k}: {v}" for k, v in skipped_reasons.items()
            )
            attrs_extra["weather_saved"] = str(save_weather).lower()

            # --- Write signal file ---
            write_signal_dataset(
                bundle, masks, signal_path,
                attrs_extra=attrs_extra,
                compress_level=compress_level,
            )
            logger.info("[ok] %s  events=%s", signal_path, supported)

            # --- Optionally write weather file ---
            if save_weather:
                weather_path = adapter.weather_output_path(task, tech)
                write_weather_dataset(bundle, weather_path, compress_level)
                logger.info("[weather] %s", weather_path)

            # --- Free memory ---
            del bundle, weather_dict, masks
            gc.collect()

    logger.info("Pipeline complete.")


# =====================================================================
# Helpers
# =====================================================================

def _bundle_to_weather_dict(bundle: WeatherBundle) -> dict[str, np.ndarray]:
    """Extract unified weather variables from *bundle* as float32 numpy arrays."""
    weather: dict[str, np.ndarray] = {}
    for var_name in ("temp_C", "wind_ms", "precip_mmh", "rsds", "rh_pct", "dust_aod"):
        if var_name in bundle.dataset.data_vars:
            weather[var_name] = bundle.dataset[var_name].values.astype(np.float32)
    return weather


def _log_task(adapter, task: dict) -> None:
    """Log a human-readable description of the current task."""
    parts = [f"{k}={v}" for k, v in task.items() if v is not None]
    logger.info("Processing: %s", " ".join(parts))
