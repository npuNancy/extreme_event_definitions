# 事件定义复用说明

所有事件定义按技术类型注册在 `registry.py`，输入为 station-only 的标准化天气数组。
`scripts/station_signals_patchify.py` 从 global_bcsd patch 文件抽取气象变量，在一次作业内生成全部可用
普通事件和低资源事件，并写出 `signal_<event>(time, station)`。

低资源事件使用 2015--2024 基线、3 小时八步滚动窗口、月×时 clim288 和每站 P5。缺测值不会被
当成事件；场站激活年前和超出 patch 匹配距离的信号置零。输出包含 patch_id、station_id、匹配距离
及阈值来源等属性。

