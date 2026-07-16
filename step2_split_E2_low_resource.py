#!/usr/bin/env python3
"""步骤 2（三阶段流程 E2）：把未来 low_resource 补写进 E1 场站输出。

该入口是三阶段流程的第三步。它读取 ``step2_split_E1_weather_extremes.py`` 已生成的
场站信号文件，并在原文件中补写 ``signal_low_resource``。
"""
from scripts.patch_pipelineB_low_resource import main
from tools.logging_utils import setup_entry_logging


if __name__ == "__main__":
    setup_entry_logging("step2_split_E2_low_resource")
    main()
