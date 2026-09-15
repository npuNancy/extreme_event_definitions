# Extreme patchify：六账号准备说明

本目录只定义和生成 **Extreme** 阶段的作业，不提交作业。入口是仓库内的 [create_extreme_patch_jobs.py](create_extreme_patch_jobs.py)，实际计算入口是 `scripts/station_signals_patchify.py`。

本轮固定矩阵为 `4 model × 3 SSP × 2 technology × 47 patch = 1,128` 个 logical units。一个 unit 的稳定身份是：

```text
extreme/<model>/<scenario>/<technology>/<patch_id>
```

当前计算入口支持场站块多进程：每个作业申请 16 个 CPU（16×3.63 GB ≈ 58 GB 内存配额），启动 16 个场站块 worker（乌镇-199 pilot 实测：R03C09/solar 27.5 分钟，MaxRSS 31.4 GB）；worker 逐块写 part 文件，父进程再流式合并最终 NetCDF。作业内设置 `OMP_NUM_THREADS=1`、`MKL_NUM_THREADS=1`、`OPENBLAS_NUM_THREADS=1`、`NUMEXPR_NUM_THREADS=1`，避免每个 worker 再创建线程池。

## 文件

- `six_account_config.csv`：本轮可用的 6 个 worker 账号。乌镇151、173、9259、9359 因欠费排除。
- `patch_assignment.csv`：47 个 patch 的逻辑 owner 与实际 submit account。逻辑 owner 永不改变；不可用 owner 的 patch 已按当前累计 patch 数转移给六个可用账号。
- `task_allocation.csv`：完整 1,128 行 unit 分配表。
- `resubmit_todo.csv`：本轮正式运行的唯一提交清单（573 个 unit = 1,128 − 555 个已有可复用结果；含 19 个旧版 station_id 损坏需重跑的 unit）。
- `allocation_summary.csv`：账号、patch 和 unit 数量。
- `develop-patch_正式运行提示词.md`：正式运行控制提示词。
- `goal.md`：本轮 Extreme-only 目标、依赖、资源和完成契约。
- `progress.md`：完成进度表，每 15 分钟控制循环必须更新。

分配结果为：乌镇1850 216 个 unit，乌镇1872/1555/1352 各 192 个，乌镇1731/1500 各 168 个。所有账号上限仍为 20 个 active jobs，全局最多 120 个 active jobs。

## 生成脚本

生成器只写 `.sh` 和 `manifest.json`，不会调用 `sbatch`。每个账号应使用自己的外部 jobs/logs 目录和 `--submit-username`：

```bash
python extreme_event_definitions/infos/scnet_patchify/create_extreme_patch_jobs.py \
  --models CANESM5 MPI-ESM1-2-HR MRI-ESM2-0 BCC-CSM2-MR \
  --scenarios ssp126 ssp245 ssp585 \
  --patches R01C01 R01C02 ... R05C12 \
  --techs wind solar --years 2015-2060 \
  --bcsd-root /work/home/acbw9wpn5k/bcsd/global_bcsd/production_v1 \
  --patch-manifest /work/home/acbw9wpn5k/project_climate_patchify/shared_run_20260913/inputs/patch_manifest.json \
  --stations-csv /work/home/acbw9wpn5k/project_climate_patchify/shared_run_20260913/inputs/stations/stations_SSP1-2.6.csv \
  --output-root /work/home/acbw9wpn5k/project_climate_patchify/shared_run_20260913/outputs/extreme \
  --project-dir /work/home/<username>/project_climate_patchify/repos/extreme_event_definitions \
  --jobs-dir /work/home/<username>/project_climate_patchify/shared_run_20260913/jobs/prepared/<username>/extreme \
  --logs-dir /work/home/<username>/project_climate_patchify/shared_run_20260913/logs/prepared/<username>/extreme \
  --submit-username <username> --partition wzhctest \
  --cpus-per-task 16 --processes 16 \
  --resource-profile resource_v3_extreme_multiprocess \
  --dry-run
```

CF/Extreme 的站点表必须按 SSP 分别生成三套脚本（ssp126、ssp245、ssp585 使用对应站点 CSV）；不要把 ssp126 的站点表用于其他情景。先 `--dry-run` 检查 1,128 个 unit，再移除 `--dry-run` 写脚本；之后执行 `bash -n`、核对 manifest、脚本 hash、`submit_username`、输出根和 `--processes 16`。本轮准备阶段不执行 `sbatch`。

脚本内不写 `billing_account`，也不生成 `#SBATCH --account`。真正提交时才由控制器显式使用 `sbatch --parsable -A <submit_username> <script>`，并在共享锁中记录 Job ID。
