# 统一 step2 场站级极端天气识别运行目标

## 目标

使用 Regional BCSD 气象数据，在一个可重试的 Slurm 作业中生成普通极端事件和低资源事件。
低资源事件直接使用 BCSD 的 `wind_ms` 或 `rsds`，在当前作业的 `2015-2024` 基线期内计算
月×小时气候态和 P5 阈值；不需要 CF、ERA5Land CF 阈值或 step1 前置阶段。

科学入口：`step2_complete_extreme_events.py`

作业单位：`model × region × scenario × tech × years`。其中 wind/solar 输出独立，可并行运行。
生产年份必须是连续范围并覆盖 `2015-2024`，默认使用 `2015-2060`。

## 输入与输出

输入：

```text
~/data/bcsd_outputs
~/data/stations/stations_SSP1-2.6.csv
~/data/stations/stations_SSP2-4.5.csv
~/data/stations/stations_SSP5-6.0.csv
~/data/maps/natural_earth/ne_110m_admin_0_countries.shp
```

输出：

```text
~/extreme_event_definitions/outputs/station_signals/
  regional_bcsd/<MODEL>/<REGION>/<SCENARIO>/
    station_signals_<TECH>_<MODEL>_<REGION>_<SCENARIO>_<START>-<END>.nc
```

有场站单元必须一次写出全部适用事件；无场站单元成功跳过且不要求输出文件。

## 作业生成

```bash
python3 infos/hpc_step2/create_step2_jobs.py \
  --models NESM3,MIROC-ES2H \
  --regions Germany \
  --scenarios ssp126,ssp245,ssp585 \
  --techs wind,solar \
  --years 2015-2060
```

生成器只写外部 jobs/logs 和 manifest，不调用 `sbatch`。生成前使用 `--dry-run`，生成后对所有
脚本执行 `bash -n`。默认资源为 `wzhctest`、6 核；核数主要用于申请内存，Python 计算本身不因
核数线性加速。作业脚本固定使用：

```bash
source /work/home/acbpgywfpz/miniconda3/bin/activate climate
```

账号所有活动作业上限为 20，提交必须持有 `$HOME/.bcsd_submit.lock`。

## 监控与完成条件

监控入口：

```text
infos/hpc_step2/completion_status/monitor_step2_jobs.py
```

监控器从远端 manifest 读取单元，查询 `squeue`/`sacct` 和轻量级输出文件状态，并更新：

```text
infos/hpc_step2/completion_status/
```

有场站单元：Slurm 成功结束且目标 NetCDF 非空；无场站单元：Slurm 成功结束且 manifest 标记
`has_stations=false`。失败、OOM、超时和不完整输出不自动重试，需根据日志和 accounting 证据
决定资源调整或代码修复。每轮检查都要更新 `progress.md`。

旧 E1/E2 manifest、旧 CF-derived 输出和旧阈值文件不作为新 campaign 的完成证据。

## 运行边界

- 生成器和 bash 语法检查在登录节点完成；科学计算在 Slurm 计算节点完成。
- 不删除或清空 `data/`、已有输出、CF 缓存、阈值目录或其他作业。
- 远端仓库使用 HTTPS fast-forward-only pull；不依赖远端持久 SSH Git key。
- 本文是通用运行契约，不绑定具体服务器、模式、区域、情景或批次。
