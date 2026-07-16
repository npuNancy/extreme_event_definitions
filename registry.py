"""事件注册表 —— 汇总所有"每事件一个文件"的定义，方便批量调用。

简单阈值事件(只需 weather 字典):
    from registry import SIMPLE, simple_signals
    masks = simple_signals("solar", weather)        # {name:(T,K) bool}
    one  = SIMPLE["wind"]["icing"].signal(weather)

低资源事件(需资源时序, 单独调用各自模块的 signal):
    from events import wind_low_resource, solar_low_resource
    lr_w = wind_low_resource.signal(wind10m, time, base_mask=...)
    lr_s = solar_low_resource.signal(rsds, time, lat, lon, base_mask=...)
"""
import logging

from events import (wind_icing, wind_high_temp, wind_hot_humid, wind_high_wind,
                    solar_freezing_rain, solar_dust, solar_rainstorm,
                    solar_high_humidity, solar_cold_highwind, solar_icing,
                    wind_low_resource, solar_low_resource)
from tools.logging_utils import setup_logging

logger = logging.getLogger(__name__)

# 简单阈值事件(输入仅 weather 字典)
SIMPLE = {
    "wind": {m.NAME: m for m in [wind_icing, wind_high_temp, wind_hot_humid, wind_high_wind]},
    "solar": {m.NAME: m for m in [solar_freezing_rain, solar_dust, solar_rainstorm,
                                  solar_high_humidity, solar_cold_highwind, solar_icing]},
}
# 低资源事件(需资源时序, 签名不同)
LOWRES = {"wind": wind_low_resource, "solar": solar_low_resource}


def simple_signals(tech, weather, skip_missing=True):
    """该技术全部简单阈值事件掩码 {name:(T,K) bool}。
    skip_missing=True 时，weather 缺所需变量的事件被跳过并写日志，而非报错。"""
    out = {}
    for name, mod in SIMPLE[tech].items():
        try:
            out[name] = mod.signal(weather)
        except KeyError as e:
            if skip_missing:
                logger.info("[跳过] %s/%s：weather 缺变量 %s", tech, name, e)
            else:
                raise
    return out


def list_all():
    logger.info("简单阈值事件：")
    for tech in ("wind", "solar"):
        for name, m in SIMPLE[tech].items():
            logger.info("  [%s] %-6s %-14s : %s", tech, m.LABEL, name, m.EXPR)
    logger.info("低资源事件（需资源时序）：")
    for tech, m in LOWRES.items():
        logger.info("  [%s] %-6s %-14s : %s", tech, m.LABEL, m.NAME, m.EXPR)


if __name__ == "__main__":
    setup_logging("registry")
    list_all()
