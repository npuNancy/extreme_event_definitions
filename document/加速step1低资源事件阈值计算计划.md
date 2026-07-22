# 加速 step1 低资源事件阈值计算计划

## 1. 背景与瓶颈

当前 `step1_low_resource_thresholds.py` 用 ERA5Land 2015-2024 风光 CF 为 SSP 场站计算稀疏低资源阈值。

已确认 ERA5Land CF 文件的 HDF5 chunk 方式是：

```text
wind  chunks = (72, 1801, 3600), gzip
solar chunks = (24, 1801, 3600), gzip
```

也就是每个压缩 chunk 覆盖一段时间和完整全球空间网格。

当前慢的根因是读取方式和文件 chunk 布局不匹配：代码按少量场站点读：

```python
d[:, lat_i, unique_lon]
```

为了几个点，HDF5 会反复解压巨大的全球空间 chunk。旧进程运行 18 小时仍未完成第一个 `station_chunk=128`，说明继续沿用点式读取不可行。

## 2. 目标

1. 改成按 HDF5 原生 chunk 读取 ERA5Land CF。
2. 先生成“场站 CF 缓存”，把全球网格 CF 抽取成 `(time, station)` 稀疏缓存。
3. 阈值计算阶段只读取场站 CF 缓存，不再反复读取全球 ERA5Land 文件。
4. 保持现有阈值算法不变：
   - 24h centered rolling mean。
   - `clim(month, hour, station)`。
   - `threshold(station) = P5(anom)`。
5. 保持现有输出阈值文件 schema 尽量不变，下游 step2 不需要修改。
6. 保留 `ssp126` 增量复用逻辑：`ssp245/ssp585` 只为非交集场站生成或读取缓存并计算阈值。

## 3. 非目标

1. 不改变低资源事件阈值定义。
2. 不改变默认空间匹配方式，仍默认 `nearest_valid`，可选 `bilinear`。
3. 不新增必要依赖。优先使用项目已有的 `h5py/netCDF4/numpy/pandas`。
4. 不把全网格 ERA5Land 阈值重新引入流程。

## 4. 总体方案

把 step1 拆成两个内部阶段：

```text
阶段 A：ERA5Land 全球 CF -> 场站 CF 缓存
阶段 B：场站 CF 缓存 -> 稀疏低资源阈值文件
```

阶段 A 只负责高效抽取场站 CF。阶段 B 只负责阈值计算。

### 4.1 阶段 A：生成场站 CF 缓存

对每个 `scenario + tech + baseline_years + threshold_interp` 生成一个场站 CF 缓存文件。

建议路径：

```text
outputs/cache/era5land_station_cf/
  station_cf_<scenario>_<tech>_ERA5Land_<baseline_years>_<threshold_interp>.nc
```

缓存维度：

```text
time
station
corner = 4
```

缓存变量：

```text
time(time)
station(station)
station_lon(station)
station_lat(station)
station_type(station)
capacity_gw(station)
activation_year(station)
era5_lat_idx(station, corner)
era5_lon_idx(station, corner)
era5_lat(station, corner)
era5_lon(station, corner)
weight(station, corner)
cf(time, station)
```

缓存关键属性：

```text
cache_kind = era5land_station_cf
scenario = ssp126 | ssp245 | ssp585
tech = wind | solar
baseline_years = 2015-2024
threshold_interp = nearest_valid | bilinear
interpolation_method = nearest_valid | bilinear_4point
source_files = ...
created_by = scripts/precompute_station_low_resource_thresholds.py
```

### 4.2 阶段 B：从缓存计算阈值

从缓存中按 station chunk 读取：

```text
cf(time, station_chunk)
```

然后沿用当前 `compute_threshold_block()`：

1. 对 CF 做 24 小时 centered rolling mean。
2. 按 `month/hour` 计算 `clim(12,24,station_chunk)`。
3. 计算距平。
4. 对距平取 P5，得到 `threshold(station_chunk)`。
5. 写入最终稀疏阈值文件。

## 5. 按 HDF5 原生 chunk 读取

缓存生成阶段不再按 station chunk 遍历月文件，而是按月文件内变量的 HDF5 chunk 遍历时间块。

伪代码：

```python
with open_h5(month_file, "r") as f:
    d = f[var_name]
    native_time_chunk = d.chunks[0] if d.chunks else fallback_time_chunk
    for t0 in range(0, d.shape[0], native_time_chunk):
        t1 = min(t0 + native_time_chunk, d.shape[0])
        slab = d[t0:t1, :, :].astype(np.float32)
        station_cf = gather_station_cf_from_slab(slab, match)
        cache["cf"][global_t0:global_t1, :] = station_cf
```

这样每个压缩 chunk 只解压一次。

### 5.1 nearest_valid 抽取

默认 `nearest_valid` 时，只有第 0 个 corner 权重为 1：

```python
station_cf = slab[:, match.lat_idx[:, 0], match.lon_idx[:, 0]]
```

### 5.2 bilinear 抽取

`bilinear` 时抽取 4 个 corner 并加权：

```python
out = 0
for c in range(4):
    out += slab[:, match.lat_idx[:, c], match.lon_idx[:, c]] * match.weights[:, c]
```

注意：为保持阈值侧算法一致，初始实现应与当前 `_read_month_station_cf()` 的权重处理保持一致，不额外引入新的缺测重归一策略。若未来需要重归一，应单独作为算法变更记录。

## 6. 缓存文件写出策略

### 6.1 原子写出

缓存文件也必须使用临时文件写出：

```text
<cache>.tmp.<pid>
```

写完并关闭后用：

```python
os.replace(tmp_path, cache_path)
```

避免半成品缓存被后续流程误用。

### 6.2 缓存复用

新增参数建议：

```text
--station_cf_cache_dir outputs/cache/era5land_station_cf
--overwrite_station_cf_cache
--no_station_cf_cache
```

默认行为：

1. 如果缓存存在且完整，直接读取缓存计算阈值。
2. 如果缓存不存在，先生成缓存。
3. 如果传入 `--overwrite_station_cf_cache`，重新生成缓存。
4. 如果传入 `--no_station_cf_cache`，退回直接从 ERA5Land 文件计算；该模式只用于调试，不推荐正式使用。

### 6.3 缓存完整性检查

缓存存在时必须检查：

1. 属性匹配：
   - `cache_kind`
   - `scenario`
   - `tech`
   - `baseline_years`
   - `threshold_interp`
   - `interpolation_method`
2. 维度匹配：
   - `time` 等于 baseline 月文件拼接后的时间长度。
   - `station` 等于当前场站数。
   - `corner == 4`。
3. 场站匹配：
   - `station_lon/station_lat/station_type` 与当前目标场站一致。
4. 变量存在：
   - `cf`
   - `era5_*`
   - `weight`
5. `weight.sum(axis=1)` 接近 1。

检查失败则重新生成缓存。

## 7. 与 ssp126 增量复用的关系

当前已经支持 `ssp245/ssp585` 从 `ssp126` 稀疏阈值文件复用交集场站。

加速后流程应为：

1. `ssp126`：
   - 生成完整 `ssp126` 场站 CF 缓存。
   - 从缓存计算完整阈值文件。
2. `ssp245/ssp585`：
   - 先尝试从 `ssp126` 阈值文件复用交集场站。
   - 只为非交集场站生成或读取当前 SSP 的场站 CF 缓存。
   - 从非交集缓存计算阈值，和复用结果合并写入目标 SSP 阈值文件。

为避免为复用场站生成多余缓存，缓存生成函数需要支持只传入 `stations.iloc[compute_idx]`。

## 8. 内存与性能估算

ERA5Land 网格大小：

```text
1801 * 3600 = 6,483,600 grid cells
```

单个 float32 全空间时次约：

```text
6,483,600 * 4 bytes ≈ 24.7 MB
```

原生 chunk 解压后内存大致：

```text
wind  72h * 24.7 MB ≈ 1.78 GB
solar 24h * 24.7 MB ≈ 593 MB
```

因此建议：

1. 不同时运行 wind 和 solar 的缓存生成。
2. 默认按原生 time chunk 读取。
3. 如内存不足，提供 `--cache_time_chunk` 参数，但提示小于原生 chunk 可能重新引入重复解压。

## 9. 代码修改点

主要修改 `scripts/precompute_station_low_resource_thresholds.py`。

新增 helper：

```text
station_cf_cache_path(...)
load_or_build_station_cf_cache(...)
build_station_cf_cache(...)
validate_station_cf_cache(...)
create_station_cf_cache_output(...)
gather_station_cf_from_slab(...)
compute_thresholds_from_station_cf_cache(...)
```

调整 `process_tech()`：

1. 读取场站和 ERA5Land 静态网格。
2. 计算或复用 ERA5Land 场站匹配 `match`。
3. 对需要新计算的场站：
   - 先确保场站 CF 缓存存在。
   - 从缓存计算阈值。
4. 对可从 `ssp126` 复用的场站：
   - 继续直接复制 `clim/threshold/valid_count/era5_*/weight`。
5. 写最终稀疏阈值文件。

## 10. 测试计划

使用小型合成 HDF5 文件覆盖：

1. 原生 chunk 读取：
   - 构造 chunked HDF5，确认每个时间块能正确抽取 station CF。
2. nearest_valid 缓存：
   - `cf(time, station)` 与手工抽取一致。
3. bilinear 缓存：
   - 四点权重结果与当前 `_read_month_station_cf()` 一致。
4. 缓存复用：
   - 缓存存在且属性匹配时不重建。
   - 属性不匹配时重建。
5. 阈值一致性：
   - 从缓存算出的 `clim/threshold/valid_count` 与当前小样本直接计算一致。
6. 原子写出：
   - 失败时不留下最终缓存文件。
7. 与 `ssp126` 增量复用同时启用：
   - 交集直接复用。
   - 非交集从缓存计算。

验收命令：

```bash
.venv/bin/python -m py_compile \
  step1_low_resource_thresholds.py \
  scripts/precompute_station_low_resource_thresholds.py

.venv/bin/pytest -q tests/test_sparse_low_resource_thresholds.py
```

## 11. 推荐运行顺序

修改完成后重新运行前，先确认没有半成品：

```bash
find outputs/low_resource_thresholds -name 'low_resource_threshold_sparse_ssp126_*_ERA5Land_2015-2024.nc'
find outputs/cache/era5land_station_cf -name 'station_cf_ssp126_*_ERA5Land_2015-2024_*.nc'
```

推荐串行运行：

```bash
.venv/bin/python step1_low_resource_thresholds.py \
  --stations_csv data/stations/stations_SSP1-2.6.csv \
  --tech wind \
  --baseline_years 2015-2024 \
  --overwrite

.venv/bin/python step1_low_resource_thresholds.py \
  --stations_csv data/stations/stations_SSP1-2.6.csv \
  --tech solar \
  --baseline_years 2015-2024 \
  --overwrite
```

不建议 wind 和 solar 同时跑，因为二者都会解压大型 gzip HDF5 chunk，容易争抢 CPU 和 I/O。

## 12. 风险与注意事项

1. 场站 CF 缓存体积可能较大，`ssp126/wind` 约为 `time * station * 4 bytes`，未压缩可超过 30 GB。
2. 缓存写出应启用压缩和合理 chunk，例如：

```text
cf chunksizes = (744, min(1024, station_count))
```

3. 如果缓存压缩过强，阈值计算读取缓存时也会慢；建议先使用 `zlib=True, complevel=1~2`。
4. 原生 chunk 读取会占用较大内存，尤其 wind 的 72 小时 chunk。应避免并行运行多个缓存生成进程。
5. 缓存文件必须记录 `source_files` 和 `threshold_interp`，防止不同输入或插值方法混用。
