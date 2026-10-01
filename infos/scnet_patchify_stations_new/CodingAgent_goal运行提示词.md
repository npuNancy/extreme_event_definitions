# 交给 CodingAgent 的正式运行提示词

以下提示词用于后续明确授权的正式运行，不表示本次目录建设已提交作业。

---

请创建并持续执行一个goal：严格按照本项目 `infos/scnet_patchify_stations_new/goal.md`，
完成全新的全球场站极端事件Climate × Station campaign，直到准备、3,384个unit抽取审核和最终发布全部验收。
本提示词授权你执行该goal规定的远程准备、ACL设置、完整脚本生成、作业提交、15分钟监控、补槽、
安全重分配和有界重试；不要仅交付计划，不要每轮重复请求已经授权的提交确认。

先完整阅读本目录README.md、goal.md、运行前准备.md、accounts.csv、campaign.example.json、
作业分工/作业组合提交顺序.md、patch_assignment.csv，以及create_jobs.py、run_job.py的完成契约。
使用scnet-parallelize-workflows技能；用户要求和本目录的固定campaign优先于技能的通用示例。
特别是生成SLURM脚本不能含 `/work/home/{username}/...` 绝对路径，激活环境和所有数据/代码位置必须通过外部环境变量获取。

固定范围，不要询问或筛选GCM：

```text
CANESM5、MPI-ESM1-2-HR、MRI-ESM2-0、BCC-CSM2-MR
3 Climate SSP × 3 Station SSP × wind/solar × 47 patch
4 × 3 × 3 × 2 × 47 = 3,384科学unit
prepare 1 + extract（含audit）3,384 + publish 1 = 3,386逻辑作业
```

包括三种对应情景在内，全部九配对都在新campaign中处理；不要复用旧场站产物或旧运行台账宣称完成。
BCSD结果已完成抽检，不再做全量BCSD检查。科学输入是已完成的网格事件；必要的源索引/sidecar和mapping校验在计算节点做。
场站目录已确定：`/work/home/acbw9wpn5k/project_climate_patchify/shared_run_20260913/inputs/stations`，
三份CSV对应ssp126、ssp245、ssp585（SSP5-6.0文件）。

汇总账号是乌镇1872/scnet-wuzhen-1872/acjpoxgsdu，**不运行作业**。
14个worker严格按accounts.csv。新结果全部直接写入1872的
`/work/home/acjpoxgsdu/extreme_stations_new/<RUN_ID>`，通过ACL共享；不改成/work/share或分散结果再迁移。
确认home额度、祖先traverse、默认ACL（含具名1872）、跨账号原子写和flock，并逐账号记录证据。
先在所有14个worker各生成并验收完整3,386脚本，14份manifest和逐脚本hash必须一致，全部bash -n通过。
不要只给各账号生成其初始分配任务。1872只存汇总与状态，不生成或提交计算任务。

代码修改必须本地测试后提交推送，远程使用HTTPS干净checkout和固定SHA，禁止直接编辑远程代码。
远程SSH Git密钥不可靠，不能自动创建/复制密钥或把token放进URL。已授权操作继续推进；
确有不可解决的权限、认证或输入问题时报告具体证据，不凭猜测增加新的全量检查。

运行时必须：

1. 启动时立即检查一次，之后每15分钟，在Asia/Shanghai的hh:00、hh:15、hh:30、hh:45依次执行
   **检查—思考—汇总—执行**，随后进入下一小时。每轮估计下一个整刻，避免sleep900秒漂移。
2. 检查全部14账号的squeue/sacct、退出码、Elapsed/MaxRSS、短日志及receipt；不能从队列消失判成功。
3. 每轮输出 **预计剩余耗时、预计完成时间（Asia/Shanghai）、估算依据及置信度**，保存到runtime/cycles.jsonl。
   初期没有样本时根据申请walltime、计划槽位和已有画像给明确标注的低置信度区间，
   说明未知排队延迟；不得只写“无法估计”，首批完成后立即按实测更新，不伪造精确ETA。
4. 每个账号按跨项目所有活动Job持续补足到20个；全局目标280个活动槽。
   各组合优先级不是屏障，无当前组合ready任务时立即使用后续ready组合；没候选时记录具体空槽原因。
5. 空闲账号用尽自身任务后，必须从其他账号尚未提交的ready队列重分配。
   不迁移、取消、复制已提交或unknown的任务；保留logical_owner，增加assignment_version，
   用目标账号已有脚本提交并记录reassignment_log，普通重分配不重新生成脚本。
6. 所有提交遵守中央锁+目标账号锁，在锁内重新统计活动作业。sbatch前持久化submitting和唯一comment token，
   成功后立即登记JobID。SSH超时进入unknown，用squeue/sacct查清，禁止盲重试。
7. 每轮更新共享和本地completion_status/progress.md，即使状态不变也更新时间。
   保持 **Last checked + 一个极简表格**：准备/prepare/publish状态、12个model/Climate行、3个Station列，
   科学格子分母94，36格合计3384。详细日志、阶段证据、ETA写JSON/JSONL，不扩写进度表。
8. 依goal区分瞬时、资源、确定性、缺产物及未知失败。最多两次自动重试；资源失败先基于证据建立新profile，
   科学代码/配置变更使用新RUN_ID。复用本轮已校验分片，保护旧结果。
9. prepare由worker运行并验收后放行extract；所有3,384个extract连同audit验收后才由worker运行publish。
   1872只做轻量汇总，不能在登录节点运行科学计算或大量NC读取。

等待下一轮期间保持可响应用户消息，不在任务仍有ready/active/unknown工作时提前结束。
只有全部阶段成功、3384个unit为已验收成功（含合法空任务）、全局索引和72组覆盖统计完整、
无重复活动Job和未解决状态、记录已镜像后，才能把goal标记complete。
若需暂停必须有用户明确指令；恢复时先读现有ledger、当前scheduler和已冻结代码/配置，不重新初始化campaign。

最终报告新RUN_ID、权威索引和进度路径、完成/合法空任务/失败数量、真实耗时与资源画像，
并说明实际部署、提交、验收范围。不要把脚本生成、本地测试或模板中的0/94当成远程生产已经完成。
