# BCSD 低资源事件与其余极端事件合并实施方案

## 1. 目标与实施口径

本次修改只纠正极端天气识别 pipeline 的低资源事件输入和执行方式：

1. 风电低资源事件使用 Regional BCSD 降尺度后的 10 m 风速
   `wind_ms = sqrt(uas^2 + vas^2)`。
2. 光伏低资源事件使用 Regional BCSD 降尺度后的地表向下短波辐照 `rsds`。
3. 不再读取未来风电/光伏容量因子（CF），不再读取 ERA5Land CF 低资源阈值，
   也不再运行 CF 阈值预计算 pipeline。
4. 低资源事件与其余极端事件在同一个场站级作业中计算，并一次写入同一个 NetCDF。
5. 保留现有低资源事件的数学定义，不在本次修改中调整事件阈值：
   - 24 小时居中滚动资源均值；
   - 减去月×小时气候态；
   - 距平小于或等于场站 P5 时记为低资源事件；
   - 保留 `t` 与下一个时间步 `t+1` 的标记；
   - 光伏夜间强制为非事件。
6. 沿用当前 pipeline 的基线期 `2015-2024`。气候态和 P5 均由当前
   `model × region × scenario × tech` 的 BCSD 场站资源序列计算，不再跨模式、
   跨情景共享 ERA5Land CF 阈值。

本方案不修改其他极端事件的气象阈值，不修改场站筛选、场站与 BCSD 网格的空间匹配规则，
也不扩展到尚未部署的 `china_cmfd_bcsd` 或 `cordex_nam12` 数据源。

## 2. 当前 pipeline 与问题定位

当前场站级生产流程实际被拆为三部分：

```text
ERA5Land CF + SSP 场站
  -> step1_low_resource_thresholds.py
     或 step1_split_E1/E2a/E2b/E3
  -> ERA5Land CF 稀疏阈值文件

Regional BCSD 气象
  -> step2_split_E1_weather_extremes.py
  -> 其余极端事件 NetCDF

未来 CF + ERA5Land CF 阈值
  -> step2_split_E2_low_resource.py
  -> 原位补写 signal_low_resource
```

问题集中在以下调用链：

```text
step2_split_E2_low_resource.py
  -> scripts/patch_pipelineB_low_resource.py
  -> grid_extreme_signals/cf_low_resource.py
  -> 未来 CF + ERA5Land CF 阈值
```

与此同时，`scripts/station_signals_direct.py` 本身也包含同一套 CF 查找和阈值读取逻辑。
因此不能只删除 E2 入口；必须先把该脚本中的低资源计算改为直接消费已经加载并标准化的
BCSD `wind_ms`/`rsds`。

## 3. 目标 pipeline

修改后只保留一个科学计算阶段：

```text
Regional BCSD uas/vas/rsds/其他气象变量
  -> RegionalBcsdAdapter 标准化
  -> 场站空间抽取
  -> 同一作业内计算：
       wind:  icing/high_temp/hot_humid/high_wind/low_resource
       solar: freezing_rain/icing/dust/rainstorm/high_humidity/
              cold_highwind/low_resource
  -> 应用场站投产年份和空间有效性掩膜
  -> 一次写出 station_signals_*.nc
```

唯一生产入口使用：

```text
./step2_complete_extreme_events.py
```

每个 Slurm 作业单位保持为：

```text
model × region × scenario × tech × years
```

不再存在 step1 低资源阈值阶段、step2 E1 普通事件阶段和 step2 E2 低资源补写阶段。

## 4. 低资源事件计算设计

### 4.1 输入变量

| 技术 | BCSD 原始变量 | 统一资源变量 | 单位 |
|---|---|---|---|
| wind | `uas`、`vas` | `wind_ms = sqrt(uas^2 + vas^2)` | m s-1 |
| solar | `rsds` | `rsds` | W m-2 |

`RegionalBcsdAdapter` 已生成并校验这两个统一变量，本次不新增数据读取路径。

### 4.2 时间和基线约束

生产作业当前使用 `--years 2015-2060`，已完整覆盖 `2015-2024` 基线。合并计算后应把
以下条件变为显式校验：

1. `--years` 必须是连续年份范围；
2. 必须覆盖完整的 `2015-2024`；
3. 各年时间轴拼接后必须严格递增、无重复，且时间步长一致；
4. Regional BCSD 为 3 小时时间步，因此 24 小时窗口为 8 个时间步；不得继续套用
   小时级数据的 24 个时间步；
5. 低资源滚动计算必须在完整连续时间轴上执行一次，不能逐年独立计算，否则每个年界两侧
   会产生不必要的缺失窗口；
6. 基线掩膜为 `2015-01-01 <= time <= 2024-12-31 23:59:59`，P5 只由该掩膜内的
   有限距平值计算。

若未来确需只输出不覆盖基线的年份，应另行设计“加载基线和连续计算期、最后裁切输出”的能力；
本次不把这一扩展混入修正。

### 4.3 计算位置

`scripts/station_signals_direct.py` 仍可逐年计算不依赖长期基线的普通事件，但每年还需要只累积
当前技术的资源数组：

```text
wind  -> weather["wind_ms"]
solar -> weather["rsds"]
```

全部年份拼接并完成时间轴校验后，调用对应的低资源事件定义：

```text
events.wind_low_resource.signal(..., window_steps=8, base_mask=...)
events.solar_low_resource.signal(..., window_steps=8, base_mask=..., lat=..., lon=...)
```

随后把结果作为 `masks_all["low_resource"]` 与普通事件一起应用投产年份掩膜、BCSD 网格距离
有效性掩膜，并一次写出。低资源事件使用的就是同一个 BCSD→场站空间匹配结果，不再进行第二次
CF 网格匹配。

### 4.4 失败策略

`wind_ms` 和 `rsds` 分别是当前 wind/solar adapter 的必要输入，所以低资源事件不能再以
“找不到 CF/阈值后警告并跳过”的方式静默缺失。以下情况均应使当前作业非零退出：

- 资源变量缺失；
- 请求年份未完整覆盖基线；
- 时间轴不连续、重复或步长不是预期的均匀步长；
- 某个有效场站在基线期没有足够有限值计算气候态或 P5；
- 低资源结果形状与其他信号或输出时间轴不一致。

无场站的 `region × scenario × tech` 单元仍成功跳过，且不要求生成输出文件。

### 4.5 输出属性

移除 CF 专属属性：

```text
low_resource_cf_file
low_resource_threshold_file
low_resource_threshold_source = ERA5Land
low_resource_target_spatial_interp
```

写入能够复现新口径的属性：

```text
low_resource_source = regional_bcsd
low_resource_resource_variable = wind_ms | rsds
low_resource_resource_units = m s-1 | W m-2
low_resource_baseline_years = 2015-2024
low_resource_climatology = month_hour
low_resource_percentile = 5
low_resource_window_hours = 24
low_resource_window_steps = 8
low_resource_timestep_hours = 3
low_resource_mark_next_step = true
low_resource_solar_night_filter = true | false
```

`supported_events` 必须包含 `low_resource`。由于资源变量是必要输入，正常完成的生产文件中不得再把
`low_resource` 放入 `skipped_events`。

## 5. `./*.py` pipeline 文件修改清单

以下清单专门回答项目根目录 `./*.py` 中哪些文件需要处理。

| 文件 | 动作 | 实施内容 |
|---|---|---|
| `./step2_complete_extreme_events.py` | 修改并保留 | 作为唯一生产入口；更新模块说明，明确一次计算全部事件且只依赖 BCSD、场站 CSV 和边界文件。继续调用 `scripts.station_signals_direct.main()`。 |
| `./registry.py` | 修改并保留 | 将低资源事件纳入“全部事件”的统一注册语义；新增接收 `time`、基线掩膜及太阳能场站经纬度的统一调用接口，或提供与普通事件同层级的 `low_resource_signal` 调度函数；删除“低资源需由 CF/单独 pipeline 处理”的说明。 |
| `./extreme_event_definitions.py` | 修改并保留 | 统一文档和示例中的资源来源，明确 wind 使用 BCSD `wind_ms`、solar 使用 BCSD `rsds`；使 3 小时数据通过显式 `window_steps=8` 进入共享算法，避免示例继续暗示小时级固定窗口。 |
| `./step2_split_E1_weather_extremes.py` | 废弃兼容入口 | 不再追加 `--no_low_resource`，转发到统一入口；新作业不得使用 E1 名称。 |
| `./step2_split_E2_low_resource.py` | 废弃入口 | 直接报错提示使用统一入口，不再补写低资源事件。 |
| `./step1_low_resource_thresholds.py` | 废弃入口 | 直接报错提示使用统一入口，不再启动 CF 阈值计算。 |
| `./step1_split_E1_union_stations.py` | 保留历史函数、禁止 CLI 运行 | 仅用于历史结果/迁移，不再作为生产前置阶段。 |
| `./step1_split_E2a_extract_union_station_cf_monthly.py` | 保留历史函数、禁止 CLI 运行 | 不再抽取生产所需的 ERA5Land 场站 CF 缓存。 |
| `./step1_split_E2b_merge_union_station_cf_cache.py` | 保留历史函数、禁止 CLI 运行 | 不再合并生产所需的 ERA5Land 场站 CF 缓存。 |
| `./step1_split_E3_thresholds_from_union_cache.py` | 保留历史函数、禁止 CLI 运行 | 不再从 CF cache 生成生产阈值。 |

新生产调用链不再引用这些兼容入口；后续彻底删除前先用 `rg` 确认没有非历史调用方。

## 6. 根目录外代码修改清单

### 6.1 必须修改

| 文件 | 实施内容 |
|---|---|
| `scripts/station_signals_direct.py` | 核心修改：删除所有 CF/阈值 CLI 参数和 `cf_low_resource` import；删除 `--no_low_resource`；逐年累积 BCSD 资源；拼接后校验时间轴、生成基线掩膜、计算低资源；与其他事件统一应用掩膜和写出属性。 |
| `events/wind_low_resource.py` | 更新注释和参数说明，明确资源是 BCSD 10 m 风速；保持 P5、滚动、`t+1` 逻辑不变。 |
| `events/solar_low_resource.py` | 更新注释和参数说明，明确资源是 BCSD `rsds`，继续使用场站经纬度过滤夜间。 |
| `tools/common.py` | 增加或复用统一时间步长校验及“24 小时→时间步数”换算；低资源算法不得默认假设输入总是逐小时。 |
| `grid_extreme_signals/adapters/base.py` | 更新 `WeatherBundle` 文档，明确 `wind_ms`/`rsds` 同时也是低资源输入。代码结构无须重构。 |

### 6.2 退出主流程（历史兼容代码暂保留）

以下文件只服务于旧 CF 低资源路径；本次已从生产调用链退出，但暂不删除，以便读取历史
结果、完成迁移和保留既有回归测试。后续确认无外部调用后再单独删除：

```text
scripts/patch_pipelineB_low_resource.py
scripts/precompute_low_resource_thresholds.py
scripts/precompute_station_low_resource_thresholds.py
scripts/hpc_step1_common.py
scripts/hpc_step1_validate_thresholds.py
grid_extreme_signals/cf_low_resource.py
```

`grid_extreme_signals/__init__.py` 如导出了 `cf_low_resource`，同步删除该导出。当前该包未导出
该模块，因此无需修改。

### 6.3 不需要修改

```text
grid_extreme_signals/adapters/regional_bcsd.py
grid_extreme_signals/station_match.py
grid_extreme_signals/signal_runner.py
scripts/generate_multi_source_grid_signals.py
```

原因如下：

- `regional_bcsd.py` 已正确生成 `wind_ms` 和 `rsds`；本次直接复用。
- `station_match.py` 的场站筛选、空间抽取、`station_id` 和写出逻辑不变。
- `signal_runner.py` 与 `generate_multi_source_grid_signals.py` 属于网格级多数据源流程，不在本次
  `./step2_complete_extreme_events.py` 场站生产 pipeline 范围内。若以后要求网格级输出也包含
  低资源事件，应单独设计跨年基线和网格内存策略。

## 7. `extreme_event_definitions/infos` 修改清单

当前 `infos` 把生产流程分为 step1、step2 E1 和 step2 E2。合并后应改为单一 step2 campaign，
并避免继续用 E1/E2 表示已经不存在的科学阶段。

### 7.1 合并为统一 step2 作业生成器

将以下两份生成器合并：

```text
infos/hpc_step2_E1/create_step2_E1_jobs.py
infos/hpc_step2_E2/create_step2_E2_jobs.py
```

目标文件建议为：

```text
infos/hpc_step2/create_step2_jobs.py
```

实现时：

1. 以现有 E1 生成器的 BCSD、模型、区域、场站、边界、输出路径和环境参数为主体；
2. 合入现有 E2 生成器的场站 inventory，使 manifest 继续记录 `station_count` 和
   `has_stations`；
3. 作业命令改为 `python step2_complete_extreme_events.py`；
4. 删除 `--cf-root`、`--threshold-dir`、`--baseline-years`、`--station-id-only` 和 E2 patch
   相关参数；基线 `2015-2024` 由科学入口显式校验并记录；
5. `--years` 的生产默认值仍由调用方传入，但生成时校验其覆盖 `2015-2024`；
6. 参数 `--e1-cpus` 改为中性名称 `--cpus`；
7. 默认作业与日志目录改为 `~/extreme_event_jobs/step2` 和
   `~/extreme_event_logs/step2`；
8. manifest 的 `stage`、campaign ID、job prefix 和 job name 全部改为不含 E1/E2 的名称，
   例如 `stage=STEP2`、`s2_<campaign>_...`；
9. manifest schema 版本升级，防止新监控器误读旧 E1/E2 manifest。

### 7.2 合并监控器和共享逻辑

需要修改：

```text
infos/hpc_step2_common.py
infos/hpc_step2_monitor_common.py
```

需要替换：

```text
infos/hpc_step2_E1/completion_status/monitor_step2_E1_jobs.py
infos/hpc_step2_E2/completion_status/monitor_step2_E2_jobs.py
```

目标监控入口建议为：

```text
infos/hpc_step2/completion_status/monitor_step2_jobs.py
```

具体修改：

1. 删除 E1/E2 stage 分支和 E2 必须绑定补写作业记录的特殊规则；
2. 保留账号活动作业上限、campaign 活动作业上限、提交锁和禁止自动重试等安全约束；
3. 有场站单元以 `COMPLETED + 非空输出` 为成功；
4. 无场站单元以 `COMPLETED + has_stations=false + 无输出` 为
   `SKIPPED_NO_STATIONS`；
5. 已有旧输出不能直接作为新 campaign 成功证据，因为其中的 `signal_low_resource` 可能来自
   CF；应要求新 campaign 调度成功，或校验新输出属性明确为 `regional_bcsd`；
6. 状态文件统一写入 `infos/hpc_step2/completion_status/`。

### 7.3 重写运行目标文档

以下文档不再分别维护：

```text
infos/hpc_step2_E1/goal.md
infos/hpc_step2_E2/goal.md
```

合并为：

```text
infos/hpc_step2/goal.md
```

新文档应写明：

- 唯一科学入口和单一作业阶段；
- 仅依赖 BCSD、场站 CSV 和 Natural Earth 边界；
- 一个作业一次生成全部适用事件；
- 低资源使用 BCSD `wind_ms`/`rsds` 和 `2015-2024` 当前任务基线；
- 不需要 CF 根目录、ERA5Land CF 阈值目录或 step1 完成闸门；
- 完成证据、无场站规则、失败处理、部署和监控命令。

### 7.4 退出运行体系但保留历史证据

以下作业生成/监控代码退出新运行体系；本次保留文件以便查询历史 campaign，后续确认无外部
调用后再单独删除：

```text
infos/hpc_step1/create_jobs_kunshan.py
infos/hpc_step1/completion_status/monitor_step1_jobs.py
```

以下目录中的既有 CSV、JSON、lock 和 `progress.md` 是旧 CF/E1/E2 campaign 的历史运行记录，
不应在本次代码修改中批量删除或改写：

```text
infos/hpc_step1/completion_status/
infos/hpc_step2_E1/completion_status/
infos/hpc_step2_E2/completion_status/
```

它们不再作为新 pipeline 的完成证据。若仓库需要精简历史运行产物，应另开清理任务处理。

### 7.5 `document/` 现有文档迁移

以下现行输入输出说明需要直接更新，不能继续保留 CF 作为当前设计：

| 文件 | 修改内容 |
|---|---|
| `document/修改输入输出说明_多数据源网格版_v2.md` | 把“低资源阈值固定来源为 ERA5Land CF”改为“低资源直接使用当前 Regional BCSD 的 `wind_ms`/`rsds`”；补充统一场站 pipeline、基线期、输出属性和不再需要的 CF 输入。 |
| `document/修改输入输出说明_多数据源网格版.md` | 若仍作为有效设计说明，同步更新低资源数据流；若已由 v2 取代，则在开头明确历史状态并链接 v2，避免维护两份当前口径。 |
| `document/场站级信号_实施计划.md` | 更新场站级事件清单和数据流，明确低资源与普通事件同一次写出。 |
| `document/场站位置与网格数据对应方式代码修改计划.md` | 删除目标 CF 网格的二次匹配描述；低资源复用 BCSD 气象与场站的同一空间匹配。 |
| `document/场站级极端事件信号station_id接入计划.md` | 删除依赖 E2 `--station-id-only` 补写的现行操作说明；统一入口直接写出并校验 `station_id`。 |

`document/原始输入输出说明.md` 描述的是 legacy ERA5-Land 场站流程，不应改写成新的 BCSD
pipeline；只需在文首明确“历史/legacy 范围”，并链接当前 v2 输入输出说明。

以下均为围绕已取消 CF 阈值 pipeline 的历史实施计划：

```text
document/低资源事件接入代码修改计划.md
document/SSP场站稀疏低资源阈值代码修改计划.md
document/加速step1低资源事件阈值计算计划.md
document/超算加速step1低资源事件阈值计算方案.md
document/step1从ssp126增量复用低资源阈值代码修改计划.md
document/加速E3阈值计算代码修改计划.md
```

这些文件不再作为有效实施依据。为保留决策和运行历史，不在本次修正中删除正文；统一在文件顶部
增加“已被 `BCSD低资源事件与其余极端事件合并实施方案.md` 替代，不得用于新生产”的醒目标记。
`document/湿度相关极端事件代码与远端数据核查结论.md` 只需检查其中对 E1 作业名的引用，湿度
数据结论本身不因本次修改而改变。

## 8. 测试修改计划

### 8.1 新增或调整核心测试

在 `tests/test_low_resource_bcsd.py` 中覆盖：

1. 3 小时时间轴自动得到 `window_steps=8`；
2. wind 使用 `wind_ms` 而非 CF；
3. solar 使用 `rsds`，且夜间信号为 0；
4. 气候态和 P5 只使用 `2015-2024` 基线；
5. `t+1` 标记保留；
6. 年界处在完整连续序列上滚动，不逐年截断；
7. 时间轴缺口、重复、非均匀步长和基线不完整时失败；
8. 资源中的 NaN 不产生低资源事件，有效场站基线完全无数据时失败；
9. 投产前和超过空间距离容差的场站信号仍全部置 0；
10. 输出同时包含普通事件和 `signal_low_resource`，并含新的可复现属性，不含 CF 属性。

### 8.2 调整 pipeline/HPC 测试

重写 `tests/test_hpc_step2.py`：

- 只测试一个统一生成器和统一 manifest；
- 生成命令必须调用 `step2_complete_extreme_events.py`；
- 命令和 manifest 不得出现 `cf_root`、`threshold_dir`、ERA5Land 阈值或 E1/E2 patch；
- 检查 `station_count`/`has_stations`、作业名唯一性、`bash -n` 和输出路径；
- 检查新监控器的成功、无场站跳过、失败不重试和活动作业上限。

以下测试只验证历史 CF pipeline；本次不把它们接入生产验证，但暂时保留以保证历史工具仍可读：

```text
tests/test_hpc_step1.py
tests/test_low_resource_thresholds.py
tests/test_sparse_low_resource_thresholds.py
```

其余 adapter、场站匹配、时间对齐和 I/O 测试继续保留。

### 8.3 验证命令

修改完成后使用项目现有 `.venv`：

```bash
.venv/bin/python -m pytest \
  tests/test_low_resource_bcsd.py \
  tests/test_hpc_step2.py \
  tests/test_signal_runner.py \
  tests/test_regional_bcsd_adapter.py \
  tests/test_station_match.py

.venv/bin/python -m pytest
```

此外，对生成的全部 Slurm 脚本运行 `bash -n`，并人工核对至少一个 wind 和一个 solar 命令。

## 9. 实施顺序

1. 为 BCSD 低资源算法补充 3 小时时间步、基线、年界和夜间测试。
2. 修改 `tools/common.py`、两个低资源事件模块及 `registry.py`，使统一事件调用支持时间和场站坐标。
3. 修改 `scripts/station_signals_direct.py`，用已抽取的 BCSD 资源计算低资源并一次写出全部事件。
4. 更新 `./step2_complete_extreme_events.py`、`./extreme_event_definitions.py` 和输出属性。
5. 将 HPC step2 生成器、manifest、监控器和 `goal.md` 合并到 `infos/hpc_step2/`。
6. 删除根目录的 step1、split E1、split E2 入口及根目录外的 CF 专属实现。
7. 删除或重写只覆盖旧 CF pipeline 的测试，运行定向测试和完整测试。
8. 在本地小样本上对同一 `model × region × scenario × tech` 生成完整文件，核对事件比例、
   时间范围、场站顺序、`station_id`、属性及普通事件是否与旧 E1 一致。
9. 在超算生成新的统一 campaign，先 `--dry-run` 和 `bash -n`，再提交少量 wind/solar 样例。
10. 样例通过后运行正式统一 campaign。旧 CF、旧阈值和旧 E2 输出不作为新结果复用。

## 10. 验收标准

代码验收：

- 新生产入口、统一作业生成器和监控器中 `rg -n "cf_low_resource|cf_root|threshold_dir|step2_split_E[12]"` 无命中；
- `./step2_complete_extreme_events.py` 及其调用链不再包含 CF 阈值预计算或低资源补写入口；旧入口仅保留废弃提示。
- 统一作业只读取 BCSD 气象、场站 CSV 和边界文件；
- 全量测试通过。

结果验收：

- 每个有场站的 wind 输出包含 5 类信号，每个有场站的 solar 输出包含 7 类信号；
- `signal_low_resource` 与其他信号的维度、时间轴、场站顺序完全一致；
- 输出属性明确记录 `low_resource_source=regional_bcsd` 和资源变量；
- 输出中不存在 CF 文件或 ERA5Land CF 阈值来源属性；
- 普通事件结果与相同 BCSD 输入下旧 E1 输出逐元素一致；
- 新低资源结果由 BCSD 风速/辐照的可控小样本测试验证，不读取任何 CF 文件；
- 旧 CF-derived `signal_low_resource` 文件不会因“文件已存在”被新监控器误判为完成。

## 11. 数据与历史结果处理

本次只修改代码、测试和运行文档，不自动删除已有数据。以下内容在新流程中不再使用：

```text
data/cfs/
outputs/cache/era5land_union_station_cf/
outputs/low_resource_thresholds/
旧 campaign 生成的 CF-derived signal_low_resource
```

正式重跑前应先把新结果写到独立验证目录，确认通过后再决定是否覆盖既有生产输出。
任何旧 CF、阈值、缓存或历史结果的删除都不属于本次实施范围。
