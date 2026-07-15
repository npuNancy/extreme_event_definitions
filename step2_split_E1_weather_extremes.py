#!/usr/bin/env python3
"""Step 2, split workflow E1: compute future station events excluding low_resource.

This stage writes the normal station-level extreme-event NetCDF files first.
``step2_split_E2_low_resource.py`` can then patch ``signal_low_resource`` into
those same files after target SSP CF files are ready.
"""
from __future__ import annotations

import sys

from scripts.station_signals_direct import main


def _force_no_low_resource() -> None:
    if "--no_low_resource" not in sys.argv:
        sys.argv.append("--no_low_resource")


if __name__ == "__main__":
    _force_no_low_resource()
    main()
