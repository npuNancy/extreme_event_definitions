# 超算加速 step1 低资源事件阈值计算方案

## 1. 目标

当前 step1 的目标是使用 ERA5Land 2015-2025 年风电/光伏容量因子，为 SSP 场站计算稀疏低资源阈值。

已有本地加速计划是：

1. 按 ERA5Land CF 文件的 HDF5 原生 chunk 读取。
2. 先生成场站 CF 缓存。
3. 再从场站 CF 缓存计算 `clim(12,24,station)` 和 `threshold(station)`。

本方案是在超算 `scnet-wuzhen-185` 上进一步加速 step1，核心思路是：

1. 不在登录节点计算，只通过 Slurm 提交批处理任务。
2. 把三个 SSP 的场站先合并成并集，避免重复读取全球 ERA5Land CF。
3. 把 2015-2025 年的月文件拆成 Slurm array job 并行处理。
4. 每个 array task 只生成一个月的场站 CF 缓存。
5. 最后合并月度缓存，再分别为 ssp126/ssp245/ssp585 计算低资源阈值。

## 2. 超算环境检查结果

已确认 `scnet-wuzhen-185` 可以登录，且远端有 Slurm 环境。

```text
登录主机：login05
用户：acbw9wpn5k
可用调度命令：sbatch, srun, qsub, bsub
Slurm 分区：wzhctest
节点规格：
  32 core / 约 126 GB 内存
  64 core / 约 255 GB 内存
共享文件系统：/work
```

注意：

1. 登录节点 `nproc=2`，不能直接运行正式 step1 计算。
2. `/work` 可用空间充足，适合作为代码、输入数据、缓存和输出目录。
3. 当前没有看到 `/data6`、当前项目目录或 ERA5Land CF 数据在超算侧可见，因此正式运行前需要先同步代码和数据。

## 3. 当前瓶颈

ERA5Land CF 文件的 HDF5 chunk 结构是：

```text
wind  chunks = (72, 1801, 3600), gzip
solar chunks = (24, 1801, 3600), gzip
```

每个压缩 chunk 覆盖一段时间和完整全球空间网格。旧代码按场站点读取，例如：

```python
d[:, lat_i, unique_lon]
```

这会为了少量场站点反复解压巨大的全球 chunk，导致运行极慢。

本地加速计划已经把读取方式改为按原生 chunk 读取：

```python
slab = d[t0:t1, :, :]
station_cf = gather_station_cf_from_slab(slab, match)
```

超算侧进一步优化的是并行粒度：按月份拆分，而不是一个进程串行处理 132 个月。

## 4. 推荐总体流程

```text
阶段 A0：同步代码和 ERA5Land CF 数据到 /work
阶段 A1：生成 ssp126/ssp245/ssp585 场站并集表
阶段 A2：Slurm array 并行生成月度场站 CF 缓存
阶段 A3：合并月度场站 CF 缓存为完整场站 CF 缓存
阶段 B1：按 SSP 子集从完整缓存计算低资源阈值
阶段 B2：检查阈值文件完整性
```

推荐优先实现这个流程，而不是分别为每个 SSP 独立跑完整 step1。

## 5. 数据和目录布局

建议在超算 `/work` 下使用独立项目目录：

```text
/work/home/acbw9wpn5k/project_climate/extreme_event_definitions/
```

建议目录结构：

```text
data/
  cfs/
    CFs_of_wind_ERA5Land/
    CFs_of_solar_ERA5Land/
  stations/
    stations_SSP1-2.6.csv
    stations_SSP2-4.5.csv
    stations_SSP5-6.0.csv

outputs/
  cache/
    era5land_station_cf_hpc/
      union_stations/
      monthly/
      merged/
  low_resource_thresholds/

logs/
  slurm/
  step1/
```

如果 ERA5Land CF 文件体积过大，不建议每次重新传输。更推荐：

1. 一次性同步到 `/work`。
2. 后续只同步代码和小文件。
3. 使用 checksum 或文件数量检查确认数据完整。

## 6. 阶段 A1：生成三个 SSP 的场站并集

目标：把 ssp126、ssp245、ssp585 的场站按严格键合并。

推荐严格键：

```text
(lon, lat, type)
```

输出文件建议：

```text
outputs/cache/era5land_station_cf_hpc/union_stations/
  stations_union_ssp126_ssp245_ssp585.csv
  stations_union_index_map_ssp126.csv
  stations_union_index_map_ssp245.csv
  stations_union_index_map_ssp585.csv
```

其中：

1. `stations_union_ssp126_ssp245_ssp585.csv` 保存去重后的并集场站。
2. `stations_union_index_map_<scenario>.csv` 保存当前 SSP 场站行号到并集场站行号的映射。

这样后续只需要为并集场站生成一次 ERA5Land station CF 缓存。

## 7. 阶段 A2：Slurm array 并行生成月度场站 CF 缓存

### 7.1 并行粒度

2015-2025 年共有：

```text
11 年 * 12 月 = 132 个年月任务
```

建议按 `tech + year + month` 作为 array task。

可以分两批提交：

```text
wind  : 132 个 array task
solar : 132 个 array task
```

也可以把 `tech` 放进任务表，一次性提交 264 个 task。为了控制 I/O 压力，初期建议 wind 和 solar 分开提交。

### 7.2 月度缓存命名

建议输出：

```text
outputs/cache/era5land_station_cf_hpc/monthly/
  station_cf_union_wind_ERA5Land_201501_nearest_valid.nc
  station_cf_union_wind_ERA5Land_201502_nearest_valid.nc
  ...
  station_cf_union_solar_ERA5Land_201501_nearest_valid.nc
```

每个月度缓存维度：

```text
time
station
corner = 4
```

核心变量：

```text
time(time)
station(station)
cf(time, station)
era5_lat_idx(station, corner)
era5_lon_idx(station, corner)
era5_lat(station, corner)
era5_lon(station, corner)
weight(station, corner)
```

核心属性：

```text
cache_kind = era5land_station_cf_monthly
tech = wind | solar
year = 2015
month = 1
baseline_source = ERA5Land
threshold_interp = nearest_valid | bilinear
station_table = stations_union_ssp126_ssp245_ssp585.csv
source_file = ...
```

### 7.3 原子写出

每个 array task 必须先写临时文件：

```text
<monthly_cache>.tmp.<jobid>.<taskid>
```

写完并关闭后再：

```python
os.replace(tmp_path, monthly_cache)
```

这样失败任务不会留下看起来完整的半成品。

### 7.4 array 并发控制

不要一次性让 132 个任务同时读取 ERA5Land 全球 CF 文件。共享文件系统和 gzip 解压都会成为瓶颈。

建议初始参数：

```bash
sbatch --array=0-131%8 ...
```

如果 I/O 稳定，再尝试：

```bash
sbatch --array=0-131%12 ...
sbatch --array=0-131%16 ...
```

不建议一开始超过 `%16`。

wind 单个原生 chunk 解压后约 1.78 GB，solar 约 593 MB。单任务内存建议：

```text
wind  : 16-32 GB
solar : 8-16 GB
```

如果同一个节点同时跑多个 wind task，需要按并发数乘以内存估算。

## 8. 阶段 A3：合并月度场站 CF 缓存

月度任务全部完成后，合并成完整缓存：

```text
outputs/cache/era5land_station_cf_hpc/merged/
  station_cf_union_wind_ERA5Land_2015-2025_nearest_valid.nc
  station_cf_union_solar_ERA5Land_2015-2025_nearest_valid.nc
```

合并时必须检查：

1. 132 个月度缓存是否全部存在。
2. 每个月度缓存的 `tech/year/month/threshold_interp/station_table` 是否匹配。
3. 时间轴是否连续且无重复。
4. `station` 维度是否一致。
5. 场站经纬度、类型和并集表是否一致。
6. `cf` 是否存在明显全 NaN 或异常填充值。

合并后的完整缓存也需要原子写出。

## 9. 阶段 B1：按 SSP 子集计算低资源阈值

完整并集缓存生成后，不需要再次读取 ERA5Land 全球 CF。

对每个 SSP：

1. 读取 `stations_union_index_map_<scenario>.csv`。
2. 从完整并集缓存中取当前 SSP 对应的 station 子集。
3. 计算：
   - 24h centered rolling mean
   - `clim(12,24,station)`
   - `threshold(station) = P5(anom)`
4. 写出当前 SSP 的稀疏低资源阈值文件。

输出仍然沿用当前 schema：

```text
outputs/low_resource_thresholds/sparse_station_ERA5Land_2015-2025/
  low_resource_threshold_sparse_ssp126_wind_ERA5Land_2015-2025.nc
  low_resource_threshold_sparse_ssp126_solar_ERA5Land_2015-2025.nc
  low_resource_threshold_sparse_ssp245_wind_ERA5Land_2015-2025.nc
  ...
```

## 10. 与 ssp126 增量复用的关系

如果采用“三个 SSP 场站并集缓存”，则 step1 的主要复用方式会变化：

1. 原来的复用：`ssp245/ssp585` 从 `ssp126` 阈值文件复制重合场站阈值。
2. 超算推荐复用：三个 SSP 共享同一个并集 station CF 缓存。

推荐保留原有 `ssp126` 阈值复用逻辑作为兼容路径，但在超算流程中优先使用并集缓存。

原因：

1. 读取 ERA5Land 全球 CF 是最慢步骤。
2. 用并集缓存可以从源头避免三个 SSP 重复读全球 CF。
3. 阈值计算本身相对快，不必过度依赖阈值层面的复制。

## 11. Slurm 脚本建议

### 11.1 月度缓存 array job

示例命令：

```bash
sbatch \
  --partition=wzhctest \
  --array=0-131%8 \
  --cpus-per-task=4 \
  --mem=32G \
  --time=12:00:00 \
  --job-name=era5cf_wind_cache \
  jobs/step1_build_monthly_station_cf_cache.sbatch wind
```

`solar` 可以使用较小内存：

```bash
sbatch \
  --partition=wzhctest \
  --array=0-131%8 \
  --cpus-per-task=4 \
  --mem=16G \
  --time=12:00:00 \
  --job-name=era5cf_solar_cache \
  jobs/step1_build_monthly_station_cf_cache.sbatch solar
```

### 11.2 合并完整缓存

```bash
sbatch \
  --partition=wzhctest \
  --cpus-per-task=4 \
  --mem=64G \
  --time=8:00:00 \
  --job-name=merge_wind_cache \
  jobs/step1_merge_station_cf_cache.sbatch wind
```

### 11.3 计算 SSP 阈值

```bash
sbatch \
  --partition=wzhctest \
  --cpus-per-task=4 \
  --mem=64G \
  --time=8:00:00 \
  --job-name=threshold_ssp126_wind \
  jobs/step1_threshold_from_union_cache.sbatch ssp126 wind
```

## 12. 推荐代码入口

建议在现有 step1 基础上增加超算专用内部入口或脚本，而不是把 Slurm 逻辑硬塞进普通入口。

推荐新增：

```text
scripts/hpc_step1_build_union_stations.py
scripts/hpc_step1_build_monthly_station_cf_cache.py
scripts/hpc_step1_merge_station_cf_cache.py
scripts/hpc_step1_threshold_from_union_cache.py
```

推荐保留当前用户入口：

```text
step1_low_resource_thresholds.py
```

普通入口适合本地或单节点运行；HPC 脚本适合 Slurm array 和批处理。

## 13. 环境准备

超算侧建议使用项目 `.venv` 或重新创建 uv 环境。

检查命令：

```bash
which python
python -V
python -c "import numpy, pandas, netCDF4, h5py; print('ok')"
```

如果超算没有 uv，可以选择：

1. 在超算上安装 uv 后同步环境。
2. 使用已有 Python module 创建 `.venv`。
3. 使用 conda/mamba 创建等价环境。

正式方案应尽量不新增依赖，优先使用现有 `numpy/pandas/netCDF4/h5py`。

## 14. 运行前检查清单

提交 Slurm 任务前检查：

```bash
pwd
ls data/stations/
ls data/cfs/CFs_of_wind_ERA5Land | head
ls data/cfs/CFs_of_solar_ERA5Land | head
python -c "import h5py, netCDF4, numpy, pandas; print('env ok')"
```

检查 ERA5Land CF 文件：

1. 2015-2025 年 wind 月文件是否齐全。
2. 2015-2025 年 solar 月文件是否齐全。
3. 文件变量名是否和本地一致。
4. HDF5 chunk 是否仍为预期的 `(72,1801,3600)` 或 `(24,1801,3600)`。

检查场站文件：

1. 三个 SSP 场站 CSV 是否存在。
2. 经纬度字段和类型字段是否能被当前代码识别。
3. 并集场站数量是否合理。

## 15. 失败恢复策略

月度缓存适合断点续跑。

推荐规则：

1. 已存在且校验通过的月度缓存不重复生成。
2. 失败或校验不通过的月份单独重跑。
3. 合并阶段只在 132 个月度缓存全部通过校验后执行。
4. 完整缓存已存在且校验通过时，阈值阶段直接复用。

重跑单个月份的方式：

```bash
sbatch --array=<task_id> jobs/step1_build_monthly_station_cf_cache.sbatch wind
```

如果一个月份反复失败，优先检查：

1. 源 ERA5Land CF 文件是否损坏。
2. 该月时间轴是否异常。
3. 该月变量名或维度是否和其他月份不一致。
4. Slurm 内存是否不足。

## 16. 性能预期

本地旧流程的问题是点式读取导致同一个巨大 gzip chunk 被反复解压。  
本地新流程按原生 chunk 读取后，每个 chunk 只解压一次。  
超算流程在此基础上按月份并行，因此 wall time 主要取决于：

1. 单个月文件读取和抽取耗时。
2. Slurm array 并发数。
3. `/work` 共享文件系统 I/O 压力。
4. gzip 解压 CPU 开销。

初期建议保守并发：

```text
wind  : array %8
solar : array %8
```

如果 I/O 等待不高，可以逐步提高到 `%12` 或 `%16`。

不建议同时高并发提交 wind 和 solar，因为二者都会读取全球 ERA5Land CF 文件并解压大型 chunk。

## 17. 风险

1. 数据未同步到超算侧时，不能直接运行。
2. 登录节点不能跑正式计算。
3. array 并发过高会导致共享文件系统 I/O 拥塞。
4. 月度缓存和完整缓存体积较大，需要提前确认 `/work` 配额。
5. 并集场站表必须稳定，否则缓存和 SSP 子集映射会错位。
6. 如果后续修改 `nearest_valid/bilinear` 算法，必须重建对应插值方法的缓存。

## 18. 推荐执行顺序

```text
1. 同步代码到 /work。
2. 同步 ERA5Land wind/solar CF 数据到 /work。
3. 在超算侧建立 Python 环境。
4. 生成三个 SSP 的场站并集表。
5. 提交 wind 月度缓存 Slurm array。
6. wind 月度缓存全部完成后，合并 wind 完整缓存。
7. 从 wind 完整缓存计算三个 SSP 的 wind 阈值。
8. 提交 solar 月度缓存 Slurm array。
9. solar 月度缓存全部完成后，合并 solar 完整缓存。
10. 从 solar 完整缓存计算三个 SSP 的 solar 阈值。
11. 检查 6 个最终阈值文件完整性。
```

如果 `/work` I/O 压力较小，也可以 wind 和 solar 分别使用较低并发同时提交，但初次运行不建议这样做。

## 19. 最终验收

最终应得到 6 个阈值文件：

```text
ssp126 wind
ssp126 solar
ssp245 wind
ssp245 solar
ssp585 wind
ssp585 solar
```

每个文件应满足：

1. `clim` 形状为 `(12, 24, station)`。
2. `threshold` 形状为 `(station,)`。
3. `valid_count` 形状为 `(station,)`。
4. `station_lon/station_lat/station_type` 与对应 SSP 场站文件一致。
5. `threshold_interp` 和缓存插值方法一致。
6. 文件属性记录 ERA5Land、baseline years、source cache 和生成时间。

完成后，下游 step2/step3 继续读取这些稀疏阈值文件，不需要知道它们来自本地串行流程还是超算并行流程。
