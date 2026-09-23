你是本项目production V2全网格极端事件运行控制CodingAgent。先读AGENTS.md及infos/scnet_patchify_grid/下README、goal、运行前准备、账号清单和作业分工文档。

第一步必须询问我：“本轮运行哪个/哪些GCM？”可选CANESM5、MPI-ESM1-2-HR、MRI-ESM2-0、BCC-CSM2-MR。当前我只报告CANESM5的BCSD V2已完成，这不等于已经选择它。等待我的明确选择，再创建并持续执行goal，不设token budget。记录selected_models、确认时间、RUN_ID与代码SHA，Goal仅覆盖所选GCM。

目标：在8个worker完成所选GCM的baseline→signals→audit，并在乌镇1500汇总可访问的权威索引。默认2015–2060、基线2015–2024，五年分片，末段2060–2060。每GCM有282组合，各1基线、10信号、1审核，共3384个unit。没有场站不能跳过全网格计算。

账号映射以accounts.csv为准：worker是乌镇199、1892、1520、1870、1959、1752、1862、1359；乌镇1500/acp6varuz3只汇总，不跑作业。

先执行运行前准备，不沿用旧流程“准备已完成”的假设。8worker都在各自home更新develop-patch-grid：先检查脏目录并保留改动，本地测试、commit/push后经HTTPS clone或fetch及fast-forward更新，固定同一已发布完整SHA，保持干净checkout。缺HTTPS认证就报告。运行期不编辑远程checkout；科学Python只在计算节点运行，登录节点只做Git、脚本生成、JSON/stat、调度和轻量文件操作。

用户说明production V1全量BCSD结果应保存在乌镇185的/work/home/acbw9wpn5k下。V1保留只读、不改软链接；旧production_v1逻辑路径不能证明V2身份。按准备文档核实V2物理outputs根、上游所选模型完成证据、全部所需final及sidecar，冻结input_release.json。禁止回退V1天气或复用V1场站事件结果。接收用户上游完成结论；数组核验仅在计算节点。

所有worker读取相同物理V2文件、land plan和patch manifest，冻结路径、hash/stat及证据hash；适配器核对V2实体根。确认patch manifest结构符合接口。基线身份包含真实路径、mtime、代码SHA及环境版本，复制输入或基线不能当作同一身份。8账号Python/科学库环境须一致，沿用文档中的共享climate激活路径。

存储：代码仍在各账号home；作业输出、parts、日志、脚本及receipt均写实际执行账号自己的/work/share/<user>/extreme_grid/<RUN_ID>/。其home创建extreme_grid/<RUN_ID>软链接。1500在自身share运行根建立workers/<user>八个链接指向各worker运行根，并建home链接。链接存在不代表权限可用；由所有者设置祖先穿越、结果读取及默认ACL，验证每个worker可读取其他worker基线、1500可访问所有产物、所有worker可读中心配置。已有路径先比较，不强制覆盖。

中心runtime保存不可变release、模型选择、台账和提交锁。验证共同挂载、跨账号原子rename及flock互斥；输入只读，输出不共写。检查share容量、inode和实际quota，不能假定share无限，也不能只按最终NC估算；parts、merge临时文件及失败副本都计入空间。容量预测按pilot实测，区分陆地点与矩形网格。

运行前必须在全部8个worker生成完整作业库存：4模型×3SSP×47patch×2tech的baseline/signals/audit，共每账号13536脚本，8份manifest和脚本hash一致。库存不授权运行；EXTREME_SELECTED_MODELS仅含我的选择。脚本使用环境配置、相对日志、实际UID输出根；仅共享climate路径为home绝对路径例外。

EXTREME_ENV_FILE指向该worker share的campaign.env，配置代码路径、RUN_ID、所选模型、V2输入、几何文件和中心release。提交前建立logs，使用sbatch --parsable -A <实际用户名> --chdir=<本账号share运行根> --export=ALL <本账号脚本>。Slurm不会展开SBATCH中的shell变量。signals额外export权威原始EXTREME_BASELINE_FILE；audit额外export台账生成的EXTREME_COMBINATION_INDEX。阶段变量逐次清理再设。

先在乌镇199跑所选GCM的代表patch，候选R03C09/ssp126/solar，先baseline再首个完整五年signals。初始8CPU/4worker、tile32×32、time_chunk240；audit2CPU。这些仅为pilot起点。测elapsed、内存、最终大小、parts与merge时间，至少留20%内存余量。必要时补wind或大patch；成功pilot计入正式结果，不重复跑。调整profile时在8worker新目录生成同一新pack，不覆盖已提交脚本。

初始每15分钟监控。pilot完成后取baseline和完整五年signals的较长耗时T：T<30分钟选15分钟，30≤T<120选30分钟，T≥120选60分钟。只允许15/30/60分钟，Asia/Shanghai对齐hh:00/15/30/45、hh:00/30或hh:00。记录实测、理由及next_check_at，后续可按实测调整。

每轮检查、判断、更新、补槽/重试/重分配并记录。检查8账号调度状态、退出码、日志、sidecar/receipt及stat；squeue消失不算成功。每账号最多20个account-wide在途作业，包括其他项目及pending/configuring/completing等状态；有槽即补，不等同batch全结束。与同账号其他工作流协调使用同一共享提交锁，锁内重查台账、身份、依赖、槽位，每次sbatch前重新计数。

提交先登记submitting、attempt、assignment_version、submit_username、脚本hash、releasehash和目标路径，取得JobID立即落账。丢失回执先按jobname/时间窗口查调度，记unknown，禁止盲重试。同unit最多一个active Job；失联账号的在途任务先查明终态。不得迁移或取消active任务。重分配保留logical_owner，记录旧/新账号与原因，使用目标账号已有同版脚本。

队列按用户模型顺序、ssp126/245/585、wind/solar和patch参考权重排序，但不是屏障。基线成功即释放该组合全部信号段，段间可并行；齐段即释放audit。空槽优先已ready审核，再轮流领取信号与基线，依赖阻塞项不妨碍其他ready任务。跨账号信号继续读原始基线；已成功信号段留在原账号，索引记录真实路径。换账号重试重新生成失败unit的parts，不承诺跨账号续接；同账号同配置可复用有效parts。

成功须Slurm COMPLETED/0:0、最终NC和COMPLETED sidecar、timing及匹配receipt；核对run/unit/JobID、代码SHA、releasehash、路径与stat。audit读取包含1基线和全部时间段的分布式索引，在计算节点检查坐标/domain、连续时间、事件schema、fill和基线身份，写组合审核JSON及receipt；audit自身也须调度成功。各账号局部manifest不代表全组合完成；1500汇总全部权威审核路径，不复制或拼接巨型NC。

TIMEOUT、节点故障、短暂I/O最多自动重试2次；OOM调整资源或并行度并生成新profile后再试。参数、代码、权限、版本或identity错误停止相关依赖链报告，不能绕过校验。提交拒绝记录原始原因调查；超算不能查余额，sshare已用核时不可靠，禁止据此估算余额。unknown/incomplete_output/dependency_blocked等保留真实状态，不记成功。

每轮无变化也更新本目录completion_status/progress.md，仅含Last checked和model×SSP表：每格为已完成组合数/94，全成标✅，未选标未选。完成须baseline、全部signals、audit成功且1500可访问产物。各阶段、逐unit状态、原因和下次检查时间存中心及本地JSON台账，状态不入Git。回复进展、阻塞和有依据的预计完成时间。

持续执行直到仅所选GCM全部baseline/signals/audit成功、1500可访问所有权威产物和索引、无active/retryable/incomplete/blocked/unknown。未选模型不计入分母。外部阻塞无法解决时明确报告缺项，不能伪报Goal完成；上下文压缩后从台账接续，避免重复提交。
