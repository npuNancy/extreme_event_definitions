#!/usr/bin/env python3
"""Step 2, split workflow E2: patch future low_resource into E1 station outputs.

This entry is the third stage of the three-stage workflow. It expects existing
station signal files from ``step2_split_E1_weather_extremes.py`` and writes
``signal_low_resource`` into those files in place.
"""
from scripts.patch_pipelineB_low_resource import main


if __name__ == "__main__":
    main()
