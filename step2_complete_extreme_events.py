#!/usr/bin/env python3
"""步骤 2（完整两阶段流程）：计算未来场站极端事件，包含 low_resource。

目标 SSP 的未来 CF 文件已经准备好时，在 ``step1_low_resource_thresholds.py`` 后运行。
该入口调用场站直接流程，并保留默认启用的低资源事件计算。
"""
from scripts.station_signals_direct import main
from tools.logging_utils import setup_entry_logging


if __name__ == "__main__":
    setup_entry_logging("step2_complete_extreme_events")
    main()
