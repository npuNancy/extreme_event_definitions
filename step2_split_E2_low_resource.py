#!/usr/bin/env python3
"""步骤 2（三阶段流程 E2）：生成或补写未来 low_resource 场站输出。

该入口是三阶段流程的第三步。有 E1 场站文件时原位补写
``signal_low_resource``；有场站但 E1 文件缺失时创建只含低资源事件的兼容文件；
区域内无对应技术场站时成功跳过。
"""
from scripts.patch_pipelineB_low_resource import main
from tools.logging_utils import setup_entry_logging


if __name__ == "__main__":
    setup_entry_logging("step2_split_E2_low_resource")
    main()
