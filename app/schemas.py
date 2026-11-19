"""请求体的 Pydantic 模型（结构校验；几何语义校验在 geo.py 中做）。"""
from __future__ import annotations

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
