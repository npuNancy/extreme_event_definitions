# 极端天气损失事件定义 + 数据调用（风电 / 光伏）

> 最终确认的事件阈值（真实场站 2023-2024 实测 + 中美采样网格验证）。
> **每种天气一个文件**，改阈值只动对应文件顶部常量。仅依赖 numpy / pandas / xarray。

## 目录结构
```
extreme_event_definitions/
  README.md
  registry.py              # 汇总注册表: simple_signals() 一次取全部
  common.py                # 低资源共享算法(24h滚动+clim288+P5, 太阳高度角)
  weather_loaders.py       # ★ ERA5-Land + MERRA-2 数据调用 -> weather 字典
  events/
    wind_low_resource.py     风电 低资源   24h资源距平<=P5
    wind_icing.py            风电 结冰     temp_C<-2 & rh_pct>=85 (与光伏统一阈值)
    wind_high_temp.py        风电 高温     temp_C>35           (最弱损失,净22%,参考)
    wind_hot_humid.py        风电 高温高湿 temp_C>30 & rh_pct>=85
    wind_high_wind.py        风电 大风     wind_ms>17 (10m)
    solar_low_resource.py    光伏 低资源   24h辐照距平<=P5(夜间置0)
    solar_freezing_rain.py   光伏 冻雨     temp_C<3 & precip_mmh>0
    solar_icing.py           光伏 结冰     temp_C<-2 & rh_pct>=85 (比风电更冷,0℃附近是增益)
    solar_dust.py            光伏 沙尘     dust_aod>0.4
    solar_rainstorm.py       光伏 暴雨     precip_mmh>2
    solar_high_humidity.py   光伏 高湿度   rh_pct>=95
    solar_cold_highwind.py   光伏 低温大风 temp_C<5 & wind_ms>8
```
**改阈值**：打开对应 `events/xxx.py`，改文件顶部 `====阈值====` 区的常量即可。

## 变量约定（weather 字典；单位/高度务必一致）
| 键 | 含义 | 单位 | 高度/来源 |
|---|---|---|---|
| temp_C | 气温 | °C | 2m (t2m−273.15) |
| rh_pct | 相对湿度 | % | 2m (Magnus: t2m+d2m) |
| wind_ms | 风速 | m/s | 10m (√(u10²+v10²)) |
| precip_mmh | 降水 | mm/h | tp 去累积 |
| dust_aod | 沙尘AOD | 1 | MERRA-2 DUEXTTAU |
| (resource) | 资源 | — | 仅低资源: 风=wind_ms, 光=辐照rsds |

数据形状统一 `(time, station)`；不插补，NaN→事件 False。

## 用法
```python
# 1) 准备 weather 字典(三选一, 见 weather_loaders.py)
from weather_loaders import load_covariates
weather, time, lat, lon = load_covariates("covariates_2024.nc")   # 最省事

# 2) 简单阈值事件
from registry import simple_signals
masks = simple_signals("solar", weather)     # {name:(T,K) bool}

# 3) 低资源(需资源时序)
from events import solar_low_resource, wind_low_resource
lr_s = solar_low_resource.signal(rsds, time, lat, lon, base_mask=base_1987_2016)
lr_w = wind_low_resource.signal(wind10m, time, base_mask=base_1987_2016)
```

## 数据调用（ERA5-Land + MERRA-2）—— 见 `weather_loaders.py`
- **入口A** `load_covariates(nc)`：读现成 `covariates_{year}.nc`（最省事）。
- **入口B** `build_from_extracted(root,year)`：从 ERA5-Land 站点抽取 `extracted/{var}/` + MERRA-2 沙尘组装。
- **入口C** 从零：原始 ERA5-Land 全球月文件 → 用权威脚本
  `extreme_signals_pipeline/pipeline/extract.py`（含 tp/ssrd **去累积** hour==1 规则）
  → `build_covariates.py`（RH + 沙尘）。

数据源（Spark02）：
- ERA5-Land：`/data1/luobaozhen/era5_download/ERA5_land/global/{u10,v10,t2m,tp,ssrd,d2t}/{var}_{YYYY}_{MM}.nc`
- MERRA-2：`/data3/luobaozhen/MERRA2/M2T1NXAER/DUEXTTAU/{YYYY}/{MM}/MERRA2.tavg1_2d_aer_Nx.{YYYYMMDD}.nc4`

## 阈值标定来源
真实场站口径结果：`real_station_analysis_lbz/analysis/event_eval/real_events_table_*.csv`；
指标定义：`real_station_analysis_lbz/整理/METRICS.md`（加权损失方向率/净损失率/暴露率）。

## 注意
- **低资源**最特殊：基线期固定 1987-2016（逐年可比）；光伏务必传夜间过滤（solar_low_resource 内部按太阳高度角处理）。
- **高温**(风电)为最弱净损失(仅22%,接近中性)、**大风**(10m)极罕见且切出需轮毂高度——见各文件内注释。
