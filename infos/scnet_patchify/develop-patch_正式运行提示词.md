# Extreme 六账号正式运行提示词（提交前版本）

你是 Extreme patchify 阶段的运行控制 CodingAgent。本提示词用于准备完成后的正式运行；当前用户要求只准备，不要提交，看到本文后必须停在生成、审查和报告阶段。

先读：本目录 `goal.md`、`README.md`、`six_account_config.csv`、`patch_assignment.csv`、`task_allocation.csv`；再读仓库 `AGENTS.md` 和 `scripts/station_signals_patchify.py`。代码修改只经本地 Git 分支、测试、commit、push；远程 checkout 不得直接编辑。作业脚本、日志、part、manifest 和 runtime 状态放在 checkout 外。

## Campaign

固定 unit 为 `extreme/<model>/<scenario>/<technology>/<patch_id>`，共 1,128 个。使用四个 model、三个 SSP、wind/solar 和 `patch_assignment.csv` 中的 47 个 patch。逻辑 owner 不变，实际提交账号只使用六个当前可用 worker 的 `submit_username`。

六个账号及 unit 数：乌镇1850/216、乌镇1872/192、乌镇1555/192、乌镇1352/192、乌镇1731/168、乌镇1500/168。每账号最多 20 个 account-wide active jobs；全局上限 120。

## 生成和提交边界

生成器为 `extreme_event_definitions/infos/scnet_patchify/create_extreme_patch_jobs.py`，只生成脚本和 manifest，不调用 `sbatch`。先按 SSP 分别使用 stations CSV，执行 `--dry-run`，确认 1,128 个 unit、唯一脚本名和六账号分配，再生成脚本并 `bash -n`。脚本必须包含：

```bash
source /work/home/acbpgywfpz/miniconda3/bin/activate climate
set -euo pipefail
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
```

每个 job 必须是 `--cpus-per-task 16 --processes 16`（16 核 ≈ 58 GB 内存配额），并调用 `scripts/station_signals_patchify.py --timing-report`。每个 unit 只能有一个 active Job。`billing_account` 只作历史元数据，不能传给 generator 或 `sbatch`；正式提交使用：

```bash
sbatch --parsable -A <submit_username> <prepared_script>
```

所有提交在共享锁内完成：统计 account-wide active jobs → 选择 ready unit → 重新统计目标账号 → 提交 → 记录 Job ID、unit、submit username、assignment version、script SHA-256。当前用户未授权时不得执行这一步。

## 依赖、监控和重试

Extreme 只依赖同一 unit 的 BCSD final/sidecar；不等待 CF。每 15 分钟执行检查—判断—记录—动作：读取六账号 `squeue/sacct`、终态、退出码、日志尾部、最终文件、sidecar、part 和 timing sidecar。不得把任务从 `squeue` 消失当作成功。

只有 Slurm `COMPLETED/0:0`、最终文件完整、sidecar identity 一致、station signal/provenance 完整、汇总校验通过才记 `succeeded`。合法 skip marker 记 `succeeded_skip`。TIMEOUT、节点故障和短暂 I/O 可有限重试；OOM 必须先增加核数；参数/权限/identity 错误停止并报告。

## 停止条件

本轮只交付：六账号配置、patch/unit 分配、生成器、dry-run/bash syntax 检查记录、goal 和提示词。不要执行 `sbatch`、不要 scancel、不要创建或覆盖生产输出、不要把生成脚本当成已提交任务。正式运行须在用户另行授权后开始。
