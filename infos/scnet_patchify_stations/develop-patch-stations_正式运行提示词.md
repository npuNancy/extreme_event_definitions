立即创建并持续执行一个 Goal，不设 token budget：在14个运行账号上，将已完成的BCSD-v2全球网格极端事件抽取为全球场站极端事件，完成部署、pilot、全量提交、监控、审核和发布。本文授权本轮部署与作业提交；按已确认范围自主推进。

一、先读运行契约
仓库为 extreme_event_definitions，分支 develop-patch-grid。先读 AGENTS.md，以及 infos/scnet_patchify_stations 下的 README.md、goal.md、campaign.json、accounts.csv、实施方案.md、输入核查记录.md，再核对 create_jobs.py、run_job.py、control_loop.py 的真实接口。按已验证代码和冻结配置执行。检查工作区和进度，保留已有修改及结果；代码尚未完成真实pilot。

二、范围、账号和路径
一次覆盖 CANESM5、MPI-ESM1-2-HR、MRI-ESM2-0、BCC-CSM2-MR；ssp126/ssp245/ssp585；wind/solar；2015–2060；47个patch。权威输入为 /work/share/acp6varuz3/extreme_grid/grid_v2/runtime/authoritative_index.json，patch定义为同目录patch_manifest.json，信号在上一级outputs下。仅从索引的signals分片抽取，禁止重新做BCSD、读取气象重识别或重算baseline。接受上游结果可用，不在登录节点扫描事件数组。
场站目录为 /work/home/acbw9wpn5k/project_climate_patchify/shared_run_20260913/inputs/stations。三SSP分别用 stations_SSP1-2.6.csv、stations_SSP2-4.5.csv、stations_SSP5-6.0.csv；第三项映射为既有约定。
汇总账号为乌镇1500/scnet-wuzhen-1500/acp6varuz3，最终入口固定 /work/share/acp6varuz3/extreme_grid/stations_v2/。它仅做控制和汇总，不运行科学作业。worker仅用accounts.csv中的14个运行账号，Host与用户名严格取该表。各worker大产物写 /work/share/<username>/extreme_grid/stations_v2/，按attempt/Job ID隔离；代码位置按campaign模板。输入软链接目标、既有网格结果和旧场站结果只读，不清理、不覆盖。

三、准备和部署
本地使用.venv，先运行相关测试、生成器help和小矩阵检查；修复须最小修改、测试、commit/push后部署。记录唯一完整40位SHA、配置和索引hash。远程使用HTTPS clone或干净fast-forward pull；不复制密钥或把令牌放URL。私有HTTPS认证缺失须明确报告；不强制reset脏目录。
逐账号检查余额、quota、分区、共享环境、SSH可达性与路径权限；1500需能无交互SSH到14个worker执行调度命令，且共享挂载可读它们的job pack和产物；worker需能读输入真实目标、站点表、准备资产及1500的runtime台账。软链接不授予权限。预建各自运行根、logs、jobs，验证原子rename和跨账号flock。找出其他并行项目已使用的共享提交锁，复用同一协议，不另建互不协调的锁。
科学准备、映射、抽取和NC审核全部放Slurm计算节点，统一激活 source /work/home/acbpgywfpz/miniconda3/bin/activate climate。登录节点只做Git、轻量JSON/stat、脚本、链接和调度查询。输入依赖共享挂载，缺挂载先解决访问。

四、pilot与正式作业包
先做真实pilot：选密集、稀疏patch，覆盖风/光、两种日历及缺测/边界，先单时间分片再完整10分片组合，测逐值一致性、RSS、读写耗时和压缩大小。隔离pilot的配置、索引副本、输出和台账，保持原索引不变；其身份与正式任务不同则不冒充正式完成。独立入口prepare_station_extraction.py、extract_station_events.py的参数以help为准。pilot须限制范围，禁止直接全量submit。
通过后冻结正式资源profile。起点为prepare 4CPU/4h，extract 2CPU/2h，audit 2CPU/1h，单进程、库线程1，分区wzhctest，输出chunk 240×256、zlib level1；按实测调整。
每个worker以相同SHA、campaign和输入索引生成全部作业包：python3 infos/scnet_patchify_stations/create_jobs.py --campaign infos/scnet_patchify_stations/campaign.json --code-sha <SHA> --jobs-dir <本账号运行根>/jobs/<profile> --dry-run。核对后去掉dry-run；目录必须新建，不覆盖已有包。检查所有脚本bash -n及manifest/script hash，14份包一致。完整库存应为2257作业：prepare 1、extract 1128、audit 1128；最多11280个输出信号分片。prepare串行生成目录和缓存映射，extract每组合顺序处理10段。把同版manifest发布到1500的runtime/job_pack.json。

五、持续提交和监控
控制器在1500运行：python3 infos/scnet_patchify_stations/control_loop.py --pack /work/share/acp6varuz3/extreme_grid/stations_v2/runtime/job_pack.json --status-dir /work/share/acp6varuz3/extreme_grid/stations_v2/completion_status。先仅监控初始化并核对台账；提交轮追加 --submit --submit-lock <已验证共享锁>。命令每次执行一轮，Agent须持续安排后续轮次。
每账号所有项目的pending/running及过渡状态合计最多20，14账号理论上限280；本项目初始全局并发8。按pilot的共享I/O吞吐决定是否扩容，不机械填满280。提交锁内重查全账号队列、依赖、unit claim和脚本身份；sbatch显式使用实际用户名和本账号chdir。submitting/unknown先查证，回执丢失不盲重提；active任务不改派，空闲账号可接未开始或可重试任务。
按prepare→extract→audit依赖逐组合释放；prepare成功才发布中心prepared清单。每轮采集squeue、sacct、exit、elapsed、MaxRSS、日志尾及receipt/产物证据，再分类、补槽/重试并记录。离开squeue不是成功，活动任务缺最终文件不是失败。初始15分钟一轮，按实测任务时长调整；等待期间保持回应。
NODE_FAIL/PREEMPTED最多自动重试2次；OOM/TIMEOUT先查资源和分片耗时；权限、身份、schema、代码问题停止受影响链并修复。不要原配置无限重试，也不自动复活CANCELLED。新尝试写新Job ID目录，完整且身份一致的分片保留原账号并通过清单复用；partial或无sidecar的NC不覆盖。

六、科学审核和发布
必须保持最近规则格点的0/1/-127、原始time数值/units/calendar、float64场站坐标和稳定station_id；不插值布尔事件、不把缺测变零、不强行对齐风光时间轴。主文件activation_mask=off，投运年只作元数据。domain外、过远、缺patch均有明确覆盖状态；没有dust等事件时保留skipped原因，不造全零变量。
成功需要Slurm COMPLETED/0:0、身份匹配的receipt、原子完成NC和sidecar。审核核对全部时间与场站坐标、分片连续性和计数，并独立抽样对比源值；报告抽样范围。所有组合齐备后发布中心runtime/authoritative_index.json、coverage_summary.csv.gz和outputs权威链接；大文件留worker原位。
每轮无变化也更新远程及本地 infos/scnet_patchify_stations/completion_status/progress.md，保留Last checked、分类计数、逐任务证据、下一步和下次检查时间。分别报告处理完成率及有效覆盖率，未匹配站点不能静默丢弃。
仅在所有选定组合成功或有有效空站跳过证据、全部站点去向可解释、中心入口可访问且没有在途或未解决状态时将Goal标为完成。外部认证、权限、配额等阻塞要记录准确原因和已完成工作，不伪报成功。最终汇报代码SHA、作业与分片计数、结果入口、覆盖缺口和验证范围。
