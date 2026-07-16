# Step1 从 ssp126 增量复用低资源阈值代码修改计划

## 1. 背景

`step1_low_resource_thresholds.py` 用 ERA5Land 2015-2025 风光 CF 为 SSP 场站计算稀疏低资源阈值。

阈值只取决于：

1. 场站位置 `lon, lat`。
2. 技术类型 `type`，即 `wind` 或 `solar`。
3. ERA5Land CF 输入数据。
4. `baseline_years`。
5. 阈值侧空间匹配方法 `threshold_interp`，当前默认 `nearest_valid`。

阈值不取决于 SSP 情景本身、`capacity_gw` 或 `activation_year`。因此，当 `ssp245` 或 `ssp585` 的某个 `(lon, lat, type)` 已经出现在 `ssp126` 阈值文件中时，可以直接复用 `ssp126` 中该场站的 `clim/threshold/valid_count` 和 ERA5Land 匹配元数据，只计算非交集场站。

当前核对结果：`outputs/low_resource_thresholds/` 为空。后续实现不能假设已有 `ssp126` 文件；若复用源文件不存在或不完整，应自动退回完整计算。

## 2. 目标

1. 优先完整计算 `ssp126` 阈值文件。
2. 计算 `ssp245` 或 `ssp585` 时，默认尝试从同目录下的 `ssp126` 同技术类型阈值文件复用重叠场站。
3. 只对目标 SSP 中不在 `ssp126` 文件内的 `(lon, lat, type)` 场站读取 ERA5Land CF 并计算阈值。
4. 输出文件仍然是目标 SSP 自己的完整稀疏阈值文件，station 顺序、`capacity_gw`、`activation_year` 保持目标 SSP CSV 的结果。
5. 若 `ssp126` 源文件缺失、不完整或属性不兼容，则记录日志并对目标 SSP 做完整计算。

## 3. 非目标

1. 不跨技术类型复用。`wind` 只复用 `wind`，`solar` 只复用 `solar`。
2. 不用 `capacity_gw` 或 `activation_year` 参与复用匹配。
3. 不把 `ssp126` 文件直接作为 `ssp245/ssp585` 的输出文件使用。
4. 不改变下游 step2 对稀疏阈值文件的读取接口。
5. 不改变目标 SSP CF 的低资源事件判定逻辑。

## 4. 命令行设计

在 `scripts/precompute_station_low_resource_thresholds.py` 中新增参数：

```text
--reuse_from_scenario ssp126
--no_reuse_thresholds
```

默认行为：

1. `scenario == ssp126` 时不复用，完整计算。
2. `scenario != ssp126` 时默认尝试从 `--reuse_from_scenario ssp126` 复用。
3. 用户传入 `--no_reuse_thresholds` 时关闭复用，保持当前完整计算逻辑。
4. 用户传入 `--reuse_from_scenario <scenario>` 时，可指定其他复用源，但首期主要支持 `ssp126`。

示例：

```bash
python step1_low_resource_thresholds.py \
  --cf_root data/cfs \
  --stations_csv data/stations/stations_SSP1-2.6.csv \
  --tech both \
  --baseline_years 2015-2025 \
  --overwrite

python step1_low_resource_thresholds.py \
  --cf_root data/cfs \
  --stations_csv data/stations/stations_SSP2-4.5.csv \
  --tech both \
  --baseline_years 2015-2025 \
  --overwrite
```

第二条命令默认尝试读取：

```text
outputs/low_resource_thresholds/sparse_station_ERA5Land_2015-2025/
  low_resource_threshold_sparse_ssp126_wind_ERA5Land_2015-2025.nc
  low_resource_threshold_sparse_ssp126_solar_ERA5Land_2015-2025.nc
```

## 5. 源文件完整性与兼容性检查

新增源阈值文件检查函数，例如：

```text
load_reuse_source_threshold(path, scenario, tech, baseline_years, threshold_interp)
```

检查项：

1. 文件存在且可由 HDF5/NetCDF 正常打开。
2. 全局属性兼容：
   - `threshold_kind == sparse_station`
   - `threshold_source == ERA5Land`
   - `scenario == reuse_from_scenario`
   - `tech == 当前 tech`
   - `baseline_years_requested == 当前 baseline_years`
   - `interpolation_method == 当前 threshold_interp 对应的输出属性`
   - `resource_variable == wind_cf | solar_cf`
   - `window_hours == 24`
   - `percentile == 5`
3. 维度完整：
   - `station`
   - `corner == 4`
   - `month == 12`
   - `hour == 24`
4. 变量完整：
   - `station_lon`
   - `station_lat`
   - `station_type`
   - `capacity_gw`
   - `activation_year`
   - `era5_lat_idx`
   - `era5_lon_idx`
   - `era5_lat`
   - `era5_lon`
   - `weight`
   - `clim`
   - `threshold`
   - `valid_count`
5. 数值完整：
   - `threshold` 对所有 source station 有限。
   - `valid_count > 0`。
   - `weight.sum(axis=1)` 接近 1。
   - `(station_lon, station_lat, station_type)` 无重复 key。

若任一检查失败：

1. 记录 warning，说明失败原因。
2. 当前目标 SSP 回退为完整计算。
3. 不使用半损坏源文件做部分复用。

## 6. 匹配 key 设计

复用 key 使用严格场站定义：

```text
(lon_norm, lat, type)
```

规则：

1. `lon` 先归一化到 `[-180, 180)`，与 `station_match.load_stations()` 保持一致。
2. `lon/lat` 使用固定小数位构造 key，建议 `round(10)`，避免 CSV 浮点解析的微小误差。
3. `type` 使用源文件中的 `station_type` 转回 `wind/solar`，或直接按当前 tech 固定。
4. 若源文件内 key 重复，视为源文件不完整或不可靠，禁用复用。

## 7. 计算流程调整

当前 `process_tech(args, scenario, tech)` 需要拆成更清晰的阶段：

1. 读取并去重目标 SSP 场站，得到 `target_stations`。
2. 查找 ERA5Land 月文件，读取静态网格。
3. 判断是否启用复用：
   - `scenario != args.reuse_from_scenario`
   - 未传入 `--no_reuse_thresholds`
   - 源文件存在、完整且兼容。
4. 构建 `reused_mask` 和 `source_idx_for_target`：
   - 交集场站：从源文件复制。
   - 非交集场站：进入新计算列表。
5. 为所有目标 station 组装 ERA5Land 匹配元数据：
   - 交集场站复制源文件的 `era5_*` 和 `weight`。
   - 非交集场站按当前 `threshold_interp` 新计算匹配。
6. 创建目标 SSP 输出文件。
7. 先写入交集场站：
   - `clim[:, :, target_idx]`
   - `threshold[target_idx]`
   - `valid_count[target_idx]`
8. 对非交集场站按 chunk 读取 ERA5Land CF 并计算阈值。
9. 写完后关闭文件并记录统计日志。

日志示例：

```text
[ssp245/wind] 复用源=ssp126，目标场站=82795，复用=39688，新计算=43107，复用比例=47.94%
```

## 8. 输出文件一致性

目标 SSP 输出文件仍保持当前 schema，不新增下游必需字段。

建议新增全局属性用于追踪复用情况：

```text
reuse_enabled = true | false
reuse_from_scenario = ssp126
reuse_source_file = ...
reuse_station_count = 39688
computed_station_count = 43107
reuse_key = lon,lat,type
```

如果没有复用：

```text
reuse_enabled = false
reuse_station_count = 0
computed_station_count = station_count
```

## 9. 原子写出与失败恢复

为避免后续“源文件存在但实际不完整”的问题，建议同步改造输出写出方式：

1. 目标文件先写入临时路径：

```text
<target>.tmp.<pid>
```

2. 全部 station 写入完成并关闭文件后，用 `os.replace(tmp, target)` 原子替换。
3. 计算失败时删除临时文件，不留下半成品目标文件。
4. 完整性检查只接受最终 `.nc` 文件，不读取 `.tmp.*`。

## 10. 测试计划

新增或更新测试：

1. `ssp126` 源文件存在且完整：
   - 目标 SSP 中一个场站与源文件重叠，一个不重叠。
   - 重叠场站的 `clim/threshold/valid_count/era5_*/weight` 与源文件完全一致。
   - 非重叠场站按 ERA5Land CF 新计算。
2. 源文件缺失：
   - 自动完整计算。
   - `reuse_enabled=false`。
3. 源文件属性不兼容：
   - 例如 `interpolation_method=bilinear_4point`，当前为 `nearest_valid`。
   - 禁用复用并完整计算。
4. 源文件变量缺失或 `threshold` 含 NaN：
   - 禁用复用并完整计算。
5. `--no_reuse_thresholds`：
   - 即使源文件存在，也完整计算。
6. key 去重：
   - 源文件中 `(lon, lat, type)` 重复时禁用复用。
7. 输出 schema：
   - 新增复用属性存在。
   - 下游读取函数仍可读取旧文件和新文件。

验收命令：

```bash
python -m py_compile \
  step1_low_resource_thresholds.py \
  scripts/precompute_station_low_resource_thresholds.py \
  grid_extreme_signals/cf_low_resource.py

pytest -q tests/test_sparse_low_resource_thresholds.py
pytest -q
```

## 11. 实施顺序

1. 新增源阈值文件读取与完整性检查 helper。
2. 新增 key 构造与交集匹配 helper。
3. 重构 `process_tech()`，支持交集复制和非交集计算。
4. 增加原子写出，避免半成品文件被后续复用。
5. 写单元测试覆盖复用、回退、禁用复用和属性不兼容。
6. 更新 README 的 step1 说明和示例。

## 12. 风险与注意事项

1. 复用必须严格检查 `threshold_interp`。旧的 `bilinear_4point` 文件不能复用于新的 `nearest_valid` 输出。
2. 当前 `outputs/low_resource_thresholds/` 为空；第一次必须先生成 `ssp126` 文件，否则 `ssp245/ssp585` 会自动完整计算。
3. 如果用户手动删除或替换 `ssp126` 文件，后续复用结果会变化，因此日志必须记录源文件路径和复用数量。
4. 若未来改变阈值算法，例如 percentile 或 rolling window，必须把对应属性加入兼容性检查。
