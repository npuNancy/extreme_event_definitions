"""数据源适配器基类。

每个适配器必须：
  1. 通过 ``iter_tasks`` 产出任务字典，每个处理单元（年或月）一个。
  2. 从 ``load_wind_weather`` / ``load_solar_weather`` 返回 :class:`WeatherBundle`。
  3. 提供输出路径辅助函数。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Sequence

import xarray as xr


@dataclass
class WeatherBundle:
    """已标准化、可用于事件识别的气象数据。

    ``dataset`` 包含源数据原生空间网格上的统一气象变量（temp_C、wind_ms、precip_mmh、
    rsds、rh_pct、dust_aod）。只包含给定数据源/技术类型实际存在的变量。

    Attributes
    ----------
    source : str
        数据源标识（如 ``regional_bcsd``、``china_cmfd_bcsd``）。
    tech : str
        ``"wind"`` 或 ``"solar"``，用于决定适用事件。
    dataset : xr.Dataset
        统一气象变量，维度为 ``(time, *spatial_dims)``。
    spatial_dims : tuple[str, ...]
        空间维度名，如 ``("lat", "lon")`` 或 ``("rlat", "rlon")``。
    grid_kind : str
        ``"regular_latlon"`` 或 ``"rotated_pole"``。
    target_time_axis : str
        所用目标时间轴的可读说明。
    source_timestep_hours : float
        源数据名义时间步长（BCSD 为 3.0，ERA5-Land / CORDEX 为 1.0）。
    source_files : Sequence[str]
        实际读取的输入文件路径。
    skipped_inputs : dict[str, str]
        统一变量名 → 缺失原因。
    attrs_extra : dict[str, str]
        合并到输出 NetCDF 的额外全局属性。
    """

    source: str
    tech: str
    dataset: xr.Dataset
    spatial_dims: tuple[str, ...]
    grid_kind: str
    target_time_axis: str
    source_timestep_hours: float
    source_files: Sequence[str] = field(default_factory=list)
    skipped_inputs: dict[str, str] = field(default_factory=dict)
    attrs_extra: dict[str, str] = field(default_factory=dict)


class WeatherAdapter(ABC):
    """特定气候数据源的抽象适配器接口。"""

    # ------------------------------------------------------------------
    # 任务迭代
    # ------------------------------------------------------------------
    @abstractmethod
    def iter_tasks(self, args) -> list[dict]:
        """返回待处理任务描述列表。

        每个任务都是普通 dict，内容取决于数据源。
        示例（regional_bcsd）：
            ``{"model": ..., "region": ..., "scenario": ..., "year": ...}``
        示例（era5land_raw）：
            ``{"year": ..., "month": ...}``
        """

    # ------------------------------------------------------------------
    # 气象加载
    # ------------------------------------------------------------------
    @abstractmethod
    def load_wind_weather(self, task: dict) -> WeatherBundle:
        """加载并标准化风电事件所需气象变量。"""

    @abstractmethod
    def load_solar_weather(self, task: dict) -> WeatherBundle:
        """加载并标准化光伏事件所需气象变量。"""

    # ------------------------------------------------------------------
    # 输出路径辅助函数
    # ------------------------------------------------------------------
    @abstractmethod
    def signal_output_path(self, task: dict, tech: str) -> str:
        """返回信号输出文件的绝对路径。"""

    @abstractmethod
    def weather_output_path(self, task: dict, tech: str) -> str:
        """返回可选气象输出文件的绝对路径。"""
