# Extreme patchify 六账号准备目标

## 范围

准备 Extreme 阶段的 1,128 个独立 unit：

```text
4 model × 3 SSP × 2 technology × 47 patch = 1,128
```

本轮只生成和审查脚本及运行文档，**不提交作业**。可用 worker 为乌镇1850、1872、1555、1352、1731、1500；乌镇151、173、9259、9359 不参与本轮。

实际输出根目录：

```text
/work/home/acbw9wpn5k/project_climate_patchify/shared_run_20260913/outputs/extreme
```

汇总节点：乌镇185（`scnet-wuzhen-185`，`acbw9wpn5k`）。六个 worker 与该根目录的共享文件系统、ACL、输入路径和环境必须在正式生成/提交前重新只读确认。

## 分配规则

`patch_assignment.csv` 同时记录：

- `logical_owner`：原 BCSD patch owner，只用于归属和审计；
- `submit_account`：本轮六账号中实际运行脚本的账号；
- `assignment_version`：unit 初始为 1；动态转移时增加版本号。

可用账号各自保持最多 20 个 account-wide active jobs，全局最多 120 个。正式运行时，空槽可以接收尚未提交且依赖满足的 unit；不能迁移、复制或取消 active Job。

## 依赖和完成契约

Extreme unit 只依赖同一 model/scenario/technology/patch 的 BCSD final 和 sidecar。它不需要等待 CF；Loss 才同时依赖 CF 与 Extreme。

一个 unit 只有同时满足以下条件才可记为 `succeeded`：

1. Slurm 终态为 `COMPLETED`，退出码 `0:0`；
2. `<tech>.nc` 和 `<tech>.nc.json` 存在，且没有 `.partial` 作为最终文件；
3. sidecar 的 model/scenario/patch_id/tech/output 与 unit identity 一致；
4. Extreme 文件含场站信号、场站元数据、低资源 provenance 和 station identity；
5. 汇总到乌镇185后的 manifest/checksum 与 worker 侧记录一致。

无场站 unit 必须写出 `<tech>.nc.SKIPPED_NO_STATIONS.json`，并记为 `succeeded_skip`。缺 BCSD 输入为 `dependency_blocked`；参数、权限或 identity 错误为 `deterministic_failure`；不完整最终文件为 `incomplete_output`。

## 资源和多进程

正式 resource profile 为 `resource_v3_extreme_multiprocess`（依据乌镇-199 pilot 实测确定）：

- `--cpus-per-task 16`，`--processes 16`；16 核对应 16×3.63 GB ≈ 58 GB 内存配额，pilot 实测 MaxRSS 31.4 GB，余量 ~45%；
- 每个 worker 只处理一个 station block，并写独立 part；父进程流式 merge；
- part 文件放在输出根之外的版本化 jobs/runtime 目录或明确的 `.extreme_parts/<model>/<scenario>/<patch>/<tech>`；不得与其他 unit 共享同名 part；
- 每个 job 写 timing sidecar，记录 dispatch、merge、I/O 和 worker 阶段；
- 计算节点才运行 NetCDF 和科学计算，登录节点只生成/检查脚本、`bash -n`、`squeue`、`sacct` 和轻量 sidecar。

后续依据完整输出的 `sacct MaxRSS` 调整 profile：至少保留 20% 内存余量，OOM 必须增加核数后重试；已提交或运行中的 unit 不改资源。

## 准备检查清单

- [ ] 读取本目录全部 CSV 与提示词，并确认 47 个 active patch。
- [ ] 读取 Extreme 当前 `--help`，确认 `--processes`、`--station-block-size`、`--parts-root` 和 `--timing-report`。
- [ ] 六个账号分别通过 SSH、`id -un`、`squeue`、`sacct` 和环境激活检查。
- [ ] 三个 SSP 分别使用正确 stations CSV；不要复用错误情景的站点表。
- [ ] 每账号 dry-run 数量与 task_allocation 对应，六账号合计 1,128。
- [ ] 所有脚本 `bash -n` 通过；每份 manifest 记录 code SHA、脚本 SHA-256、submit username、resource profile、cpus 和 processes。
- [ ] 检查输出根、patch manifest、BCSD sidecar、ACL 和共享锁。
- [ ] 只在用户明确授权后进入提交循环；本轮目标在此停止。
