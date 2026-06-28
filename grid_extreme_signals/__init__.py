"""Multi-source grid extreme weather signal pipeline.

Provides adapters for four climate data sources:
  - regional_bcsd   (CMIP6–ERA5Land BCSD, regular lat/lon)
  - china_cmfd_bcsd (CMIP6–CMFD BCSD, regular lat/lon, uses sfcWind)
  - cordex_nam12    (CMIP6–CORDEX NAM-12, rotated pole grid)
  - era5land_raw    (raw ERA5-Land global hourly)
"""
from grid_extreme_signals.adapters.base import WeatherAdapter, WeatherBundle

__all__ = ["WeatherAdapter", "WeatherBundle"]
