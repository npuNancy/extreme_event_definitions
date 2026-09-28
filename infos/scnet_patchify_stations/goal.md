# stations_v2 运行契约

## 范围与终点

输入为 `/work/share/acp6varuz3/extreme_grid/grid_v2/` 的现成事件信号；四模式、三 SSP、47 patch、两技术、2015–2060。发布到 `/work/share/acp6varuz3/extreme_grid/stations_v2/`。只允许 accounts.csv 的13个 worker 运行作业；1500 是汇总和控制账号。每个 worker 写自己的 `/work/share/<username>/extreme_grid/stations_v2/`。

终点：prepare 成功；全部1,128组合 extract/audit 成功或有明确空站点跳过证据；所有合法目录站点有覆盖/未覆盖解释；索引、覆盖报告和链接可访问；没有 active、retryable、unknown 或未解决失败。

本文件是运行约定，不会自动启动 goal 或提交任务。

## 工作负载契约

| 阶段 | 入口 | 独立身份 | 依赖 | 结果 |
|---|---|---|---|---|
| prepare | run_job → station_pipeline.prepare | station-v1/prepare | 索引、patch manifest、CSV、代码SHA | catalogs、容量记录、mappings、prepared.json |
| extract | run_job → extract_combination | station-v1/extract/model/ssp/patch/tech | prepare receipt、现成signals | 原分片NC、sidecar、extraction.json |
| audit | run_job → audit | station-v1/audit/model/ssp/patch/tech | 对应extract receipt | audit.json |
| publish | control_loop | 整轮 | 全部audit及scheduler成功 | 全局索引、coverage_summary、链接 |

prepare 串行打包目录/映射，按内容身份复用一致空间组。extract 每组合顺序处理10个源文件；最小恢复粒度是完成分片。作业包不包含气象或 baseline 计算。

## 数据和完成判据

保留源 time 数值、units、calendar，坐标float64，项目station_id和0/1/-127；缺事件不生成全零变量；activation_mask=off。域外、过远和缺patch保留覆盖记录。

生成与prepare的源索引SHA相同。prepare冻结每个signals的真实路径/stat/sidecar SHA/原identity；extract检查网格、domain与科学文件identity。NC写partial后原子rename，再写sidecar；receipt最后写，含任务、Job ID、账号、代码/包身份、结果JSON的stat和SHA。

成功必须同时满足 scheduler COMPLETED/0:0 与有效receipt，不能凭文件存在或离队判断。audit全量核对时间和站点坐标，验证计数、分片边界，用固定种子独立抽样对比源值并注明范围。publish只读JSON/stat，不在登录节点重读网格事件数组。

## 资源与部署

初始prepare4CPU/4h、extract2CPU/2h、audit2CPU/1h，单进程、库线程为1；分区wzhctest，内存以实测为准。每账号所有项目active≤20，13账号理论上限260；本项目初始全局active≤8。根据实测可通过控制器 `--global-active-limit` 调整当轮上限，实际值记录在台账submission_policy；科学配置和作业包保持冻结。

本地开发用.venv，生成器用标准库python3；科学作业激活 `source /work/home/acbpgywfpz/miniconda3/bin/activate climate`。先本地测试、commit/push，再远程HTTPS clone或干净fast-forward pull；固定SHA，不重置脏目录，不自动建立Git SSH认证。私有HTTPS无认证时报告缺项。

每worker在自身share生成同一包，预建logs；sbatch明确chdir。部署时验证13个SSH alias、共享挂载、读ACL、worker读取中心台账、1500读取各worker产物、quota、余额和跨账号flock。软链接不授予权限。

CSV、网格信号/sidecar及共享准备资产不是Git部署内容。共享挂载直接读取；非共享机器须按分片清单非破坏性传输并验证身份。本控制器要求共享挂载，不隐式复制输入。所有科学准备/抽取/审核只在计算节点执行。

## 提交、监控、判断与行动

控制器在1500运行，一次命令一轮；默认仅监控，显式submit才补槽，同时必须指定已与其他项目共同使用的submit-lock。控制器锁保护台账，共享提交锁保护重计账号全项目active、领取任务、sbatch和保存回执。

查询失败不向该账号提交。新任务先持久化submitting；提交回执不明时记unknown，按job name及历史Job ID查证，不重复提交。依赖逐组合释放；active任务不改派；可重试或未开始任务允许其他有槽账号执行。已完成分片留原账号，以索引引用。

NODE_FAIL/PREEMPTED最多自动重试两次；TIMEOUT/OOM为resource_failure；代码、权限、身份、schema问题需调查修复；CANCELLED不自动复活。资源或代码修改使用新的不可变profile/运行配置，不覆盖在途脚本或绕过任务身份。

启动阶段读取中央ledger的明确EIO异常，仅在Slurm FAILED/1:0、首次尝试且尚无attempt目录时允许一次重试；再次失败须调查。该规则不覆盖运行中I/O错误，max_retries=0时禁用。

每轮更新Job ID、状态、exit、elapsed、MaxRSS、错误日志尾、receipt、attempt、原因与next_action。status-dir的progress.md包含Last checked、分类计数和逐任务表；无变化也更新，未观测任务保留unknown。本地镜像目录为本目录的completion_status，已忽略于Git。

## Pilot和发布

先测密集/稀疏patch、wind/solar、两类日历，再冻结压缩chunk和并发；默认资源仅为pilot起点。初期约15分钟一轮监控，依据实测耗时调整。用户授权提交后才开始运行。

最终大产物保留worker原位，中心outputs为权威链接，索引记录物理路径。报告处理完成率和有效覆盖率，列出NO_SOURCE_PATCH、OUTSIDE_DOMAIN、TOO_FAR，不能将执行成功等同于全站点有效覆盖。
