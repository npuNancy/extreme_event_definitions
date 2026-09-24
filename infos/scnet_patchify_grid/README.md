# BCSD production V2 全网格 Extreme 运行包

本目录管理 `develop-patch-grid` 的 **baseline → signals → audit** 三阶段。只使用经确认的 BCSD production V2 气象输入；已完成的 V1 场站结果不能作为本轮结果或跳过依据。

用户补充的历史数据位置：production V1 全量气象结果保存在 `scnet-wuzhen-185` 的 `/work/home/acbw9wpn5k` 下，本轮保留只读，不搬迁、删除或修改软链接。上游重跑文档曾沿用带 `production_v1` 名称的逻辑路径，不能据此推断当前 V1/V2 布局。本轮准备时核实 V2 实体目录和完成证据，再冻结路径。

## 文件与边界

- `accounts.csv`：18 个计算账号和 1 个汇总账号，唯一角色清单。
- `create_jobs.py`：仅生成账号无关的 Slurm 脚本、依赖和脚本 SHA-256 清单；标准库运行，不提交。
- `run_job.py`：计算节点上的 V2/代码/账号预检，调用现有计算入口，执行分布式结果审核并生成 receipt。
- `运行前准备.md`：所有计算账号更新代码、共享目录和权限探测、**每个账号生成完整作业包**。
- `作业分工/patch_assignment.csv`、`作业分工/作业组合提交顺序.md`：初始均衡分工与滚动提交顺序。
- `goal.md`：运行契约、依赖、完成判据、动态重分配、监控与重试。
- `completion_status/progress.md`：统一的本地结果汇总，仅含 Last checked 和模型 × SSP 完成计数表，忽略于 Git；计数口径见 goal。
- `develop-patch-grid_正式运行提示词.md`：3800–4000 UTF-16 字符的 CodingAgent Goal 提示词。

本轮文件建设不等于远程准备或作业提交已完成；真实 V2 路径、权限、容量和 pilot 结果必须在运行前记录。

## 账号与存储

乌镇1500（`scnet-wuzhen-1500 / acp6varuz3`）只做汇总。计算账号以 `accounts.csv` 为准，共18个。每个计算账号最多 20 个 account-wide active jobs，18 个账号最多 360，其他项目在途作业同样占槽。

```text
代码：$HOME/project_climate/repos/extreme_event_definitions
计算账号实体根：/work/share/<实际用户名>/extreme_grid/<RUN_ID>/
    outputs/<model>/<ssp>/<patch>/<tech>/...
    parts/、jobs/、logs/、runtime/receipts/
计算账号 home 链接：$HOME/extreme_grid/<RUN_ID> → 上述实体根
汇总账号实体根：/work/share/acp6varuz3/extreme_grid/<RUN_ID>/
    workers/<计算用户名> → 对应计算账号实体根，共 18 个链接
    runtime/：选择范围、输入版本、台账、共享提交锁、结果索引
汇总账号 home 链接：$HOME/extreme_grid/<RUN_ID> → 汇总实体根
```

软链接不复制数据，也不授予访问权限。需验证共同挂载、目录穿越和读取 ACL，特别是 signals 读取其他账号完成的基线。`/work/share` 的配额情况以实测为准，不写成“无限容量”；所有大输出、parts 和日志都放 share，home 仅放代码和链接。

## 数量与生成

**脚本库存**包含 4 个模型；**本轮运行范围**已确定为 MPI-ESM1-2-HR、MRI-ESM2-0、BCC-CSM2-MR。CANESM5 已完成，不纳入本轮提交。

默认 2015–2060，基线 2015–2024，信号按五年分片：

| 范围 | baseline | signals | audit | Slurm unit 总数 |
|---|---:|---:|---:|---:|
| 1 个 GCM × 3 SSP × 47 patch × 2 tech | 282 | 2,820 | 282 | 3,384 |
| 本轮 3 个 GCM | 846 | 8,460 | 846 | 10,152 |
| 4 个 GCM 脚本库存 | 1,128 | 11,280 | 1,128 | 13,536 |

在 **每个计算账号** 用同一 SHA、参数及 profile 生成全部 13,536 份脚本；18 账号总共 243,648 份副本，但逻辑任务仅一套。生成器也支持显式缩小模型/SSP/patch 范围供测试，不自动 SSH 或复制到其他账号。

```bash
python3 infos/scnet_patchify_grid/create_jobs.py \
  --models CANESM5 MPI-ESM1-2-HR MRI-ESM2-0 BCC-CSM2-MR \
  --code-sha <部署的完整40位SHA> \
  --jobs-dir <本账号share运行根>/jobs/grid_v2_pilot_v1 \
  --dry-run
```

去掉 dry-run 才写脚本。默认 `wzhctest`；baseline/signals 为 8 CPU、4 worker、24 小时，audit 为 2 CPU、2 小时。这只是 pilot 起点，不能直接视为已测定生产资源。profile 变化用新目录生成并同步到全部计算账号，不覆盖已提交脚本。

脚本除现有共享 climate 激活路径外不内嵌账户 home 绝对路径。数据和代码由环境变量传入，输出路径按实际 UID 构造。`#SBATCH` 日志使用相对路径，**提交必须使用 `--chdir=<本账号share运行根>`，并提前建立 logs**；Slurm 指令不会展开 shell 环境变量。

signals 使用台账中权威基线的原始物理路径；audit 读取分布式组合索引，核对全部时段并输出 JSON 索引，不拼接或复制大型 NetCDF。各计算账号的原生 `manifest.json` 只覆盖本账号产物，全组合以 audit 索引为准。

## 验证

```bash
python3 infos/scnet_patchify_grid/create_jobs.py --help
.venv/bin/python -m pytest tests/test_grid_job_workflow.py -q
python3 /data6/yanxiaokai/project_climate/bcsd/utils/check_prompt_chars.py \
  infos/scnet_patchify_grid/develop-patch-grid_正式运行提示词.md
```

本地验证（2026-09-24）：工作流测试12项通过，覆盖库存/依赖、账号与分工、跨目录生成一致性、Shell语法、拒绝覆盖、V1软链接隔离、跨账号目录的基线与信号审核；全量dry-run为13,536个脚本。提示词经指定检查器统计为3,996个UTF-16字符。新增账号的远程权限、共享环境和V2访问需在运行前验证。
