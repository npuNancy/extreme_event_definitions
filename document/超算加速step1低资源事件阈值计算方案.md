# 超算加速 step1 低资源事件阈值计算方案

## 1. 文档状态

- 工作分支：`hpc-step1-low-resource-acceleration`
- 当前阶段：超算拆分代码、E1 产物、远端 Python 环境和代码仓库均已准备；需要先把作业生成器的 CF 根目录改为 `$HOME/data/cfs` 并重新生成 Slurm 脚本，同时等待 wind 缺失月份上传完成，之后才能提交 E2a 前期测试作业。
- 参考文档：
  - `/data6/yanxiaokai/project_climate/bcsd/document/超算BCSD运行优化代码修改计划.md`
  - `/data6/yanxiaokai/project_climate/bcsd/infos/goal.md`
- 安全约束：不把网页密码、rayfile token、账号密码表写入 Git 文档、脚本、日志或提交信息。上传命令只能记录脱敏模板。

## 2. 目标

使用昆山超算的两台服务器分别计算 step1 的 ERA5Land 低资源阈值：

| 技术类型 | 服务器 | Host | 账号 | 输入数据 |
|---|---|---|---|---|
| wind | 昆山185 | `scnet-kunshan-185` | `acbw9wpn5k` | `~/data/cfs/CFs_of_wind_ERA5Land` |
| solar | 昆山199 | `scnet-kunshan-199` | `aclym5felp` | `~/data/cfs/CFs_of_solar_ERA5Land` |

每台服务器只处理自己的技术类型，但都需要对三个 SSP 场站输出低资源阈值：

```text
ssp126
ssp245
ssp585
```

最终应得到 6 个阈值文件：

```text
ssp126 wind
ssp245 wind
ssp585 wind
ssp126 solar
ssp245 solar
ssp585 solar
```

输出 schema 继续沿用当前 step1 稀疏阈值文件：

```text
clim(month=12, hour=24, station)
threshold(station)
valid_count(station)
station_lon/station_lat/station_type
era5_lat_idx/era5_lon_idx/weight
```

## 3. 远端环境当前检查结果

### 3.1 scnet-kunshan-185

已确认：

```text
Host: login07
User: acbw9wpn5k
Home: /public/home/acbw9wpn5k
Slurm: /opt/gridview/slurm/bin/sbatch, /opt/gridview/slurm/bin/squeue
/data: 约 29T，总体可用约 26T
/public/home: 共享文件系统可用
uv: /public/home/acbw9wpn5k/.local/bin/uv
共享 venv: /public/home/acbw9wpn5k/.venv
Python: 3.12.13
```

当前准备状态：

```text
代码分支：hpc-step1-low-resource-acceleration
代码提交：27705c1e919c1cbdb2b7c693ac4ad5bfe774a758
E1 产物：已同步并通过 SHA-256 校验
Slurm 脚本：已生成，但旧脚本仍使用错误的 /data/cfs；修正生成器后必须重新生成
共享 venv：step1 入口依赖已安装并通过导入检查
```

wind 数据位于 `~/data/cfs/CFs_of_wind_ERA5Land`。当前共发现 124 个 `wind_cf_*.nc`，其中2015-2024基准期为116/120，缺少：

```text
2015-11
2018-06
2019-08
2022-06
```

缺失月份补齐前不提交 wind E2a 作业。

### 3.2 scnet-kunshan-199

已确认：

```text
Host: login09
User: aclym5felp
Home: /public/home/aclym5felp
Slurm: /opt/gridview/slurm/bin/sbatch, /opt/gridview/slurm/bin/squeue
/data: 约 29T，总体可用约 22T
/public/home: 共享文件系统可用
可访问共享 venv: /public/home/acbw9wpn5k/.venv
Python: 3.12.13
```

当前准备状态：

```text
代码分支：hpc-step1-low-resource-acceleration
代码提交：27705c1e919c1cbdb2b7c693ac4ad5bfe774a758
E1 产物：已同步并通过 SHA-256 校验
Slurm 脚本：已生成，但旧脚本仍使用错误的 /data/cfs；修正生成器后必须重新生成
共享 venv：可访问且 step1 入口依赖已通过导入检查
```

solar 数据位于 `~/data/cfs/CFs_of_solar_ERA5Land`。当前共发现132个 `solar_cf_*.nc`，2015-2024基准期120个月完整。

## 4. 总体设计

本项目保留两个 step1 入口体系：

1. 普通完整入口：`step1_low_resource_thresholds.py`
   - 用于本地、单 SSP、调试或兼容已有流程。
   - 该入口可以逐 SSP 生成场站 CF 缓存，并允许 `ssp245/ssp585` 从 `ssp126` 阈值文件复用交集场站。
   - 不参与昆山超算生产主流程，不被超算作业生成器或监控程序调用。
2. 超算拆分入口：`step1_split_E1/E2a/E2b/E3`
   - 这是昆山超算生产运行的唯一方案。
   - 所有超算生产作业只调用 E1/E2a/E2b/E3 对应入口，禁止转调 `step1_low_resource_thresholds.py`。
   - 不逐 SSP 从 ERA5Land CF 文件抽取场站 CF。
   - 先合并三个 SSP 的所有场站，按 `(lon, lat, type)` 去重，生成并集场站表和 SSP 到并集的索引映射。
   - 每个技术类型按年月分块从 ERA5Land CF 中抽取并集场站 CF，生成分块缓存。
   - 合并分块缓存得到完整 union station CF cache。
   - 再从完整 union cache 按 SSP 子集分别计算 6 个最终阈值文件。

整体 step1 阈值使用的 ERA5Land CF 年份固定为：

```text
2015-2024，共 10 年，120 个月
```

超算版拆分为四个明确步骤：

```text
step1_split_E1_union_stations.py
  本地执行：计算 ssp126/ssp245/ssp585 场站并集和映射，结果保存到 outputs/。

step1_split_E2a_extract_union_station_cf_monthly.py
  远端执行：每个 job 处理一个 year 或一个 year-month/month-range，可通过参数指定。

step1_split_E2b_merge_union_station_cf_cache.py
  远端执行：合并所有月度/年度分块缓存为完整 union station CF cache。

step1_split_E3_thresholds_from_union_cache.py
  远端执行：从 union station CF cache 按 SSP 映射计算最终稀疏阈值文件。
```

超算生产运行流程：

```text
阶段 A：远端一次性配置
阶段 B：本地执行 step1_split_E1，生成三个 SSP 场站并集
阶段 C：同步 E1 结果到两台昆山服务器
阶段 D：前期测试 E2a 的 2015 年 1 月作业，标定核心数和耗时
阶段 E：为 wind/solar 分别生成 1 个 E2a 前期测试作业和 20 个 E2a 正式分块作业
阶段 F：监控程序识别 2015.1 测试缓存有效且 20 个正式 E2a 作业全部完成后提交 E2b
阶段 G：监控程序识别 E2b 完成后提交 E3
阶段 H：检查输出完整性
阶段 I：记录完成状态和核时
阶段 J：同步最终阈值文件回本地
```

本阶段不引入 MPI，也不做跨节点 Python 分布式计算。每个 Slurm 作业仍是单节点任务；wind 和 solar 的并行来自两台服务器各自独立运行。

## 5. 目录约定

### 5.1 远端目录

两台服务器均使用相同项目目录结构：

```text
/public/home/<USER>/extreme_event_definitions/
  data/
    stations/
  jobs/
    step1_low_resource/
  logs/
    step1/
    slurm/
  outputs/
    cache/
      era5land_union_station_cf/
        union_stations/
        time_chunks/
        merged_cache/
    low_resource_thresholds/
      sparse_station_ERA5Land_2015-2024/
```

ERA5Land CF 数据位于每个账号自己的 `~/data/cfs/`：

```text
scnet-kunshan-185:/public/home/acbw9wpn5k/data/cfs/CFs_of_wind_ERA5Land
scnet-kunshan-199:/public/home/aclym5felp/data/cfs/CFs_of_solar_ERA5Land
```

作业中通过参数指定：

```bash
--cf_root "$HOME/data/cfs"
```

### 5.2 本地记录目录

建议新增：

```text
infos/hpc_step1/
  create_jobs_kunshan.py
  completion_status/
    completion_scnet-kunshan-185.csv
    completion_scnet-kunshan-199.csv
    usage_scnet-kunshan-185.csv
    usage_scnet-kunshan-199.csv
    latest_snapshot.json
```

`infos/hpc_step1/create_jobs_kunshan.py` 是本地模板；每台服务器可以复制到家目录形成自己的可编辑副本：

```text
~/create_step1_jobs_kunshan.py
```

这沿用 BCSD 的经验：远端专用作业配置允许按服务器修改，但主程序代码仍在本地开发、提交、推送，远端只做 fast-forward 更新。

### 5.3 本地 E1 输出目录

`step1_split_E1_union_stations.py` 在本地执行，输出必须放在 `outputs/` 下：

```text
outputs/cache/era5land_union_station_cf/union_stations/
  stations_union_ssp126_ssp245_ssp585.csv
  station_index_map_ssp126.csv
  station_index_map_ssp245.csv
  station_index_map_ssp585.csv
  union_stations_manifest.json
  e2a_chunk_plan_2015-2024.csv
```

其中：

1. `stations_union_ssp126_ssp245_ssp585.csv` 保存三个 SSP 去重后的并集场站。
2. `station_index_map_<scenario>.csv` 保存对应 SSP 原始场站顺序到并集场站顺序的映射。
3. `union_stations_manifest.json` 记录输入场站文件、去重键、行数、生成时间和代码版本。
4. `e2a_chunk_plan_2015-2024.csv` 是 E2a 唯一作业清单，固定记录每个技术类型需要处理的 21 个时间缓存块。表头为：

```csv
chunk_id,year_month_start,year_month_end,is_probe,expected_month_count,job_name,cache_file
```

清单第一行为 `2015-01` 测试块，之后是 `2015-02~2015-06` 和 2015-2024 各半年的 20 个正式块。`expected_month_count` 总和必须为 120，`chunk_id` 和 `cache_file` 必须唯一。由于 E1 与技术类型无关，`job_name` 和 `cache_file` 使用 `{tech}` 占位符，由 wind/solar 作业生成器展开。作业生成器、监控程序和 E2b 都读取该清单，不在各自代码中重复维护时间范围。

E1 输出生成后，需要同步到两台远端服务器同一路径：

```text
~/extreme_event_definitions/outputs/cache/era5land_union_station_cf/union_stations/
```

同步命令示例：

```bash
rsync -av \
  outputs/cache/era5land_union_station_cf/union_stations/ \
  scnet-kunshan-185:/public/home/acbw9wpn5k/extreme_event_definitions/outputs/cache/era5land_union_station_cf/union_stations/

rsync -av \
  outputs/cache/era5land_union_station_cf/union_stations/ \
  scnet-kunshan-199:/public/home/aclym5felp/extreme_event_definitions/outputs/cache/era5land_union_station_cf/union_stations/
```

## 6. 数据上传

用户当前正在上传：

```text
本地 wind  源：/data6/yanxiaokai/project_climate/extreme_event_definitions/data/cfs/CFs_of_wind_ERA5Land
远端 wind  目标：scnet-kunshan-185:/public/home/acbw9wpn5k/data/cfs/

本地 solar 源：/data6/yanxiaokai/project_climate/extreme_event_definitions/data/cfs/CFs_of_solar_ERA5Land
远端 solar 目标：scnet-kunshan-199:/public/home/aclym5felp/data/cfs/
```

脱敏上传命令模板：

```bash
# scnet-kunshan-185 wind
rayfile-c \
  -a ksefile.hpccube.com \
  -P 65245 \
  -u acbw9wpn5k \
  -w '<RAYFILE_TOKEN_185>' \
  -tm -no-meta -symbolic-links follow \
  -retry 10 -retrytimeout 30 \
  -o upload \
  -d /public/home/acbw9wpn5k/data/cfs/ \
  -s /data6/yanxiaokai/project_climate/extreme_event_definitions/data/cfs/CFs_of_wind_ERA5Land

# scnet-kunshan-199 solar
rayfile-c \
  -a ksefile.hpccube.com \
  -P 65245 \
  -u aclym5felp \
  -w '<RAYFILE_TOKEN_199>' \
  -tm -no-meta -symbolic-links follow \
  -retry 10 -retrytimeout 30 \
  -o upload \
  -d /public/home/aclym5felp/data/cfs/ \
  -s /data6/yanxiaokai/project_climate/extreme_event_definitions/data/cfs/CFs_of_solar_ERA5Land
```

上传完成后检查：

```bash
# wind on scnet-kunshan-185
find "$HOME/data/cfs/CFs_of_wind_ERA5Land" -maxdepth 1 -type f -name 'wind_cf_*.nc' | wc -l
ls -lh "$HOME/data/cfs/CFs_of_wind_ERA5Land/wind_cf_2015_01.nc"
ls -lh "$HOME/data/cfs/CFs_of_wind_ERA5Land/wind_cf_2024_12.nc"

# solar on scnet-kunshan-199
find "$HOME/data/cfs/CFs_of_solar_ERA5Land" -maxdepth 1 -type f -name 'solar_cf_*.nc' | wc -l
ls -lh "$HOME/data/cfs/CFs_of_solar_ERA5Land/solar_cf_2015_01.nc"
ls -lh "$HOME/data/cfs/CFs_of_solar_ERA5Land/solar_cf_2024_12.nc"
```

2015-2024 完整基准期应为 120 个月文件。若文件数不足，不提交生产作业。

## 7. 新服务器一次性配置

### 7.1 Clone 代码

分别在两台服务器执行：

```bash
ssh scnet-kunshan-185
```

或：

```bash
ssh scnet-kunshan-199
```

检查项目目录：

```bash
ls -ld ~/extreme_event_definitions 2>/dev/null || true
```

如果不存在：

```bash
git clone \
  --branch hpc-step1-low-resource-acceleration \
  --single-branch \
  <REPO_URL> \
  ~/extreme_event_definitions
```

如果目录已存在但不是 Git 仓库，不能删除或覆盖；先检查是否有 `data/`、`outputs/`、`logs/` 或其他手工文件。

### 7.2 更新已有仓库

```bash
cd ~/extreme_event_definitions
git rev-parse --abbrev-ref HEAD
git status --short
```

只有当前分支是 `hpc-step1-low-resource-acceleration` 且工作区干净时，才执行：

```bash
git pull --ff-only origin hpc-step1-low-resource-acceleration
git rev-parse --short HEAD
```

如果远端工作区不干净，只报告，不使用 `git reset --hard`、`git checkout --` 等破坏性命令。

### 7.3 配置 shell

建议在 `~/.bashrc` 中追加，已有则不重复添加：

```bash
export PS1='[\u@\h \w]\$ '
alias sq='squeue --sort=j -o "%.18i %.9P %.50j %.8u %.2t %.10M %.6D %R"'
alias ll='ls -alh'
```

生效：

```bash
source ~/.bashrc
```

### 7.4 配置 Python 环境

当前 185 已有共享 venv：

```bash
source /public/home/acbw9wpn5k/.venv/bin/activate
python -V
```

该环境可以被199访问，step1所需依赖已经在185上完成安装，并在两台服务器分别通过导入检查。后续需要更新依赖时仍只在185上操作，让199复用同一个环境：

```bash
ssh scnet-kunshan-185
source /public/home/acbw9wpn5k/.venv/bin/activate
cd ~/extreme_event_definitions

uv pip install -r requirements.txt
```

当前已确认的关键依赖版本：

```text
numpy 2.2.6
pandas 2.2.3
netCDF4 1.7.2
h5py 3.13.0
global-land-mask 1.0.0
scipy 1.15.3
xarray 2025.1.2
shapely 2.0.7
```

依赖检查：

```bash
source /public/home/acbw9wpn5k/.venv/bin/activate
python - <<'PY'
mods = [
    "numpy",
    "pandas",
    "netCDF4",
    "h5py",
    "global_land_mask",
]
missing = []
for name in mods:
    try:
        __import__(name)
    except Exception as exc:
        missing.append((name, repr(exc)))
if missing:
    raise SystemExit(f"缺少依赖: {missing}")
print("step1 Python 环境正常")
PY
```

作业脚本中必须显式激活：

```bash
source /public/home/acbw9wpn5k/.venv/bin/activate
```

不要使用裸 `python`。

## 8. 作业生成器设计

新增脚本：

```text
infos/hpc_step1/create_jobs_kunshan.py
```

职责：

1. 按服务器生成对应技术类型的 E2a/E2b/E3 Slurm 作业。
2. 自动写入 `--cf_root "$HOME/data/cfs"`；不得使用节点级 `/data/cfs`。
3. 自动写入 E1 生成的并集场站表和 SSP 映射文件路径。
4. 读取 E1 生成的 `e2a_chunk_plan_2015-2024.csv`，自动生成 1 个 E2a 前期测试作业和 20 个 E2a 正式分块作业，共 21 个 E2a 时间缓存块。
5. 自动设置 E2a 时间分块缓存、E2b 完整 union cache、最终阈值输出路径和日志路径；生产作业默认不传 `--overwrite`。
6. 生成 E2b 和 E3 作业脚本，但不在初次提交时提交它们；监控程序在依赖条件满足后提交。
7. 只生成脚本，不自动提交。
8. 默认拒绝覆盖已有作业脚本；传入 `--force` 才覆盖。
9. 生成后打印后续检查和提交命令。
10. 生成器必须校验清单正好包含 21 行任务、覆盖 120 个月且时间范围无重叠、无缺月。

推荐生成出的远端脚本：

```text
~/jobs/step1_low_resource/
  job_step1_split_E2a_extract_union_cf_wind_2015_01.sh
  job_step1_split_E2a_extract_union_cf_wind_2015_02_2015_06.sh
  job_step1_split_E2a_extract_union_cf_wind_2015_07_2015_12.sh
  ...
  job_step1_split_E2a_extract_union_cf_wind_2024_07_2024_12.sh
  job_step1_split_E2b_merge_union_cf_wind.sh
  job_step1_split_E3_thresholds_wind.sh
  submit_step1_split_E2a_initial_wind.sh
  submit_step1_split_E2a_batches_wind.sh

~/jobs/step1_low_resource/
  job_step1_split_E2a_extract_union_cf_solar_2015_01.sh
  job_step1_split_E2a_extract_union_cf_solar_2015_02_2015_06.sh
  job_step1_split_E2a_extract_union_cf_solar_2015_07_2015_12.sh
  ...
  job_step1_split_E2a_extract_union_cf_solar_2024_07_2024_12.sh
  job_step1_split_E2b_merge_union_cf_solar.sh
  job_step1_split_E3_thresholds_solar.sh
  submit_step1_split_E2a_initial_solar.sh
  submit_step1_split_E2a_batches_solar.sh
```

每个技术类型的两个 E2a submit 脚本职责不同：

1. `submit_step1_split_E2a_initial_<tech>.sh`：只提交 `2015-01` 一个前期测试作业，用于测量 MaxRSS 和运行时间，据此调整正式作业的 `kernel_num` 与 `--time`。
2. `submit_step1_split_E2a_batches_<tech>.sh`：测试作业完成、输出有效且资源参数调整后，提交其余20个正式分块作业；不重复提交 `2015-01`。

两个 submit 脚本不能连续直接执行。必须先运行 initial，等待测试完成并完成资源校准，再运行 batches。

服务器分工由生成器内置默认值控制：

```python
SERVER_CONFIG = {
    "scnet-kunshan-185": {
        "tech": "wind",
        "user": "acbw9wpn5k",
        "project_dir": "/public/home/acbw9wpn5k/extreme_event_definitions",
        "python_activate": "/public/home/acbw9wpn5k/.venv/bin/activate",
    },
    "scnet-kunshan-199": {
        "tech": "solar",
        "user": "aclym5felp",
        "project_dir": "/public/home/aclym5felp/extreme_event_definitions",
        "python_activate": "/public/home/acbw9wpn5k/.venv/bin/activate",
    },
}
```

SSP 场站映射：

```python
SCENARIOS = {
    "ssp126": "data/stations/stations_SSP1-2.6.csv",
    "ssp245": "data/stations/stations_SSP2-4.5.csv",
    "ssp585": "data/stations/stations_SSP5-6.0.csv",
}
```

E1 产物路径：

```python
UNION_STATION_DIR = "outputs/cache/era5land_union_station_cf/union_stations"
UNION_STATION_FILE = f"{UNION_STATION_DIR}/stations_union_ssp126_ssp245_ssp585.csv"
UNION_MAP_FILES = {
    "ssp126": f"{UNION_STATION_DIR}/station_index_map_ssp126.csv",
    "ssp245": f"{UNION_STATION_DIR}/station_index_map_ssp245.csv",
    "ssp585": f"{UNION_STATION_DIR}/station_index_map_ssp585.csv",
}
```

### 8.1 Slurm 资源建议

昆山节点有类似 BCSD 的 20 个作业限制，应按“已提交 + 排队 + 运行”一起控制。E2a 的正式分块作业正好是 20 个，因此同一台服务器在 E2a 正式阶段不再额外提交 E2b/E3；E2b 和 E3 由监控程序在整点检查时顺序提交。

初始资源建议：

| 阶段 | 技术类型 | kernel_num (`#SBATCH -n`) | time | 备注 |
|---|---|---:|---:|---|
| E2a 测试 2015.1 | wind | 待测试 | 4:00:00 | 用 2015 年 1 月标定内存和时间 |
| E2a 测试 2015.1 | solar | 待测试 | 4:00:00 | 用 2015 年 1 月标定内存和时间 |
| E2a 正式分块 | wind | 按测试结果调整 | 24:00:00 | wind 原生 chunk 为 72h，单 chunk 解压后约 1.78GB |
| E2a 正式分块 | solar | 按测试结果调整 | 24:00:00 | solar 原生 chunk 为 24h，单 chunk 解压后约 593MB |
| E2b 合并完整缓存 | wind | 4 | 8:00:00 | 只合并 E2a 分块缓存 |
| E2b 合并完整缓存 | solar | 4 | 8:00:00 | 只合并 E2a 分块缓存 |
| E3 从缓存算阈值 | wind | 4 | 8:00:00 | 不再读取 ERA5Land 全球 CF |
| E3 从缓存算阈值 | solar | 4 | 8:00:00 | 不再读取 ERA5Land 全球 CF |

如果平台内存按核分配，按 BCSD 经验可先按约 `3.5 GB/核` 估算。前期测试结束后，根据 `sacct` 的 `MaxRSS` 按 25% 余量调整正式 E2a 的核心数：

```text
kernel_num = ceil(MaxRSS_GB * 1.25 / 3.5)
```

作业脚本应包含：

```bash
#SBATCH -N 1
#SBATCH -n <KERNEL_NUM>
#SBATCH --time=<TIME_LIMIT>
#SBATCH --job-name=step1_E2a_<tech>_<yyyymm_range> 或 step1_E2b_<tech> 或 step1_E3_<tech>
#SBATCH --output=/public/home/<USER>/extreme_event_definitions/logs/slurm/step1_<stage>_<tech>_%j.out
```

这里按用户当前要求使用 `#SBATCH -n {kernel_num}` 表示申请进程数。Python 代码本身仍应按单进程运行；该参数主要用于向平台申请足够核数和内存。

是否需要 `--partition` 由昆山默认分区决定。生成器支持 `--partition`，但默认不强制写入。

### 8.2 E1：本地计算三个 SSP 场站并集

E1 在本地执行，不需要超算。示例命令：

```bash
python step1_split_E1_union_stations.py \
  --stations_csv_ssp126 data/stations/stations_SSP1-2.6.csv \
  --stations_csv_ssp245 data/stations/stations_SSP2-4.5.csv \
  --stations_csv_ssp585 data/stations/stations_SSP5-6.0.csv \
  --output_dir outputs/cache/era5land_union_station_cf/union_stations \
  --key lon,lat,type
```

E1 必须检查：

1. 三个 SSP 场站 CSV 都可读取。
2. `type` 只包含 `wind/solar`。
3. 并集表中 `(lon,lat,type)` 不重复。
4. 三个映射文件都能把原 SSP 场站顺序映射回并集表。
5. 输出 manifest 记录输入文件大小、mtime、sha256 或等价指纹。

### 8.3 E2a：远端分块抽取 union station CF cache

E2a 每个 job 处理一个 year 或一个 year-month/month-range，由参数指定。

前期测试只提交 2015 年 1 月：

```bash
python step1_split_E2a_extract_union_station_cf_monthly.py \
  --cf_root "$HOME/data/cfs" \
  --union_stations_csv outputs/cache/era5land_union_station_cf/union_stations/stations_union_ssp126_ssp245_ssp585.csv \
  --tech wind \
  --year_month_start 2015-01 \
  --year_month_end 2015-01 \
  --threshold_interp nearest_valid \
  --output_dir outputs/cache/era5land_union_station_cf/time_chunks
```

solar 测试作业只需把 `--tech wind` 换成 `--tech solar`。

`--overwrite` 只允许在这个一次性前期测试被明确判定需要重跑，并由人工确认旧缓存不再使用时传入。首次测试提交和所有生产作业默认都不传该参数；目标缓存已存在时程序应拒绝覆盖并以非零状态退出。

E2a 正式作业以 6 个月为一个作业。由于前期测试已经完成 2015 年 1 月，2015 年第一个正式作业只处理 2015 年 2-6 月。每个技术类型生成 1 个测试作业和 20 个正式分块作业：

```text
2015.1
2015.2~2015.6
2015.7~2015.12
2016.1~2016.6
2016.7~2016.12
...
2024.1~2024.6
2024.7~2024.12
```

其中 `2015.1` 是前期测试作业。若测试作业输出通过校验，正式提交阶段直接复用该分块缓存，不重复提交 2015 年 1 月。正式批量提交的是除 `2015.1` 以外的 20 个作业。因此，每个技术类型实际必须生成并保留 **21 个 E2a 时间缓存块**：1 个测试缓存块加 20 个正式缓存块，共同覆盖 2015-2024 的 120 个月。

正式分块作业示例：

```bash
python step1_split_E2a_extract_union_station_cf_monthly.py \
  --cf_root "$HOME/data/cfs" \
  --union_stations_csv outputs/cache/era5land_union_station_cf/union_stations/stations_union_ssp126_ssp245_ssp585.csv \
  --tech wind \
  --year_month_start 2016-01 \
  --year_month_end 2016-06 \
  --threshold_interp nearest_valid \
  --output_dir outputs/cache/era5land_union_station_cf/time_chunks
```

E2a 是唯一读取 ERA5Land 全球 CF 文件的阶段。三个 SSP 不再分别抽取。
正式 E2a 作业不得默认传 `--overwrite`；需要重跑某一块时，先确认旧作业已结束并隔离或删除损坏缓存，再由人工显式传入。

### 8.4 E2b：远端合并完整 union station CF cache

E2b 在监控程序确认 `2015.1` 测试缓存有效、20 个 E2a 正式作业全部完成且分块缓存全部通过校验后提交。

```bash
python step1_split_E2b_merge_union_station_cf_cache.py \
  --time_chunk_dir outputs/cache/era5land_union_station_cf/time_chunks \
  --chunk_plan_csv outputs/cache/era5land_union_station_cf/union_stations/e2a_chunk_plan_2015-2024.csv \
  --tech wind \
  --baseline_years 2015-2024 \
  --threshold_interp nearest_valid \
  --output_cache outputs/cache/era5land_union_station_cf/merged_cache/station_cf_union_wind_ERA5Land_2015-2024_nearest_valid.nc
```

solar 服务器只需把 `--tech wind` 换成 `--tech solar`，并把 cache 文件改成 solar。

### 8.5 E3：远端从完整 union cache 计算三个 SSP 阈值

E3 在监控程序确认 E2b 完成且完整 union cache 通过校验后提交。

```bash
python step1_split_E3_thresholds_from_union_cache.py \
  --union_station_cf_cache outputs/cache/era5land_union_station_cf/merged_cache/station_cf_union_wind_ERA5Land_2015-2024_nearest_valid.nc \
  --union_stations_csv outputs/cache/era5land_union_station_cf/union_stations/stations_union_ssp126_ssp245_ssp585.csv \
  --index_map_ssp126 outputs/cache/era5land_union_station_cf/union_stations/station_index_map_ssp126.csv \
  --index_map_ssp245 outputs/cache/era5land_union_station_cf/union_stations/station_index_map_ssp245.csv \
  --index_map_ssp585 outputs/cache/era5land_union_station_cf/union_stations/station_index_map_ssp585.csv \
  --tech wind \
  --baseline_years 2015-2024 \
  --output_dir outputs/low_resource_thresholds/sparse_station_ERA5Land_2015-2024
```

solar 服务器只需把 `--tech wind` 换成 `--tech solar`，并把 cache 文件改成 solar。

## 9. 提交顺序

超算 split step1 的提交顺序固定为：

```text
本地：
1. step1_split_E1_union_stations.py
2. rsync E1 输出到 scnet-kunshan-185 和 scnet-kunshan-199

scnet-kunshan-185:
3. 提交 wind 2015.1 E2a 前期测试作业
4. 根据测试 MaxRSS/耗时调整 wind E2a 正式作业核心数和 time limit
5. 提交 wind 的 20 个 E2a 分块作业
6. 监控程序识别 2015.1 测试缓存有效且 20 个正式 E2a 作业全部 VALID 后提交 wind E2b
7. 监控程序识别 wind E2b VALID 后提交 wind E3

scnet-kunshan-199:
3. 提交 solar 2015.1 E2a 前期测试作业
4. 根据测试 MaxRSS/耗时调整 solar E2a 正式作业核心数和 time limit
5. 提交 solar 的 20 个 E2a 分块作业
6. 监控程序识别 2015.1 测试缓存有效且 20 个正式 E2a 作业全部 VALID 后提交 solar E2b
7. 监控程序识别 solar E2b VALID 后提交 solar E3
```

E2a 正式分块作业不使用一个包含 120 个 array element 的大数组，避免触发 20 个作业上限。生成器应创建 20 个普通 Slurm 作业脚本，或者在平台确认 array element 不计入作业数量后才考虑数组模式。

监控程序每隔 1 小时检查一次，并对齐自然整点，例如 `18:00`、`19:00`、`20:00`。监控程序负责：

1. 检查 E2a 20 个正式分块作业是否全部结束。
2. 按 E2a 作业清单校验 `2015.1` 测试缓存和 20 个正式 E2a 分块缓存，即 21 个时间缓存块是否全部有效并覆盖 120 个月。
3. 条件满足且 E2b 未提交时，提交 E2b。
4. 校验 E2b 完整 union cache 是否有效。
5. 条件满足且 E3 未提交时，提交 E3。

E2b/E3 的自动提交应显式检查当前队列，确保 `PENDING + RUNNING + 本次新增作业 <= 20`。

监控程序必须具备幂等提交保护。每次准备提交 E2b 或 E3 前，必须同时检查：

1. `latest_snapshot.json` 和 completion CSV 中是否已有该 `server + tech + stage` 的 Job ID 或已提交状态。
2. `squeue` 中是否已有相同阶段和技术类型的活动作业。
3. `sacct -X` 中是否已有相同阶段和技术类型的历史作业；对于失败作业，不自动重提，记录原因并等待人工确认。
4. 目标输出是否已经存在且通过完整性校验；有效则直接记录 `VALID`，不得重新提交。
5. 当前队列是否满足 20 个作业上限。

只有以上检查均证明“从未提交且输出无效或不存在”时，才允许调用一次 `sbatch --parsable`。获得 Job ID 后必须先原子写入 `latest_snapshot.json` 和 completion CSV，再结束本轮检查。监控程序重启、重复执行 `--once` 或两个整点检查重叠时，都不得产生重复的 E2b/E3 作业；可使用主机级文件锁防止同一服务器上并发运行两个监控实例。

E2b 提交示例：

```bash
e2b_id=$(sbatch --parsable job_step1_split_E2b_merge_union_cf_wind.sh)
e2b_id=${e2b_id%%;*}
printf 'E2b=%s\n' "$e2b_id"
```

E3 提交示例：

```bash
e3_id=$(sbatch --parsable job_step1_split_E3_thresholds_wind.sh)
e3_id=${e3_id%%;*}
printf 'E3=%s\n' "$e3_id"
```

两台服务器可以并行：

```text
scnet-kunshan-185: wind E2a -> wind E2b -> wind E3
scnet-kunshan-199: solar E2a -> solar E2b -> solar E3
```

## 10. 提交前检查

### 10.1 Git 和环境

```bash
cd ~/extreme_event_definitions
git rev-parse --abbrev-ref HEAD
git status --short
git rev-parse --short HEAD

source /public/home/acbw9wpn5k/.venv/bin/activate
python -V
python -m py_compile \
  step1_split_E1_union_stations.py \
  step1_split_E2a_extract_union_station_cf_monthly.py \
  step1_split_E2b_merge_union_station_cf_cache.py \
  step1_split_E3_thresholds_from_union_cache.py \
  scripts/precompute_station_low_resource_thresholds.py
```

### 10.2 输入数据

wind 服务器：

```bash
find "$HOME/data/cfs/CFs_of_wind_ERA5Land" -maxdepth 1 -type f -name 'wind_cf_*.nc' | wc -l
```

solar 服务器：

```bash
find "$HOME/data/cfs/CFs_of_solar_ERA5Land" -maxdepth 1 -type f -name 'solar_cf_*.nc' | wc -l
```

检查时只统计2015-2024基准期，不能用包含2025年的目录总文件数代替。基准期必须完整覆盖120个月；任何缺月都不得提交对应技术类型的 E2a 作业。

### 10.3 队列去重

```bash
squeue \
  --sort=j \
  -u "$USER" \
  -o "%.18i %.9P %.60j %.8u %.10T %.10M %.6D %R"
```

检查历史作业：

```bash
export STEP1_HISTORY_START=2026-07-17

sacct \
  -X \
  -u "$USER" \
  -S "$STEP1_HISTORY_START" \
  -E now \
  --units=G \
  -o "JobIDRaw,JobName%60,State,ExitCode,AllocCPUS,ReqMem,Start,End,Elapsed"
```

提交前若队列、历史账单、union cache 或最终阈值文件中已有同一 `stage + tech`，先核实，不重复提交。`step1_low_resource_thresholds.py` 不在超算生产主流程的编译检查、作业脚本和提交链中。

## 11. 提交作业

先提交 E2a 2015 年 1 月前期测试作业：

```bash
script=~/jobs/step1_low_resource/submit_step1_split_E2a_initial_wind.sh
bash "$script"
```

测试作业完成后，用 `sacct`/`seff` 记录 MaxRSS 和耗时，调整正式 E2a 分块作业的 `#SBATCH -n` 和 `--time`。之后提交 20 个 E2a 分块作业：

```bash
script=~/jobs/step1_low_resource/submit_step1_split_E2a_batches_wind.sh
bash "$script"
```

提交脚本必须打印每个 E2a 作业的 Job ID。提交后用打印出的实际 Job ID 抽查：

```bash
job_id='<E2A_JOB_ID_FROM_SUBMIT_OUTPUT>'

scontrol show job "$job_id" |
grep -E 'JobId=|JobName=|JobState=|Partition=|NumCPUs=|ReqMem=|Command=|StdOut='
```

E2b 和 E3 不由人工批量提交；由监控程序在整点检查时根据完成状态自动提交。

## 12. 完成状态记录

新增监控脚本：

```text
infos/hpc_step1/completion_status/monitor_step1_jobs.py
```

完成状态 CSV：

```text
infos/hpc_step1/completion_status/completion_scnet-kunshan-185.csv
infos/hpc_step1/completion_status/completion_scnet-kunshan-199.csv
```

表头：

```csv
更新时间,服务器,阶段,技术类型,年月范围,作业ID,Slurm状态,输出状态,输出路径,缓存路径,运行时长,分配核数,申请内存GB,峰值内存GB,日志路径,备注
```

`输出状态` 只使用：

```text
NOT_STARTED SUBMITTED RUNNING VALID MISSING BROKEN FAILED CANCELLED OOM
```

完成判定不能只看 Slurm `COMPLETED`。

E2a 判定为 `VALID` 必须同时满足：

1. Slurm 作业完成或日志显示 E2a 正常结束。
2. 当前年月范围的分块 union station CF cache 存在且大小大于 0。
3. NetCDF/HDF5 可以打开。
4. `cache_kind == era5land_union_station_cf_chunk`。
5. `tech`、`year_month_start`、`year_month_end`、`threshold_interp` 与任务一致。
6. `cf` 形状为 `(time, union_station)`。
7. `station` 维度等于 E1 并集场站数。
8. `time` 覆盖当前年月范围，时间轴递增且无重复。
9. `weight.sum(axis=1)` 接近 1。

E2b 判定为 `VALID` 必须同时满足：

1. Slurm 作业完成或日志显示 E2b 正常结束。
2. 完整 union station CF cache 存在且大小大于 0。
3. NetCDF/HDF5 可以打开。
4. `cache_kind == era5land_union_station_cf`。
5. `tech`、`baseline_years == 2015-2024`、`threshold_interp` 与任务一致。
6. `cf` 形状为 `(time, union_station)`。
7. `station` 维度等于 E1 并集场站数。
8. `time` 覆盖 2015-2024，时间轴递增且无重复。
9. E2a 作业清单中的 21 个时间缓存块全部被纳入 manifest，合计覆盖 120 个月。
10. `weight.sum(axis=1)` 接近 1。

E3 判定为 `VALID` 必须同时满足：

1. Slurm 作业完成或日志显示 step1 正常结束。
2. 当前技术类型的三个 SSP 阈值文件均存在且大小大于 0。
3. NetCDF/HDF5 可以打开。
4. `threshold_kind == sparse_station`。
5. `scenario`、`tech`、`baseline_years_requested` 与任务一致。
6. `clim` 形状为 `(12,24,station)`。
7. `threshold`、`valid_count` 形状为 `(station,)`。
8. `threshold` 中没有 NaN 或 Inf。
9. `valid_count > 0`。
10. `weight.sum(axis=1)` 接近 1。

检查单个阈值文件的 Python 逻辑应纳入监控脚本，不能只靠人工 `ncdump`。

## 13. 核时记录

核时 CSV：

```text
infos/hpc_step1/completion_status/usage_scnet-kunshan-185.csv
infos/hpc_step1/completion_status/usage_scnet-kunshan-199.csv
```

表头：

```csv
检查时间,服务器,统计起始日期,等待作业数,运行作业数,完成作业数,失败作业数,累计核时,备注
```

累计 step1 核时使用顶层作业的 `CPUTimeRAW`，避免重复统计 `.batch` 和 `.extern`：

```bash
sacct \
  -X \
  -n \
  -P \
  -u "$USER" \
  -S "$STEP1_HISTORY_START" \
  -E now \
  -o "JobName%80,CPUTimeRAW,State" |
awk -F'|' '
    $1 ~ /^step1_/ && $2 ~ /^[0-9]+$/ {
        seconds += $2
    }
    END {
        printf "step1 CPU hours: %.3f\n", seconds / 3600
    }
'
```

分别统计状态：

```bash
sacct \
  -X \
  -n \
  -P \
  -u "$USER" \
  -S "$STEP1_HISTORY_START" \
  -E now \
  -o "JobName%80,State" |
awk -F'|' '
    $1 ~ /^step1_/ {
        state=$2
        sub(/[+ ].*$/, "", state)
        count[state]++
    }
    END {
        for (state in count) {
            print state, count[state]
        }
    }
'
```

每次监控后追加一行 usage CSV，并更新 `latest_snapshot.json`。

## 14. 输出同步回本地

远端最终输出：

```text
~/extreme_event_definitions/outputs/low_resource_thresholds/sparse_station_ERA5Land_2015-2024/
```

本地目标：

```text
/data6/yanxiaokai/project_climate/extreme_event_definitions/outputs/low_resource_thresholds/sparse_station_ERA5Land_2015-2024/
```

同步前先在远端通过监控校验。同步后本地再次校验 6 个文件。

建议只同步最终阈值文件，不同步大型 station CF 缓存，除非需要排查问题。

## 15. 失败排查和恢复

### 15.1 OOM

```bash
sacct \
  -j "$job_id" \
  --units=G \
  -o "JobID,State,ExitCode,Elapsed,AllocCPUS,ReqMem,MaxRSS,MaxVMSize"
```

真实 `MaxRSS` 通常记录在 `${job_id}.batch` 行。建议按峰值内存留 25% 余量后换算核数：

```text
new_cores = ceil(MaxRSS_GB * 1.25 / 3.5)
```

修改对应作业生成配置后重新生成脚本，不直接手改已提交脚本。

### 15.2 Python 依赖缺失

重新检查：

```bash
source /public/home/acbw9wpn5k/.venv/bin/activate
python - <<'PY'
import numpy, pandas, netCDF4, h5py, global_land_mask
print("ok")
PY
```

当前依赖已经补齐。后续若出现缺失，仍在185上补装，并在199上复查共享 venv 是否可用。

### 15.3 输入数据缺失

检查 120 个月文件和首尾文件：

```bash
find "$HOME/data/cfs/CFs_of_wind_ERA5Land" -maxdepth 1 -type f -name 'wind_cf_*.nc' | sort | head
find "$HOME/data/cfs/CFs_of_wind_ERA5Land" -maxdepth 1 -type f -name 'wind_cf_*.nc' | sort | tail
```

如果上传尚未完成，不提交作业。

### 15.4 缓存半成品

step1 本地实现已经使用原子写出，但失败后仍需检查：

```bash
find outputs/cache/era5land_union_station_cf -name '*.tmp.*' -print
find outputs/low_resource_thresholds -name '*.tmp.*' -print
```

确认没有运行中的作业使用这些临时文件后，再人工清理。

## 16. 实施代码清单

后续代码实现建议分为以下文件：

```text
step1_split_E1_union_stations.py
step1_split_E2a_extract_union_station_cf_monthly.py
step1_split_E2b_merge_union_station_cf_cache.py
step1_split_E3_thresholds_from_union_cache.py
infos/hpc_step1/create_jobs_kunshan.py
infos/hpc_step1/completion_status/monitor_step1_jobs.py
infos/hpc_step1/completion_status/README.md
scripts/hpc_step1_validate_thresholds.py
```

### 16.1 step1_split_E1_union_stations.py

必须支持：

1. `--stations_csv_ssp126`
2. `--stations_csv_ssp245`
3. `--stations_csv_ssp585`
4. `--output_dir`
5. `--key lon,lat,type`
6. `--overwrite`

输出：

1. 并集场站 CSV。
2. 三个 SSP 的索引映射 CSV。
3. manifest JSON。
4. `e2a_chunk_plan_2015-2024.csv`，包含 1 个测试块和 20 个正式块，共 21 个时间缓存块。

### 16.2 step1_split_E2a_extract_union_station_cf_monthly.py

必须支持：

1. `--cf_root`
2. `--union_stations_csv`
3. `--tech wind|solar`
4. `--year_month_start YYYY-MM`
5. `--year_month_end YYYY-MM`
6. `--threshold_interp nearest_valid|bilinear`
7. `--output_dir`
8. `--overwrite`

要求：

1. 只读取当前年月范围内的 ERA5Land CF 月文件。
2. 按 HDF5 原生 time chunk 抽取并集场站 CF。
3. 生成分块 `cf(time, union_station)` 缓存。
4. 使用临时文件和 `os.replace` 原子发布。

### 16.3 step1_split_E2b_merge_union_station_cf_cache.py

必须支持：

1. `--time_chunk_dir`
2. `--chunk_plan_csv`
3. `--tech wind|solar`
4. `--baseline_years 2015-2024`
5. `--threshold_interp nearest_valid|bilinear`
6. `--output_cache`
7. `--overwrite`

要求：

1. 按作业清单校验 `2015.1` 测试缓存和 20 个正式 E2a 分块缓存全部存在且有效，即严格校验 21 个时间缓存块。
2. 按时间顺序合并为完整 union station CF cache。
3. 完整 cache 时间轴覆盖 2015-2024 且无重复。
4. 使用临时文件和 `os.replace` 原子发布。
5. 生产作业默认不传 `--overwrite`；目标完整缓存已存在时拒绝覆盖。

### 16.4 step1_split_E3_thresholds_from_union_cache.py

必须支持：

1. `--union_station_cf_cache`
2. `--union_stations_csv`
3. `--index_map_ssp126`
4. `--index_map_ssp245`
5. `--index_map_ssp585`
6. `--tech wind|solar`
7. `--baseline_years`
8. `--output_dir`
9. `--overwrite`

要求：

1. 不读取 ERA5Land 全球 CF。
2. 从 union station CF cache 按 SSP 映射取子集。
3. 为三个 SSP 分别计算并写出稀疏阈值文件。
4. 输出 schema 与 `step1_low_resource_thresholds.py` 保持兼容。
5. 生产作业默认不传 `--overwrite`；任一目标文件已存在时拒绝部分覆盖。

### 16.5 create_jobs_kunshan.py

必须支持：

1. `--server scnet-kunshan-185|scnet-kunshan-199`
2. `--force`
3. `--partition`
4. `--history-start`
5. `--dry-run`

输出：

1. 生成 E2a/E2b/E3 Slurm 脚本和 submit 脚本。
2. 打印脚本路径。
3. 打印 E2a 测试提交命令、E2a 正式分块提交命令，以及监控程序后续自动提交说明。
4. 不自动 `sbatch`。
5. 读取并校验 E2a 作业清单，不自行推导另一套分块范围。
6. 生成的生产作业默认不包含 `--overwrite`。

### 16.6 monitor_step1_jobs.py

必须支持：

1. `--server all|scnet-kunshan-185|scnet-kunshan-199`
2. `--once`
3. `--interval 3600`
4. `--history-start YYYY-MM-DD`

`--interval 3600` 必须对齐自然整点检查，例如 `18:00`、`19:00`，而不是以上一次检查结束后再等待 3600 秒。

必须采集：

1. `squeue`
2. `sacct -X`
3. 必要时 `sstat`
4. 远端日志 tail
5. 远端输出阈值文件校验
6. Git 状态
7. Python 环境状态
8. 累计核时

自动提交 E2b/E3 前必须执行第 9 节规定的幂等检查，并使用文件锁避免并发监控实例重复提交。自动流程不得对失败作业直接重提，重跑必须由人工确认。

## 17. 验收标准

1. 本地 E1 生成并集场站表、三个 SSP 映射文件和 manifest。
2. E1 产物已同步到两台昆山服务器。
3. 两台服务器均能通过作业生成器生成对应技术类型的 E2a/E2b/E3 Slurm 脚本和 submit 脚本。
4. 作业脚本激活 `/public/home/acbw9wpn5k/.venv`，并使用远端项目目录中的代码。
5. 185 和 199 分别完成 2015 年 1 月 E2a 前期测试，并记录 MaxRSS、耗时和调整后的 `kernel_num`。
6. 185 按 E2a 作业清单完成 1 个测试缓存块和 20 个 wind 正式缓存块，共 21 个时间缓存块并覆盖 120 个月。
7. 199 按 E2a 作业清单完成 1 个测试缓存块和 20 个 solar 正式缓存块，共 21 个时间缓存块并覆盖 120 个月。
8. 监控程序在整点识别测试缓存有效且 E2a 正式分块全部完成后自动提交 E2b。
9. 185 完成 wind 完整 union station CF cache。
10. 199 完成 solar 完整 union station CF cache。
11. 监控程序在整点识别 E2b 完成后自动提交 E3。
12. 185 从 wind union cache 计算出 ssp126/245/585 三个 wind 阈值文件。
13. 199 从 solar union cache 计算出 ssp126/245/585 三个 solar 阈值文件。
14. 每个 cache 和阈值文件通过第 12 节完整性校验。
15. completion CSV 记录 E2a/E2b/E3 阶段状态。
16. usage CSV 记录两台服务器的累计 step1 核时。
17. 本地同步后，下游 step2/step3 可以按现有路径读取这些阈值文件。
18. 重复运行监控程序或重启监控程序不会重复提交 E2b/E3。
19. 超算作业脚本和监控程序均不调用 `step1_low_resource_thresholds.py`。

## 18. 安全边界

1. 不在登录节点直接运行完整 step1，只通过 Slurm 作业运行。
2. 不把网页密码、rayfile token、账号密码表写入仓库。
3. 不删除远端 `~/data/cfs`、`outputs/`、`logs/`。
4. 不使用 `git reset --hard` 或覆盖远端脏工作区。
5. 不因作业 `PENDING` 就重复提交。
6. 不把 Slurm `COMPLETED` 直接等同于输出有效。
7. 不跳过 E1，也不让三个 SSP 分别从 ERA5Land CF 抽取场站 CF。
8. 不在 E2a 分块缓存全部完成并校验前提交同技术类型的 E2b。
9. 不在 E2b 完整 union cache 未完成并校验前运行同技术类型的 E3。
10. 不同步大型 union station CF cache 回本地，除非需要排查。
11. 不在超算生产作业中默认使用 `--overwrite`；覆盖任何缓存或结果前必须人工确认。
