# CLAUDE.md

本仓库只维护 global_bcsd patchify 场站极端天气信号。生产入口为
`scripts/station_signals_patchify.py`，作业轴为 `model × scenario × patch × tech`。
普通事件和低资源事件在同一作业内完成；低资源的 24 小时滚动量、clim288 和 P5 只计算一次。

事件定义在 `events/`，单位转换在 `grid_extreme_signals/unit_conversion.py`，规则网格场站匹配和
station_id 契约在 `grid_extreme_signals/station_match.py`。旧的多阶段入口、其他数据源适配器和
国家边界逻辑不再存在。生成器 `scnet/create_extreme_patch_jobs.py` 只生成脚本，不提交作业。

使用仓库 `.venv/bin/python`，输出和 SCNet 运行态目录由 `.gitignore` 忽略。
每次回复用户称呼“小凯”，结尾使用“希望对你有帮助，小凯！”。

