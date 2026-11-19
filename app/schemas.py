"""请求体的 Pydantic 模型（结构校验；几何语义校验在 geo.py 中做）。"""
from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, Field, field_validator

MAX_PARCELS = 40
MAX_WELLS = 100


def _coerce_id(v: Any) -> Any:
    """编号允许字符串或整数，统一归一成字符串。"""
    if isinstance(v, bool):
        raise ValueError("编号必须是非空字符串或整数")
    if isinstance(v, int):
        return str(v)
    return v


def _finite_number(v: Any, *, positive: bool, label: str) -> float:
    """有限数值校验：positive=True 要求 >0，否则要求 >=0。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{label}必须是数值")
    if not math.isfinite(v):
        raise ValueError(f"{label}必须是有限数值（不接受 NaN/Infinity）")
    if positive and v <= 0:
        raise ValueError(f"{label}必须是正数")
    if not positive and v < 0:
        raise ValueError(f"{label}必须是非负数")
    return float(v)


class ParcelIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    geometry: dict[str, Any]

    _coerce = field_validator("id", mode="before")(_coerce_id)


class WellIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    geometry: dict[str, Any]

    _coerce = field_validator("id", mode="before")(_coerce_id)


class InitRequest(BaseModel):
    """首次提交：区域 + 全部宗地 + 全部井。"""
    region: dict[str, Any]
    parcels: list[ParcelIn] = Field(min_length=1, max_length=MAX_PARCELS)
    wells: list[WellIn] = Field(default_factory=list, max_length=MAX_WELLS)


class ReplaceRequest(BaseModel):
    """整批替换指定宗地：携带所见修订号 + 这些宗地的完整新边界。"""
    base_revision: int = Field(ge=1)
    parcels: list[ParcelIn] = Field(min_length=1, max_length=MAX_PARCELS)


class CoverageSampleIn(BaseModel):
    """一口井的采样读数：井号 + 有限非负浓度。"""
    well_id: str = Field(min_length=1, max_length=64)
    concentration: float

    _coerce = field_validator("well_id", mode="before")(_coerce_id)
    _conc = field_validator("concentration")(
        lambda v: _finite_number(v, positive=False, label="浓度"))


class CoverageAnalysisRequest(BaseModel):
    """污染覆盖分析：边界修订号 + 同坐标单位的半径与浓度限值 + 采样读数。"""
    base_revision: int = Field(ge=1)
    radius: float
    limit: float
    samples: list[CoverageSampleIn] = Field(default_factory=list, max_length=MAX_WELLS)

    _radius = field_validator("radius")(
        lambda v: _finite_number(v, positive=True, label="半径"))
    _limit = field_validator("limit")(
        lambda v: _finite_number(v, positive=False, label="浓度限值"))
