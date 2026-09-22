# production V2 全网格 Extreme 运行契约

## 目标与范围

先询问用户本轮运行哪个/哪些 GCM，确认后记录 `selected_models`、确认时间、RUN_ID 和代码 SHA。只对所选 GCM 创建并持续执行 Goal；未选模型即使 V2 就绪或已有脚本，也不提交。当前仅 CANESM5 被用户报告已完成 V2，不自动扩大为 4 模型。

在 `accounts.csv` 的 8 个 worker 上完成所选模型的 `baseline → signals → audit`，所有结果写执行账号自己的 `/work/share/<user>/extreme_grid/<RUN_ID>`，并通过乌镇1500的 8 个 worker 链接和权威产物索引可访问。1500 不运行 Slurm 作业。

V1 场站结果及乌镇185 `/work/home/acbw9wpn5k` 下的 V1 气象结果保留只读；不复用其事件结果、不改动其目录或软链接。本轮输入必须通过 V2 实体路径和上游完成证据确认。

## 工作负载契约

| 项目 | baseline | signals | audit |
|---|---|---|---|
| 实际入口 | `scripts/prepare_grid_baseline.py` | `scripts/grid_signals_patchify.py --years <段>` | `infos/scnet_patchify_grid/run_job.py --stage audit` |
| 独立维度 | model/SSP/patch/tech | 左侧维度 + 时间段 | model/SSP/patch/tech |
| 依赖 | wind: uas/vas；solar: rsds 的 V2 final+sidecar，完整分析轴，land plan | 同组合权威基线；wind: tas/uas/vas/hurs；solar 再加 pr/rsds | 同组合基线及所有信号段的成功记录 |
| 产物 | `baseline_2015-2024.nc`、sidecar、timing、receipt | `signals_<start>-<end>.nc`、sidecar、timing、receipt | `audit_2015-2060.json`、receipt |
| 内部并行 | 空间 tile worker + 流式 merge | 空间 tile worker + 时间 chunk + 流式 merge | 单进程读取 header、坐标与时间，生成分布式索引 |
| 默认 unit 身份 | `grid-v2/baseline/<model>/<SSP>/<tech>/<patch>/2015-2024` | `grid-v2/signals/.../<start>-<end>` | `grid-v2/audit/.../2015-2060` |

日期可通过生成器参数配置，整轮保持一致。默认每模型 282 个基线、2820 个信号、282 个审核，共 3384 个 Slurm unit。无场站不是跳过条件；纯域外 tile 由计算程序写缺测并正常完成。

## 输入与代码冻结

准备阶段维护中心 `runtime/input_release.json`，格式见 `运行前准备.md`。记录 V2 实体 outputs 根、明确 BCSD_ROOT、模型就绪证据及其 hash、patch manifest hash、land plan 的原始物理路径/大小/mtime。所选模型存在缺输入时记 dependency_blocked，不降级到 V1。接收用户的上游可用性结论，不在登录节点重新扫描气象数组。

8 个 worker 使用干净 `develop-patch-grid`，同一个完整 Git SHA 和相同 Python/NumPy/pandas/xarray/netCDF4 版本。代码仍在各自 home，通过本地 commit/push 后 HTTPS clone 或 `pull --ff-only` 更新；Git SSH key 不作为可持续依赖。私有 HTTPS 无认证时报告认证缺项，不把令牌写进 URL。运行中固定 SHA；不在线编辑 checkout，不强制 reset 脏目录。

网格基线身份包含真实输入路径、mtime、实现内容摘要和 Git SHA。所有账号读同一个真实 land plan 及同一批 V2 文件；不能复制到各自目录后视为同一输入。跨账号读取基线保留原文件及 sidecar 的物理路径；不复制、搬迁或伪造 sidecar 以绕过身份检查。

## 目录、权限与作业包

运行前按 `运行前准备.md` 完成：8 个计算账号的 share 实体目录及 home 链接、1500 的 8 个 worker 链接、同一文件系统挂载/ACL/原子 rename/flock 探测。共享链接只是访问入口；读取权限由 ACL/目录权限保证。

科学输出、parts、日志和 job 包在执行账号自己的 share 根；汇总端只存链接、JSON 索引、台账及状态。`df`、inode、实际 quota 按账号检查，不能假定 share 不限额。结果容量还要包含保留 parts。

每个 worker 必须预先生成 **所有模型库存、所有阶段、所有组合** 的同版脚本，manifest 和脚本 hash 在 8 账号一致。聚合账号不必生成可提交作业包。已提交脚本不可变；pilot 后的新 profile 在全部 worker 的新目录重新生成，再用于未提交/可重试 unit。

脚本统一加载 `EXTREME_ENV_FILE`，climate 激活沿用 `source /work/home/acbpgywfpz/miniconda3/bin/activate climate`；该路径是已存在的共享环境例外。Python 科学计算只能在计算节点。登录节点只做 Git、脚本、轻量状态、JSON/stat、软链接及调度操作。

## Pilot 和监控间隔

用户确认 GCM 后，从所选范围选择一个代表性非空 patch（默认候选 R03C09/ssp126/solar），在乌镇199先运行 baseline，再运行第一个完整五年 signals 段，不能只用 toy 数据决定生产配置。必要时再补 wind 或大 patch；已成功的正式身份 pilot 计入完成，不重复运行。

初始每 15 分钟检查；取得 baseline 和 signals 的实际 `sacct Elapsed` 后，令 T 为两者较长的分钟数：

| T | 后续监控间隔 | Asia/Shanghai 对齐时刻 |
|---|---|---|
| T < 30 分钟 | 15 分钟 | hh:00、15、30、45 |
| 30 ≤ T < 120 分钟 | 30 分钟 | hh:00、30 |
| T ≥ 120 分钟 | 60 分钟 | hh:00 |

记录测量、所选间隔、理由和 next_check_at；按不同任务后续实测可再次调整，但只能取 15/30/60 分钟。pilot 未完成前不宣称资源或间隔已确定。

baseline/signals 初始 8 CPU/4 worker、tile32×32、time_chunk240；audit 2 CPU。历史每核约3.63GB仅供初始估算，实际限制以本轮分区为准。采集各阶段 elapsed、进程/Slurm 内存、parts/最终空间及 merge；至少保留20%内存余量。MaxRSS 可能是 step/进程口径，不能仅把一个 worker 峰值当作多进程总内存。

## 提交、依赖与重分配

所有项目共享账号的提交应使用同一把锁（本轮候选：1500 share 下 `runtime/.submit.lock`；准备时与并行工作流协调并验证跨账号 flock）。锁内执行：

1. 查权威台账，确认 unit 无 active Job、未成功、模型在 allowlist，依赖已成功。
2. 统计目标账号 **所有项目** 的 pending/running/configuring/completing 等全部在途作业；可用槽=`max(0,20-active)`。每次 sbatch 前重计数。
3. 领取 unit 并记录 assignment_version、attempt、submit_username、脚本 SHA、输入 release hash、目标输出路径；有未核实的 submitting 状态先查调度，不能重复提交。
4. `sbatch --parsable -A <实际用户名> --chdir=<该账号share运行根> --export=ALL <该账号既有脚本>`，立即保存 Job ID。丢失回执时先按 job name/提交窗口查证，记 unknown，不能盲重试。

提交环境先 export `EXTREME_ENV_FILE`；signals 另外 export 台账权威 `EXTREME_BASELINE_FILE`；audit 另外 export 已完成组合的 `EXTREME_COMBINATION_INDEX`。不要在 sbatch 的 export 逗号字符串中拼复杂路径；导出变量后用 `--export=ALL`。每轮清除不适用的旧阶段变量。

依赖按组合释放，不设“全部基线完成”屏障。同一模型最多160个活跃槽仍要扣除其他项目；不用 Slurm array，不整批等待。初始分工仅用于均衡，同一 stage unit 可分给任何有空槽的 worker。保留 logical_owner，只改 submit_username、assignment_version。不得迁移/取消/复制 active Job。

已完成基线保持在原生产账号，其他账号只读。失败 unit 换账号后在新账号自己的 share 重新生成 parts；当前 parts 身份含路径，**不承诺跨账号自动续接旧 parts**。原账号同配置重试可复用有效 parts。已成功的信号段不因后续段重分配而复制或重跑；中心索引记录每个文件实际所在地。

## 完成证据与失败处理

baseline/signals 成功必须同时满足：Slurm `COMPLETED/0:0`；最终 NC、sidecar、timing 和 wrapper receipt 存在；sidecar `status=COMPLETED`，身份/参数正确；receipt 的 run_id、unit_id、Job ID、代码 SHA、V2 release hash、产物路径及 stat 一致。登录节点只核对 JSON/stat；需要开 NetCDF 的核验在计算节点进行。

audit 依赖的组合索引必须覆盖一个基线及全部时间段，列出每项 unit_id、Job ID、COMPLETED/0:0、receipt 和物理 output。审核任务检查原生坐标、domain、时间连续性、0/1/fill schema、基线一致性及事件列表，写包含所有实际路径的组合索引。audit 本身也须 Slurm 成功和匹配 receipt；1500 保存其权威路径并通过 worker 链接可访问。不合并全球大 NC。

状态：`not_submitted/submitting/active/succeeded/dependency_blocked/retryable/resource_failure/deterministic_failure/incomplete_output/unknown`。作业从 squeue 消失不能判成功。TIMEOUT/节点故障/短暂 I/O 可最多自动重试2次；OOM调整资源或减小并行度后用新 profile，不能不变配置反复重试。代码、数据版本、身份或权限错误停止相关依赖链并报告，不绕过契约。账号提交被拒先记录原始原因、调查；不能用余额查询或不可靠的 sshare 用量推断余额。失联账号 active unit 记 unknown，查清终态后才重分配。

## 进度与终止

每轮按“检查 → 判断 → 更新状态 → 执行补槽/重试/重分配 → 记录动作”执行。无状态变化也更新：

- 本地 `infos/scnet_patchify_grid/progress.md`：准备、pilot、baseline、signals、audit、汇总的阶段表和按 GCM/SSP 的计数。
- 每个使用到的本地 `completion_status/` 下必须有 `progress.md`，一行一个 unit；另存 JSONL/JSON 台账。字段为 unit_id、stage、model、scenario、tech、patch、years、logical_owner、submit_username、assignment_version、attempt、job_id、scheduler_state、exit_code、elapsed、memory、output/receipt、classification、reason、observed_at、next_action。
- 时间均记录 Asia/Shanghai，保留未观测 unit 并标 unknown；记录下一次对齐检查时间。进度和调度快照被 Git ignore。

Goal 终点：仅所选 GCM 的全部基线、信号、audit 成功，1500 可访问全部权威产物及索引；无 active/retryable/incomplete/blocked/unknown。其他未选 GCM 不计入分母，也不等待它们的 BCSD。遇无法自主解决的外部阻塞，明确报告缺项，不伪报完成。
