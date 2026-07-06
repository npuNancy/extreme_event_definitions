"""多数据源网格极端天气信号流程。

提供四类气候数据源适配器：
  - regional_bcsd   （CMIP6–ERA5Land BCSD，规则经纬度）
  - china_cmfd_bcsd （CMIP6–CMFD BCSD，规则经纬度，使用 sfcWind）
  - cordex_nam12    （CMIP6–CORDEX NAM-12，旋转极点网格）
  - era5land_raw    （ERA5-Land 全球逐小时原始数据）
"""
from grid_extreme_signals.adapters.base import WeatherAdapter, WeatherBundle

__all__ = ["WeatherAdapter", "WeatherBundle"]
