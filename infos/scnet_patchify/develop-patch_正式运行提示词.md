# CodingAgent 提示词：六账号正式运行 Extreme patchify

立即创建并持续执行一个 goal，不设 token budget。你是本 campaign 的远程超算运行控制 CodingAgent，负责在 6 个 worker 账号上提交、监控、重分配 Extreme（极端天气识别）作业，直到 1,128 个 unit 全部完成。

## 先读取运行契约

读取并遵守：

1. 本目录 `goal.md`（完成契约、状态分类）、`README.md`
2. `six_account_config.csv`（六个 worker 账号）、`patch_assignment.csv`（47 个 patch 的归属）、`task_allocation.csv`（1,128 个 unit 的初始分配）、`allocation_summary.csv`
3. **`resubmit_todo.csv`（本轮唯一提交清单，573 个 unit，见下节）**
4. 仓库 `AGENTS.md` 和 `scripts/station_signals_patchify.py --help`
5. 本目录 `progress.md`：555 个 unit 已有可复用结果，每轮必须更新

准备阶段已完成，直接进入运行控制：六账号 checkout 已在 `develop-patch@51399a8`，每账号的 1,128 个作业脚本已生成并通过 `bash -n`（`resource_v3_extreme_multiprocess`，16 核/16 进程）。不重复 clone/pull、ACL、脚本生成、BCSD 全量扫描；只在提交前核对目标脚本 hash、unit identity 和 `submit_username`。

## 本轮范围：只提交剩余 573 个 unit

交叉核对结论（2026-09-15，详见 `tmp/codex检查结果/交叉对比报告.md`）：555 个 unit 已有可复用的最终文件 + sidecar（元数据校验通过），**不重跑**；本轮只提交 `resubmit_todo.csv` 中的 573 个 unit：

```text
522 个 never-submitted（MRI-ESM2-0 ssp245/585 全量、BCC-CSM2-MR 全量为主）
 32 个 cancelled（历史取消，含可复用的已完成 part）
 19 个 incomplete_output（CANESM5 旧版 writer 的 station_id 截断损坏文件，必须重跑）
```

19 个损坏 unit 的判定依据：`.nc` 和 sidecar 存在但 `station_id` dtype 为 `S1`（每 ID 1 字符、不唯一），文件存在 ≠ 可复用；重跑脚本带 `--overwrite` 会原子替换损坏文件。这 573 个 unit 按 `task_allocation.csv` 的 `submit_account` 分布：乌镇1850/118、乌镇1555/100、乌镇1872/99、乌镇1352/91、乌镇1731/83、乌镇1500/82。19 + 32 个 unit 的历史残留 `.partial`/损坏文件由重跑自动覆盖或复用，提交前不必手工清理（作业的 `--overwrite` + part-reuse 语义已覆盖）；若某 unit 的输出路径存在非预期文件冲突，记 `unknown` 并报告，不删除任何已有文件。

**ready 判定**：`resubmit_todo.csv` 中的 unit 且同 unit 的 BCSD 六变量 final + sidecar 可用。不在清单中的 555 个 unit 即使槽位空闲也不提交。model/SSP/tech 顺序（CANESM5 → MPI-ESM1-2-HR → MRI-ESM2-0 → BCC-CSM2-MR；ssp126 → ssp245 → ssp585；wind → solar）只是清单内提交优先级，不是屏障。

## 固定路径

```text
<AGG_ROOT> = /work/home/acbw9wpn5k/project_climate_patchify/shared_run_20260913
脚本:   <AGG_ROOT>/jobs/prepared/<username>/extreme/<ssp>/extp_<model>_<ssp>_<patch>_<tech>.sh
日志:   <AGG_ROOT>/logs/prepared/<username>/extreme/<ssp>/..._%j.out
输出:   <AGG_ROOT>/outputs/extreme/<model>/<ssp>/<patch>/<tech>.nc(+.json/+.timing.json)
part:   <AGG_ROOT>/outputs/extreme/.extreme_parts/...（作业自动管理）
代码:   /work/home/<username>/project_climate_patchify/repos/extreme_event_definitions
BCSD:   /work/home/acbw9wpn5k/bcsd/global_bcsd/production_v1/outputs/<model>/<ssp>/<var>/
锁:     <AGG_ROOT>/runtime/patchify_generation_loss_202609/.submit.lock
runtime:<AGG_ROOT>/runtime/patchify_generation_loss_202609/（task_state.jsonl、assignment/reassignment/scheduler 日志、completion_status/）
```

## 代码修改边界

远程 checkout 只能经 Git 更新，禁止编辑、`sed -i`、覆盖或删除 checkout 内文件。需要改代码/参数时：本地 `develop-patch` 改好、测试、commit、push；远程只 `git status`/`fetch`/`pull --ff-only`，确认 SHA。作业脚本、日志、manifest、runtime 状态在 checkout 外的版本化目录。不得为修运行错误改远程文件；无法本地更新时停止相关 unit 并报告。

## 资源策略

所有脚本固定 `#SBATCH --cpus-per-task=16` + `--processes 16`（16 核 ≈ 58 GB 内存配额；乌镇-199 pilot 实测 MaxRSS 31.4 GB，余量 ~45%），线程 pinning（OMP/MKL/OPENBLAS/NUMEXPR=1）和 `--timing-report` 已写入脚本。首个作业完成后按 `sacct MaxRSS` 建资源画像：仅用 `COMPLETED` 且输出完整的样本；OOM 标 `resource_failure`，至少 `current_cpus+2` 或 `×1.5`（取大）生成新 resource-profile 脚本后重试；已提交/运行中的 unit 不改资源。资源画像是运行期重新生成脚本的唯一例外；普通重分配直接用目标账号已有脚本。

## Campaign 与可提交条件

```text
unit_id = extreme/<model>/<ssp>/<tech>/<patch>，共 1,128 个；本轮提交 573 个（resubmit_todo.csv）
model 顺序: CANESM5 → MPI-ESM1-2-HR → MRI-ESM2-0 → BCC-CSM2-MR
SSP 顺序: ssp126 → ssp245 → ssp585；tech: wind → solar
六账号初始分配 573 个: 乌镇1850/118、乌镇1555/100、乌镇1872/99、乌镇1352/91、乌镇1731/83、乌镇1500/82
每账号 20 个 account-wide active jobs；全局目标 6 × 20 = 120
```

依赖仅一条：同 unit 的 BCSD 六变量 final + sidecar 可用即 ready（不等待 CF）。BCC-CSM2-MR 的 BCSD 仍在补齐，其未就绪 unit 记 `dependency_blocked`，不阻塞其他 model。

## 15 分钟控制循环（Asia/Shanghai，hh:00/15/30/45）

依次执行"检查—思考—汇总—执行"，无状态变化也不跳过：

1. **检查**：六账号 `squeue/sacct`（含 `PENDING/RUNNING/CONFIGURING/COMPLETING` 均占槽）、终态退出码、日志尾部、输出根 sidecar、timing sidecar；
2. **思考**：按依赖、优先级、槽位决定提交/补槽/重分配/等待；
3. **汇总**：更新 `task_state.jsonl`、`assignment_manifest.jsonl`、`scheduler_observations.jsonl`、`reassignment_log.jsonl`（runtime 目录）；**必须更新仓库 `infos/scnet_patchify/progress.md`**（`Last checked` + model × SSP 完成数，无变化也更新）并在运行时 progress.md 保存同轮详细记录；回复中写明"本轮预计耗时：约 X 分钟；预计完成时间：……"；
4. **执行**：共享锁内从目标账号脚本集选脚本提交 ready unit，或记录阻塞原因。

## 提交和槽位控制

```text
acquire <AGG_ROOT>/runtime/patchify_generation_loss_202609/.submit.lock
  统计 account-wide active jobs（不能只看本项目）
  选择 resubmit_todo.csv 中 ready 且 not_submitted 的 unit
  提交前重新统计目标账号
  sbatch --parsable -A <SUBMIT_USERNAME> <SCRIPT>
  记录 Job ID、unit_id、submit_username、assignment_version、脚本 SHA-256
release lock
```

`billing_account`（数字标识）仅是历史元数据，不传给 generator 或 `sbatch`；`-A` 只用 worker 的 `username`。不得因作业从 `squeue` 消失判成功。一个 unit 同一时刻最多一个 active Job。

## 动态重分配

账号提前跑完、出现空槽而其他账号仍有 ready unit 时：从 `resubmit_todo.csv` 排除 active/已成功/依赖未满足/已有 active Job 的 unit；保留 `logical_owner`，只改 `submit_account`、`submit_username`，递增 `assignment_version`；直接选目标账号既有 `.sh`（普通重分配禁止重跑 generator）；核对脚本 hash、unit identity、`--processes 16`、`--submit-username`、输入/输出根；锁内重估槽位后提交；旧/新账号、脚本、原因、时间、版本写 `reassignment_log.jsonl`。每账号备有全部 1,128 个脚本正是为了这一步。不得迁移/取消 active Job 或重复提交。

## 状态、完成与失败处理

unit 记 `succeeded` 须同时满足：Slurm `COMPLETED/0:0`；`<tech>.nc` + `.nc.json` 存在且无 `.partial` 残留为最终文件；sidecar 的 model/scenario/patch_id/tech/output 与 unit 一致；含场站信号、元数据、低资源 provenance、station identity；timing sidecar 存在。无场站 unit 有合法 `SKIPPED_NO_STATIONS` marker 记 `succeeded_skip`（此类作业几分钟即完，属正常）。

允许状态：`not_submitted/active/succeeded/succeeded_skip/retryable/resource_failure/deterministic_failure/incomplete_output/dependency_blocked/unknown`。TIMEOUT、节点故障、短暂 I/O 可重试（默认每 unit 最多 2 次）；**作业中途失败重试时，已完成的 part 会被自动复用，只重算缺失 part**；OOM 先加核；参数/权限/identity 错误停止并报告。merge 阶段（约 10 分钟，单进程）是设计内行为，不是卡死。

## 终止条件

1,128 个 unit 全部 `succeeded` 或 `succeeded_skip`（含已有 555 个——其完成状态在启动时从输出根 + `progress.md` 继承，不需重验），本轮 573 个全部终态，无 active/retryable/incomplete/dependency-blocked/unknown，所有输出在共享输出根可审计，`progress.md`、assignment/reassignment 日志完整——goal 才能结束。BCC-CSM2-MR 若 BCSD 长期未就绪，其剩余 unit 以 `dependency_blocked` 呈报并等用户决定，不得伪造完成。
