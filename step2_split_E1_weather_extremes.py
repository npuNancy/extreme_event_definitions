#!/usr/bin/env python3
"""步骤 2（三阶段流程 E1）：计算未来场站极端事件，不包含 low_resource。

本阶段先写出普通场站级极端事件 NetCDF 文件。目标 SSP 的未来 CF 文件准备好后，
``step2_split_E2_low_resource.py`` 会把 ``signal_low_resource`` 补写到同一批文件中。
"""
from __future__ import annotations

import sys

from scripts.station_signals_direct import main
from tools.logging_utils import setup_entry_logging


def _force_no_low_resource() -> None:
    if "--no_low_resource" not in sys.argv:
        sys.argv.append("--no_low_resource")


if __name__ == "__main__":
    setup_entry_logging("step2_split_E1_weather_extremes")
    _force_no_low_resource()
    main()
