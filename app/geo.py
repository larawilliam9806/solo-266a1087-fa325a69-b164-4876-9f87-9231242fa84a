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


# ---------------------------------------------------------------- 污染覆盖分析

ROUND_DIGITS = 6


def _safe_union(geoms: list):
    """空列表返回空几何；否则求并集（同类重叠只计一次）。"""
    if not geoms:
        return Polygon()  # 空多边形，area == 0
    return unary_union(geoms)


def compute_coverage(*, parcels: dict[str, Polygon], region: Polygon,
                     well_points: dict[str, Point],
                     samples: list[tuple[str, float]], radius: float,
                     limit: float) -> dict:
    """按井位圆形覆盖范围计算每块宗地的超限/仅达标/未采样面积。

    规则：
    - 每口采样井以井位为圆心、radius 为半径生成圆形范围（与坐标同单位）；
      区域外（圆与区域完全不沾）的井不贡献任何面积。
    - 浓度严格大于限值才超限；等于限值算达标。未采样井不生成圆，不视为达标。
    - 超限圆并集与达标圆并集求交，达标圆被超限圆覆盖的部分从达标面积中剔除
      （超限与达标范围重叠时超限优先）；同类圆重叠通过并集天然只计一次。
    - 落在宗地共边上的圆与两侧宗地分别求交，共边两侧都计入。
    - 三类面积之和等于宗地面积（先各算后按 1e-6 四舍五入，舍入残差并入
      未采样面积，保证 JSON 输出中三者之和恰等于宗地面积）。
    """
    exceed_circles: list = []
    comply_circles: list = []
    included: list[str] = []
    excluded: list[str] = []

    for well_id, concentration in samples:
        point_geom = well_points[well_id]
        if not region.intersects(point_geom):
            # 井在区域外：其圆形范围不贡献面积（共边井 region.intersects 为真，保留）
            excluded.append(well_id)
            continue
        circle = point_geom.buffer(radius, quad_segs=64)
        included.append(well_id)
        if concentration > limit:
            exceed_circles.append(circle)
        else:
            comply_circles.append(circle)

    exceed_union = _safe_union(exceed_circles)
    comply_union = _safe_union(comply_circles)
    # 达标范围中被超限范围盖住的部分让给超限（超限优先）
    comply_only = comply_union.difference(exceed_union)

    parcel_results: list[dict] = []
    for pid in sorted(parcels):
        geom = parcels[pid]
        parcel_area = geom.area
        exceed_area = geom.intersection(exceed_union).area
        comply_area = geom.intersection(comply_only).area
        unsampled_area = parcel_area - exceed_area - comply_area
        if abs(unsampled_area) < AREA_TOL:
            unsampled_area = 0.0

        exceed_r = round(exceed_area, ROUND_DIGITS)
        comply_r = round(comply_area, ROUND_DIGITS)
        parcel_r = round(parcel_area, ROUND_DIGITS)
        # 舍入残差并入未采样面积，保证三类之和严格等于宗地面积
        unsampled_r = round(parcel_r - exceed_r - comply_r, ROUND_DIGITS)

        parcel_results.append({
            "parcel_id": pid,
            "area": parcel_r,
            "exceed_area": exceed_r,
            "compliant_area": comply_r,
            "unsampled_area": unsampled_r,
        })

    totals = {
        "area": round(sum(r["area"] for r in parcel_results), ROUND_DIGITS),
        "exceed_area": round(sum(r["exceed_area"] for r in parcel_results), ROUND_DIGITS),
        "compliant_area": round(sum(r["compliant_area"] for r in parcel_results), ROUND_DIGITS),
        "unsampled_area": round(sum(r["unsampled_area"] for r in parcel_results), ROUND_DIGITS),
    }
    return {
        "parcels": parcel_results,
        "totals": totals,
        "included_wells": included,
        "excluded_wells": excluded,
    }
