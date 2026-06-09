# 极端天气损失事件定义（风电 / 光伏）—— 复用说明

> 给同学的最终事件定义。阈值已经真实场站(2023-2024 实测出力)+ 中国/美国采样网格验证确认。
> 代码：`extreme_event_definitions.py`（自包含，仅依赖 numpy / pandas）。

## 一、最终阈值表

### 风电 WIND
| 风险类别 | 名称(name) | 判定阈值 | 暴露率 | 加权损失方向率 | 净损失率 |
|---|---|---|---|---|---|
| 低资源 | low_resource | 24h 资源距平 ≤ 每站 P5 | 5.87% | 97.4% | 80.6% |
| 结冰 | icing | temp_C < −2 & rh_pct ≥ 85 | 1.17% | 86.8% | 56.6% |
| 高温 | high_temp | temp_C > 35 | 0.26% | 63.1% | 22.1% |
| 高温高湿 | hot_humid | temp_C > 30 & rh_pct ≥ 85 | 0.02% | 69.4% | 50.4% |
| 大风 | high_wind | wind_ms > 17 | 0.0003% | 100% | 100% |

### 光伏 SOLAR
| 风险类别 | 名称(name) | 判定阈值 | 暴露率 | 加权损失方向率 | 净损失率 |
|---|---|---|---|---|---|
| 低资源 | low_resource | 24h 资源距平 ≤ 每站 P5（夜间置0） | 5.97% | 97.3% | 39.6% |
| 冻雨 | freezing_rain | temp_C < 3 & precip_mmh > 0 | 4.43% | 88.1% | 38.9% |
| 结冰 | icing | temp_C < −2 & rh_pct ≥ 85 | 0.32% | 90.0% | 66.9% |
| 沙尘 | dust | dust_aod > 0.4 | 0.03% | 83.4% | 65.7% |
| 暴雨 | rainstorm | precip_mmh > 2 | 1.59% | 93.2% | 57.4% |
| 高湿度 | high_humidity | rh_pct ≥ 95 | 3.86% | 81.3% | 32.5% |
| 低温大风 | cold_highwind | temp_C < 5 & wind_ms > 8 | 0.05% | 86.5% | 38.7% |

> 指标含义见 `整理/METRICS.md`：加权损失方向率(是不是损失)、净损失率(损失多少电量)、暴露率(多常发生)。

## 二、变量约定（务必按此口径准备数据）
| 变量 | 含义 | 单位 | 高度/来源 |
|---|---|---|---|
| temp_C | 气温 | °C | 2 m (ERA5-Land t2m−273.15) |
| rh_pct | 相对湿度 | % | 2 m (Magnus: t2m + 2m 露点 d2m) |
| wind_ms | 风速 | m/s | 10 m (√(u10²+v10²)) |
| precip_mmh | 降水 | mm/h | ERA5-Land tp（去累积后逐小时）|
| dust_aod | 沙尘 AOD | 1 | MERRA-2 DUEXTTAU 逐小时 |
| resource | 资源量 | — | 仅 low_resource 用：风=10m风速，光=辐照 rsds |

数据形状统一 `(time, station)`；不做插补，NaN 处事件判为 False。

## 三、用法
```python
import numpy as np
from extreme_event_definitions import all_event_signals, event_signal, low_resource_signal

# weather: {变量名: (T,K) ndarray}，单位见上
masks = all_event_signals("solar", weather)      # {name: (T,K) bool}，简单阈值事件
icing = event_signal("wind", "icing", weather)    # 单个事件

# 低资源(需资源时序 + 时间轴；光伏需夜间掩码 night=(T,K) 太阳高度角<=0)
lr = low_resource_signal(resource, time, pct=5.0,
                         base_mask=base_1987_2016,   # 推荐固定基线
                         night=night_mask_or_None)
```

## 四、注意
- **低资源**最特殊：需 24h 居中滚动 + 月×时气候态(clim288)距平 + 每站 P5。基线期建议固定
  1987-2016 以便逐年可比；光伏务必传 `night`（夜间置 0，否则夜间 cf≈0 污染）。
- **高温**对风电是最弱的净损失(净损失率仅 22%，远低于其他事件)，物理上接近中性、判别力弱，列出供参考，按需取用。
- **结冰统一用 temp<−2 & rh≥85**(风电/光伏同名事件同阈值，便于交代、防被质疑)。
  - **光伏**尤其需要更冷阈值：0℃附近低温常伴晴空，光伏反而**增益**(temp<0 net −0.044、temp<1 net −0.324)；净损失转折点在 **−1℃**(net +0.355)，−2℃ 为更纯净真结冰(net +0.669)。
  - **风电**：temp<0 也是强损失(net 0.509、捕获更全 862GWh)，temp<−2 净损失率反更高(0.566)、捕获 613GWh。统一取 −2℃；若单独评估风电、要最全捕获，可把 `wind_icing.py` 的 `TEMP_MAX_C` 改回 0.0。
- **大风(10m)**在真实场站极罕见(暴露 ~3e-6)；风机切出损失在轮毂高度(~100m≈25m/s)，
  10m 口径无法体现，如需评估切出请改用轮毂高度风速。
- 阈值标定数据：真实场站口径见 `real_station_analysis_lbz/analysis/event_eval/real_events_table_*.csv`。
