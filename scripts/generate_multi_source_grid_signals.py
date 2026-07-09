#!/usr/bin/env python3
"""多数据源网格极端天气信号生成的统一 CLI 入口。

支持四类数据源：
  - ``regional_bcsd``  — CMIP6–ERA5Land BCSD（规则经纬度，3 小时）
  - ``china_cmfd_bcsd`` — 中国区域 CMIP6–CMFD BCSD（sfcWind，3 小时）
  - ``cordex_nam12``   — CMIP6–CORDEX NAM-12（旋转极点网格，逐小时）
  - ``era5land_raw``   — ERA5-Land 全球原始数据（逐小时，需要解累计）

示例::

    # 区域 BCSD
    python scripts/generate_multi_source_grid_signals.py \\
        --source regional_bcsd \\
        --data_dir data/bcsd_outputs \\
        --model MIROC-ES2H \\
        --region Austria \\
        --scenario ssp126 \\
        --years 2015-2060

    # 中国 CMFD BCSD
    python scripts/generate_multi_source_grid_signals.py \\
        --source china_cmfd_bcsd \\
        --data_dir data/cmip6_downscaling_3hr \\
        --model MIROC-ES2H \\
        --scenario ssp126 \\
        --years 2015-2100

    # CORDEX NAM-12
    python scripts/generate_multi_source_grid_signals.py \\
        --source cordex_nam12 \\
        --data_dir data/CORDEX-CMIP6/NAM-12/1hr \\
        --gcm_model MPI-ESM1-2-LR \\
        --realization r1i1p1f1 \\
        --rcm_model CRCM5 \\
        --scenario ssp126 \\
        --years 2020-2060

    # ERA5-Land
    python scripts/generate_multi_source_grid_signals.py \\
        --source era5land_raw \\
        --data_dir data \\
        --years 2024 \\
        --months 1,2
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

# 确保项目根目录可导入
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from grid_extreme_signals.adapters.regional_bcsd import RegionalBcsdAdapter
from grid_extreme_signals.adapters.china_cmfd_bcsd import ChinaCmfdBcsdAdapter
from grid_extreme_signals.adapters.cordex_nam12 import CordexNam12Adapter
from grid_extreme_signals.adapters.era5land_raw import Era5LandRawAdapter
from grid_extreme_signals.signal_runner import run_signal_pipeline

logger = logging.getLogger("grid_extreme_signals")

# 各数据源默认时间块大小
_DEFAULT_CHUNK_TIME = {
    "regional_bcsd": 512,
    "china_cmfd_bcsd": 512,
    "cordex_nam12": 24,
    "era5land_raw": 24,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="从多数据源网格气候数据生成极端天气信号。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # ---- 通用参数 ----
    p.add_argument(
        "--source", required=True,
        choices=["regional_bcsd", "china_cmfd_bcsd", "cordex_nam12", "era5land_raw"],
        help="数据源类型。",
    )
    p.add_argument("--data_dir", required=True, help="输入数据根目录。")
    p.add_argument(
        "--output_root", default="outputs/grid_extreme_signals",
        help="输出根目录（默认：outputs/grid_extreme_signals）。",
    )
    p.add_argument(
        "--years", required=True,
        help="年份范围：'YYYY' 或 'YYYY-YYYY'。",
    )
    p.add_argument(
        "--months", default="",
        help="月份筛选：'1,2,3'；留空表示所有月份（默认：全部）。",
    )
    p.add_argument(
        "--stations_dir", default=None,
        help="场站筛选目录（预留；第一阶段中会抛出 NotImplementedError）。",
    )
    p.add_argument(
        "--require_events", nargs="*", default=[],
        help="必须计算的事件；若缺少输入则报错。",
    )
    p.add_argument(
        "--allow_missing_optional", action="store_true",
        help="可选输入缺失时跳过而不中断。",
    )
    p.add_argument(
        "--allow_unit_inference", action="store_true",
        help="缺少 units 属性时允许启发式单位推断。",
    )
    p.add_argument(
        "--chunk_time", type=int, default=None,
        help="每个处理块的时间步数（默认值随数据源变化）。",
    )
    p.add_argument(
        "--compress_level", type=int, default=4,
        help="NetCDF zlib 压缩级别（默认：4）。",
    )
    p.add_argument(
        "--overwrite", action="store_true",
        help="重新计算并覆盖已有输出文件。",
    )
    p.add_argument(
        "--dry_run", action="store_true",
        help="只打印任务计划，不执行计算。",
    )
    p.add_argument(
        "--save_weather", action="store_true",
        help="除信号文件外，同时保存标准化气象 NetCDF 文件。",
    )
    p.add_argument(
        "--cf_root", default="../data/cfs",
        help="容量因子数据根目录，用于默认启用的低资源事件（默认：../data/cfs）。",
    )
    p.add_argument(
        "--lowres_baseline_years", default="2015-2029",
        help="低资源事件基线期（默认：2015-2029）。",
    )
    p.add_argument(
        "--lowres_cf_years", default="2015-2060",
        help="CF 文件覆盖年份，用于查找 allmonths 文件（默认：2015-2060）。",
    )
    p.add_argument(
        "--lowres_grid_lat_chunk", type=int, default=1,
        help="网格低资源计算的纬向块大小（默认：1，降低内存占用）。",
    )
    p.add_argument(
        "--no_low_resource", action="store_true",
        help="跳过默认启用的风电/光伏低资源事件。",
    )

    # ---- 数据源专用参数 ----
    # regional_bcsd / china_cmfd_bcsd 参数
    p.add_argument("--model", default=None, help="气候模式名称。")
    p.add_argument("--region", default=None, help="区域名称（仅 regional_bcsd）；使用 'all' 处理全部区域。")
    p.add_argument("--scenario", default=None, help="情景代码（如 ssp126、ssp245、ssp585）。")

    # cordex_nam12 参数
    p.add_argument("--gcm_model", default=None, help="GCM 模式（CORDEX）。")
    p.add_argument("--realization", default=None, help="成员编号，例如 r1i1p1f1（CORDEX）。")
    p.add_argument("--rcm_model", default=None, help="RCM 模式，例如 CRCM5（CORDEX）。")

    # era5land_raw 参数
    p.add_argument(
        "--era5land_d2m_root", default=None,
        help="d2m 变量的备用根目录（ERA5-Land）。",
    )
    p.add_argument(
        "--dust_dir", default=None,
        help="MERRA-2 沙尘数据根目录（ERA5-Land，可选）。",
    )

    return p


def validate_args(args: argparse.Namespace) -> None:
    """检查数据源专用必填参数是否齐全。"""
    src = args.source

    if src == "regional_bcsd":
        for name in ("model", "region", "scenario"):
            if getattr(args, name) is None:
                raise ValueError(f"--{name} is required for --source regional_bcsd")

    elif src == "china_cmfd_bcsd":
        for name in ("model", "scenario"):
            if getattr(args, name) is None:
                raise ValueError(f"--{name} is required for --source china_cmfd_bcsd")

    elif src == "cordex_nam12":
        for name in ("gcm_model", "realization", "rcm_model", "scenario"):
            if getattr(args, name) is None:
                raise ValueError(f"--{name} is required for --source cordex_nam12")

    # era5land_raw 不需要额外参数


def create_adapter(args: argparse.Namespace):
    """按所选数据源创建对应适配器。"""
    if args.source == "regional_bcsd":
        return RegionalBcsdAdapter(args)
    elif args.source == "china_cmfd_bcsd":
        return ChinaCmfdBcsdAdapter(args)
    elif args.source == "cordex_nam12":
        return CordexNam12Adapter(args)
    elif args.source == "era5land_raw":
        return Era5LandRawAdapter(args)
    else:
        raise ValueError(f"未知数据源：{args.source}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = build_parser()
    args = parser.parse_args()

    # 按数据源设置默认 chunk_time
    if args.chunk_time is None:
        args.chunk_time = _DEFAULT_CHUNK_TIME[args.source]

    validate_args(args)

    logger.info("数据源：%s", args.source)
    adapter = create_adapter(args)
    run_signal_pipeline(adapter, args)


if __name__ == "__main__":
    main()
