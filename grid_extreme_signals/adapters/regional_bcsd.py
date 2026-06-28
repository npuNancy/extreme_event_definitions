"""Adapter for CMIP6–ERA5Land BCSD data (most regions / countries).

File layout::

    {data_dir}/{model}/{region}/{model}/{var}_3h_bcsd_on_0p1deg_{region}_{model}_{scenario}_*.nc

Variables: pr, rsds, tas, uas, vas.  Three-hourly, regular lat/lon grid.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

import numpy as np
import xarray as xr

from grid_extreme_signals.adapters.base import WeatherAdapter, WeatherBundle
from grid_extreme_signals.io_utils import (
    discover_regions,
    find_bcsd_file,
    get_var_name,
    is_valid_region_name,
    output_complete,
    prepare_dataarray,
    skip_existing,
    validate_same_spatial_grid,
)
from grid_extreme_signals.time_alignment import (
    LAT_CANDIDATES,
    LON_CANDIDATES,
    TIME_CANDIDATES,
    build_time_index,
    filter_year,
    find_coord_name,
    interp_instantaneous_to_target,
    parse_years,
)
from grid_extreme_signals.unit_conversion import (
    pr_to_mmh,
    rsds_to_wm2,
    tas_to_celsius,
    wind_to_ms,
)

logger = logging.getLogger(__name__)


class RegionalBcsdAdapter(WeatherAdapter):
    """Adapter for regional CMIP6–ERA5Land BCSD data."""

    def __init__(self, args) -> None:
        self.data_dir = args.data_dir
        self.model = args.model
        self.region = args.region
        self.scenario = args.scenario
        self.output_root = getattr(args, "output_root", "outputs/grid_extreme_signals")
        self.allow_unit_inference = getattr(args, "allow_unit_inference", False)
        self.allow_missing_optional = getattr(args, "allow_missing_optional", False)

    # ------------------------------------------------------------------
    # Task iteration
    # ------------------------------------------------------------------

    def iter_tasks(self, args) -> list[dict]:
        y0, y1 = parse_years(args.years)
        regions = self._resolve_regions()
        tasks = []
        for region in regions:
            for y in range(y0, y1 + 1):
                tasks.append({
                    "data_dir": self.data_dir,
                    "model": self.model,
                    "region": region,
                    "scenario": self.scenario,
                    "year": y,
                })
        return tasks

    def _resolve_regions(self) -> list[str]:
        if self.region == "all":
            return discover_regions(self.data_dir, self.model)
        if not is_valid_region_name(self.region):
            raise ValueError(f"Invalid region name: {self.region!r}")
        return [self.region]

    # ------------------------------------------------------------------
    # Output paths
    # ------------------------------------------------------------------

    def _base_dir(self, task: dict, tech: str) -> Path:
        return (
            Path(self.output_root)
            / "regional_bcsd"
            / task["model"]
            / task["region"]
            / task["scenario"]
        )

    def signal_output_path(self, task: dict, tech: str) -> str:
        return str(
            self._base_dir(task, tech) / "signals" /
            f"extreme_signals_{tech}_{task['model']}_{task['region']}_{task['scenario']}_{task['year']}.nc"
        )

    def weather_output_path(self, task: dict, tech: str) -> str:
        return str(
            self._base_dir(task, tech) / "weather" /
            f"weather_{tech}_{task['model']}_{task['region']}_{task['scenario']}_{task['year']}.nc"
        )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _open_and_prepare(
        self,
        task: dict,
        var: str,
        time_name: str,
        lat_name: str,
        lon_name: str,
    ) -> tuple[xr.DataArray, str, str | None]:
        """Open file, resolve variable name, return prepared DataArray + units."""
        fpath = find_bcsd_file(
            task["data_dir"], task["model"], task["region"],
            task["scenario"], var,
        )
        ds = xr.open_dataset(fpath)
        var_name = get_var_name(ds, var)
        units = ds[var_name].attrs.get("units", None)
        da = prepare_dataarray(ds, var, time_name, lat_name, lon_name)
        ds.close()
        return da, str(fpath), units

    def _discover_coords(self, task: dict) -> tuple[str, str, str]:
        """Discover coordinate names from a sample file."""
        fpath = find_bcsd_file(
            task["data_dir"], task["model"], task["region"],
            task["scenario"], "rsds",
        )
        ds = xr.open_dataset(fpath)
        time_name = find_coord_name(ds, TIME_CANDIDATES)
        lat_name = find_coord_name(ds, LAT_CANDIDATES)
        lon_name = find_coord_name(ds, LON_CANDIDATES)
        ds.close()
        return time_name, lat_name, lon_name

    def _filter_year(self, da: xr.DataArray, time_name: str, year: int) -> xr.DataArray:
        idx = filter_year(da[time_name], year)
        return da.isel({time_name: idx})

    # ------------------------------------------------------------------
    # Wind weather
    # ------------------------------------------------------------------

    def load_wind_weather(self, task: dict) -> WeatherBundle:
        time_name, lat_name, lon_name = self._discover_coords(task)
        year = task["year"]
        source_files = []
        skipped_inputs: dict[str, str] = {}

        # Required: uas, vas
        uas_da, f_uas, uas_units = self._open_and_prepare(task, "uas", time_name, lat_name, lon_name)
        vas_da, f_vas, vas_units = self._open_and_prepare(task, "vas", time_name, lat_name, lon_name)
        source_files.extend([f_uas, f_vas])

        # Optional: tas (needed for high_temp signal)
        try:
            tas_da, f_tas, tas_units = self._open_and_prepare(task, "tas", time_name, lat_name, lon_name)
            source_files.append(f_tas)
        except FileNotFoundError:
            tas_da = None
            tas_units = None
            skipped_inputs["temp_C"] = "tas file not found"

        # Filter to target year
        uas_da = self._filter_year(uas_da, time_name, year)
        vas_da = self._filter_year(vas_da, time_name, year)
        wind_time = uas_da[time_name].values

        # Interpolate tas to wind time axis if needed
        if tas_da is not None:
            tas_da = self._filter_year(tas_da, time_name, year)
            tas_time = tas_da[time_name].values
            if not np.array_equal(tas_time, wind_time):
                logger.info("Interpolating tas to wind time axis")
                temp_C = tas_to_celsius(
                    interp_instantaneous_to_target(tas_da, time_name, wind_time),
                    tas_units,
                    allow_inference=self.allow_unit_inference,
                )
                time_alignment = "tas interpolated to uas/vas time"
            else:
                temp_C = tas_to_celsius(
                    tas_da.values, tas_units,
                    allow_inference=self.allow_unit_inference,
                )
                time_alignment = "uas/vas instantaneous native"
        else:
            temp_C = None
            time_alignment = "uas/vas instantaneous native"

        # Wind speed from components
        wind_ms = np.sqrt(
            uas_da.values.astype(np.float32) ** 2
            + vas_da.values.astype(np.float32) ** 2
        )

        # Build dataset
        ds = xr.Dataset(
            {
                "wind_ms": xr.DataArray(wind_ms, dims=(time_name, lat_name, lon_name)),
            },
            coords={
                time_name: uas_da[time_name],
                lat_name: uas_da[lat_name],
                lon_name: uas_da[lon_name],
            },
        )
        if temp_C is not None:
            ds["temp_C"] = xr.DataArray(temp_C, dims=(time_name, lat_name, lon_name))

        # Mark unavailable inputs
        skipped_inputs.setdefault("rh_pct", "no humidity data in BCSD")
        skipped_inputs.setdefault("dust_aod", "no dust data in BCSD")
        skipped_inputs.setdefault("rsds", "not used for wind signals")
        skipped_inputs.setdefault("precip_mmh", "not used for wind signals")

        return WeatherBundle(
            source="regional_bcsd",
            tech="wind",
            dataset=ds,
            spatial_dims=(lat_name, lon_name),
            grid_kind="regular_latlon",
            target_time_axis=time_alignment,
            source_timestep_hours=3.0,
            source_files=source_files,
            skipped_inputs=skipped_inputs,
            attrs_extra={
                "model": task["model"],
                "region": task["region"],
                "scenario": task["scenario"],
                "year": str(year),
                "time_alignment": time_alignment,
            },
        )

    # ------------------------------------------------------------------
    # Solar weather
    # ------------------------------------------------------------------

    def load_solar_weather(self, task: dict) -> WeatherBundle:
        time_name, lat_name, lon_name = self._discover_coords(task)
        year = task["year"]
        source_files = []
        skipped_inputs: dict[str, str] = {}

        # Required: rsds (solar target time axis)
        rsds_da, f_rsds, rsds_units = self._open_and_prepare(task, "rsds", time_name, lat_name, lon_name)
        source_files.append(f_rsds)
        rsds_da = self._filter_year(rsds_da, time_name, year)
        target_times = rsds_da[time_name].values

        # Required: tas
        tas_da, f_tas, tas_units = self._open_and_prepare(task, "tas", time_name, lat_name, lon_name)
        source_files.append(f_tas)
        tas_da = self._filter_year(tas_da, time_name, year)

        # Required: uas, vas
        uas_da, f_uas, uas_units = self._open_and_prepare(task, "uas", time_name, lat_name, lon_name)
        vas_da, f_vas, vas_units = self._open_and_prepare(task, "vas", time_name, lat_name, lon_name)
        source_files.extend([f_uas, f_vas])
        uas_da = self._filter_year(uas_da, time_name, year)
        vas_da = self._filter_year(vas_da, time_name, year)

        # Optional: pr
        pr_da = None
        pr_units = None
        try:
            pr_da, f_pr, pr_units = self._open_and_prepare(task, "pr", time_name, lat_name, lon_name)
            source_files.append(f_pr)
            pr_da = self._filter_year(pr_da, time_name, year)
        except FileNotFoundError:
            if not self.allow_missing_optional:
                logger.warning("pr file not found for %s/%s — precipitation events will be skipped",
                               task["model"], task["region"])
            skipped_inputs["precip_mmh"] = "pr file not found"

        # Validate spatial grids
        validate_same_spatial_grid(
            rsds_da.to_dataset(name="rsds"),
            {"tas": tas_da.to_dataset(name="tas"),
             "uas": uas_da.to_dataset(name="uas"),
             "vas": vas_da.to_dataset(name="vas")},
            lat_name, lon_name,
        )

        # Interpolate instantaneous variables to rsds time axis (Scheme 4)
        tas_time = tas_da[time_name].values
        uas_time = uas_da[time_name].values
        need_interp_tas = not np.array_equal(tas_time, target_times)
        need_interp_wind = not np.array_equal(uas_time, target_times)

        if need_interp_tas:
            logger.info("Interpolating tas to rsds time axis")
            temp_C = tas_to_celsius(
                interp_instantaneous_to_target(tas_da, time_name, target_times),
                tas_units,
                allow_inference=self.allow_unit_inference,
            )
        else:
            temp_C = tas_to_celsius(tas_da.values, tas_units, allow_inference=self.allow_unit_inference)

        if need_interp_wind:
            logger.info("Interpolating uas/vas to rsds time axis")
            uas_interp = interp_instantaneous_to_target(uas_da, time_name, target_times)
            vas_interp = interp_instantaneous_to_target(vas_da, time_name, target_times)
            wind_ms = np.sqrt(uas_interp ** 2 + vas_interp ** 2)
        else:
            wind_ms = np.sqrt(
                uas_da.values.astype(np.float32) ** 2
                + vas_da.values.astype(np.float32) ** 2
            )

        # rsds unit conversion (BCSD rsds is typically already W m-2)
        rsds_wm2 = rsds_to_wm2(
            rsds_da.values, rsds_units,
            timestep_seconds=3 * 3600,
            allow_inference=self.allow_unit_inference,
        )

        # pr handling
        precip_mmh = None
        if pr_da is not None:
            pr_time = pr_da[time_name].values
            if not np.array_equal(pr_time, target_times):
                raise ValueError(
                    "pr time axis does not match rsds time axis. "
                    "Do not silently intersect or interpolate accumulated precipitation. "
                    "Check your input data or provide matching time axes."
                )
            precip_mmh = pr_to_mmh(
                pr_da.values, pr_units,
                timestep_hours=3.0,
                allow_inference=self.allow_unit_inference,
            )

        # Build dataset
        data_vars = {
            "temp_C": xr.DataArray(temp_C, dims=(time_name, lat_name, lon_name)),
            "wind_ms": xr.DataArray(wind_ms, dims=(time_name, lat_name, lon_name)),
            "rsds": xr.DataArray(rsds_wm2, dims=(time_name, lat_name, lon_name)),
        }
        if precip_mmh is not None:
            data_vars["precip_mmh"] = xr.DataArray(precip_mmh, dims=(time_name, lat_name, lon_name))

        ds = xr.Dataset(
            data_vars,
            coords={
                time_name: rsds_da[time_name],
                lat_name: rsds_da[lat_name],
                lon_name: rsds_da[lon_name],
            },
        )

        # Mark unavailable inputs
        skipped_inputs.setdefault("rh_pct", "no humidity data in BCSD")
        skipped_inputs.setdefault("dust_aod", "no dust data in BCSD")

        interp_desc = []
        if need_interp_tas:
            interp_desc.append("tas→rsds")
        if need_interp_wind:
            interp_desc.append("uas/vas→rsds")
        time_alignment = ("rsds half-point, " + ", ".join(interp_desc) + " interpolated") if interp_desc else "rsds native"

        return WeatherBundle(
            source="regional_bcsd",
            tech="solar",
            dataset=ds,
            spatial_dims=(lat_name, lon_name),
            grid_kind="regular_latlon",
            target_time_axis=time_alignment,
            source_timestep_hours=3.0,
            source_files=source_files,
            skipped_inputs=skipped_inputs,
            attrs_extra={
                "model": task["model"],
                "region": task["region"],
                "scenario": task["scenario"],
                "year": str(year),
                "time_alignment": time_alignment,
            },
        )
