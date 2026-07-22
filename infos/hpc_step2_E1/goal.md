# SCNet step2 E1 并行运行目标

## 1. 目标与完成条件

E1 直接读取 Regional BCSD 气象数据，为场站生成不含低资源事件的普通极端事件信号。

- 科学入口：`step2_split_E1_weather_extremes.py`
- 作业单位：一个 `model × region × scenario × tech × years`
- 作业数：`models × 各模型 regions × scenarios × techs`
- 完成证据：作业未失败，且预期 NetCDF 文件存在并且大小大于零
- 不做：低资源事件、网格级输出、登录节点科学计算、自动重试失败作业、自动启动 E2

`data/grid_of_regions` 不在 E1 调用链中，不需要上传。

## 2. 输入、输出和依赖

远端默认输入：

```text
~/data/bcsd_outputs
~/data/stations/stations_SSP1-2.6.csv
~/data/stations/stations_SSP2-4.5.csv
~/data/stations/stations_SSP5-6.0.csv
~/data/maps/natural_earth/ne_110m_admin_0_countries.shp
```

SSP 映射：

```text
ssp126 -> stations_SSP1-2.6.csv
ssp245 -> stations_SSP2-4.5.csv
ssp585 -> stations_SSP5-6.0.csv
```

`ssp560` 非法，生成器会提示使用 `ssp585`。

默认输出：

```text
~/extreme_event_definitions/outputs/station_signals/
  regional_bcsd/<MODEL>/<REGION>/<SCENARIO>/
    station_signals_<TECH>_<MODEL>_<REGION>_<SCENARIO>_<START>-<END>.nc
```

wind 与 solar 写不同文件，可以并发。缺少请求范围内任一年必要输入时，E1 必须失败，不能把部分年份文件视为完整结果。

## 3. 作业生成器

生成器：

```text
infos/hpc_step2_E1/create_step2_E1_jobs.py
```

示例：

```bash
python3 infos/hpc_step2_E1/create_step2_E1_jobs.py \
  --models NESM3,MIROC-ES2H,MPI-ESM1-2-HR,CANESM5 \
  --regions Germany \
  --scenarios ssp126,ssp245,ssp585 \
  --techs wind,solar \
  --years 2015-2060
```

`--regions all` 会扫描每个模型的实际输入目录，但仍为每个国家生成独立作业。显式区域在任一模型中不存在时直接报错。

稳定安装默认值：

```text
--project-dir ~/extreme_event_definitions
--job-root ~/extreme_event_jobs/step2_E1
--log-root ~/extreme_event_logs/step2_E1
--partition wzhctest
--e1-cpus 6
--activate-path /work/home/acbpgywfpz/miniconda3/bin/activate
--environment-name climate
```

生成脚本不设置 `#SBATCH --time`，使用分区默认时限。资源行固定采用项目既有形式：

```bash
#SBATCH -N 1
#SBATCH -n 6
```

每核约对应 3.5 GB 内存；6 核约申请 21 GB。程序本身为单 Python 进程，多核主要用于获得内存。脚本把常见数值库线程数限制为 1。

生成器只生成脚本和 manifest，不调用 `sbatch`、SSH、Git 或科学程序。默认拒绝覆盖同名脚本；`--force` 只覆盖生成物。科学输出默认不覆盖，只有显式 `--overwrite` 才向 E1 入口传递覆盖选项。

生成器不绑定服务器或技术类型。需要一台或多台服务器时，由操作者在各目标服务器上用对应的模型/区域/SSP/技术子集生成 campaign；本地可为不同 `server + campaign` 同时运行独立监控实例。

## 4. 仓库与远端边界

- 本地：修改、测试、提交、推送、运行监控器、保存状态记录
- 远端登录节点：干净仓库 fast-forward pull、运行生成器、`bash -n`、查询 Slurm
- 远端计算节点：E1 科学计算
- 仓库外生成物：`~/extreme_event_jobs/step2_E1/`
- 仓库外日志：`~/extreme_event_logs/step2_E1/`
- 仓库内科学输出：`~/extreme_event_definitions/outputs/station_signals/`

不得删除或清空 `~/data/`、项目输出、其他项目的作业或共享提交锁。

## 5. 本地验证与部署

本地修改后使用项目 `.venv`：

```bash
.venv/bin/python -m pytest tests/test_hpc_step2.py
```

远端部署：

1. 本地 commit/push。
2. 远端确认工作区干净，执行 fast-forward-only pull。
3. 先用 `--dry-run` 核对维度和数量。
4. 正式生成作业和 manifest。
5. 对生成脚本执行 `bash -n`。
6. 检查至少一个 wind 和一个 solar 脚本。

生成器输出的 manifest 是本次 campaign 的唯一作业清单。不要手改生成后的单个脚本来改变模型、区域、情景、技术或年份；应重新运行生成器。

## 6. 监控、提交与限额

本地监控器：

```text
infos/hpc_step2_E1/completion_status/monitor_step2_E1_jobs.py
```

供 Agent、Codex CLI 或 Claude Code `/loop` 单次调用：

```bash
.venv/bin/python infos/hpc_step2_E1/completion_status/monitor_step2_E1_jobs.py \
  --server <SSH_HOST> \
  --remote-manifest '~/extreme_event_jobs/step2_E1/manifest_step2_E1_<ID>.json' \
  --history-start 2026-07-22 \
  --max-active-jobs 20 \
  --once
```

监控器默认自动提交本阶段从未提交过的单元。`--no-submit` 只检查不提交。不传 `--once` 时，按 `--interval` 周期持续运行。

每次提交必须：

1. 获取远端 `$HOME/.bcsd_submit.lock`。
2. 重新统计账号所有项目的活动作业。
3. 保证账号活动作业总数小于 20。
4. 保证当前 campaign 活动作业数小于 `--max-active-jobs`。
5. 只提交没有输出、没有 squeue/sacct 记录、也没有本地提交记录的 E1 单元。

`--max-active-jobs` 默认为 20，可在其他项目占用资源时手动调低，但不能高于账号硬上限 20。

## 7. 状态分类和恢复

监控器查询 `squeue`、`sacct` 和远端 `stat`。输出检查仅判断文件存在且大小大于零，不读取 NetCDF 内容。

分类：

- `NOT_SUBMITTED`：可在有空槽时自动提交
- `ACTIVE`：继续监控
- `SUCCEEDED` / `SUCCEEDED_PREEXISTING`：非空输出证据成立
- `FAILED`：调度失败，禁止自动重试
- `INCOMPLETE_OUTPUT`：作业完成但输出缺失或为空，禁止自动重试
- `UNKNOWN_HISTORY`：本地有提交记录但调度历史不可见，禁止重复提交

失败、OOM、超时、取消和不完整输出均由 Agent 查看 accounting 与日志后决定。调整核数或覆盖输出时，只为目标单元重新生成脚本；监控器不自动重试。

状态文件位于：

```text
infos/hpc_step2_E1/completion_status/
```

JSON 快照与 CSV 状态是运行时文件，不提交 Git；监控程序本身需要跟踪。

## 8. 阶段边界

E1 监控器永远不会生成或提交 E2。只有全部 E1 campaign 单元完成后，由用户明确要求 Agent 开始独立的 E2 流程。
