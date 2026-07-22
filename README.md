# 极端天气损失事件定义 + 数据调用（风电 / 光伏）

> 最终确认的事件阈值（真实场站 2023-2024 实测 + 中美采样网格验证）。
> **每种天气一个文件**，改阈值只动对应文件顶部常量。仅依赖 numpy / pandas / xarray。

## 目录结构
```
extreme_event_definitions/
  README.md
  step1_low_resource_thresholds.py          # ① ERA5Land 2015-2024 风光 CF 预计算低资源阈值
  step2_complete_extreme_events.py          # 两阶段②：未来极端事件，包含 low_resource
  step2_split_E1_weather_extremes.py        # 三阶段②：未来极端事件，不包含 low_resource
  step2_split_E2_low_resource.py            # 三阶段③：补写未来 low_resource 到三阶段②输出
  registry.py                              # 汇总注册表: simple_signals() 一次取全部
  tools/
    common.py                              # 低资源共享算法(24h滚动+clim288+P5, 太阳高度角)
    merge_weather_nc.py                    # 工具：合并场站天气 NC
    plot_raw.py                            # 工具：历史 benchmark 绘图
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
| signal_low_resource | 场站级 Pipeline B | 场站级 Pipeline B | 场站级 Pipeline B | 需 CF 文件 |

✅* = 需要 pr 文件或额外参数；❌ = 因缺少输入变量被跳过

### 跳过事件的原因

当数据源缺少某个事件所需的输入变量时，该事件不会出现在输出文件中。输出文件属性 `skipped_events` 和 `skipped_event_reasons` 记录了所有跳过事件及其原因。

## 推荐流程入口

当前低资源事件使用 ERA5Land 2015-2024 风光 CF 先计算阈值；未来模式/SSP
只负责被判定是否发生事件。项目根目录提供两种入口组合。

`step1_low_resource_thresholds.py` 计算 ERA5Land 稀疏阈值时默认使用
`--threshold_interp nearest_valid`，即为每个场站选择最近的 ERA5Land 有效格点，
以避开近海无 CF 数据的格点；如需沿用四点双线性，可显式传入
`--threshold_interp bilinear`。

计算 `ssp245` 或 `ssp585` 时，step1 默认会尝试从同目录下已完成的 `ssp126`
同技术类型阈值文件复用重叠 `(lon, lat, type)` 场站，只计算非交集场站；
如需强制完整计算，可传入 `--no_reuse_thresholds`。

### 两阶段流程

适用于未来模式/SSP 的目标 CF 已经可用，希望一次性输出所有事件：

```bash
python step1_low_resource_thresholds.py \
  --cf_root data/cfs \
  --stations_csv data/stations/stations_SSP1-2.6.csv \
  --tech both \
  --baseline_years 2015-2024

python step2_complete_extreme_events.py \
  --source regional_bcsd \
  --data_dir data/bcsd_outputs \
  --model NESM3 \
  --scenario ssp126 \
  --stations_csv data/stations/stations_SSP1-2.6.csv \
  --region all \
  --years 2030-2060 \
  --tech both \
  --allow_unit_inference \
  --allow_missing_optional
```

### 三阶段流程

适用于先判断普通极端事件，等未来模式/SSP 的 CF 准备好后再补写低资源事件：

```bash
python step1_low_resource_thresholds.py \
  --cf_root data/cfs \
  --stations_csv data/stations/stations_SSP1-2.6.csv \
  --scenario ssp126 \
  --tech both \
  --baseline_years 2015-2024

python step2_split_E1_weather_extremes.py \
  --source regional_bcsd \
  --data_dir data/bcsd_outputs \
  --model NESM3 \
  --scenario ssp126 \
  --stations_csv data/stations/stations_SSP1-2.6.csv \
  --region all \
  --years 2030-2060 \
  --tech both \
  --allow_unit_inference \
  --allow_missing_optional

python step2_split_E2_low_resource.py \
  --output_root outputs/station_signals/regional_bcsd/NESM3 \
  --cf_root data/cfs \
  --threshold_dir outputs/low_resource_thresholds/sparse_station_ERA5Land_2015-2024 \
  --model NESM3 \
  --scenario ssp126 \
  --tech both \
  --years 2030-2060 \
  --overwrite
```

说明：

- 根目录流程入口和 `scripts/` CLI 会把日志写入 `logs/`，文件名格式为
  `<入口名>_YYYYMMDD_HHMMSS.log`；日志行时间戳格式为 `YYYY-MM-DD HH:MM:SS`。
- `step2_complete_extreme_events.py` 会调用场站流程并默认计算 `low_resource`。
- `step2_split_E1_weather_extremes.py` 会强制附加 `--no_low_resource`。
- `step2_split_E2_low_resource.py` 会读取 E1 输出文件中的 `match_method`，确保低资源目标 CF 抽取方式与 E1 一致。

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

## 场站级信号（Pipeline B）

场站选址结果已是 **0.1°（≈10km）级别**（`data/stations/stations_SSP*.csv`，全球范围）。我们只对**有气象数据的国家**（BCSD 26 国 + China + NAM-12）范围内的场站计算极端天气信号。

- **Pipeline B（直接场站）** `scripts/station_signals_direct.py`：跳过网格，复用适配器标准化气象 → 最近邻 gather 到场站 → `registry.simple_signals` 检测 → 写场站级信号。

产出**场站级 NetCDF**（`dims: time, station`；`signal_<event>(time,station)` + 场站元数据）。

### 匹配与经度（关键）
- 场站经度统一 `[-180,180)`；**BCSD 网格经度约定逐区域不同**（Germany 5–15 像 `[-180,180]`，Portugal 存为 `328.7–353.7` 即 `[0,360)`）——脚本**逐文件检测并归一**后再做最近邻。
- 最近邻用环形经度距离（正确处理 ±180° 缝合）；距离容差 `--max_dist` 默认 `0.15°`，超容差场站信号置 0。
- 场站→国家：Natural Earth 国界多边形 point-in-polygon（`--shp`）；`(lon,lat,type)` 去重，`activation_year=min(year)`。
- 场站激活年前的信号默认置 0（`--no_activation_mask` 关闭）。

### 运行示例（regional_bcsd）
```bash
python scripts/station_signals_direct.py \
  --source regional_bcsd --data_dir data/bcsd_outputs \
  --model MIROC-ES2H --stations_csv data/stations/stations_SSP1-2.6.csv \
  --region all --years 2015-2050 --allow_unit_inference
```

> China(CMFD) / NAM-12(CORDEX) 的匹配逻辑已在共享模块就绪（规则网 / 2D 旋转极），待数据落盘后启用。详见 `document/场站级信号_实施计划.md`。

## 重要参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--save_weather` | `False` | 不保存 `weather_*.nc` 中间文件；仅生成信号 |
| `--stations_dir` | `None` | **预留参数**：非空时抛出 `NotImplementedError`（第一阶段未实现场站筛选） |
| `--require_events` | 空 | 显式要求必须生成的事件；缺失输入时报错 |
| `--overwrite` | `False` | 覆盖已有输出 |
| `--dry_run` | `False` | 仅打印任务计划 |
| `--spatial_interp` | `nearest` | 场站到网格数据抽取方法；规则经纬度网格支持 `nearest`/`bilinear`，NAM-12 当前只支持 `nearest` |
| `--lowres_threshold_dir` | `outputs/low_resource_thresholds/sparse_station_ERA5Land_2015-2024` | ERA5Land 2015-2024 SSP 场站稀疏低资源阈值目录 |

场站级低资源阈值需先按 SSP + 技术类型预计算，例如：

```bash
python scripts/precompute_station_low_resource_thresholds.py \
  --cf_root data/cfs \
  --stations_csv data/stations/stations_SSP1-2.6.csv \
  --scenario ssp126 \
  --tech both \
  --baseline_years 2015-2024
```

### 天气中间文件

默认不保存标准化天气变量。传入 `--save_weather` 后，会在 `weather/` 目录额外写出 `weather_wind_*.nc` 和 `weather_solar_*.nc` 文件，用于调试单位转换、检查时间轴对齐或复用中间结果。

### 场站筛选（预留）

`--stations_dir` 参数已预留但第一阶段未实现。传入非空值时会抛出 `NotImplementedError`。场站筛选将在后续阶段实现，届时需要明确不同空间结构间的映射规则。

## 开发阶段（第一阶段 / 第二阶段）

本项目分两个阶段推进。第一阶段负责全格点信号和多源适配；场站信号由 Pipeline B 处理。

### 第一阶段（当前已实现）

- **统一入口** `scripts/generate_multi_source_grid_signals.py`，按 `--source` 选适配器 → 标准化气象 → 生成信号。
- **四类数据源适配器**：`regional_bcsd`、`china_cmfd_bcsd`、`cordex_nam12`、`era5land_raw`，保留各自原生网格（CORDEX 旋转极 `(rlat, rlon)` 不强行改造）。
- **全格点模式**：`--stations_dir` 默认 `None`，对数据源中的全部网格点计算信号；传非空值则显式抛 `NotImplementedError`。**第一阶段不做场站筛选、不做 1° 级聚合。**
- **事件生成**：各源可用信号见上方「各数据源可用信号」矩阵；缺输入变量的事件记入输出属性 `skipped_events`，不伪造全零数组。
- **全网格不生成 `low_resource`**：低资源事件只在场站级 Pipeline B 中计算。
- **默认不保存 `weather_*.nc`**：只产出 `extreme_signals_*.nc`；传入 `--save_weather` 才额外写天气中间文件。
- **基础设施**：`WeatherBundle` / `WeatherAdapter` 接口、单位转换、原子写出、断点续跑、事件跳过记录；旧真实场站脚本迁入 `legacy_station_pipeline/`。

### 第二阶段（未实现 / 待定）

| 待办 | 卡点 |
|---|---|
| **场站级信号**（`station_signals_direct.py`） | ✅ 已实现（regional_bcsd）：见上方「场站级信号」。China/NAM-12 待数据落盘 |
| **低资源事件**（`signal_low_resource`） | ✅ 场站级默认启用；依赖目标 CF 文件和 ERA5Land 2015-2024 阈值文件 |
| **MERRA-2 沙尘全网格重采样** | 第一阶段只做简单最近邻 |
| **跨区域拼接** | — |
| **不同来源结果的统一评估** | — |

## 注意事项

1. **3 小时数据与逐小时数据的暴露率不能直接比较**：BCSD/CMFD/CORDEX 数据的时间分辨率不同，极端事件的暴露小时数不能跨分辨率直接比较。
2. **NAM-12 使用旋转极网格**：输出维度为 `(time, rlat, rlon)`，保留二维 `lat(rlat, rlon)` 和 `lon(rlat, rlon)` 辅助坐标。
3. **ERA5-Land 累积量需要跨月边界反累积**：`tp` 和 `ssrd` 是日内累积量，在月初 00:00 需要读取前月最后一小时作为差分起点。
4. **低资源事件阈值固定**：阈值来自 ERA5Land 2015-2024 CF 预计算结果；不同模式/SSP 不再用自身 CF 重新估计阈值。
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
