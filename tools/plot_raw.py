#!/usr/bin/env python
"""绘制 raw ERA5-Land 基准测试：端到端耗时主要受固定 bbox I/O 支配。"""
import logging
import os
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.logging_utils import setup_logging

logger = logging.getLogger(__name__)

# (场站数、选择并构建、简单事件、风电低资源、光伏低资源、随 K 变化部分)，单位毫秒 [中国 bbox，1 个月]
ROWS = [
    (10,      0.7,  0.04,    4.8,    5.8,    11.4),
    (100,     4.4,  0.10,   14.3,   17.3,    36.2),
    (1000,   46.7,  0.62,  119.8,  148.1,   315.2),
    (5000,  286.2,  2.82,  541.6,  689.4,  1519.9),
    (10000, 597.7,  5.80, 1109.6, 1413.9,  3127.0),
    (20000,1247.2, 12.28, 2271.7, 2918.2,  6449.4),
]
IO_COLD = 112.64   # 秒，冷启动读取（首次运行）
IO_WARM = 74.57    # 秒，热缓存读取（页缓存重复读取）

K = np.array([r[0] for r in ROWS], float)
kpart = np.array([r[5] for r in ROWS]) / 1000.0           # 秒
build = np.array([r[1] for r in ROWS])
loww = np.array([r[3] for r in ROWS])
lows = np.array([r[4] for r in ROWS])

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))

# 图 A：端到端耗时（冷启动 I/O 底噪 + 随 K 变化部分），展示 I/O 主导
ax1.axhline(IO_COLD, color="firebrick", ls="--", lw=1.5, label=f"固定 bbox I/O（冷启动）= {IO_COLD:.0f}s")
ax1.plot(K, IO_COLD + kpart, marker="o", color="firebrick", label="端到端 = I/O + 计算")
ax1.fill_between(K, IO_COLD, IO_COLD + kpart, color="orange", alpha=0.35, label="随 K 变化的计算")
ax1.set_xscale("log")
ax1.set_xlabel("场站数 K")
ax1.set_ylabel("从 raw 数据计算的端到端耗时（秒）")
ax1.set_title("raw ERA5-Land（中国 bbox，1 个月）：I/O 主导")
ax1.set_ylim(0, IO_COLD + 12)
ax1.grid(True, which="both", alpha=0.3)
ax1.legend(fontsize=9, loc="center left")
for k, e in zip(K, IO_COLD + kpart):
    ax1.annotate(f"+{(e-IO_COLD):.1f}s", (k, e), textcoords="offset points",
                 xytext=(0, 7), ha="center", fontsize=8)

# 图 B：随 K 变化的计算分解（双对数），近似随 K 线性增长
ax2.loglog(K, build, marker="o", label="选择并构建气象变量")
ax2.loglog(K, loww, marker="o", label="wind_low_resource")
ax2.loglog(K, lows, marker="o", label="solar_low_resource")
ax2.loglog(K, K / K[0] * build[0], color="gray", ls=":", label="线性参考（斜率 1）")
ax2.set_xlabel("场站数 K")
ax2.set_ylabel("计算耗时（毫秒）")
ax2.set_title("仅随 K 变化部分（计算）：近似随 K 线性增长")
ax2.grid(True, which="both", alpha=0.3)
ax2.legend(fontsize=9)

fig.suptitle("eed raw 数据效率：场站数与耗时关系（Spark02, global_era）")
fig.tight_layout()
out = "/data1/luobaozhen/extreme_event_definitions/bench/bench_raw_scaling.png"
fig.savefig(out, dpi=130)
setup_logging("plot_raw")
logger.info("已保存 %s", out)
