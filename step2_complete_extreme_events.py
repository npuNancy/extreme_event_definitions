#!/usr/bin/env python3
"""统一步骤 2：基于 Regional BCSD 一次生成全部场站极端事件。

普通事件和低资源事件均直接使用同一批 BCSD 场站气象数据；低资源事件的风速/辐照
气候态与 P5 阈值在当前任务的 2015-2024 基线内计算，不依赖 CF 或独立阈值文件。
"""
from scripts.station_signals_direct import main
from tools.logging_utils import setup_entry_logging


if __name__ == "__main__":
    setup_entry_logging("step2_complete_extreme_events")
    main()
