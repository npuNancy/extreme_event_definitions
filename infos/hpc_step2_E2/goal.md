# SCNet step2 E2 并行运行目标

## 1. 目标与完成条件

E2 读取未来容量因子和 ERA5Land `2015-2024` 固定阈值，生成或补写 `signal_low_resource`。

- 科学入口：`step2_split_E2_low_resource.py`
- 作业单位：一个 `model × region × scenario × tech × years`
- 作业数：`models × 各模型 regions × scenarios × techs`
- 有场站单元的完成证据：对应 E2 作业在 Slurm 中成功结束，且目标 NetCDF 存在并且大小大于零
- 无场站单元的完成证据：对应 E2 作业在 Slurm 中成功结束，日志明确记录“国家内无场站，跳过”；不要求输出文件
- 不做：普通极端事件重算、登录节点科学计算、自动重试失败作业、由 E1 监控器自动启动 E2

E1 文件存在时，E2 原位补写同一个 NetCDF；有场站但 E1 文件缺失时，E2 新建一个只含 `signal_low_resource` 的兼容 NetCDF。文件非空不能单独证明 E2 完成，因此 E2 还必须有本 campaign 的成功调度记录或本地持久化成功记录。

## 2. 依赖、输入与输出

E2 是独立 campaign，只有在 E1 campaign 全部进入终态并由用户明确启动 E2 后才能运行。启动 E2 监控器本身代表人工阶段授权；不能再以“全部 E1 文件非空”作为全局闸门，因为无场站单元不会产生 E1 文件，而有场站但缺少 E1 文件的单元需要由 E2 兜底建档。

默认输入：

```text
~/data/cfs
~/data/stations
~/data/maps/natural_earth/ne_110m_admin_0_countries.shp
~/data/extreme_event_outputs/low_resource_thresholds/
  sparse_station_ERA5Land_2015-2024/
```

阈值基准期固定为 `2015-2024`。代码、文件名、目录、属性和文档不得改用其他基准期。

E1/E2 共用文件：

```text
~/extreme_event_definitions/outputs/station_signals/
  regional_bcsd/<MODEL>/<REGION>/<SCENARIO>/
    station_signals_<TECH>_<MODEL>_<REGION>_<SCENARIO>_<START>-<END>.nc
```

wind 与 solar 文件独立，可以并发处理。每个 E2 作业先用当前 SSP 场站表和 Natural Earth 国家边界筛选对应技术场站：

1. 有场站且 E1 输出存在：原位补写。
2. 有场站但 E1 输出不存在：新建只含 `signal_low_resource` 的兼容 NC。
3. 无场站：成功跳过，日志写明“国家内无场站，跳过”。

有场站时，目标 CF、阈值文件缺失或处理异常必须以非零状态退出。

`data/grid_of_regions` 不参与 E2。

## 3. 作业生成器

生成器：

```text
infos/hpc_step2_E2/create_step2_E2_jobs.py
```

示例：

```bash
python3 infos/hpc_step2_E2/create_step2_E2_jobs.py \
  --models NESM3,MIROC-ES2H,MPI-ESM1-2-HR,CANESM5 \
  --regions Germany \
  --scenarios ssp126,ssp245,ssp585 \
  --techs wind,solar \
  --years 2015-2060
```

`--regions all` 只负责展开区域，仍生成逐区域作业。`ssp560` 会报错并提示使用 `ssp585`。

稳定安装默认值：

```text
--project-dir ~/extreme_event_definitions
--cf-root ~/data/cfs
--stations-dir ~/data/stations
--shp ~/data/maps/natural_earth/ne_110m_admin_0_countries.shp
--threshold-dir ~/data/extreme_event_outputs/low_resource_thresholds/sparse_station_ERA5Land_2015-2024
--baseline-years 2015-2024
--job-root ~/extreme_event_jobs/step2_E2
--log-root ~/extreme_event_logs/step2_E2
--partition wzhctest
--e2-cpus 6
--activate-path /work/home/acbpgywfpz/miniconda3/bin/activate
--environment-name climate
```

生成脚本使用：

```bash
#SBATCH -N 1
#SBATCH -n 6
```

不设置 `#SBATCH --time`。每核约 3.5 GB，增加核数主要用于申请内存；常见数值库线程数固定为 1。

生成器会在 manifest 中记录每个单元的 `station_count` 和 `has_stations`，但作业运行时仍重新读取场站表和边界作最终判断。生成器只生成脚本和 manifest，不提交作业。默认不覆盖 `signal_low_resource`；Agent 确认重试目标后，可对单个单元使用 `--overwrite` 和 `--force` 重新生成。

生成器不绑定服务器。若 E2 分配到多台服务器，应在每台服务器生成其负责的 campaign，并分别启动对应的本地监控实例。

## 4. 仓库与远端边界

- 本地：代码修改、测试、Git、监控和状态记录
- 远端登录节点：fast-forward pull、生成脚本、`bash -n`、Slurm 查询和 `stat`
- 远端计算节点：E2 低资源事件计算与补写
- 仓库外作业：`~/extreme_event_jobs/step2_E2/`
- 仓库外日志：`~/extreme_event_logs/step2_E2/`
- 仓库内科学输出：`~/extreme_event_definitions/outputs/station_signals/`

不得删除或清空 CF、阈值、E1 输出、其他项目作业或共享锁。

## 5. 本地测试与远端准备

```bash
.venv/bin/python -m pytest tests/test_hpc_step2.py
```

部署顺序：

1. 确认全部 E1 作业已进入终态，并核验无输出单元的日志原因。
2. 本地修改、测试、commit、push。
3. 远端干净工作区执行 fast-forward-only pull。
4. 使用 `--dry-run` 检查 E2 数量和路径。
5. 正式生成 E2 脚本和 manifest。
6. 对全部脚本执行 `bash -n`，人工检查 wind/solar 样例。
7. 用户明确要求 Agent 开始 E2 后，才运行 E2 监控器。

## 6. 独立监控和自动提交

监控器：

```text
infos/hpc_step2_E2/completion_status/monitor_step2_E2_jobs.py
```

Agent `/loop` 单轮示例：

```bash
.venv/bin/python infos/hpc_step2_E2/completion_status/monitor_step2_E2_jobs.py \
  --server <SSH_HOST> \
  --remote-manifest '~/extreme_event_jobs/step2_E2/manifest_step2_E2_<ID>.json' \
  --history-start 2026-07-22 \
  --max-active-jobs 20 \
  --once
```

默认行为：

1. 通过 SSH 加载 E2 manifest。
2. 获取 `$HOME/.bcsd_submit.lock`。
3. 统计账号所有项目活动作业，硬上限为 20。
4. 按 `--max-active-jobs`（默认 20）限制当前 E2 campaign 活动作业。
5. 补充提交从未提交过的 E2 单元。

`--no-submit` 为只读模式。E2 监控器不会提交 E1，也不会修复 E1。

## 7. 完成判断与失败处理

监控器只使用 `squeue`、`sacct` 和 `stat`，不读取 NetCDF 内容。

- `NOT_SUBMITTED`：有空槽时可自动提交
- `ACTIVE`：继续监控
- `SUCCEEDED`：E2 调度成功且目标文件非空
- `SKIPPED_NO_STATIONS`：E2 调度成功、manifest 标记无场站且没有输出文件
- `FAILED`：失败、OOM、超时、取消等，禁止自动重试
- `INCOMPLETE_OUTPUT`：E2 调度成功但文件缺失或为空，禁止自动重试
- `UNKNOWN_HISTORY`：已有提交记录但调度历史不可见，禁止重复提交

E2 的 `NOT_SUBMITTED` 不能因文件存在而变成成功。成功或无场站跳过状态必须绑定 E2 作业记录。

失败时由 Agent 检查 `ExitCode`、`Elapsed`、`MaxRSS` 和日志。OOM 后按证据增加 `--e2-cpus`；确定输出需要重建时才使用 `--overwrite`。监控器不自动重试。

本地状态目录：

```text
infos/hpc_step2_E2/completion_status/
```

运行时 JSON/CSV/lock 不提交 Git，监控器源码需要跟踪。

## 8. 进度记录

每次检查（监控器 `--once`、`sacct`/日志排查或手动核验）完成后，必须更新 `completion_status/progress.md` 的进度表格：

- 更新顶部“最后更新”时间与 campaign 信息。
- 更新“汇总”表（已完成 / 运行或排队 / 失败 / 总计）。
- 更新“区域进度”网格，按 `region × (scenario × tech)` 用图例标记：`✅ 已完成（E2 调度成功且目标文件非空）`、`↷ 无场站跳过`、`⏳ 运行/排队`、`❌ 失败`、`— 未提交/未生成`。
- 在“异常明细”记录 FAILED / INCOMPLETE_OUTPUT / OOM 的单元及原因。

`progress.md` 是 Agent 手动维护的人类可读进度概览，仅保留在本地工作区；自动明细仍以 `completion_<server>_<campaign>.csv`、`latest_snapshot_<server>_<campaign>.json` 和 `usage_<server>_<campaign>.csv` 为准。
