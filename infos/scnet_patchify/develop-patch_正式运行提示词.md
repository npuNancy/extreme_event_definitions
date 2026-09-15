# CodingAgent 提示词：六账号正式运行 Extreme patchify

立即创建并持续执行一个 goal，不设 token budget。你是本 campaign 的远程超算运行控制 CodingAgent，在 6 个 worker 账号上提交、监控、重分配 Extreme 作业，直到 1,128 个 unit 全部完成。

## 先读

本目录 `goal.md`（完成契约、状态分类）、`README.md`、`six_account_config.csv`、`patch_assignment.csv`、`task_allocation.csv`、**`resubmit_todo.csv`（本轮唯一提交清单）**、`progress.md`（555 个 unit 已有可复用结果）；仓库 `AGENTS.md` 与入口 `--help`。

准备已完成，直接进入运行控制：六账号 checkout 在 `develop-patch` 最新提交，每账号 1,128 个脚本已生成并通过 `bash -n`（16 核/16 进程）。不重复 clone/ACL/脚本生成/BCSD 扫描；只在提交前核对脚本 hash、unit identity 和 `submit_username`。

## 本轮范围：只提交剩余 573 个 unit

2026-09-15 交叉核对（`tmp/codex检查结果/交叉对比报告.md`）：555 个 unit 已有可复用的最终文件 + sidecar，**不重跑**。本轮只提交 `resubmit_todo.csv` 的 573 个：

```text
522 never-submitted（MRI ssp245/585 与 BCC 全量为主）
 32 cancelled（历史取消，已完成 part 可复用）
 19 incomplete_output（CANESM5 旧 writer 截断 station_id，文件存在≠可复用，须重跑）
```

按 `task_allocation.csv` 分布：乌镇1850/118、1555/100、1872/99、1352/91、1731/83、1500/82。残留 `.partial`/损坏文件由重跑自动覆盖或复用，不手工清理；遇非预期文件冲突记 `unknown` 报告，不删除任何已有文件。

**ready 判定**：清单内的 unit 且同 unit 的 BCSD 六变量 final + sidecar 可用（不等待 CF）。不在清单的 555 个即使有空槽也不提交。model/SSP/tech 顺序（CANESM5 → MPI-ESM1-2-HR → MRI-ESM2-0 → BCC-CSM2-MR；ssp126 → ssp245 → ssp585；wind → solar）只是优先级，不是屏障。BCC 未就绪 unit 记 `dependency_blocked`，不阻塞其他 model。

## 固定路径

```text
<AGG_ROOT> = /work/home/acbw9wpn5k/project_climate_patchify/shared_run_20260913
脚本: <AGG_ROOT>/jobs/prepared/<username>/extreme/<ssp>/extp_<model>_<ssp>_<patch>_<tech>.sh
日志: <AGG_ROOT>/logs/prepared/<username>/extreme/<ssp>/..._%j.out
输出: <AGG_ROOT>/outputs/extreme/<model>/<ssp>/<patch>/<tech>.nc(+.json/+.timing.json)
代码: /work/home/<username>/project_climate_patchify/repos/extreme_event_definitions
BCSD: /work/home/acbw9wpn5k/bcsd/global_bcsd/production_v1/outputs/<model>/<ssp>/<var>/
锁/runtime: <AGG_ROOT>/runtime/patchify_generation_loss_202609/（.submit.lock、task_state.jsonl、assignment/reassignment/scheduler 日志、completion_status/）
```

## 代码与资源

远程 checkout 只能经 Git 更新（本地 `develop-patch` 测试、commit、push；远程 `pull --ff-only`），禁止编辑 checkout 内文件；作业脚本、日志、runtime 状态在 checkout 外。无法本地更新时停止相关 unit 报告。

脚本固定 `--cpus-per-task=16 --processes 16`（≈58 GB 内存；pilot 实测 MaxRSS 31.4 GB），线程 pinning 和 `--timing-report` 已写入。OOM 标 `resource_failure`，按 `MaxRSS` 加核（至少 +2 或 ×1.5 取大）生成新 profile 脚本后重试；已提交 unit 不改资源。资源画像是运行期重生成脚本的唯一例外。

## 15 分钟控制循环（hh:00/15/30/45）

依次"检查—思考—汇总—执行"，无状态变化也不跳过：

1. **检查**：六账号 `squeue/sacct`（PENDING 等在途状态均占槽）、终态退出码、日志尾部、输出 sidecar；
2. **思考**：按依赖、优先级、槽位决定提交/补槽/重分配/等待；
3. **汇总**：更新 runtime 下 `task_state.jsonl`、assignment/reassignment/scheduler 日志；**必须更新 `infos/scnet_patchify/progress.md`**（`Last checked` + model × SSP 完成数，无变化也更新）；回复写明"本轮预计耗时/预计完成时间"；
4. **执行**：锁内提交 ready unit 或记录阻塞原因。

## 提交与重分配

提交须在共享锁内：统计 account-wide active jobs → 从 `resubmit_todo.csv` 选 ready 且 not_submitted 的 unit → 重估目标账号槽位 → `sbatch --parsable -A <SUBMIT_USERNAME> <SCRIPT>` → 记录 Job ID、unit_id、assignment_version、脚本 SHA-256。`billing_account` 仅是历史元数据，不传给 `sbatch`；不得因作业从 `squeue` 消失判成功；一个 unit 同时最多一个 active Job。

账号有空槽而其他账号仍有 ready unit 时重分配：排除 active/已成功/依赖未满足的 unit；保留 `logical_owner`，只改 `submit_account`、递增 `assignment_version`；直接选目标账号既有 `.sh`（禁止重跑 generator）；核对 hash、identity、`--processes 16`、`--submit-username`、输入/输出根；旧/新账号、原因写 `reassignment_log.jsonl`。不得迁移/取消 active Job 或重复提交。

## 状态与终止

`succeeded` 须同时满足：Slurm `COMPLETED/0:0`；`<tech>.nc` + `.nc.json` 完整且无 `.partial` 为最终文件；sidecar identity 与 unit 一致；含场站信号、低资源 provenance、station identity、timing sidecar。无场站 unit 有 `SKIPPED_NO_STATIONS` marker 记 `succeeded_skip`（几分钟即完，属正常）。

状态集：`not_submitted/active/succeeded/succeeded_skip/retryable/resource_failure/deterministic_failure/incomplete_output/dependency_blocked/unknown`。TIMEOUT、节点故障、短暂 I/O 可重试（每 unit 最多 2 次）；**重试时已完成的 part 自动复用**；参数/权限/identity 错误停止报告。merge 阶段（约 10 分钟单进程）是设计内行为，不是卡死。

终止：1,128 个 unit 全部 `succeeded`/`succeeded_skip`（555 个已有结果启动时继承、不重验；本轮 573 个全部终态），无 active/retryable/incomplete/blocked/unknown，输出可审计，日志完整。BCC 的 BCSD 若长期未就绪，其剩余 unit 以 `dependency_blocked` 呈报等用户决定。
