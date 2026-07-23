# 场站级极端事件信号 `station_id` 接入计划

## 1. 结论

`station_id` 应由 `extreme_event_definitions` 在生成场站级极端事件信号时写入，
不应由下游 `calculate_wind_solar_generation_loss` 根据经纬度临时恢复。

本次采用以下职责划分：

```text
场站 CSV
  └─ step2E1 普通极端事件
       ├─ 筛选并去重场站
       ├─ 生成稳定 station_id
       └─ 写出含 station_id 的场站信号 NetCDF
            └─ step2E2
                 ├─ 校验或兼容补齐 station_id
                 └─ 补写 signal_low_resource
                      └─ generation_loss 只读取和校验 station_id
```

`step1` 的 ERA5Land 低资源阈值与 `station_id` 无依赖关系。阈值由场站位置、
技术类型和固定基准期决定，现有 step1 产物可以继续复用，因此：

- 不修改 `step1_low_resource_thresholds.py` 及 step1 拆分流程；
- 不要求 step1 阈值文件新增 `station_id`；
- 不为接入 `station_id` 重跑 step1；
- step2E2 继续用现有经纬度规则匹配 step1 阈值。

## 2. 当前代码事实

### 2.1 step2E1

入口为：

```text
step2_split_E1_weather_extremes.py
  -> scripts/station_signals_direct.py
  -> grid_extreme_signals/station_match.py::write_station_signals
```

`write_station_signals` 已同时拿到以下生成 ID 所需信息：

```text
scenario
tech
station_lon
station_lat
```

因此它是新文件强制写入 `station_id` 的合适边界。

### 2.2 step2E2

入口为：

```text
step2_split_E2_low_resource.py
  -> scripts/patch_pipelineB_low_resource.py
```

E2 有两条写入路径：

1. E1 文件存在：通过 HDF5 原位补写 `signal_low_resource`；
2. E1 文件不存在但区域内有场站：调用 `write_station_signals` 新建兼容文件。

第二条路径会自动继承 E1 的新输出契约。第一条路径还需要在处理
`signal_low_resource` 前校验或补齐旧文件中的 `station_id`。

### 2.3 当前缺口

当前信号文件的 `station` 只是文件内整数序号，并只有：

```text
station_lon
station_lat
station_type
activation_year
capacity_gw
```

下游不得把这个整数序号当成稳定场站标识。不同文件的场站顺序可能不同，按位置
拼接会静默错配 CF 和极端事件信号。

## 3. 稳定 ID 契约

### 3.1 唯一生成规则

在 `grid_extreme_signals/station_match.py` 中增加唯一的公共实现，禁止 E1、E2
各自复制一份算法：

```python
normalized_lon = ((float(lon) + 180.0) % 360.0) - 180.0
key = f"{scenario}|{tech}|{normalized_lon:.4f}|{float(lat):.4f}"
station_id = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
```

该规则必须与 `calculate_wind_solar_generation_loss/generation_loss/stations.py`
当前规则逐字节一致：

- 情景使用规范值：`ssp126`、`ssp245`、`ssp585`；
- 技术类型只允许 `wind` 或 `solar`；
- 经度先归一化到 `[-180, 180)`；
- 经纬度统一量化到 4 位小数；
- key 分隔符固定为 `|`；
- SHA1 结果取前 20 个小写十六进制字符。

4 位小数是接口契约，不是展示格式。它用于消除场站 CSV 的 `float64` 坐标写入
NetCDF `float32` 后产生的微小误差，同时仍远小于当前 0.1° 网格间距。

### 3.2 NetCDF 输出契约

每个有场站的 `station_signals_*.nc` 必须包含：

```text
dimensions:
    time
    station

coordinates:
    station_id(station)  string

data variables:
    station_lon(station)
    station_lat(station)
    station_type(station)
    activation_year(station)
    capacity_gw(station)
    signal_<event>(time, station)
```

保留整数 `station` 维度坐标，以免破坏现有 E1/E2 代码；`station_id` 作为
`station` 上的辅助坐标写出。建议同时记录全局属性：

```text
station_id_scheme = sha1-20:scenario|tech|lon4|lat4
station_id_coordinate_decimals = 4
```

写出前必须校验：

- `scenario` 和 `tech` 合法；
- 经纬度均为有限值，纬度在 `[-90, 90]`；
- `station_id` 数量等于 `station` 长度；
- 文件内 `station_id` 无空值、无重复；
- 同一 ID 不能对应两组不同的 4 位量化坐标；
- ID 必须匹配正则 `[0-9a-f]{20}`。

任何校验失败都应使作业非零退出，不能退回整数 `station` 或最近邻 ID 匹配。

## 4. 代码修改步骤

### 4.1 增加公共 ID 函数

修改：

```text
grid_extreme_signals/station_match.py
```

增加：

1. 单场站 ID 生成函数；
2. 向量化生成一组 ID 的函数；
3. 根据 `scenario + tech + lon + lat` 校验已有 ID 的函数；
4. 文件内唯一性和哈希碰撞检查。

公共函数直接复用本模块已有的经度归一化逻辑。生成时使用 `float64` 做归一化和
格式化，不以已经降精度的字符串作为输入。

### 4.2 让 E1 新文件原生携带 `station_id`

修改：

```text
grid_extreme_signals/station_match.py::write_station_signals
```

在构造 `xarray.Dataset` 时：

1. 使用函数参数中的 `scenario`、`tech` 和最终场站表中的 `lon/lat` 生成 ID；
2. 将 `station_id=("station", ids)` 放入 `coords`，而不是普通 data variable；
3. 写入 ID 方案属性；
4. 写文件前执行完整校验。

这样以下两个调用方都会自动满足新契约：

- step2E1 正常生成的普通极端事件文件；
- E1 缺失时由 step2E2 创建的仅含 `low_resource` 文件。

不在 `scripts/station_signals_direct.py` 中维护第二套哈希实现。

### 4.3 让 E2 先处理 ID，再判断是否跳过低资源

修改：

```text
scripts/patch_pipelineB_low_resource.py
```

对已有 E1 文件，处理顺序改为：

1. 从文件路径和属性交叉确认 `scenario`、`tech`；
2. 读取 `station_lon/station_lat`；
3. 根据统一规则计算期望 `station_id`；
4. 若已有 ID，逐项校验值、顺序、数量和唯一性；
5. 若缺少 ID，在原文件中补齐辅助坐标及 ID 方案属性；
6. 完成 ID 后，才判断 `signal_low_resource` 是否因未传 `--overwrite` 而跳过；
7. 需要时继续执行原有低资源计算。

必须调整当前“发现 `signal_low_resource` 已存在便立即返回”的逻辑，否则已完成
E2 的旧文件永远没有机会补齐 `station_id`。

E2 补齐旧文件时还应使用 `--stations_csv` 和 `--shp` 重新得到该
`region × scenario × tech` 的预期场站清单，并验证：

- 场站数量相同；
- 4 位量化后的经纬度顺序相同；
- 文件属性中的 `scenario/region/model` 与作业单元相同。

不允许仅凭旧文件中的属性和坐标盲目写入 ID。

### 4.4 提供仅迁移元数据的 E2 模式

为已有 23 国输出增加显式的轻量模式，例如：

```bash
python step2_split_E2_low_resource.py ... --station-id-only
```

该模式：

- 只校验或补齐 `station_id` 和相关属性；
- 不读取 CF；
- 不读取或修改 step1 阈值；
- 不重算任何普通事件或 `signal_low_resource`；
- 不修改信号值、时间轴、场站顺序和其他科学属性；
- 已有且正确时幂等成功；
- 已有但错误时失败，不静默覆盖。

对已有 NetCDF 的迁移采用同目录临时文件加 `os.replace` 的原子替换方式。写入前保留
原文件，校验临时文件成功后再替换，避免中断后留下半写文件。禁止先删除旧输出。

该模式只是旧产物的过渡兼容路径，不替代后续因湿度事件缺失而需要执行的 E1/E2
正式重跑。

### 4.5 扩展 SCNet E2 作业生成器

修改：

```text
infos/hpc_step2_E2/create_step2_E2_jobs.py
infos/hpc_step2_E2/goal.md
tests/test_hpc_step2.py
```

生成器增加显式的 `--station-id-only` campaign 选项，并把该选项写入 campaign
选择、manifest 和每条命令，确保：

- 元数据迁移 campaign 与科学 E2 campaign 的 ID 不相同；
- 仍按 `model × region × scenario × tech × years` 形成可独立重试单元；
- 生成器只生成脚本，不提交；
- 元数据迁移不要求 CF 和阈值目录存在；
- 默认仍不覆盖已有科学信号；
- 迁移作业使用独立 job/log 目录，避免与历史 E2 日志混淆。

元数据迁移的完成条件不能继续只检查“文件非空”，而应是计算节点上的轻量校验程序
确认：

```text
station_id 存在
station_id 被 xarray 识别为坐标
station_id 数量与 station 相同
station_id 唯一
station_id 与 scenario/tech/lon/lat 重算结果逐项一致
```

无场站单元继续成功跳过，且日志明确记录原因。

### 4.6 下游切换为“只消费、不恢复”

上游新契约和已有文件迁移完成后，再修改当前损失计算仓库：

```text
calculate_wind_solar_generation_loss/generation_loss/signals.py
calculate_wind_solar_generation_loss/tests/
```

删除 `normalize_signal_dataset` 中根据 `scenario/tech/station_lon/station_lat`
恢复 `station_id` 的分支。新行为为：

- 信号文件必须原生提供 `station_id(station)` 坐标；
- 缺失、为空、重复、格式错误或长度不一致时立即失败；
- CF 与信号仍按 ID 精确取交集和重排；
- 不使用经纬度最近邻或现场重新哈希作为兜底。

切换顺序必须是“上游代码部署并迁移/重算输出”在前，“下游删除恢复逻辑”在后，
避免产生新旧接口不兼容窗口。最终状态不保留永久兼容开关。

## 5. 测试计划

### 5.1 ID 单元测试

在 `tests/test_station_match.py` 中覆盖：

1. 同一坐标的 CSV `float64` 值和 NetCDF `float32` 往返值得到同一 ID；
2. `49.600000` 和 `float32(49.6)` 得到同一 ID；
3. `[0, 360)` 与 `[-180, 180)` 的等价经度得到同一 ID；
4. 不同 scenario、不同 tech 或不同量化位置得到不同 ID；
5. 非法 scenario、tech、NaN、无穷值和非法纬度失败；
6. 重复 ID 和 ID/坐标不一致失败。

测试中使用固定 golden values，防止未来无意修改分隔符、精度或哈希截断长度。

### 5.2 E1 写出测试

扩展场站信号写出测试，重新打开 NetCDF 后确认：

- `station_id` 位于 `ds.coords`；
- dtype 可稳定转为字符串；
- ID 数量、顺序和重算结果正确；
- wind/solar、三个 SSP 均遵守相同规则；
- 原有信号变量、场站元数据和属性没有改变。

### 5.3 E2 兼容测试

覆盖以下情况：

1. 旧 E1 文件无 ID、无 `signal_low_resource`：先补 ID，再补低资源；
2. 旧 E1 文件无 ID、已有 `signal_low_resource`、未传 `--overwrite`：
   仍补 ID，但不改低资源信号；
3. 新 E1 文件已有正确 ID：E2 校验后保持不变；
4. 文件已有错误或重复 ID：作业失败；
5. 文件场站顺序或量化坐标与 SSP 场站清单不一致：作业失败；
6. E1 缺失、E2 新建文件：新文件原生包含 ID；
7. `--station-id-only` 不访问 CF/阈值、不改变任何 `signal_*` 数组；
8. 同一个迁移命令重复运行结果不变。

### 5.4 HPC 生成器测试

扩展 `tests/test_hpc_step2.py`，验证：

- 普通 E2 与 metadata-only campaign 的 manifest 不冲突；
- metadata-only 脚本包含 `--station-id-only`；
- metadata-only 模式不校验或传入 CF/阈值依赖；
- 脚本数量、稳定文件名和命令参数正确；
- 默认不提交、不覆盖；
- 生成脚本全部通过 `bash -n`。

本地验证命令：

```bash
.venv/bin/python -m pytest \
  tests/test_station_match.py \
  tests/test_hpc_step2.py \
  tests/test_sparse_low_resource_thresholds.py
```

## 6. 远程执行顺序

远程服务器固定为用户已确认的：

```text
scnet-wuzhen-199
```

本计划只规定部署和运行顺序，不在代码修改阶段自动提交作业。

### 阶段 A：代码部署与小样本验收

1. 本地完成代码和测试，commit/push。
2. 在远端确认 checkout 干净后执行 fast-forward-only pull。
3. 选择一个 wind 和一个 solar 已完成文件，生成 metadata-only 小 campaign。
4. 对生成脚本执行 `bash -n`。
5. 提交两个小样本作业。
6. 在计算节点完成输出契约校验。
7. 对迁移前后的 `signal_*` 逐变量计算校验值，确认完全不变。

### 阶段 B：已有 23 国文件的可选轻量迁移

如果正式湿度数据尚未就绪，但需要先让现有输出带 ID：

1. 为已有 campaign 生成 `--station-id-only` 作业；
2. 使用独立 manifest、日志目录和进度记录；
3. 按账号所有项目合计活动作业上限和共享提交锁分批提交；
4. 每个单元只做元数据迁移与轻量校验；
5. 不启动 step1，不重算 E1/E2 科学变量；
6. 失败单元不自动覆盖或重试，先检查场站清单、路径和文件属性。

### 阶段 C：湿度数据接入后的正式重跑

问题 2 表明现有 E1 文件缺少湿度相关事件。最终生产结果仍需重新运行：

```text
step2E1 -> step2E2
```

但继续复用现有 step1 阈值，不运行 step1。

正式 E1 重跑后，新文件由 `write_station_signals` 原生携带 `station_id`；正式 E2
只需校验并保留 ID，再补写低资源事件。此前执行过 metadata-only 迁移的旧文件会被
正式 E1 结果替代，因此迁移只解决过渡期可用性。

湿度输入接入本身不属于本计划的代码范围，但在正式重跑前必须设为独立前置门槛：

- adapter 已实际产出 `rh_pct`；
- wind 输出包含 `icing`、`hot_humid`；
- solar 输出包含 `icing`、`high_humidity`；
- 输出属性不再把这些事件列入 `skipped_events`。

此外，问题 3 的投产年份掩膜口径也必须在正式 campaign 前单独修正并验证。不得只因
文件已含 `station_id` 就把仍为 `activation_mask=on` 的信号交给损失计算。

## 7. 完成标准

代码层面：

- ID 只有一套公共实现；
- E1 和 E2 新建文件原生含 `station_id` 坐标；
- E2 可严格校验并安全补齐旧文件；
- 上游输出完成迁移后，`calculate_wind_solar_generation_loss` 删除 ID 恢复逻辑；
- 所有相关本地测试通过。

过渡迁移层面：

- 已迁移文件的全部 `signal_*` 数值、shape、时间轴和场站顺序保持不变；
- 每个有场站文件的 ID 均唯一且可从文件元数据精确重算；
- 每个迁移单元有 Slurm 成功记录和输出契约校验记录；
- 无场站单元有明确成功跳过日志。

最终生产层面：

- 不重跑 step1；
- 湿度数据就绪后完成新的 step2E1、step2E2 campaign；
- E1 普通事件、E2 低资源事件、`station_id` 和投产年份掩膜均通过验收；
- 下游仅按上游提供的 `station_id` 做精确交集和排序，不再由经纬度生成 ID，
  也不使用最近邻代替稳定 ID。

## 8. 非目标

本计划不做以下工作：

- 不改变极端事件阈值；
- 不改变低资源算法或 ERA5Land `2015-2024` 固定阈值；
- 不重排场站；
- 不改变场站容量；
- 不把不同情景或不同技术的同位置场站合并；
- 不用最近邻、模糊字符串或整数 `station` 序号代替 `station_id`；
- 不在登录节点运行科学计算或批量读取大型 NetCDF；
- 不自动提交远程作业。
