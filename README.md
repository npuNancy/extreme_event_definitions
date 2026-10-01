# BCSD 全网格极端天气事件识别

`develop-patch-grid` 在 BCSD 原生 patch 网格上计算风电、光伏两套天气事件，输出
`signal_<event>(time, lat, lon)` NetCDF。输入不需要场站 CSV、装机容量或激活年份。
计算覆盖上游 land plan 的有效域，保留 BCSD 的完整矩形坐标，域外及无法判定的位置为缺测。

## 本地运行

使用现有 `.venv` 环境，无新增依赖。输入为 BCSD final 文件及 sidecar、patch manifest，
以及同一生产批次的 `land_plan.nc`。输入布局沿用：

```text
<BCSD_ROOT>/outputs/<model>/<scenario>/<variable>/<patch>.nc
<BCSD_ROOT>/outputs/<model>/<scenario>/<variable>/<patch>.nc.json
```

也支持现有 `<variable>_<model>_<scenario>_<patch>.nc` 命名，每个变量必须唯一匹配。

先准备每格点固定基线：

```bash
.venv/bin/python scripts/prepare_grid_baseline.py \
  --bcsd-root /path/to/bcsd --model CANESM5 --scenario ssp126 \
  --patch R01C01 --patch-manifest /path/to/patch_manifest.json \
  --land-plan /path/to/land_plan.nc --tech solar \
  --analysis-years 2015-2060 --baseline-years 2015-2024 \
  --tile-shape 32 32 --time-chunk 240 --processes 4 \
  --output-root /path/to/extreme_grid/run1 --timing-report
```

再计算信号；省略 `--years` 时自动按五年分文件，最后一份为 2060 年：

```bash
.venv/bin/python scripts/grid_signals_patchify.py \
  --bcsd-root /path/to/bcsd --model CANESM5 --scenario ssp126 \
  --patch R01C01 --patch-manifest /path/to/patch_manifest.json \
  --land-plan /path/to/land_plan.nc --tech solar \
  --baseline-file /path/to/extreme_grid/run1/CANESM5/ssp126/R01C01/solar/baseline_2015-2024.nc \
  --tile-shape 32 32 --time-chunk 240 --processes 4 \
  --output-root /path/to/extreme_grid/run1 --timing-report
```

用 `--years 2025-2029` 单独处理一个时间段，或用 `--years-per-file 1` 自动按年分文件。
`--analysis-years` 决定滚动窗口的完整时间边界，必须与基线一致；输出时间段不能超出它。
基线年份和分析年份均需输入完整覆盖。支持 Gregorian、proleptic Gregorian 和 365_day/noleap，
时间步固定为 3 小时；保留源变量的分钟偏移与 CF 时间编码。

wind 使用 `uas` 时间轴，solar 使用 `rsds` 时间轴；其他变量按源时间索引线性对齐，不外推。
经纬度直接复制输入，不做空间插值。wind 读取 tas/uas/vas/hurs，solar 额外读取 pr/rsds。

## 输出及恢复

```text
<output-root>/<model>/<scenario>/<patch>/<tech>/
    baseline_2015-2024.nc[.json]
    signals_2015-2019.nc[.json]
    ...
    signals_2060-2060.nc[.json]
    manifest.json
```

- 信号为 int8：`0` 无事件、`1` 有事件、`_FillValue=-127` 无法判定。
  xarray 默认将缺测解码成 NaN；查看原始编码可用 `mask_and_scale=False`。
- 基线含 float32 `clim288(month,hour,lat,lon)`、float64 `p5(lat,lon)`、
  有效基线样本数和 `domain_mask`。所有文件记录事件、输入身份及计算版本。
- `--timing-report` 写阶段耗时、进程峰值 RSS 和每个 tile 的完成/复用信息。
- 默认临时结果放在 `<output-root>/.grid_parts/`；可用 `--parts-root` 指定其他路径。
  每个 worker 独立写 part，父进程逐时间块和空间 tile 合并，不整体载入 patch。
- 相同身份的完整结果直接复用；缺失 sidecar、损坏文件等不完整结果会重算，完成的 part 继续复用。
  输入版本、代码、年份、tile 或时间 chunk 改变后不会误用旧缓存。改变进程数不影响结果身份。
- 已有完整文件若配置不同，使用新输出根，或显式 `--overwrite` 重建。
  该参数仍允许复用与当前配置完全一致的完整 parts。临时 parts 不自动删除，容量规划需计入它们。
- 不同时间分片方案使用不同输出根，避免重叠时间文件混入同一 manifest。

进程数默认 1，示例的 4 只是试验起点。基线内存主要随“基线时长 × tile 格点数”增长，
信号内存主要随“time chunk × tile 格点数”增长；增加进程数需同时考虑总内存和磁盘吞吐。
spawn worker 的 BLAS/OpenMP 线程限制为 1。

## 事件与验证

事件常量位于 `events/`，变量依赖位于 `registry.py`，单位转换位于
`grid_extreme_signals/unit_conversion.py`。wind 有 5 个事件，solar 有 6 个事件；
solar dust 因 BCSD 缺少 dust_aod 而跳过并记录原因。

低资源保持现有定义：2015–2024 基线、24 小时居中滚动、月×小时 climatology、
每格点 P5、t/t+1 标记，光伏随后过滤夜间。内部时间块包含窗口和下一步标记所需 halo，
计算边界不随年份分文件改变。太阳几何沿用旧流程映射到 2000 年的月日时算法。

```bash
.venv/bin/python -m pytest tests/ -q
```

新测试覆盖网格中心与旧场站算法对照、完整时序与分块/分年等价、不同进程数、
闰年和 noleap、错位时间轴、缺测、域外 tile、缓存身份及中断恢复。
旧场站入口暂保留用于回归参考；新 NetCDF 不能直接交给现有场站 Loss 读取器。

实现说明见 [全网格实施方案](document/全网格极端天气事件识别实施方案.md)。
当前范围为计算代码和本地验证，暂不建设超算运行目录。

## 从网格事件抽取 Climate × Station 场站事件

`grid_extreme_signals/station_pipeline.py` 将已识别的网格事件按最近格点抽取到场站位置。
`climate_scenario` 选择网格事件源，`station_scenario` 选择场站 CSV 和 station_id。
固定 Station 时，跨 Climate 的场站 ID 保持一致；固定 Climate 和源格点时，各 Station 的事件值一致。
全量输入产生 `4 GCM × 3 Climate × 3 Station × 2 tech × 47 patch = 3,384` 个 unit，
每个 unit 处理该网格源已有的全部时间分片。空场站 unit 仍保留审核记录。

准备配置示例（保存为本地 `station_campaign.json`，替换输入路径）：

```json
{
  "models": ["CANESM5", "MPI-ESM1-2-HR", "MRI-ESM2-0", "BCC-CSM2-MR"],
  "analysis_years": "2015-2060",
  "input_index": "/path/to/grid_v2/runtime/authoritative_index.json",
  "patch_manifest": "/path/to/grid_v2/runtime/patch_manifest.json",
  "stations": {
    "ssp126": "/path/to/stations_SSP1-2.6.csv",
    "ssp245": "/path/to/stations_SSP2-4.5.csv",
    "ssp585": "/path/to/stations_SSP5-6.0.csv"
  },
  "max_distance_deg": 0.15,
  "extraction": {"time_chunk": 240, "station_chunk": 256, "complevel": 1}
}
```

准备阶段将所选模型在源索引中的全部 Climate/patch/tech 与 `stations` 中的全部 Station 做笛卡尔积。
小样本可使用较少模型、网格输入和 Station CSV；实际 unit 数以 `prepared.json` 为准。
Station 接受 `ssp560` 作为 `ssp585` 的别名，两者不能同时配置；Climate 仅接受
`ssp126/ssp245/ssp585`。场站 ID 使用规范化后的 Station 名称。

```bash
.venv/bin/python scripts/prepare_station_extraction.py \
  --campaign station_campaign.json --shared-root /path/to/station_run/shared \
  --code-sha <CODE_SHA>

.venv/bin/python scripts/extract_station_events.py \
  --prepared /path/to/station_run/shared/prepared.json \
  --model CANESM5 --climate-scenario ssp126 --station-scenario ssp585 \
  --patch R01C01 --tech wind --output-root /path/to/station_run/outputs \
  --code-sha <CODE_SHA>
```

两步使用相同代码 SHA。省略 `--years` 时提取所有源时间分片；指定时必须与一个源分片完全一致。
输出路径为 `<output-root>/<model>/climate_<C>/station_<S>/<patch>/<tech>/signals_<years>.nc`。
NetCDF、sidecar 身份、审核索引和覆盖统计均区分两种情景。
相同身份的完整分片可恢复；身份冲突或缺少完成证据时使用新的输出目录。

本地批量抽取、审核和发布可调用现有 Python 接口：

```python
from pathlib import Path
from grid_extreme_signals import station_contract as ct
from grid_extreme_signals.station_pipeline import extract_combination, audit, publish
from grid_extreme_signals.station_reader import open_station_signals

root = Path("/path/to/station_run")
prepared = ct.read_json(root / "shared/prepared.json")
audits = {}
for key in prepared["combinations"]:
    extraction = extract_combination(prepared, key, root / "outputs", prepared["release"]["code_sha"])
    audit_path = root / "audits" / key / "audit.json"
    audit(prepared, key, extraction, audit_path)
    audits[key] = str(audit_path)
index = publish(prepared, audits, root)

with open_station_signals(
    index, model="CANESM5", climate_scenario="ssp126",
    station_scenario="ssp585", tech="wind", station_ids=["<station_id>"], years="2030-2039",
) as ds:
    print(ds)
```

新场站产物和索引采用 `station-extreme-v2`，读取接口显式要求两种情景。
已有单情景产物需重新准备并抽取到新目录。事件保留原生时间、`0/1/fill` 和现有不应用投运 mask 的行为。
`infos/scnet_patchify_stations/` 的作业工具尚未适配双情景流程，本节仅说明本地计算接口。
