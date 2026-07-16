"""极端天气损失事件 —— 最终确认定义 (风电 / 光伏)

本模块给出经真实场站(2023-2024 实测出力)+ 中国/美国采样网格验证后**最终确认**的
极端天气损失事件判定阈值，供同学直接复用。每个事件 = 在 (time, station) 气象数组上的
一个布尔掩码 (True=该站-小时发生该事件)。

------------------------------------------------------------------------------
变量约定 (单位 / 高度)  —— 调用方需按此口径准备气象数据
------------------------------------------------------------------------------
  temp_C        2 米气温            [°C]      (ERA5-Land t2m - 273.15)
  rh_pct        2 米相对湿度        [%]       (Magnus 公式: t2m + 2m 露点 d2m)
  wind_ms       10 米风速           [m/s]     (sqrt(u10^2 + v10^2))
  precip_mmh    逐小时降水          [mm/h]    (ERA5-Land tp, 已去累积)
  dust_aod      沙尘气溶胶光学厚度  [1]       (MERRA-2 DUEXTTAU, 逐小时)
  resource      资源量             [-]        (风: 10m 风速; 光: 地表辐照 rsds W/m^2)
                  -- 仅 low_resource 事件需要; 见 low_resource_signal()

算子约定: 低温/结冰/冻雨/低温大风 用 "<"; 高温 用 ">"; 高湿 用 ">="; 降水/风速/沙尘 用 ">".

------------------------------------------------------------------------------
最终阈值 (源: 真实场站 2023-2024 实测验证)
------------------------------------------------------------------------------
风电 WIND:
  低资源 low_resource   : 24h 资源距平 <= 每站 P5   (见 low_resource_signal)
  结冰   icing          : temp_C < -2 & rh_pct >= 85 (与光伏统一阈值; 风电 temp<0 也是强损失,单看风电可放宽)
  高温   high_temp      : temp_C > 35                 (注: 高温对风电是最弱净损失(仅22%,接近中性),按需取用)
  高温高湿 hot_humid    : temp_C > 30 & rh_pct >= 85
  大风   high_wind      : wind_ms > 17                (10m 口径; 真实场站极罕见)
光伏 SOLAR:
  低资源 low_resource   : 24h 资源距平 <= 每站 P5   (夜间置 0)
  冻雨   freezing_rain  : temp_C < 3  & precip_mmh > 0
  结冰   icing          : temp_C < -2 & rh_pct >= 85 (注: 比风电更冷; 0℃附近晴空对光伏是增益,
                                                       -1℃为净损失转折点, 取 -2℃ 更纯净)
  沙尘   dust           : dust_aod > 0.4
  暴雨   rainstorm      : precip_mmh > 2
  高湿度 high_humidity  : rh_pct >= 95
  低温大风 cold_highwind: temp_C < 5  & wind_ms > 8
"""
from __future__ import annotations
import logging
import numpy as np

from tools.logging_utils import setup_logging

logger = logging.getLogger(__name__)

# ============================================================================
# 1) 简单阈值事件 —— 每个返回 (time, station) 的 bool 掩码
#    w: dict[str, np.ndarray]，键为上面的变量名，值为 (T, K) 数组
# ============================================================================

EVENT_DEFS = {
    "wind": {
        "icing":       {"label": "结冰",   "expr": "temp_C < -2 & rh_pct >= 85",
                        "fn": lambda w: (w["temp_C"] < -2) & (w["rh_pct"] >= 85)},
        "high_temp":   {"label": "高温",   "expr": "temp_C > 35",
                        "fn": lambda w: w["temp_C"] > 35},
        "hot_humid":   {"label": "高温高湿", "expr": "temp_C > 30 & rh_pct >= 85",
                        "fn": lambda w: (w["temp_C"] > 30) & (w["rh_pct"] >= 85)},
        "high_wind":   {"label": "大风",   "expr": "wind_ms > 17",
                        "fn": lambda w: w["wind_ms"] > 17},
        # low_resource: 见 low_resource_signal()
    },
    "solar": {
        "freezing_rain": {"label": "冻雨", "expr": "temp_C < 3 & precip_mmh > 0",
                          "fn": lambda w: (w["temp_C"] < 3) & (w["precip_mmh"] > 0)},
        "icing":         {"label": "结冰", "expr": "temp_C < -2 & rh_pct >= 85",
                          "fn": lambda w: (w["temp_C"] < -2) & (w["rh_pct"] >= 85)},
        "dust":          {"label": "沙尘", "expr": "dust_aod > 0.4",
                          "fn": lambda w: w["dust_aod"] > 0.4},
        "rainstorm":     {"label": "暴雨", "expr": "precip_mmh > 2",
                          "fn": lambda w: w["precip_mmh"] > 2},
        "high_humidity": {"label": "高湿度", "expr": "rh_pct >= 95",
                          "fn": lambda w: w["rh_pct"] >= 95},
        "cold_highwind": {"label": "低温大风", "expr": "temp_C < 5 & wind_ms > 8",
                          "fn": lambda w: (w["temp_C"] < 5) & (w["wind_ms"] > 8)},
        # low_resource: 见 low_resource_signal()
    },
}


def event_signal(tech: str, name: str, weather: dict) -> np.ndarray:
    """返回某简单阈值事件的 bool 掩码 (T, K)。
    tech in {'wind','solar'}; name 见 EVENT_DEFS; weather 为变量名->(T,K) 数组。
    NaN 输入处自动为 False (NaN 比较结果为 False)。不做插补。"""
    d = EVENT_DEFS[tech][name]
    m = d["fn"](weather)
    return np.asarray(m, dtype=bool)


def all_event_signals(tech: str, weather: dict) -> dict:
    """一次返回该技术的全部简单阈值事件掩码 {name: (T,K) bool}。
    (low_resource 需单独调用 low_resource_signal)"""
    return {name: event_signal(tech, name, weather) for name in EVENT_DEFS[tech]}


# ============================================================================
# 2) 低资源 low_resource (P5) —— 需要资源时序 + 气候态基线
#    口径: 24h 居中滚动资源 − clim288(月×时, 基线期) 的距平 <= 每站 P5。
#          光伏夜间(太阳高度角<=0)强制为 0; 不完整 24h 窗口 -> 无事件。
# ============================================================================

def roll24_centered(resource: np.ndarray, half_back: int = 11, half_fwd: int = 12) -> np.ndarray:
    """24h 居中滚动均值 (T,K)。窗口不完整(NaN)的时刻 -> NaN。"""
    T, K = resource.shape
    out = np.full((T, K), np.nan, np.float32)
    cs = np.cumsum(np.where(np.isfinite(resource), resource, 0.0), axis=0)
    cn = np.cumsum(np.isfinite(resource).astype(np.int32), axis=0)
    for t in range(T):
        a = max(0, t - half_back); b = min(T, t + half_fwd + 1)
        s = cs[b - 1] - (cs[a - 1] if a > 0 else 0.0)
        n = cn[b - 1] - (cn[a - 1] if a > 0 else 0)
        win = b - a
        full = n == win                      # 窗口内无缺测
        out[t] = np.where(full, s / np.maximum(n, 1), np.nan)
    return out


def clim288(roll: np.ndarray, time, base_mask=None) -> np.ndarray:
    """月×时 (12,24,K) 气候态。time 为 pandas DatetimeIndex; base_mask 选基线期(如1987-2016)。"""
    import pandas as pd
    t = pd.DatetimeIndex(time)
    mo = t.month.to_numpy() - 1; hr = t.hour.to_numpy()
    sel0 = np.ones(len(t), bool) if base_mask is None else np.asarray(base_mask, bool)
    K = roll.shape[1]; tbl = np.full((12, 24, K), np.nan, np.float32)
    for m in range(12):
        for h in range(24):
            idx = sel0 & (mo == m) & (hr == h)
            if idx.any():
                tbl[m, h] = np.nanmean(roll[idx], axis=0)
    return tbl


def low_resource_signal(resource: np.ndarray, time, pct: float = 5.0,
                        base_mask=None, night=None,
                        clim_tbl=None, thr=None):
    """低资源 P5 信号 (T,K) bool。
    resource : 资源时序 (风: 10m 风速; 光: 辐照 rsds) (T,K)
    time     : DatetimeIndex (T,)
    pct      : 距平百分位 (默认 5 = P5)
    base_mask: 基线期布尔 (T,) (默认全期; 推荐 1987-2016 固定基线以利逐年可比)
    night    : 光伏夜间布尔 (T,K) (太阳高度角<=0), 这些处强制无事件; 风电传 None
    clim_tbl/thr: 可传入预计算的气候态(12,24,K)与每站阈值(K,)以复用; 否则内部计算。
    返回: bool (T,K)。不完整 24h 窗口 / NaN -> False。"""
    import pandas as pd
    t = pd.DatetimeIndex(time)
    roll = roll24_centered(resource)
    if clim_tbl is None:
        clim_tbl = clim288(roll, t, base_mask)
    base = clim_tbl[t.month.to_numpy() - 1, t.hour.to_numpy()]   # (T,K)
    anom = roll - base
    if thr is None:
        ab = anom if base_mask is None else anom[np.asarray(base_mask, bool)]
        thr = np.nanpercentile(np.where(np.isfinite(ab), ab, np.nan), pct, axis=0)  # (K,)
    sig = (anom <= thr[None, :]) & np.isfinite(anom)
    if night is not None:
        sig = sig & (~np.asarray(night, bool))
    return sig.astype(bool)


# ============================================================================
# 3) 用法示例
# ============================================================================
if __name__ == "__main__":
    # weather: 每个变量是 (T,K) numpy 数组, 单位见文件头
    T, K = 100, 5
    rng = np.random.default_rng(0)
    weather = {
        "temp_C": rng.normal(5, 10, (T, K)).astype("f4"),
        "rh_pct": rng.uniform(40, 100, (T, K)).astype("f4"),
        "wind_ms": rng.uniform(0, 20, (T, K)).astype("f4"),
        "precip_mmh": rng.exponential(0.3, (T, K)).astype("f4"),
        "dust_aod": rng.uniform(0, 0.8, (T, K)).astype("f4"),
    }
    setup_logging("extreme_event_definitions")
    logger.info("风电事件掩码占比：")
    for name, m in all_event_signals("wind", weather).items():
        logger.info("  %-6s %-14s %-30s -> %.1f%%",
                    EVENT_DEFS["wind"][name]["label"], name,
                    EVENT_DEFS["wind"][name]["expr"], m.mean() * 100)
    logger.info("光伏事件掩码占比：")
    for name, m in all_event_signals("solar", weather).items():
        logger.info("  %-6s %-14s %-30s -> %.1f%%",
                    EVENT_DEFS["solar"][name]["label"], name,
                    EVENT_DEFS["solar"][name]["expr"], m.mean() * 100)
