# 极端天气损失事件定义 + 数据调用（风电 / 光伏）

> 最终确认的事件阈值（真实场站 2023-2024 实测 + 中美采样网格验证）。
> **每种天气一个文件**，改阈值只动对应文件顶部常量。仅依赖 numpy / pandas / xarray。

## 目录结构
```
extreme_event_definitions/
  README.md
  registry.py                              # 汇总注册表: simple_signals() 一次取全部
  common.py                                # 低资源共享算法(24h滚动+clim288+P5, 太阳高度角)
  events/                                  # 共享事件定义 (每个事件一个文件)
  legacy_station_pipeline/                 # 旧的真实场站流程 (已迁移)
    weather_loaders.py                       # ERA5-Land + MERRA-2 数据调用
    global_extreme_simple_signals.py         # 全球场站一步到位
    extract_station_weather_nc.py            # 场站天气抽取
    generate_extreme_signals.py              # 从天气 NC 生成信号
    compute_lowres_baseline.py               # 低资源基线计算
  grid_extreme_signals/                    # 新增：多数据源网格信号流程
    adapters/
      base.py                                # WeatherBundle / WeatherAdapter 接口
      regional_bcsd.py                       # CMIP6–ERA5Land BCSD 适配器
      china_cmfd_bcsd.py                     # CMIP6–CMFD BCSD 适配器
      cordex_nam12.py                        # CORDEX NAM-12 适配器 (旋转极网格)
      era5land_raw.py                        # ERA5-Land 原始数据适配器 (反累积)
    io_utils.py                              # 文件发现、变量名解析、原子写入
    time_alignment.py                        # 时间轴插值、cftime 支持
    unit_conversion.py                       # 单位转换
    signal_runner.py                         # 核心流程：加载→标准化→检测→写入
  scripts/
    generate_multi_source_grid_signals.py  # 统一 CLI 入口
  tests/                                    # 单元测试
  document/                                # 文档
```

## 四类数据源

| `--source` | 数据名称 | 空间网格 | 时间分辨率 | 输出粒度 |
|---|---|---|---|---|
| `regional_bcsd` | CMIP6–ERA5Land BCSD | 规则经纬度 ~0.1° | 3 小时 | 按年 |
| `china_cmfd_bcsd` | CMIP6–CMFD BCSD | 规则经纬度 ~0.1° | 3 小时 | 按年 |
| `cordex_nam12` | CMIP6–CORDEX NAM-12 | 旋转极网格 `rlat × rlon` | 逐小时 | 按年 |
| `era5land_raw` | ERA5-Land 原始 | 规则经纬度 ~0.1° | 逐小时 | 按月 |

### 各数据源可用信号

| 信号 | regional_bcsd | china_cmfd_bcsd | cordex_nam12 | era5land_raw |
|---|:---:|:---:|:---:|:---:|
| **风电** | | | | |
| signal_high_temp | ✅ | ✅ | ✅ | ✅ |
| signal_high_wind | ✅ | ✅ | ✅ | ✅ |
| signal_icing | ❌ 无湿度 | ❌ 无湿度 | ❌ 无湿度 | ✅ |
| signal_hot_humid | ❌ 无湿度 | ❌ 无湿度 | ❌ 无湿度 | ✅ |
| **光伏** | | | | |
| signal_freezing_rain | ✅ | ✅* | ✅* | ✅ |
| signal_rainstorm | ✅ | ✅* | ✅* | ✅ |
| signal_cold_highwind | ✅ | ✅ | ✅ | ✅ |
| signal_icing | ❌ 无湿度 | ❌ 无湿度 | ❌ 无湿度 | ✅ |
| signal_high_humidity | ❌ 无湿度 | ❌ 无湿度 | ❌ 无湿度 | ✅ |
| signal_dust | ❌ | ❌ | ❌ | ✅* 需 --dust_dir |
| signal_low_resource | 第一阶段暂缓 | 第一阶段暂缓 | 第一阶段暂缓 | 第一阶段暂缓 |

✅* = 需要 pr 文件或额外参数；❌ = 因缺少输入变量被跳过

### 跳过事件的原因

当数据源缺少某个事件所需的输入变量时，该事件不会出现在输出文件中。输出文件属性 `skipped_events` 和 `skipped_event_reasons` 记录了所有跳过事件及其原因。

## 运行示例

### 1. Regional BCSD（多数区域/国家）
```bash
python scripts/generate_multi_source_grid_signals.py \
  --source regional_bcsd \
  --data_dir data/bcsd_outputs \
  --model MIROC-ES2H \
  --region Austria \
  --scenario ssp126 \
  --years 2015-2060
```

支持 `--region all` 遍历所有有效区域。

### 2. China CMFD BCSD（中国区域）
```bash
python scripts/generate_multi_source_grid_signals.py \
  --source china_cmfd_bcsd \
  --data_dir data/cmip6_downscaling_3hr \
  --model MIROC-ES2H \
  --scenario ssp126 \
  --years 2015-2100
```

### 3. CORDEX NAM-12（北美 12km）
```bash
python scripts/generate_multi_source_grid_signals.py \
  --source cordex_nam12 \
  --data_dir data/CORDEX-CMIP6/NAM-12/1hr \
  --gcm_model MPI-ESM1-2-LR \
  --realization r1i1p1f1 \
  --rcm_model CRCM5 \
  --scenario ssp126 \
  --years 2020-2060
```

### 4. ERA5-Land（原始全球数据）
```bash
python scripts/generate_multi_source_grid_signals.py \
  --source era5land_raw \
  --data_dir data \
  --years 2024 \
  --months 1,2
```

## 重要参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--save_weather` | `False` | 不保存 `weather_*.nc` 中间文件；仅生成信号 |
| `--stations_dir` | `None` | **预留参数**：非空时抛出 `NotImplementedError`（第一阶段未实现场站筛选） |
| `--require_events` | 空 | 显式要求必须生成的事件；缺失输入时报错 |
| `--overwrite` | `False` | 覆盖已有输出 |
| `--dry_run` | `False` | 仅打印任务计划 |

### 天气中间文件

默认不保存标准化天气变量。传入 `--save_weather` 后，会在 `weather/` 目录额外写出 `weather_wind_*.nc` 和 `weather_solar_*.nc` 文件，用于调试单位转换、检查时间轴对齐或复用中间结果。

### 场站筛选（预留）

`--stations_dir` 参数已预留但第一阶段未实现。传入非空值时会抛出 `NotImplementedError`。场站筛选将在后续阶段实现，届时需要明确不同空间结构间的映射规则。

## 注意事项

1. **3 小时数据与逐小时数据的暴露率不能直接比较**：BCSD/CMFD/CORDEX 数据的时间分辨率不同，极端事件的暴露小时数不能跨分辨率直接比较。
2. **NAM-12 使用旋转极网格**：输出维度为 `(time, rlat, rlon)`，保留二维 `lat(rlat, rlon)` 和 `lon(rlat, rlon)` 辅助坐标。
3. **ERA5-Land 累积量需要跨月边界反累积**：`tp` 和 `ssrd` 是日内累积量，在月初 00:00 需要读取前月最后一小时作为差分起点。
4. **低资源事件暂缓**：不同来源时间分辨率不同（3h vs 1h），低资源事件的基线和阈值需要进一步明确。
5. **旧真实场站脚本已迁移到 `legacy_station_pipeline/`**：推荐使用 `python -m legacy_station_pipeline.xxx` 运行。
6. **默认仅写出 `extreme_signals_*.nc`**：只有显式传入 `--save_weather` 才保存 `weather_*.nc`。

## 变量约定（weather 字典；单位/高度务必一致）
| 键 | 含义 | 单位 | 高度/来源 |
|---|---|---|---|
| temp_C | 气温 | °C | 2m (t2m−273.15) 或 tas |
| rh_pct | 相对湿度 | % | 2m (Magnus: t2m+d2m) |
| wind_ms | 风速 | m/s | 10m (√(u10²+v10²)) 或 sfcWind |
| precip_mmh | 降水 | mm/h | pr 转换 或 tp 去累积 |
| rsds | 短波辐射 | W/m² | rsds 或 ssrd 去累积 |
| dust_aod | 沙尘AOD | 1 | MERRA-2 DUEXTTAU |

## 阈值标定来源
真实场站口径结果：`real_station_analysis_lbz/analysis/event_eval/real_events_table_*.csv`；
指标定义：`real_station_analysis_lbz/整理/METRICS.md`（加权损失方向率/净损失率/暴露率）。

## 旧真实场站流程

旧脚本继续服务于 ERA5-Land / MERRA-2 + 真实场站 CSV 或 GPKG 的流程，但已迁移到 `legacy_station_pipeline/`。运行方式：

```bash
python -m legacy_station_pipeline.generate_extreme_signals --weather_nc weather.nc ...
python -m legacy_station_pipeline.global_extreme_simple_signals ...
```
