# 全球场站事件：Climate × Station 独立运行

本目录用于全新的场站事件 campaign。读取已完成的全球网格事件，在三套场站位置上分别抽取；
包括对角和非对角的全部九种 Climate × Station 配对，不读取旧场站运行的完成记录或复用其产物。

| 项目 | 本轮约定 |
|---|---|
| 模型 | CANESM5、MPI-ESM1-2-HR、MRI-ESM2-0、BCC-CSM2-MR |
| Climate / Station | 各 ssp126、ssp245、ssp585，共九配对 |
| 技术 / 空间 / 时间 | wind、solar；47 patch；原生 2015–2060 |
| 科学 unit | 4 × 3 × 3 × 2 × 47 = **3,384** |
| 作业 | prepare 1 → extract+audit 3,384 → publish 1，共 **3,386** |
| 账号 | 14 worker，每账号跨项目最多20个活动作业；1872仅汇总 |
| 结果根 | `/work/home/acjpoxgsdu/extreme_stations_new/<RUN_ID>`，ACL共享，所有worker直接写入 |
| 每账号脚本 | 完整3,386份，14账号共47,404份脚本副本，逻辑任务仍只有3,386个 |
| 前置状态 | BCSD已抽检，不再做全量BCSD检查 |

先读 [运行前准备](运行前准备.md)，运行时以 [goal.md](goal.md) 为操作契约；
[作业组合提交顺序](作业分工/作业组合提交顺序.md) 定义优先级、初始归属和重分配。
[CodingAgent 提示词](CodingAgent_goal运行提示词.md) 可直接交给后续执行代理。

## 文件与入口

- `accounts.csv`：汇总账号和14个运行账号。
- `作业分工/patch_assignment.csv`：47 patch的初始逻辑归属及排序参考。来源为CF参考目录同名表，复制后独立维护。
- `campaign.example.json`：新运行配置模板；填写实际 RUN_ID 后保存到共享 `inputs/campaign.json`。
- `create_jobs.py`：只生成完整脚本包，不提交；没有GCM或情景筛选参数。
- `run_job.py`：计算节点适配器，校验代码、配置、调度claim和依赖，调用现有双情景科学接口，最后写receipt。
- `progress.py`：初始化外部台账或按已验证状态更新极简表。
- `completion_status/progress.md`：本地忽略的运行记录，初始未执行；新checkout通过progress.py重建。

```bash
python3 infos/scnet_patchify_stations_new/create_jobs.py \
  --campaign "$STATION_CAMPAIGN" --code-sha "$CODE_SHA" \
  --jobs-dir "$STATION_WORK_ROOT/jobs/events_new_v1" --dry-run
# 检查计划后去掉 --dry-run，在所有14个worker各自生成完整包。
```

生成器仅需Python标准库；科学任务使用既有climate环境，没有新增依赖。
所有生成脚本字节一致，无 `/work/home/...` 字面路径、无账号专属SBATCH参数。
脚本从 `STATION_ENV_FILE` 读取路径，`sbatch --account` 使用实际运行账号。
环境激活路径也外置，这是用户要求脚本不包含账号home绝对路径的具体实现。

## 产物和恢复

```text
<shared-root>/
  inputs/campaign.json
  attempts/<task_id>/<job_id>/
    shared/prepared.json                  # prepare：catalog、mapping也在shared下
    outputs/<model>/climate_<C>/station_<S>/<patch>/<tech>/signals_<years>.nc[.json]
    extraction.json                       # extract作业
    audit.json                            # 同作业独立源值抽样审核
    publication/runtime/...               # publish的发布候选
  runtime/
    ledger.json
    receipts/<task_id>/<job_id>.json
    unit_locks/<task_id>.lock
    authoritative_index.json
    coverage_summary.csv.gz
    preparation.json
    observations.jsonl
    reassignment_log.jsonl
    cycles.jsonl
    completion_status/progress.md
```

不同账号执行同一unit时使用同一逻辑身份和共享根；JobID决定尝试目录。
同一新campaign内重试可复用已校验完整的时间分片，剩余分片写入新尝试目录；最终索引指向实际权威路径。
不能仅因NC存在就判定成功。一个extract作业顺序完成所有源时间分片并审核，receipt写在审核成功之后。
合法 `SKIPPED_NO_STATIONS` 算完成，有站点但全fill不算空任务。场站产物schema为 `station-extreme-v2`。
按现有十段时间轴，最多33,840个NC；实际数量由非空unit和源分片数决定。

完整发布后使用 `open_station_signals(index, model, climate_scenario, station_scenario, tech, ...)` 读取。
场站CSV固定使用用户指定目录，Station ssp585对应 `stations_SSP5-6.0.csv`。
本轮不重算BCSD、CF或网格事件，也不改变装机字段和事件算法。

## 本地验证

```bash
.venv/bin/python -m pytest tests/test_station_extraction.py \
  tests/test_station_extraction_workflow.py tests/test_station_gather_crop.py \
  tests/test_station_new_workflow.py -q
```

本目录文件和本地测试不代表远程部署或作业已经提交。新的运行状态、输出根和任务前缀均独立于旧目录。
