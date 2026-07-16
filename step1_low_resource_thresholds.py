#!/usr/bin/env python3
"""步骤 1：预计算 ERA5Land 2015-2025 SSP 场站稀疏低资源阈值。

这是两阶段和三阶段流程共用的第一步：

1. 两阶段流程：
    step1_low_resource_thresholds.py -> step2_complete_extreme_events.py
2. 三阶段流程：
    step1_low_resource_thresholds.py -> step2_split_E1_weather_extremes.py
    -> step2_split_E2_low_resource.py

使用示例:
python step1_low_resource_thresholds.py --stations_csv data/stations/stations_SSP1-2.6.csv  --tech wind --baseline_years 2015-2025
python step1_low_resource_thresholds.py --stations_csv data/stations/stations_SSP1-2.6.csv  --tech solar --baseline_years 2015-2025

python step1_low_resource_thresholds.py --stations_csv data/stations/stations_SSP2-4.5.csv  --tech wind --baseline_years 2015-2025
python step1_low_resource_thresholds.py --stations_csv data/stations/stations_SSP2-4.5.csv  --tech solar --baseline_years 2015-2025

python step1_low_resource_thresholds.py --stations_csv data/stations/stations_SSP5-6.0.csv  --tech wind --baseline_years 2015-2025
python step1_low_resource_thresholds.py --stations_csv data/stations/stations_SSP5-6.0.csv  --tech solar --baseline_years 2015-2025
"""

from scripts.precompute_station_low_resource_thresholds import main
from tools.logging_utils import setup_entry_logging

if __name__ == "__main__":
    setup_entry_logging("step1_low_resource_thresholds")
    main()
