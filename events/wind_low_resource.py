"""风电 · 低资源 low_resource (P5)
判定: 24h 居中滚动 10m 风速 − clim288 距平 <= 每站 P5
标定: 真实场站 暴露5.87% 加权损失方向97.4% 净损失80.6% (风电最严重事件)

用法与简单阈值事件不同 —— 需要资源时序 + 时间轴:
    from events.wind_low_resource import signal
    mask = signal(resource_wind10m, time, base_mask=base_1987_2016)
其中 resource = 10m 风速 (T,K); time = DatetimeIndex; 不需要 night。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import common

TECH, NAME, LABEL = "wind", "low_resource", "低资源"
PCT = 5.0   # 改这里
EXPR = f"24h资源距平 <= 每站 P{PCT}"


def signal(resource, time, base_mask=None, clim_tbl=None, thr=None,
           window_steps=None, mark_next_step=True):
    """resource = 10m 风速 wind_ms (T,K); 返回 (T,K) bool。"""
    return common.low_resource(resource, time, pct=PCT, base_mask=base_mask,
                               night=None, clim_tbl=clim_tbl, thr=thr,
                               window_steps=window_steps,
                               mark_next_step=mark_next_step)
