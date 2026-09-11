#!/usr/bin/env python3
"""历史兼容模块：旧的 ERA5Land CF 低资源阈值入口。

这是两阶段和三阶段流程共用的第一步：

1. 两阶段流程：
    step1_low_resource_thresholds.py -> step2_complete_extreme_events.py
2. 三阶段流程：
    step1_low_resource_thresholds.py -> step2_split_E1_weather_extremes.py
    -> step2_split_E2_low_resource.py

使用示例:
python step1_low_resource_thresholds.py --stations_csv data/stations/stations_SSP1-2.6.csv  --tech wind --baseline_years 2015-2024
python step1_low_resource_thresholds.py --stations_csv data/stations/stations_SSP1-2.6.csv  --tech solar --baseline_years 2015-2024

python step1_low_resource_thresholds.py --stations_csv data/stations/stations_SSP2-4.5.csv  --tech wind --baseline_years 2015-2024
python step1_low_resource_thresholds.py --stations_csv data/stations/stations_SSP2-4.5.csv  --tech solar --baseline_years 2015-2024

python step1_low_resource_thresholds.py --stations_csv data/stations/stations_SSP5-6.0.csv  --tech wind --baseline_years 2015-2024
python step1_low_resource_thresholds.py --stations_csv data/stations/stations_SSP5-6.0.csv  --tech solar --baseline_years 2015-2024
"""

if __name__ == "__main__":
    raise SystemExit(
        "step1_low_resource_thresholds.py 已废弃；低资源事件现在直接使用 BCSD 风速/辐照，"
        "请运行 step2_complete_extreme_events.py。"
    )
