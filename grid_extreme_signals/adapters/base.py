"""Base classes for data source adapters.

Every adapter must:
  1. Yield *task dicts* via ``iter_tasks`` — one per processing unit (year or month).
  2. Return a :class:`WeatherBundle` from ``load_wind_weather`` / ``load_solar_weather``.
  3. Provide output-path helpers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Sequence

import xarray as xr


@dataclass
class WeatherBundle:
    """Standardised weather data ready for event detection.

    ``dataset`` contains unified weather variables (temp_C, wind_ms, precip_mmh,
    rsds, rh_pct, dust_aod) on the source's native spatial grid.  Only the
    variables that exist for the given source / tech are included.

    Attributes
    ----------
    source : str
        Data source identifier (``regional_bcsd``, ``china_cmfd_bcsd``, etc.).
    tech : str
        ``"wind"`` or ``"solar"`` — determines which events are applicable.
    dataset : xr.Dataset
        Unified weather variables with dims ``(time, *spatial_dims)``.
    spatial_dims : tuple[str, ...]
        Spatial dimension names, e.g. ``("lat", "lon")`` or ``("rlat", "rlon")``.
    grid_kind : str
        ``"regular_latlon"`` or ``"rotated_pole"``.
    target_time_axis : str
        Human-readable description of the target time axis used.
    source_timestep_hours : float
        Nominal time step of the source data (3.0 for BCSD, 1.0 for ERA5-Land / CORDEX).
    source_files : Sequence[str]
        Input file paths that were actually read.
    skipped_inputs : dict[str, str]
        Unified variable name → reason it is missing.
    attrs_extra : dict[str, str]
        Extra global attributes to merge into the output NetCDF.
    """

    source: str
    tech: str
    dataset: xr.Dataset
    spatial_dims: tuple[str, ...]
    grid_kind: str
    target_time_axis: str
    source_timestep_hours: float
    source_files: Sequence[str] = field(default_factory=list)
    skipped_inputs: dict[str, str] = field(default_factory=dict)
    attrs_extra: dict[str, str] = field(default_factory=dict)


class WeatherAdapter(ABC):
    """Abstract adapter interface for a specific climate data source."""

    # ------------------------------------------------------------------
    # Task iteration
    # ------------------------------------------------------------------
    @abstractmethod
    def iter_tasks(self, args) -> list[dict]:
        """Return a list of task descriptors to process.

        Each task is a plain dict whose contents depend on the source.
        Example (regional_bcsd):
            ``{"model": ..., "region": ..., "scenario": ..., "year": ...}``
        Example (era5land_raw):
            ``{"year": ..., "month": ...}``
        """

    # ------------------------------------------------------------------
    # Weather loading
    # ------------------------------------------------------------------
    @abstractmethod
    def load_wind_weather(self, task: dict) -> WeatherBundle:
        """Load & standardise weather variables for *wind* events."""

    @abstractmethod
    def load_solar_weather(self, task: dict) -> WeatherBundle:
        """Load & standardise weather variables for *solar* events."""

    # ------------------------------------------------------------------
    # Output path helpers
    # ------------------------------------------------------------------
    @abstractmethod
    def signal_output_path(self, task: dict, tech: str) -> str:
        """Return the absolute path for the signal output file."""

    @abstractmethod
    def weather_output_path(self, task: dict, tech: str) -> str:
        """Return the absolute path for the (optional) weather output file."""
