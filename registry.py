"""事件注册表 —— 汇总所有"每事件一个文件"的定义，方便批量调用。

普通阈值事件(只需 weather 字典):
    from registry import SIMPLE, simple_signals
    masks = simple_signals("solar", weather)        # {name:(T,K) bool}
    one  = SIMPLE["wind"]["icing"].signal(weather)

低资源事件使用 BCSD 标准化资源时序，与普通事件在同一 pipeline 中写出:
    masks = all_signals("solar", weather, time, lat=lat, lon=lon,
                        base_mask=base_mask, window_steps=8)
"""
import logging

from events import (wind_icing, wind_high_temp, wind_hot_humid, wind_high_wind,
                    solar_freezing_rain, solar_dust, solar_rainstorm,
                    solar_high_humidity, solar_cold_highwind, solar_icing,
                    wind_low_resource, solar_low_resource)

logger = logging.getLogger(__name__)

# 普通阈值事件(输入仅 weather 字典)
SIMPLE = {
    "wind": {m.NAME: m for m in [wind_icing, wind_high_temp, wind_hot_humid, wind_high_wind]},
    "solar": {m.NAME: m for m in [solar_freezing_rain, solar_dust, solar_rainstorm,
                                  solar_high_humidity, solar_cold_highwind, solar_icing]},
}
# 低资源事件(使用 BCSD 的 wind_ms 或 rsds)
LOWRES = {"wind": wind_low_resource, "solar": solar_low_resource}
LOWRES_RESOURCE = {"wind": "wind_ms", "solar": "rsds"}

# Required weather fields also define each event's validity mask on a grid.
REQUIRED = {
    "wind": {"icing": ("temp_C", "rh_pct"), "high_temp": ("temp_C",),
             "hot_humid": ("temp_C", "rh_pct"), "high_wind": ("wind_ms",)},
    "solar": {"freezing_rain": ("temp_C", "precip_mmh"), "dust": ("dust_aod",),
              "rainstorm": ("precip_mmh",), "high_humidity": ("rh_pct",),
              "cold_highwind": ("temp_C", "wind_ms"), "icing": ("temp_C", "rh_pct")},
}


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


def low_resource_signal(
    tech,
    weather,
    time,
    *,
    lat=None,
    lon=None,
    base_mask=None,
    window_steps=None,
    clim_tbl=None,
    thr=None,
    mark_next_step=True,
):
    """计算当前技术的 BCSD 低资源信号。"""
    resource_name = LOWRES_RESOURCE[tech]
    resource = weather[resource_name]
    kwargs = {
        "base_mask": base_mask,
        "window_steps": window_steps,
        "clim_tbl": clim_tbl,
        "thr": thr,
        "mark_next_step": mark_next_step,
    }
    if tech == "solar":
        if lat is None or lon is None:
            raise ValueError("solar 低资源计算需要场站 lat/lon")
        kwargs.update(lat=lat, lon=lon)
    return LOWRES[tech].signal(resource, time, **kwargs)


def all_signals(
    tech,
    weather,
    time,
    *,
    lat=None,
    lon=None,
    base_mask=None,
    window_steps=None,
    clim_tbl=None,
    thr=None,
    skip_missing=True,
):
    """一次返回普通事件和 BCSD 低资源事件。"""
    masks = simple_signals(tech, weather, skip_missing=skip_missing)
    masks["low_resource"] = low_resource_signal(
        tech,
        weather,
        time,
        lat=lat,
        lon=lon,
        base_mask=base_mask,
        window_steps=window_steps,
        clim_tbl=clim_tbl,
        thr=thr,
    )
    return masks


def list_all():
    logger.info("普通阈值事件：")
    for tech in ("wind", "solar"):
        for name, m in SIMPLE[tech].items():
            logger.info("  [%s] %-6s %-14s : %s", tech, m.LABEL, name, m.EXPR)
    logger.info("低资源事件（BCSD 资源时序）：")
    for tech, m in LOWRES.items():
        logger.info("  [%s] %-6s %-14s : %s", tech, m.LABEL, m.NAME, m.EXPR)
