"""光伏 · 低资源 low_resource (P5)
判定: 24h 居中滚动辐照(rsds) − clim288 距平 <= 每站 P5; 夜间(太阳高度角<=0)置 0
标定: 真实场站 暴露5.97% 加权损失方向97.3% 净损失39.6%

用法:
    from events.solar_low_resource import signal
    mask = signal(resource_rsds, time, lat, lon, base_mask=base_1987_2016)
其中 resource = 地表辐照 rsds W/m^2 (T,K); time = DatetimeIndex; lat/lon = (K,) 用于夜间过滤。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import common

TECH, NAME, LABEL = "solar", "low_resource", "低资源"
PCT = 5.0   # 改这里
EXPR = f"24h辐照距平 <= 每站 P{PCT} (夜间置0)"


def signal(resource, time, lat, lon, base_mask=None, clim_tbl=None, thr=None,
           window_steps=None, mark_next_step=True):
    """resource = 辐照 rsds (T,K); lat/lon=(K,); 返回 (T,K) bool。"""
    night = (common.solar_elevation(lat, lon, time) <= 0).T   # (K,T)->(T,K)
    return common.low_resource(resource, time, pct=PCT, base_mask=base_mask,
                               night=night, clim_tbl=clim_tbl, thr=thr,
                               window_steps=window_steps,
                               mark_next_step=mark_next_step)
