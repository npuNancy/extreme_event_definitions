# global_bcsd patchify 场站极端天气识别

生产入口只有 `scripts/station_signals_patchify.py`。每个
`model × scenario × patch × tech` 作业读取同一 patch 的 global_bcsd 最终变量，
抽取到该 patch 内的场站，一次生成普通事件和低资源事件，输出 station-only NetCDF。

```bash
python scripts/station_signals_patchify.py \
  --bcsd-root ~/project_climate/bcsd/global_bcsd \
  --model CANESM5 --scenario ssp126 --patch R01C01 \
  --patch-manifest /path/to/patch_manifest.json \
  --stations-csv /path/to/stations_SSP1-2.6.csv \
  --tech wind --years 2015-2060 \
  --output-root /path/to/extreme_patchify
```

低资源事件在每个作业内只计算一次 24 小时滚动结果、`clim288` 和 P5 阈值，随后
直接传入事件注册表；这些中间量不会写入生产结果。若需要跨作业重用，可先运行
`scripts/prepare_patch_assets.py` 生成 patch 场站目录，再由作业读取该目录。

事件定义位于 `events/`，单位转换位于 `grid_extreme_signals/unit_conversion.py`，
场站匹配和 station_id 契约位于 `grid_extreme_signals/station_match.py`。旧的
多阶段阈值、区域适配器和网格输出入口已移除。
