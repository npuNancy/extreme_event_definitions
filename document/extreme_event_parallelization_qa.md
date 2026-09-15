# 极端事件计算提速：问题与分析

## 问题

当前 `extreme_event_definitions` 在超算上的运行速度比较慢：

1. 梳理极端事件计算 pipeline；
2. 思考如何提速，特别是对每个任务并行化，只考虑多进程并行，不考虑多 SLURM 作业并行；
3. `calculate_bcsd_-cfs` 的 CF 计算利用 BCSD Step6 三阶段作业的年份分块实现了并行，是否可以参考，尽管 pipeline 可能不同；
4. 对应的 10 个账号位于 `document_for_patchify_pipeline_of_generation_loss/account_config.csv`。

## 回答

结论先说：

- 可以参考 BCSD Step6 的年份块并行思想，但不能直接照搬。
- 对当前 `extreme_event_definitions`，最稳妥的第一步是按场站块做多进程并行，每个进程负责一部分场站，父进程最后按 station 维度合并。
- BCSD 年份块适合作为第二阶段优化，前提是先解决低资源事件的滚动窗口、2015–2024 基线和 P5 阈值依赖。

## 1. 当前极端事件 pipeline

当前作业单位是：

```text
model × scenario × patch × tech
= 4 × 3 × 47 × 2
= 1128 个 Extreme logical units
```

对应代码见 `extreme_event_definitions/infos/scnet_patchify/create_extreme_patch_jobs.py:18`。

单个任务的流程是：

```text
读取 patch manifest
    ↓
读取并筛选完整站点 CSV
    ↓
按技术类型和 patch bbox 筛选、去重场站
    ↓
检查 BCSD final 文件和 sidecar
    ↓
打开所需气象变量
    ↓
选择参考时间轴
    ↓
规则网格 → 场站空间匹配
    ↓
按场站块、时间子块读取并插值
    ↓
单位转换、uas/vas → wind_ms
    ↓
计算全部普通极端事件
    ↓
计算 24 h rolling、clim288、P5
    ↓
计算 low_resource
    ↓
应用空间有效性和 activation_year mask
    ↓
写 station-only NetCDF + sidecar
```

实现集中在 `extreme_event_definitions/scripts/station_signals_patchify.py:133-229`。

### 输入变量

- 风电：`tas, uas, vas, hurs, pr`
- 光伏：上述变量加 `rsds`

定义见 `station_signals_patchify.py:17`。

普通事件由 `registry.simple_signals()` 一次性计算；低资源事件额外使用：

- 3 小时数据上的 8 步居中滚动；
- 2015–2024 基线；
- `clim288(month, hour, station)`；
- 每站 P5；
- 光伏夜间过滤；
- `t+1` 标记规则。

低资源算法见 `extreme_event_definitions/tools/common.py:53-130`。

## 2. 当前主要瓶颈

### P1：场站空间匹配被重复计算

当前先计算一次 `full_match`，然后每个 station block 又重新调用，见 `station_signals_patchify.py:154` 和 `:171`。

多进程版本中应：

1. 父进程计算一次完整 `grid_match_map`；
2. 每个 worker 只切片使用自己的索引和权重；
3. 不再对每个 station block 重复 nearest/bilinear 搜索。

### P1：变量读取和插值

当前每个 station block 都要对每个变量执行时间切片、必要时 `xarray.interp`、规则网格 gather 和单位转换，见 `station_signals_patchify.py:183-207`。

尤其是 `tas/uas/vas/hurs/pr` 时间轴可能和参考变量有偏移，`xarray.interp()` 会带来额外开销。

可优化为：

- 预先建立变量时间轴对齐索引；
- 对固定偏移的时间轴使用直接线性插值索引和权重；
- 避免每个时间子块重新构造 xarray 插值对象；
- 保留当前按时间子块读取，不能重新一次性 `.values` 读完整文件。

### P2：rolling、clim288、P5

当前每个 station block 都会重新计算 `roll`、`clim` 和 `np.nanpercentile`。这已经避免了“每个事件重复计算”，但仍然会在每个 station block 运行一次。

可以进一步优化：

- `clim288` 改成一次性 sum/count 聚合，减少 288 次布尔筛选；
- 评估 `bottleneck.move_mean` 或等价 NumPy 实现；
- 对 P5 做专门 benchmark，确认是否为 CPU 热点；
- 不要为了追求速度改变 NaN、居中窗口和 percentile 语义。

### P2：NetCDF 压缩写出

每个 signal 都是 `int8` 二值变量，但当前使用 zlib level 4，见 `station_signals_patchify.py:115-125`。

可以在 pilot 中比较 `complevel=1/4`。如果计算节点 CPU 紧张，level 1 可能明显降低尾部写出时间。

## 3. 推荐的单任务多进程方案

### 第一阶段：按场站块并行

这是最适合当前 pipeline 的方案，因为普通事件和低资源事件都可以对每个场站独立计算。

```text
一个 Extreme logical unit
    ├── worker 0：station[0:n0]
    ├── worker 1：station[n0:n1]
    ├── worker 2：station[n1:n2]
    └── worker ...
            ↓
       parent merge
            ↓
    final station signal NetCDF
```

每个 worker：

1. 读取自己的 station slice；
2. 使用父进程传入的匹配索引和权重；
3. 独立打开 BCSD 输入；
4. 继续按时间子块读取；
5. 计算所有普通事件和 `low_resource`；
6. 写出一个独立的 `part_XX.nc` 和 sidecar。

父进程：

1. 等待所有 worker；
2. 按原始 station 顺序检查并合并；
3. 校验 `station_id`、坐标、容量、时间轴；
4. 原子发布最终文件和 sidecar。

merge 必须流式实现：单个事件变量 `signal_*` 的体量是
`T(约 134k) × 每站 int8`，大 patch 有数万站，一次物化整个
`(T, all stations)` 数组可达 GB 级，会把多进程省下的内存重新吃回去。
父进程应按事件变量 × station 列块（或时间块）从各 `part_XX.nc`
分块读出、按列拼接、直接写入最终文件，任何时刻内存中只保留一个
块，不整体物化。part 的校验（station_id、坐标、时间轴）只读
station 维的一维元数据变量，代价很小。

重要约束：

- worker 不能并发写同一个最终 NetCDF；
- 使用 `multiprocessing.get_context("spawn")`；
- worker 内部必须继续限制时间块大小；
- 每个 part 必须可单独重试；
- 最终完成判据仍是“所有 part 完成 + merge 成功 + final sidecar 有效”。

建议新增参数：

```text
--processes
--station-block-size
--parts-root
```

生成器也应像 CF 生成器一样传入 `--processes`。当前 Extreme generator 还没有这个参数，而 CF generator 已经有，见 `calculate_bcsd_-cfs/infos/scnet_patchify/create_cf_patch_jobs.py:23-39`。

### 进程数建议

不要直接默认 8。

CF 的已有 pilot 结果是：

```text
n=1：3:52
n=2：2:18
n=4：0:37
n=8：1:14
```

说明共享文件系统 I/O 和内存会限制扩展性，`n=4` 只是候选值，不是普适结论。

Extreme 建议先测：

```text
processes = 1, 2, 4, 8
```

同时记录 `Elapsed`、`MaxRSS`、`AveCPU`、读取字节量、merge 时间和输出文件大小。通常应从 `n=2` 或 `n=4` 开始。

不做 campaign 级并发复测：多账号 × 多作业同时读 BCSD 文件带来的共享存储压力，由 SCNet 侧排队和 I/O 带宽自然限制，不在本方案调参范围内。单作业 pilot 选出的 `n` 直接用于正式 campaign。

## 4. BCSD Step6 年份块能否复用

答案是：**架构可以复用，数据读取层不能直接复用。**

当前仓库里实际存在两类 BCSD block：

1. 旧/兼容路径：`<output>/.blocks/<output_stem>/...`，通常是 `(time, lat, lon)`；
2. `bcsd/global_bcsd` 三阶段路径：`blocks/<model>/<scenario>/<variable>/...`，通常是 `(time, point)`，并附带 `global_point`、`y_index`、`x_index`、`flat_index`、`lat`、`lon`。

这点在 `calculate_bcsd_-cfs/document/patchify_CF_复用BCSD年份块的多进程并行方案.md:180-223` 中已有明确记录。

因此 Extreme 不能只把当前的 `final(...)` 改成“按年份拼文件名”，必须从 BCSD manifest 读取 block 路径和 schema。

### 年份块方案的关键问题

#### 1. 低资源事件不是完全按年份独立

24 h 居中滚动会跨 block 边界，至少需要读取相邻 block 的边界时次。

同时存在 `roll(t)`、`clim288`、`P5` 和 `mark_next_step`。如果每个年份 worker 独立计算 2015–2060，会得到错误的基线阈值。

可选方案：

```text
方案 A：先生成 2015–2024 的 clim288/P5 cache，再运行年份 worker
方案 B：一个作业内先做 baseline pass，再做全时段 signal pass
```

不能直接把每个年份块自己的 P5 当成全局 P5。

#### 2. Point block 不是规则网格

对于 `(time, point)` 文件，需要：

- 根据 `land_plan` 恢复全局点身份；
- 建立稳定的 station → point 索引；
- 保持 nearest/bilinear 的原有空间语义；
- 对缺失海洋点继续返回缺测；
- 不能静默改成“最近可用陆地点”。

否则会改变海岸附近场站的事件结果。

#### 3. 内存和 I/O 仍需控制

一个年份 block 只减少了时间范围，但如果 worker 一次加载所有场站，仍可能很大。更稳妥的是：

```text
年份 block worker
    └── 内部继续按 station block 和 time chunk 流式计算
```

这样会形成“年份分片 + 场站分片”的二维结构，代码复杂度和 merge 复杂度都明显高于单纯 station parallel。

## 5. 推荐实施顺序

### Phase 0：基准测试

选择一个大 patch 和一个小 patch，测当前串行版本的 station CSV、input open、spatial match、weather gather/interp、rolling/clim/P5、ordinary events、write/compression 时间。

### Phase 1：低风险提速

优先实现：

1. 复用完整 `grid_match_map`；
2. 增加 station-block `ProcessPoolExecutor`；
3. 增加 part sidecar 和 parent merge（必须流式，见第 3 节）；
4. generator 增加 `--processes`；
5. 作业脚本设置 `OMP_NUM_THREADS=1`、`MKL_NUM_THREADS=1`、`OPENBLAS_NUM_THREADS=1`、`NUMEXPR_NUM_THREADS=1`。

### Phase 2：局部计算优化

再评估时间轴插值索引缓存、`clim288` 聚合优化、rolling 实现、NetCDF 压缩等级，以及是否需要只读 station-weather cache。

### Phase 3：BCSD block input mode

只有当 Phase 1 后仍然受完整 final 文件读取限制，再增加 `--input-mode final|blocks|auto`，并复用 CF 已经验证过的 manifest 驱动、block schema 校验、邻接时间边界、point-order 校验、part sidecar 和最终 merge 契约。

## 6. 与 10 个账号的关系

本次只讨论单个 Slurm 任务内部的多进程，不改变：

- 10 个账号；
- 每账号 20 个 active-job 上限；
- patch ownership；
- logical unit 数量；
- CF → Extreme → Loss 的任务依赖。

当前脚本统一申请 10 核，进程数增加后应确保：

```text
processes <= cpus-per-task
```

并依据实际 `MaxRSS` 调整资源。多进程会增加总内存，不能因为“单个 worker 内存下降”就忽略整个任务的峰值内存。

## 验证状态

Phase 0 + Phase 1 已实施（`scripts/station_signals_patchify.py`、`infos/scnet_patchify/create_extreme_patch_jobs.py`）。Extreme 专项测试已通过：

```text
39 passed
```

其中新增 `tests/test_multiprocess_signals_equivalence.py`：`--processes 2/4`（wind/solar 各两组）与串行输出逐位一致；part 布局与 sidecar 契约；part 重试语义（无有效 sidecar 的 part 重算，其余复用）。以及 `tests/test_slab_read_regression.py`：chunk-cache 活锁回归测试。

### 2026-09-15 乌镇199 pilot（slab 读取修复后）

unit：`BCC-CSM2-MR / ssp126 / R03C09 / solar`（17823 站，58 part）：

```text
n=4  (10 核): COMPLETED  1:10:42   MaxRSS 14.4 GB
n=8  (10 核): COMPLETED  0:41:14   MaxRSS 17.6 GB
n=16 (16 核): COMPLETED  0:27:28   MaxRSS 31.4 GB
```

- 修复前：n=4/n=8 作业在 part 16/40/53 上 HDF5 解压活锁（同一批 chunk 无限重复解压），3 小时零进展；n=1/n=2 串行路径同样会命中。
- 修复后：part 16/40/53 分别以 62.8s/90.8s/43.0s 正常完成；hurs 单变量耗时从 55–62s 降到 22–25s。
- n=4 与 n=16 输出逐位一致（全部 6 个事件 × 全时序抽查通过）。
- merge 阶段稳定在 ~10 分钟，与 n 无关；compute 阶段 n=4→8→16 为 59.5→30.3→16.9 分钟，近线性扩展。
- 换算：n=16 时每 part 平均 ~29s；正式 campaign 若用 n=16/16 核，单个 logical unit 约 0.5 小时，1128 个 unit 在 10 账号 × 20 并发下约 3 小时批次（I/O 竞争会拉长）。

完整 pytest 会额外收集 `ref_code` 下的历史测试，并出现 6 个收集错误：4 个因缺少 `windpowerlib` 环境依赖，1 个是 `ref_code/bcsd/repair_bcsd_missing` 中 `convert_era5land_var` 导入失败，1 个是 `ref_code/calculate_wind_solar_out/scnet` 中 `create_station_output_jobs` 导入失败。后两类是 `ref_code` 历史代码自身的问题，与当前 Extreme 主流程无关。
