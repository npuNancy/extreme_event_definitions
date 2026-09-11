#!/usr/bin/env python3
"""已废弃的历史入口。

普通事件和低资源事件现在由 ``step2_complete_extreme_events.py`` 一次生成；此入口不再
读取 CF，也不再执行低资源补写。
"""

if __name__ == "__main__":
    raise SystemExit(
        "step2_split_E2_low_resource.py 已废弃，请运行 "
        "step2_complete_extreme_events.py；低资源事件直接使用 BCSD 风速/辐照计算。"
    )
