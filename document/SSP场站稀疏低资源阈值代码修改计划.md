# SSP 场站稀疏低资源阈值代码修改计划

## 1. 目标

只针对 SSP 场站计算 ERA5Land 低资源阈值，不再生成全网格
`signal_low_resource`，也不要求预计算完整 ERA5Land 阈值网格。

具体目标：

1. 对每个 SSP 情景、每个技术类型分别生成一个稀疏阈值文件。
2. 对每个场站点，默认选取最近的 ERA5Land 有效格点，避开海上缺测格点。
3. 可选使用四点双线性权重从 ERA5Land CF 得到场站级 ERA5Land CF 时间序列。
4. 基于场站级 ERA5Land CF 的 24h 滚动平均异常计算月-小时气候态和 P5 低资源阈值。
5. 下游 Pipeline B 直接读取稀疏阈值文件中对应场站的阈值和气候态。

## 2. 输入与输出

### 输入

ERA5Land CF：

```text
/data6/yanxiaokai/project_climate/extreme_event_definitions/data/cfs/CFs_of_wind_ERA5Land
/data6/yanxiaokai/project_climate/extreme_event_definitions/data/cfs/CFs_of_solar_ERA5Land
```

场站 CSV：

```text
data/stations/stations_SSP1-2.6.csv
data/stations/stations_SSP2-4.5.csv
data/stations/stations_SSP5-8.5.csv
```

### 输出目录

统一放在：

```text
outputs/low_resource_thresholds/sparse_station_ERA5Land_2015-2024/
```

### 输出文件

按 SSP + 技术类型拆分：

```text
low_resource_threshold_sparse_ssp126_wind_ERA5Land_2015-2024.nc
low_resource_threshold_sparse_ssp126_solar_ERA5Land_2015-2024.nc
low_resource_threshold_sparse_ssp245_wind_ERA5Land_2015-2024.nc
low_resource_threshold_sparse_ssp245_solar_ERA5Land_2015-2024.nc
low_resource_threshold_sparse_ssp585_wind_ERA5Land_2015-2024.nc
low_resource_threshold_sparse_ssp585_solar_ERA5Land_2015-2024.nc
```

## 3. 稀疏阈值文件结构

维度：

```text
station
corner = 4
month = 12
hour = 24
```

变量：

```text
station_lon(station)              float32
station_lat(station)              float32
station_type(station)             int8    # solar=0, wind=1
capacity_gw(station)              float32
activation_year(station)          int16

era5_lat_idx(station, corner)     int32
era5_lon_idx(station, corner)     int32
era5_lat(station, corner)         float32
era5_lon(station, corner)         float32
weight(station, corner)           float32

clim(month, hour, station)        float32
threshold(station)                float32
valid_count(station)              int32
```

全局属性：

```text
threshold_kind = sparse_station
threshold_source = ERA5Land
scenario = ssp126 | ssp245 | ssp585
tech = wind | solar
baseline_years_requested = 2015-2024
baseline_years_effective = 2015-2024
interpolation_method = nearest_valid | bilinear_4point
window_hours = 24
percentile = 5
created_by = scripts/precompute_station_low_resource_thresholds.py
```

## 4. ERA5Land 网格选择与权重

ERA5Land 是规则经纬度网格，经度统一按 `[-180, 180)` 做匹配。

默认对每个场站使用 `nearest_valid`：

1. 先找到经纬度最近的 ERA5Land 格点。
2. 若该格点 CF 有效，直接使用该格点。
3. 若该格点在海上或 CF 缺测，则向外寻找最近的有效格点。
4. 输出仍保持 `corner=4`，第 0 个 corner 权重为 1，其余 corner 权重为 0。

可选 `bilinear` 时，对每个场站：

1. 在纬度轴上找到包围场站纬度的两个索引。
2. 在经度轴上找到包围场站经度的两个索引，经度距离按 360 度环形处理。
3. 四个 corner 顺序固定为：
   - southwest
   - southeast
   - northwest
   - northeast
4. 计算双线性权重，权重和必须接近 1。
5. 场站落在边界外或正好落在网格线上时，允许退化为重复索引或零权重，但输出仍保持 `corner=4`。

## 5. 阈值计算

对每个场站：

1. 按时间拼接 ERA5Land 月文件。
2. 按 `nearest_valid` 或 `bilinear` 读取 ERA5Land 网格点 CF 并加权，得到场站级 ERA5Land CF。
3. 默认 `nearest_valid` 只读取最近有效格点，避免近海无数据格点污染阈值。
4. 计算 24h centered rolling mean，`min_periods = window_steps`。
5. 计算月-小时气候态 `clim(month,hour,station)`。
6. 计算异常 `anom = roll - clim[month,hour,station]`。
7. 取异常的 P5 作为 `threshold(station)`。

太阳能仍沿用现有 `LOWRES["solar"].signal(...)` 的夜间剔除逻辑；阈值文件本身只保存
ERA5Land CF 的气候态和异常阈值。

## 6. 下游 Pipeline B 修改

Pipeline B 运行 `regional_bcsd` 不再查找完整 ERA5Land 阈值网格文件，而是：

1. 按 `scenario + tech + baseline_years` 定位稀疏阈值文件。
2. 根据输出场站的 `(lon, lat, type)` 在稀疏阈值文件中查找同一场站。
3. 读取该场站的 `clim(month,hour,station)` 和 `threshold(station)`。
4. 目标 SSP CF 仍按当前逻辑从目标 CF 文件最近邻网格抽取。
5. 使用目标 SSP CF 的 24h 滚动异常与 ERA5Land 稀疏阈值比较，生成
   `signal_low_resource(time, station)`。

这里仅改变阈值来源，不改变目标 CF 的空间抽取方式。目标 CF 的最近邻、双线性等插值选择在
后续“场站位置和网格数据如何对应”计划中单独设计。

## 7. 新增脚本

新增：

```text
scripts/precompute_station_low_resource_thresholds.py
```

核心参数：

```text
--cf_root data/cfs
--stations_csv data/stations/stations_SSP1-2.6.csv
--scenario ssp126
--tech wind|solar|both
--baseline_years 2015-2024
--threshold_interp nearest_valid|bilinear
--output_dir outputs/low_resource_thresholds/sparse_station_ERA5Land_2015-2024
--station_chunk 128
--time_chunk 512
--allow_incomplete
--overwrite
--dry_run
```

完整 2015-2024 示例：

```bash
python scripts/precompute_station_low_resource_thresholds.py \
  --cf_root data/cfs \
  --stations_csv data/stations/stations_SSP1-2.6.csv \
  --scenario ssp126 \
  --tech both \
  --baseline_years 2015-2024
```

只用 2015-2024 示例：

```bash
python scripts/precompute_station_low_resource_thresholds.py \
  --cf_root data/cfs \
  --stations_csv data/stations/stations_SSP1-2.6.csv \
  --scenario ssp126 \
  --tech both \
  --baseline_years 2015-2024 \
  --output_dir outputs/low_resource_thresholds/sparse_station_ERA5Land_2015-2024
```

## 8. 测试与验收

新增或更新测试：

1. 最近有效 ERA5Land 格点选择：最近邻无效时能替换为最近有效格点，权重和为 1。
2. 经度接缝：`179.95/-179.95` 附近能正确选取环形经度邻点。
3. 稀疏阈值文件 schema：维度、变量、关键属性齐全。
4. Pipeline B 读取稀疏阈值：同一 `(lon, lat, type)` 能匹配到正确 station。
5. 外部阈值路径：计算低资源时不再用目标 SSP 自身基线期计算阈值。

验收命令：

```bash
python -m py_compile \
  scripts/precompute_station_low_resource_thresholds.py \
  grid_extreme_signals/cf_low_resource.py \
  scripts/station_signals_direct.py \
  scripts/patch_pipelineB_low_resource.py

python -m pytest tests/test_low_resource_thresholds.py tests/test_sparse_low_resource_thresholds.py
```

若环境没有 pytest，则用项目当前做法直接调用测试函数完成 smoke test。
