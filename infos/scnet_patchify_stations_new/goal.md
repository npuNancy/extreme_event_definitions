# Climate × Station 场站事件：运行目标与操作契约

## 1. 目标与边界

完成 CANESM5、MPI-ESM1-2-HR、MRI-ESM2-0、BCC-CSM2-MR，三种Climate与三种Station全部九配对，
wind/solar、47 patch、2015–2060，共3,384个科学unit。1872（acjpoxgsdu）只提供共享存储和轻量控制，
14个worker见accounts.csv；包括prepare/publish在内的全部科学作业均由worker提交。

这是新campaign，不读取 `infos/scnet_patchify_stations` 的台账、receipt、prepared或场站结果来认领完成。
源输入仍是已完成的全球**网格事件**，不重新识别天气，不计算CF。
BCSD已通过抽检；禁止添加全量BCSD检查。源网格索引/sidecar、坐标和mapping校验是抽取自身合同，
由prepare或对应unit在计算节点执行，不扩大为上游全量数值检查。

固定新RUN_ID并在重启时沿用。只写 `/work/home/acjpoxgsdu/extreme_stations_new/<RUN_ID>`；
所有worker通过ACL直接读写，不改成worker分散产出再汇总。

## 2. 工作负载合同

| 阶段 | 入口与粒度 | 输入 / 完成证据 | 依赖 / 恢复 |
|---|---|---|---|
| prepare（1） | run_job.py → station_pipeline.prepare | 全四模式网格索引、patch manifest、三份场站CSV；prepared.json及receipt | 运行前关卡全通过；仅一个活动prepare；失败换JobID尝试目录 |
| extract+audit（3,384） | run_job.py → extract_combination → audit | 一个model/Climate/Station/patch/tech所有时间分片；audit.json及receipt | prepare调度成功且产物验收；同campaign复用有效分片 |
| publish（1） | run_job.py → station_pipeline.publish | 全3,384份已验收audit；全局index、coverage及receipt | 全extract成功；不能以部分表格满格代替发布 |

prepare共享场站目录、网格几何和mapping；不同Climate的网格与mask只有指纹相同才复用。
抽取和审核相邻执行，以同一个unit为失败重试边界，避免新增3,384个独立审核作业。
逻辑身份为 `station-events-new/extract/<model>/climate_<C>/station_<S>/<patch>/<tech>`；
`task_id` 是unit_id的SHA256前24位，由生成器写入manifest；不得由运行账号或JobID改变。

所有账号使用同一固定SHA、配置文件字节、源索引/patch manifest快照、资源profile和完整脚本包。
生成器强制1128个网格组合及47 patch集合与分工表一致，进而生成3384个场站组合；缺组合必须修复输入，不能静默缩小范围。
3,386份脚本/账号是可重分配副本，不是要求把每份副本都提交。

## 3. 准备关卡和初始台账

依次完成运行前准备.md：新目录与配额 → 代码/环境 → ACL与锁 → 输入/配置冻结 → 全14账号完整脚本生成与验收。
每步逐账号的 `status/checked_at/evidence/error/next_action` 写 `runtime/preparation.json`，不能只验证一个账号。
prepare提交前将ledger.preparation.status置为verified，并记录这份证据文件的SHA256。

```bash
python3 infos/scnet_patchify_stations_new/progress.py \
  --pack "$STATION_JOB_PACK/manifest.json" \
  --ledger "$STATION_SHARED_ROOT/runtime/ledger.json" --initialize \
  --output "$STATION_SHARED_ROOT/runtime/completion_status/progress.md"
```

初始化只执行一次；已有ledger时去掉--initialize，禁止重新清零。`campaign_identity`对代码/科学配置/输入与初始归属冻结，
资源profile可以变化但campaign_identity不变。每个任务state保存：

```text
unit_id, logical_owner, username, classification, job_id,
assignment_version, attempt, pack_identity, resource_profile, history,
submission_token, submitted_at, verified_at, scheduler_state, exit_code,
elapsed_seconds, max_rss, receipt, reason, next_action
```

前十项由初始化工具建立，其余随观察和提交补充。history是旧attempt完整state的快照（去掉其嵌套history），
至少保留job_id、username、assignment_version、pack_identity、resource_profile、最终调度状态和原因。
缺少记录属于unknown，不补造成功。明细保存JSON/JSONL，不塞入progress.md。

## 4. 提交和锁：每账号持续补到20

账户上限按**跨项目**全部活动作业统计，包括PENDING、RUNNING、CONFIGURING、COMPLETING、SUSPENDED及其他非终态；
不能只统计本campaign的作业名。目标是14×20=280个槽位，已有其他项目占槽时本campaign只使用余额。
prepare阶段只有一个ready作业，publish阶段只有一个ready作业；没有ready候选时据实记录空槽原因。
优先级是ready queue排序，不是整组完成屏障。

每次提交必须同时持有中央 `runtime/.submit.lock` 和目标账号 `$HOME/.bcsd_submit.lock`，顺序始终中央→账号，
有界等待，远程账号锁需覆盖重新查队列和sbatch全过程。其他控制器须遵守同一约定；不能假设中央锁约束了其他项目。
持锁过程中：

1. 重新读取ledger，确认unit未提交且依赖verified；重试则确认前次已终止、已分类和有剩余额度。
2. 重新执行目标账号 `squeue -h -u <username>`，计算 `max(0,20-active)`；0则换账号或结束本次提交。
3. 校验目标账号已准备的manifest和脚本SHA，检查环境文件、日志目录和当前profile；禁止临场补造缺失脚本。
4. 在执行sbatch**之前**原子写state为submitting，job_id=null，记录新attempt、assignment_version、username、pack_identity和唯一submission_token。
   submission_token用作sbatch的 `--comment`，持久化后才发送提交请求。
5. 在该账号下运行下面形式的命令，--account为实际运行用户。收到 `sbatch --parsable` 的 `JobID[;cluster]` 后先验证数字JobID再立即写回ledger，state改active。
6. 若SSH超时或返回不明确，state改unknown并保留token；先从squeue/sacct的Comment、作业名、提交时间和账号找回JobID，不能重提。
   仅在证据证明请求未被接收时才能回到not_submitted；无证据时保持unknown。

```bash
export STATION_ENV_FILE="$STATION_WORK_ROOT/campaign.events_new_v1.env"
sbatch --parsable --account="$(id -un)" --chdir="$STATION_WORK_ROOT" \
  --export=ALL --comment="$SUBMISSION_TOKEN" "$STATION_JOB_PACK/$SCRIPT"
```

命令片段只能在上述锁和台账流程中使用。SBATCH日志是相对 `logs/%x-%j.out/.err`，
提交前在WORK_ROOT创建logs，不能依赖脚本开始运行后才创建，因为Slurm先打开日志。
run_job.py核对claim中的JobID/账号/pack，必要时等待30秒让控制器登记；登记失败的作业会拒绝执行，之后按失败证据处理。
每个unit另有跨账号执行锁，防止并发写同一逻辑任务。

## 5. 每15分钟：检查—思考—汇总—执行

以Asia/Shanghai的hh:00、hh:15、hh:30、hh:45为监控时间点。启动时立即做一轮，然后计算下一个整刻；
不能简单sleep900秒导致漂移。某轮超过整刻时立即补查并记录延迟，不重叠启动两个控制循环。
等待时使用可响应用户输入的短等待，不能在任务未完成时提前结束代理运行。

每轮严格按以下顺序：

1. **检查**：收集14账号的squeue、sacct（主Job和batch步骤，包括ExitCode/Elapsed/MaxRSS）、短日志尾部、receipt和小JSON状态。
   保存观测到 `runtime/observations.jsonl`；缺失账号的上轮状态保留并标unknown，不能假设账号空闲。
2. **思考**：分类终态、核对依赖、计算各账号空槽、生成重试与重分配计划；检查存储增长和共享IO。
   只针对失败unit追查输入，不能重新发起全量BCSD检查。失败分支不阻塞其他ready工作。
3. **汇总**：原子更新ledger和progress，输出本轮计划、阻塞以及预计剩余时间，写 `runtime/cycles.jsonl`。
4. **执行**：在锁内复核计划，持续补满每个账号到20个活动作业或耗尽安全ready候选；完成重分配、有限重试及下游发布。
   执行后再同步ledger/progress，记录实际JobID和差异，不把计划当成已执行。

每轮必须打印：`预计剩余耗时：约 X–Y 小时；预计完成时间：YYYY-MM-DD HH:MM Asia/Shanghai；依据：…`。
使用已成功样本按tech、场站密度/patch和Climate/Station分组的耗时中位数或高分位，结合剩余任务量、实际吞吐、
活动作业预计尾部及排队时间；同时预留prepare/publish时间。可用 `剩余unit/最近完成速率` 与
`剩余估计CPU作业时长/实际可用槽位` 交叉估算。零样本阶段依据申请walltime、计划槽位和已有画像给出低置信度粗估区间，
说明未计入的未知排队延迟；不得只写“无法估计”，首批成功后立即用实测替换。ETA写cycles.jsonl及对话，不增加progress表的冗余说明。

## 6. 重分配和重试

初始逻辑归属见patch_assignment.csv，14账号均有完整脚本包。
某账号处理完自身预分配任务、有空槽而其他账号仍有未提交任务时，必须重分配：

- 只迁移 `not_submitted` 且依赖满足的unit；不得迁移、取消或复制PENDING/RUNNING/submitting/unknown作业。
- 优先空槽最多的账号，轮询打破平局；遵循model→Climate→Station→tech→patch优先级。
- 保持unit_id、key、logical_owner、输入、编码和共享输出根不变，仅改变实际username；assignment_version递增。
- 使用目标账号已有对应profile脚本；普通重分配不能在live loop重新运行生成器。
- 写 `runtime/reassignment_log.jsonl`：unit_id、old/new username、old/new script路径及hash、版本、时间、原因、JobID。
- 重新查询目标账号总活动数后才提交；每个unit同一时刻至多一个active Job。

分类：not_submitted、submitting、active、succeeded、retryable、resource_failure、deterministic_failure、
incomplete_output、dependency_blocked、unknown。合法空unit的分类同为succeeded，receipt.status保留SKIPPED_NO_STATIONS。

节点故障、可确认的短暂IO等最多自动重试2次（总attempt≤3）。OOM或TIMEOUT先记录MaxRSS/Elapsed/退出码，再改CPU或walltime，
不能相同参数盲重试；资源profile更新须先在全部14账号生成并校验新完整包，保留旧包与旧环境文件。
配置、身份、缺输入、ACL错误停止该分支，先修复再处理，不循环重试。
科学配置/编码/代码变化改变campaign_identity，应建立新RUN_ID，不向旧运行混入新产物。

prepare默认16CPU、最多16个源文件准备进程（不超过申请CPU数），catalog/mapping串行；extract+audit默认10CPU、单进程；publish默认16CPU、最多16个独立进程。各阶段默认24小时。prepare日志提供阶段耗时、每25个源组合的进度和源扫描剩余时间；结合这些记录估计整体ETA，不能按CPU数线性推算。
wzhctest、每核约3.5GB是参考项目画像，首次运行前验证平台限制；只用成功任务的充分MaxRSS样本调节资源，保留至少20%余量。
重试创建新JobID目录，history列出的本轮有效完整分片可复用；旧场站campaign的文件不能加入history。

## 7. 成功判定与阶段交接

不能从squeue消失推出成功。必须主Job终态COMPLETED、ExitCode=0:0，batch步骤无失败，且receipt身份与当前state完全一致。
控制器在调度成功后调用 `valid_receipt(state, row, pack, shared, require_success=False)` 验收候选receipt，
校验campaign/code/pack/task/job/user/version、output stat/SHA及分片sidecar；成功后才持久化succeeded。
计算作业检查依赖时使用默认require_success=True，必须先有已成功的台账记录；
监控时只读JSON与文件stat，不在登录节点打开NC。接收成功后记录verified_at、receipt路径和证据。

- prepare：prepared身份、完整3384组合、所有目录/映射说明齐备，来源code/config一致；资产所有者冻结资产ACL，
  逐账号验证可读。共享元数据/容量与存储估算通过后才能放行extract。
- extract：receipt.output为audit.json，内容与本unit/key/mapping/全部源分片对应；普通unit所有NC/sidecar契约有效。
  输出全time和station坐标检查、固定种子源值抽样已在同一计算作业完成；合法空任务由mapping计数为0证明。
- publish：仅在所有extract经过上述调度及receipt验收后提交，由worker读全部审核产物，生成统一索引和coverage。
  索引精确3384个组合，输出指向本共享根下权威尝试；coverage含4×3×3×2=72组，每组47个已处理组合，
  目录总数与MATCHED/OUTSIDE_DOMAIN/TOO_FAR/NO_SOURCE_PATCH之和一致。无需再全量重读网格事件或BCSD。

最终须prepare成功、3384个extract成功（含合法空unit）、publish成功，且无active/submitting/unknown/可重试/未完成unit。
没有重复Job或未记录重分配；全局index、coverage、receipt、ledger、观察/重分配日志和进度已同步到1872和本地。
仅此时将代理goal标记完成。权限或输入阻塞必须记录具体路径、账号、错误与可执行下一步，不能报为成功。

## 8. 进度、部署与保护

每轮使用progress.py更新共享与本地 `infos/scnet_patchify_stations_new/completion_status/progress.md`；
文件只保留带Asia/Shanghai的Last checked和一个表：准备步骤、prepare、四模型×三Climate行、三个Station列、publish。
每个科学格子分母94=2tech×47patch；全表36格合计3384。prepare/publish各0/1或1/1。
只有已记录verified_at的succeeded任务计入完成；不是NC数量或提交数量。详细步骤和ETA在JSON/JSONL。
这个目录已被Git忽略，新checkout需运行progress.py创建；即使状态不变也更新时间，不改写旧目录的进度。

远程checkout不得直接编辑。代码变更在本地完成并测试、提交推送，远程HTTPS clone或pull --ff-only，
固定完整SHA；代码仓库为 `https://gitee.com/nancyyyyy/extreme_event_definitions.git`。
不对脏目录reset，不更新活动作业使用的checkout；远程Git SSH密钥可能被清除，不能依赖它或自动复制密钥。
若HTTPS认证不可用，报告具体访问错误并等待有效认证方式，不在URL或日志中写token。
生成脚本、日志、环境文件、ledger和科学产物都放checkout外。
登录节点只做小文件/权限/SSH/Git/脚本生成与语法检查/调度查询；科学计算、NC读取、站点全表和映射在Slurm计算节点进行。
现有网格事件、旧场站结果、其他campaign和源CSV只读，不删除或递归改写其ACL。
