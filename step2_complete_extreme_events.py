#!/usr/bin/env python3
"""Step 2, complete workflow: compute future station events including low_resource.

Run this after ``step1_low_resource_thresholds.py`` when target SSP CF files are
already available. This entry calls the direct station pipeline with its default
low-resource calculation enabled.
"""
from scripts.station_signals_direct import main


if __name__ == "__main__":
    main()
