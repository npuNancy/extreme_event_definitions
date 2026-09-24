你是production V2全网格极端事件运行控制CodingAgent。先读AGENTS.md及本目录README、goal、运行前准备、账号清单和作业分工。

直接运行剩余三模式：MPI-ESM1-2-HR、MRI-ESM2-0、BCC-CSM2-MR；CANESM5已完成，不重跑。立即创建并持续执行goal，不设token budget。记录selected_models、确认时间、RUN_ID与代码SHA，Goal只覆盖这三个模式。

目标：18个worker完成三模式的baseline→signals→audit，乌镇1500汇总权威索引。2015–2060、基线2015–2024，五年分片至2060。每GCM 282组合×12 unit=3384个unit。无场站也须计算全网格。

账号及角色以accounts.csv为准；乌镇1500/acp6varuz3只汇总。1872/acjpoxgsdu余额96、1850/acf9hhlwmd余额277、1555/ac4wf1cvxp余额434核时，均预留10核时。按CANESM5实测0.408核时/作业估算，三账号各领1个轻patch，1872最多200次提交；不抢其他任务。其余不限核时。

先执行运行前准备。18worker在各自home更新develop-patch-grid：保留脏目录改动，本地测试、commit/push后经HTTPS clone或fast-forward更新，固定同一完整SHA与干净checkout。缺HTTPS认证就报告。运行期不改远程checkout；科学Python仅在计算节点运行，登录节点只做轻量操作。

乌镇185的/work/home/acbw9wpn5k下V1只读，不改软链接；旧production_v1逻辑路径不能证明V2身份。核实V2物理outputs根、三模式上游完成证据、所需final及sidecar，冻结input_release.json。禁止回退V1天气或复用V1场站结果；数组核验仅在计算节点。

所有worker读取相同物理V2文件、land plan和patch manifest，冻结路径、hash/stat及证据hash；适配器核对V2实体根。确认patch manifest结构符合接口。基线身份包含真实路径、mtime、代码SHA及环境版本，复制输入或基线不能当作同一身份。18账号Python/科学库环境须一致，沿用文档中的共享climate激活路径。

存储：代码仍在各账号home；作业输出、parts、日志、脚本及receipt均写实际执行账号自己的/work/share/<user>/extreme_grid/<RUN_ID>/。其home创建extreme_grid/<RUN_ID>软链接。1500在自身share运行根建立workers/<user>十八个链接指向各worker运行根，并建home链接。链接存在不代表权限可用；由所有者设置祖先穿越、结果读取及默认ACL，验证每个worker可读取其他worker基线、1500可访问所有产物、所有worker可读中心配置。已有路径先比较，不强制覆盖。

中心runtime保存不可变release、模型选择、台账和提交锁。验证共同挂载、跨账号原子rename及flock互斥；输入只读，输出不共写。检查share容量、inode和实际quota，不能假定share无限，也不能只按最终NC估算；parts、merge临时文件及失败副本都计入空间。容量预测按pilot实测，区分陆地点与矩形网格。

运行前必须在全部18个worker生成完整作业库存：4模型×3SSP×47patch×2tech的baseline/signals/audit，共每账号13536脚本，18份manifest和脚本hash一致。库存不授权运行；EXTREME_SELECTED_MODELS仅含上述三模式。脚本使用环境配置、相对日志、实际UID输出根；仅共享climate路径为home绝对路径例外。

EXTREME_ENV_FILE指向该worker share的campaign.env，配置代码路径、RUN_ID、所选模型、V2输入、几何文件和中心release。提交前建立logs，使用sbatch --parsable -A <实际用户名> --chdir=<本账号share运行根> --export=ALL <本账号脚本>。Slurm不会展开SBATCH中的shell变量。signals额外export权威原始EXTREME_BASELINE_FILE；audit额外export台账生成的EXTREME_COMBINATION_INDEX。阶段变量逐次清理再设。

先在乌镇199跑上述三模式的代表patch，候选R03C09/ssp126/solar，先baseline再首个完整五年signals。初始8CPU/4worker、tile32×32、time_chunk240；audit2CPU。这些仅为pilot起点。测elapsed、内存、最终大小、parts与merge时间，至少留20%内存余量。必要时补wind或大patch；成功pilot计入正式结果，不重复跑。调整profile时在18worker新目录生成同一新pack，不覆盖已提交脚本。

初始每15分钟监控。pilot后取baseline与五年signals较长耗时T：T<30选15分钟，30≤T<120选30分钟，T≥120选60分钟；Asia/Shanghai按整点/半点/刻钟对齐。记录实测、理由及next_check_at。

每轮检查、判断、更新、补槽/重试/重分配并记录。查18账号调度、退出码、日志、sidecar/receipt及stat；squeue消失不算成功。每账号最多20个account-wide在途作业，含其他项目与pending/configuring/completing；有槽即补。共用提交锁，锁内重查台账、依赖、槽位，每次sbatch前重计数。三账号每轮及提交前用sacct统计2026-09-24 16:30:00 Asia/Shanghai后全项目已耗核时：只计主Job/数组元素，排除.batch/.extern，按AllocCPUS×运行时间与截止点后交集/3600求和，运行中计至当前时刻。累计+10>余额时锁内暂停新提交；提交前还计在途预计核时，未提交unit改派其他账号，active原地监测。记录原始查询与累计；sacct缺失或不可信时暂停提交并核实。

提交先登记submitting、attempt、assignment_version、submit_username、脚本hash、releasehash和目标路径，取得JobID立即落账。丢失回执先按jobname/时间窗口查调度，记unknown，禁止盲重试。同unit最多一个active Job；失联账号的在途任务先查明终态。不得迁移或取消active任务。重分配保留logical_owner，记录旧/新账号与原因，使用目标账号已有同版脚本。

队列按用户模型顺序、ssp126/245/585、wind/solar和patch参考权重排序，但不是屏障。基线成功即释放该组合全部信号段，段间可并行；齐段即释放audit。空槽优先已ready审核，再轮流领取信号与基线，依赖阻塞项不妨碍其他ready任务。跨账号信号继续读原始基线；已成功信号段留在原账号，索引记录真实路径。换账号重试重新生成失败unit的parts，不承诺跨账号续接；同账号同配置可复用有效parts。

成功须Slurm COMPLETED/0:0、最终NC和COMPLETED sidecar、timing及匹配receipt；核对run/unit/JobID、代码SHA、releasehash、路径与stat。audit读取包含1基线和全部时间段的分布式索引，在计算节点检查坐标/domain、连续时间、事件schema、fill和基线身份，写组合审核JSON及receipt；audit自身也须调度成功。各账号局部manifest不代表全组合完成；1500汇总全部权威审核路径，不复制或拼接巨型NC。

TIMEOUT、节点故障、短暂I/O最多自动重试2次；OOM调整资源或并行度并生成新profile后再试。参数、代码、权限、版本或identity错误停止相关依赖链报告，不能绕过校验。提交拒绝记录原始原因调查；超算不能查余额，sshare已用核时不可靠，禁止据此估算余额。unknown/incomplete_output/dependency_blocked等保留真实状态，不记成功。

每轮无变化也更新本目录completion_status/progress.md，仅含Last checked和model×SSP表：每格为已完成组合数/94，全成标✅，未选标未选。完成须baseline、全部signals、audit成功且1500可访问产物。各阶段、逐unit状态、原因和下次检查时间存中心及本地JSON台账，状态不入Git。回复进展、阻塞和有依据的预计完成时间。

持续执行直到仅上述三模式全部baseline/signals/audit成功、1500可访问所有权威产物和索引、无active/retryable/incomplete/blocked/unknown。未选模型不计入分母。外部阻塞无法解决时明确报告缺项，不能伪报Goal完成；上下文压缩后从台账接续，避免重复提交。
