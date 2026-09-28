# BCSD-v2 格点极端事件 → 全球场站极端事件

已实现场站目录、最近格点映射、原事件的分块抽取、独立审核、分片读取、Slurm 作业生成和单轮提交/监控控制器。输入仅为已完成的 `signals_*.nc` 及元数据。

本轮四模式为 `CANESM5 / MPI-ESM1-2-HR / MRI-ESM2-0 / BCC-CSM2-MR`，三 SSP、两技术、2015–2060；使用 14 个 worker，1500 仅汇总。当前尚未部署、运行远程 pilot 或提交生产作业。

```text
输入：/work/share/acp6varuz3/extreme_grid/grid_v2/
汇总：/work/share/acp6varuz3/extreme_grid/stations_v2/
worker：/work/share/<username>/extreme_grid/stations_v2/
```

## 文件

| 文件 | 用途 |
|---|---|
| [campaign.json](campaign.json) | 模式、CSV、路径、资源和并发配置 |
| [accounts.csv](accounts.csv) | 14 个 worker 和 1 个 aggregator |
| [create_jobs.py](create_jobs.py) | 标准库生成器；仅生成，不提交 |
| [run_job.py](run_job.py) | 计算节点预检、科学入口、完成 receipt |
| [control_loop.py](control_loop.py) | 在1500执行一轮监控、可选补槽和最终发布 |
| [goal.md](goal.md) | 运行契约、依赖、重试和验收 |
| [正式运行提示词](develop-patch-stations_正式运行提示词.md) | CodingAgent 使用 Goal 持续部署、提交、监控和验收的提示词 |
| [实施方案.md](实施方案.md) | 科学口径和设计依据；实际 CLI 以本文为准 |
| [输入核查记录.md](输入核查记录.md) | 输入样本和场站统计 |

科学代码在 `grid_extreme_signals/station_{contract,catalog,mapping,extract,pipeline,reader}.py`；直接入口为 `scripts/prepare_station_extraction.py` 和 `scripts/extract_station_events.py`。

## 数据语义

- 经纬轴最近邻，周期经度，先找全局格点再确定来源 patch。域外、过远、缺 patch 明确登记。
- station_id 沿用 `sha1-20:scenario|tech|lon4|lat4`；坐标 float64。同址跨年记录合并，年度容量另存 CSV.gz。
- `signal_<event>(time,station)` 为压缩 NetCDF4 int8，`0/1/-127` 分别为无事件、事件、缺测。
- 原样保留每模式/技术的时间值、units、calendar；主文件保留投运前气象事件及 activation_year。
- `ssp585 → stations_SSP5-6.0.csv` 沿用现有项目约定。真稀疏 COO 是方案中的可选后续导出，本实现交付直接可读的压缩站点 NC。

## 作业生成

当前全矩阵为 **2,257 个逻辑作业**：prepare 1、extract 1,128、audit 1,128。prepare 将 catalog、网格元数据和 mapping 串行打包，防止共享资产并发写入。每个 extract 顺序处理原有10个时间段，共最多11,280个信号文件；无站点组合写跳过证据。

默认 prepare 为4 CPU/4h，extract 为2 CPU/2h，audit 为2 CPU/1h，均单进程。14账号全项目理论上限280，本任务初始全局并发8；各账号所有项目 active 合计不超过20。初始 logical_owner 轮转分配，运行时按当前槽位动态分配。资源需经 pilot 调整。

本地测试、commit/push 后，在每个 worker 用相同 SHA、campaign 和输入索引生成完整作业包：

```bash
python3 infos/scnet_patchify_stations/create_jobs.py \
  --campaign infos/scnet_patchify_stations/campaign.json \
  --code-sha <部署的40位SHA> \
  --jobs-dir /work/share/<username>/extreme_grid/stations_v2/jobs/stations_v2_pilot_v1 \
  --dry-run
```

确认数量后去掉 dry-run；目标目录必须不存在，不覆盖旧包。本地可用 `--input-index <远程索引副本>` 验证生成；科学任务仍读取 campaign 的真实路径，且索引 SHA 必须一致。

远程采用 HTTPS clone 或干净 fast-forward pull，执行前要求固定 SHA 和干净工作区。提前建立 worker 输出根和 logs，核实 quota、余额、分区内存和以下访问条件：

1. worker 能读网格软链接目标、CSV 和准备资产；1500 能读每个 worker 的结果和 job pack。
2. worker 能读中心 `runtime/ledger.json`、`runtime/prepared.json`。
3. 1500 上14个 SSH Host alias 可无交互执行调度命令；控制器使用1500的共享挂载。
4. 代码路径与 campaign 一致；共享 climate 环境可访问。脚本统一激活 `/work/home/acbpgywfpz/miniconda3/bin/activate climate`。
5. 跨项目共享提交锁已协调并验证跨账号 flock。软链接不授予访问权限。

将任一相同作业包的 manifest 非破坏性复制到1500的 `runtime/job_pack.json`。运行中不修改 campaign 或已提交脚本。

## 监控和提交

在1500运行仅监控模式，初始化并更新台账，不提交：

```bash
python3 infos/scnet_patchify_stations/control_loop.py \
  --pack /work/share/acp6varuz3/extreme_grid/stations_v2/runtime/job_pack.json \
  --status-dir /work/share/acp6varuz3/extreme_grid/stations_v2/completion_status
```

需要提交时追加 `--submit --submit-lock <各并行项目已共同使用的共享锁文件>`。控制器不猜测锁路径；在锁内重查账号全项目 active 数量、领取任务和保存 sbatch 回执。回执丢失记 unknown，查证前不重提。

每次命令一轮，不创建后台服务；初期约15分钟重复，之后按任务时长调整。prepare 成功后发布中心 prepared 清单；extract 成功才释放对应 audit；所有审核成功后发布全局索引。离开 squeue 不等于成功。

中心 `runtime/ledger.json` 为权威台账，status-dir 保存 `ledger.json/progress.md`。本地镜像位于 `infos/scnet_patchify_stations/completion_status/`，已忽略于 Git。

## 产物和恢复

```text
worker-root/
  attempts/<task-id>/<job-id>/shared/...          # 目录、容量、映射
  attempts/<task-id>/<job-id>/outputs/<model>/<ssp>/<source_patch>/<tech>/
    signals_<years>.nc
    signals_<years>.nc.json
    extraction.json
  attempts/<task-id>/<job-id>/extraction.json     # 整个抽取包的结果
  attempts/<task-id>/<job-id>/audit.json
  runtime/receipts/<task-id>/<job-id>.json
  jobs/stations_v2_pilot_v1/
  logs/

aggregate-root/
  runtime/job_pack.json
  runtime/ledger.json
  runtime/prepared.json
  runtime/authoritative_index.json
  runtime/coverage_summary.csv.gz
  outputs/<model>/<ssp>/<source_patch>/<tech>/*  # 权威结果软链接
```

大文件保留在实际 worker；catalog/mapping 的物理路径从 prepared/index 获取。每次重试用新的 Job ID 目录，复用失败包中已完成且身份一致的分片。NC 存在但没有有效 sidecar 时不覆盖。节点失败/抢占最多自动重试两次；OOM、超时、代码/权限/身份错误需调整配置或修复原因。

## 直接接口与读取

独立准备入口：`python scripts/prepare_station_extraction.py --campaign <json> --shared-root <共享资产目录> --code-sha <SHA>`。

独立抽取入口：

```bash
python scripts/extract_station_events.py \
  --prepared <prepared.json> --model CANESM5 --scenario ssp126 \
  --tech wind --patch R03C09 --output-root <本账号输出目录> \
  --code-sha <SHA> --years 2015-2019
```

省略 years 则处理全部源分片；资源与编码来自 prepared 中的冻结配置。两者用于计算节点执行，生产建议通过 run_job 获得完整 receipt。

```python
from grid_extreme_signals.station_reader import open_station_signals

with open_station_signals(
    "/work/share/acp6varuz3/extreme_grid/stations_v2/runtime/authoritative_index.json",
    model="CANESM5", scenario="ssp126", tech="wind",
    station_ids=["<真实station_id>"], years="2030-2034",
) as ds:
    event = ds.signal_icing == 1
    valid = ds.signal_icing.notnull()
```

读取器按映射筛选来源 patch，加载指定子集，不依赖 Dask。全球统计应按索引逐分片/时间块累计，不把全球46年数组全部载入内存。缺来源站点重建为 NaN，并保留 mapping_status。旧损失入口的单文件布局和缺测转 False 行为在其仓库接入时另行调整。

## 验证

```bash
.venv/bin/python -m pytest tests -q
python3 infos/scnet_patchify_stations/create_jobs.py --help
.venv/bin/python scripts/prepare_station_extraction.py --help
.venv/bin/python scripts/extract_station_events.py --help
```

测试覆盖科学三态、日历、风光时间偏移、空间边界、同格多站、跨尝试恢复、空站读取、四模式2,257个脚本的 shell 语法、dry-run、账号上限和丢失回执。根目录直接 pytest 会额外搜集被忽略的 ref_code/tmp；推荐指定本仓库 tests/。
