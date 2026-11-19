"""几何校验与井归属计算（Shapely）。

规则：
- 边界：无洞 GeoJSON Polygon，同一平面（2D）整数坐标，几何必须有效。
- 井：2D Point，有限数值坐标。
- 宗地集合：内部互不重叠（共边/共点允许），合起来恰好覆盖区域。
- 井归属：严格落在唯一宗地内部 → assigned；落在一条/多条宗地边界上
  → pending 并列出全部候选宗地；不沾任何宗地 → uncovered。

容差说明：整数顶点多边形的面积是 0.5 的整数倍（鞋带公式），因此真实的
空缺/重叠面积至少为 0.5，而浮点噪声远小于 1e-6，用 AREA_TOL 即可可靠区分。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from shapely.geometry import Point, Polygon, shape
from shapely.ops import unary_union
from shapely.validation import explain_validity

AREA_TOL = 1e-6


# ---------------------------------------------------------------- 基础检查

def _is_finite_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _check_position(pos: Any, require_integer: bool) -> Optional[str]:
    if not isinstance(pos, (list, tuple)) or len(pos) != 2:
        return "坐标必须是 [x, y] 二元组（同一平面，不接受高程维）"
    x, y = pos
    if not (_is_finite_number(x) and _is_finite_number(y)):
        return "坐标必须是有限数值"
    if require_integer and not (float(x).is_integer() and float(y).is_integer()):
        return f"边界坐标必须为整数，得到 ({x}, {y})"
    return None


def _err(code: str, message: str, **extra: Any) -> dict:
    return {"code": code, "message": message, **extra}


# ---------------------------------------------------------------- 几何解析

def parse_polygon(geom: Any, *, parcel_id: Optional[str] = None,
                  label: str = "geometry") -> tuple[Optional[Polygon], list[dict]]:
    """解析并校验无洞、整数坐标、有效的 GeoJSON Polygon。"""
    errors: list[dict] = []
    tag = {"parcel_id": parcel_id} if parcel_id is not None else {}

    def fail(code: str, message: str, **extra: Any) -> None:
        errors.append(_err(code, message, **tag, **extra))

    if not isinstance(geom, dict) or geom.get("type") != "Polygon":
        fail("invalid_geometry", f"{label}：必须是 GeoJSON Polygon 几何对象")
        return None, errors

    rings = geom.get("coordinates")
    if not isinstance(rings, (list, tuple)) or not rings:
        fail("invalid_geometry", f"{label}：coordinates 必须是非空环数组")
        return None, errors
    if len(rings) > 1:
        fail("hole_not_allowed", f"{label}：边界不允许带洞（内环），发现 {len(rings) - 1} 个内环")
        return None, errors

    ring = rings[0]
    if not isinstance(ring, (list, tuple)) or len(ring) < 4:
        fail("invalid_geometry", f"{label}：外环至少需要 4 个坐标点（首尾闭合）")
        return None, errors
    for pos in ring:
        problem = _check_position(pos, require_integer=True)
        if problem:
            fail("non_integer_coordinate", f"{label}：{problem}")
            return None, errors

    try:
        poly = shape({"type": "Polygon", "coordinates": [list(ring)]})
    except Exception as exc:  # noqa: BLE001 - shapely 抛出的异常类型不固定
        fail("invalid_geometry", f"{label}：无法构造多边形：{exc}")
        return None, errors

    if poly.is_empty or poly.area <= 0:
        fail("invalid_geometry", f"{label}：多边形面积为 0")
        return None, errors
    if not poly.is_valid:
        fail("invalid_geometry", f"{label}：多边形无效（{explain_validity(poly)}）")
        return None, errors
    return poly, errors


def parse_point(geom: Any, *, well_id: Optional[str] = None,
                label: str = "geometry") -> tuple[Optional[Point], list[dict]]:
    """解析并校验 GeoJSON Point（井）。"""
    errors: list[dict] = []
    tag = {"well_id": well_id} if well_id is not None else {}

    def fail(code: str, message: str, **extra: Any) -> None:
        errors.append(_err(code, message, **tag, **extra))

    if not isinstance(geom, dict) or geom.get("type") != "Point":
        fail("invalid_geometry", f"{label}：必须是 GeoJSON Point 几何对象")
        return None, errors
    problem = _check_position(geom.get("coordinates"), require_integer=False)
    if problem:
        fail("invalid_geometry", f"{label}：{problem}")
        return None, errors
    x, y = geom["coordinates"]
    return Point(float(x), float(y)), errors


# ---------------------------------------------------------------- 宗地集合核查

@dataclass
class TilingReport:
    ok: bool
    gap_area: float = 0.0
    overflow_area: float = 0.0
    overlaps: list[dict] = field(default_factory=list)        # [{"parcel_ids": [a, b], "area": x}]
    overlap_parcel_ids: list[str] = field(default_factory=list)
    reasons: list[dict] = field(default_factory=list)


def validate_tiling(parcels: dict[str, Polygon], region: Polygon) -> TilingReport:
    """核查整批新边界：在区域内、互不重叠、恰好覆盖区域。"""
    reasons: list[dict] = []
    overlaps: list[dict] = []

    # 1) 每块宗地必须位于区域内
    for pid, geom in parcels.items():
        outside = geom.difference(region)
        if outside.area > AREA_TOL:
            reasons.append(_err(
                "outside_region",
                f"宗地 {pid} 超出区域范围 {outside.area:.6g} 平方单位",
                parcel_id=pid, area=round(outside.area, 6),
            ))

    # 2) 两两内部不重叠（共边/共点面积为零，允许）
    ids = sorted(parcels)
    geoms = {pid: parcels[pid] for pid in ids}
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            inter = geoms[a].intersection(geoms[b])
            if inter.area > AREA_TOL:
                overlaps.append({"parcel_ids": [a, b], "area": round(inter.area, 6)})
    for ov in overlaps:
        a, b = ov["parcel_ids"]
        reasons.append(_err(
            "overlap",
            f"宗地 {a} 与 {b} 内部重叠 {ov['area']:.6g} 平方单位",
            parcel_ids=[a, b], area=ov["area"],
        ))

    # 3) 合起来恰好覆盖区域
    union = unary_union(list(parcels.values()))
    gap_area = region.difference(union).area
    overflow_area = union.difference(region).area
    if gap_area > AREA_TOL:
        reasons.append(_err(
            "gap",
            f"宗地合起来未覆盖区域，空缺 {gap_area:.6g} 平方单位",
            area=round(gap_area, 6),
        ))
    if overflow_area > AREA_TOL:
        reasons.append(_err(
            "overflow",
            f"宗地合起来超出区域 {overflow_area:.6g} 平方单位",
            area=round(overflow_area, 6),
        ))

    overlap_ids = sorted({pid for ov in overlaps for pid in ov["parcel_ids"]})
    return TilingReport(
        ok=not reasons,
        gap_area=round(gap_area, 6),
        overflow_area=round(overflow_area, 6),
        overlaps=overlaps,
        overlap_parcel_ids=overlap_ids,
        reasons=reasons,
    )


# ---------------------------------------------------------------- 井归属

def attribute_wells(wells: dict[str, Point],
                    parcels: dict[str, Polygon]) -> dict[str, dict]:
    """按新边界重算全部井归属。

    - 严格位于唯一宗地内部 → assigned
    - 落在宗地边界上（共边/共点）→ pending，candidates 列出所有候选宗地
    - 不沾任何宗地（含区域外）→ uncovered
    """
    items = sorted(parcels.items())
    result: dict[str, dict] = {}
    for wid in sorted(wells):
        pt = wells[wid]
        containing = [pid for pid, geom in items if geom.contains(pt)]
        if len(containing) == 1:
            result[wid] = {"status": "assigned", "parcel_id": containing[0],
                           "candidates": containing}
            continue
        touching = [pid for pid, geom in items if geom.intersects(pt)]
        if touching:
            result[wid] = {"status": "pending", "parcel_id": None,
                           "candidates": touching}
        else:
            result[wid] = {"status": "uncovered", "parcel_id": None,
                           "candidates": []}
    return result
