#!/usr/bin/env python3
"""Step 1: precompute ERA5Land 2015-2025 sparse station low-resource thresholds.

This is the shared first step for both workflows:

1. Two-stage workflow:
   step1_low_resource_thresholds.py -> step2_complete_extreme_events.py
2. Three-stage workflow:
   step1_low_resource_thresholds.py -> step2_split_E1_weather_extremes.py
   -> step2_split_E2_low_resource.py
"""
from scripts.precompute_station_low_resource_thresholds import main


if __name__ == "__main__":
    main()
