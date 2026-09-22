# 全网格事件计算说明

普通事件通过 `registry.simple_signals()` 计算，`registry.REQUIRED` 列出每个事件所需的
标准天气字段。计算内部使用 `(time_chunk, tile_points)` 数组，写出时恢复为
`(time, lat, lon)`；各事件分别判断输入有效性。

生产入口：

- `scripts/prepare_grid_baseline.py`：按空间 tile 计算并保存固定基线 climatology 和精确 P5。
- `scripts/grid_signals_patchify.py`：读取基线，按带 halo 的时间 chunk 计算所有可用事件。

共享低资源规则位于 `tools/common.py`。网格路径调用 `low_resource_from_anomaly()`，
直接使用本块已计算的距平，避免重复 rolling。原 `low_resource()` 也复用该判定函数。
8 步居中窗口使用 `[i-4,i+3]`；包含 t+1 传播的输出区间 `[a,b)` 读取 `[a-5,b+3)`，
并在真实分析区间首尾裁剪。光伏夜间过滤位于 t+1 标记之后。

输入无效、窗口不完整、基线无效或域外格点写 `_FillValue=-127`，有效无事件写 0，
事件写 1。NaN/Inf 不应成为有效事件。光伏 dust 缺输入时跳过，不生成伪全零变量。

输出保留原生经纬度和各技术参考时间轴，基线及信号均有身份 sidecar，
最终信号清单在技术输出目录的 `manifest.json`。具体命令和恢复方式见 [README](README.md)。
