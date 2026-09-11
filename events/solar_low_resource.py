"""光伏 · 低资源 low_resource (P5)
判定: 24h 居中滚动 BCSD 辐照(rsds) − clim288 距平 <= 每站 P5; 夜间(太阳高度角<=0)置 0
标定: 真实场站 暴露5.97% 加权损失方向97.3% 净损失39.6%

用法:
resource 为 BCSD 降尺度地表短波辐照 ``rsds`` (T,K)，单位 W/m²；
``lat``/``lon`` 为场站经纬度，用于夜间过滤。3 小时 BCSD 时间轴需传入
``window_steps=8``。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import common

TECH, NAME, LABEL = "solar", "low_resource", "低资源"
PCT = 5.0   # 改这里
EXPR = f"24h BCSD辐照距平 <= 每站 P{PCT} (夜间置0)"


def signal(resource, time, lat, lon, base_mask=None, clim_tbl=None, thr=None,
           window_steps=None, mark_next_step=True):
    """resource = 辐照 rsds (T,K); lat/lon=(K,); 返回 (T,K) bool。"""
    night = (common.solar_elevation(lat, lon, time) <= 0).T   # (K,T)->(T,K)
    return common.low_resource(resource, time, pct=PCT, base_mask=base_mask,
                               night=night, clim_tbl=clim_tbl, thr=thr,
                               window_steps=window_steps,
                               mark_next_step=mark_next_step)
