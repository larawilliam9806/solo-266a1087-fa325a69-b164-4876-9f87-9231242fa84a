"""FastAPI 入口：版本化发布、整批替换、同版一致性查询、污染覆盖分析。

并发与原子性：
- 重活（几何核查、井归属重算）在写事务之外基于 base 版本快照完成；
- 写事务用 BEGIN IMMEDIATE 串行化，进入后先核对 meta.current_revision
  仍等于 base_revision，不等则回滚返回 409 —— 同版并发修订只成功一份；
- 版本行、宗地行、井归属行、版本指针在同一个事务里提交，失败/重启不留半版。
- 污染覆盖分析同样基于快照计算，提交时复核修订号：过期请求返回 409 且
  不落任何报告；已完成报告不可变，边界更新后仍按原值回查。
"""
from __future__ import annotations

import json
import sqlite3
import math
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .db import connect, init_db
from .geo import (
    attribute_wells,
    compute_coverage,
    parse_point,
    parse_polygon,
    validate_tiling,
)
from .schemas import CoverageAnalysisRequest, InitRequest, ReplaceRequest


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="宗地边界与地下水采样井归属发布 API",
    version="1.0.0",
    lifespan=lifespan,
)


def _sanitize_json(value: Any) -> Any:
    """把非有限浮点（NaN/Infinity）及其余不可 JSON 序列化对象转为可编码形式。"""
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, dict):
        return {k: _sanitize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_json(v) for v in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    # Pydantic v2 的 ctx 里可能内嵌异常等对象
    return str(value)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # 默认 422 契约会回显输入；非有限浮点无法被严格 JSON 编码，先清洗再返回。
    return JSONResponse(
        status_code=422,
        content={"detail": _sanitize_json(exc.errors())},
    )


# ---------------------------------------------------------------- 工具

def _err(status: int, payload: dict) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": payload})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _current_revision(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute("SELECT current_revision FROM meta WHERE id = 1").fetchone()
    return int(row[0]) if row is not None else None


def _load_region(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT geojson FROM regions WHERE id = 1").fetchone()
    return json.loads(row[0])


def _load_parcels(conn: sqlite3.Connection, revision: int) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT parcel_id, geojson FROM parcels WHERE revision = ? ORDER BY parcel_id",
        (revision,),
    ).fetchall()
    return {r[0]: json.loads(r[1]) for r in rows}


def _load_well_geoms(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT well_id, geojson FROM wells ORDER BY well_id").fetchall()
    return {r[0]: json.loads(r[1]) for r in rows}


def _load_attributions(conn: sqlite3.Connection, revision: int) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT well_id, status, parcel_id, candidates FROM well_attributions "
        "WHERE revision = ? ORDER BY well_id",
        (revision,),
    ).fetchall()
    return {
        r[0]: {"status": r[1], "parcel_id": r[2], "candidates": json.loads(r[3])}
        for r in rows
    }


def _insert_version(conn: sqlite3.Connection, revision: int,
                    parcels: dict[str, dict], attributions: dict[str, dict],
                    region: Optional[dict], wells: Optional[dict[str, dict]]) -> None:
    """在已开启的事务内写入一个完整版本（不含 meta 指针）。"""
    if region is not None:
        conn.execute("INSERT INTO regions (id, geojson) VALUES (1, ?)",
                     (json.dumps(region),))
    if wells is not None:
        conn.executemany(
            "INSERT INTO wells (well_id, geojson) VALUES (?, ?)",
            [(wid, json.dumps(g)) for wid, g in wells.items()],
        )
    conn.execute(
        "INSERT INTO versions (revision, created_at, parcel_count, well_count) "
        "VALUES (?, ?, ?, ?)",
        (revision, _now(), len(parcels),
         len(wells) if wells is not None else len(attributions)),
    )
    conn.executemany(
        "INSERT INTO parcels (revision, parcel_id, geojson) VALUES (?, ?, ?)",
        [(revision, pid, json.dumps(g)) for pid, g in parcels.items()],
    )
    conn.executemany(
        "INSERT INTO well_attributions (revision, well_id, status, parcel_id, candidates) "
        "VALUES (?, ?, ?, ?, ?)",
        [(revision, wid, a["status"], a["parcel_id"], json.dumps(a["candidates"]))
         for wid, a in attributions.items()],
    )


def _validation_error(reasons: list[dict], *, gap_area: Optional[float] = None,
                      overlaps: Optional[list] = None,
                      overlap_parcel_ids: Optional[list] = None) -> JSONResponse:
    return _err(422, {
        "error": "validation_failed",
        "message": "整批新边界未通过核查，当前版本保持不变",
        "gap_area": gap_area,
        "overlaps": overlaps or [],
        "overlap_parcel_ids": overlap_parcel_ids or [],
        "reasons": reasons,
    })


def _well_summary(attributions: dict[str, dict]) -> dict:
    summary = {"assigned": 0, "pending": 0, "uncovered": 0}
    for a in attributions.values():
        summary[a["status"]] += 1
    return summary


def _parse_parcels(items, reasons: list[dict]) -> tuple[dict[str, Any], dict[str, dict]]:
    """解析一批宗地几何，把重复编号与几何错误追加到 reasons。

    返回 (id -> shapely Polygon, id -> 原始 GeoJSON)。
    """
    geoms: dict[str, Any] = {}
    geojson_by_id: dict[str, dict] = {}
    for p in items:
        if p.id in geojson_by_id:
            reasons.append({"code": "duplicate_parcel_id", "parcel_id": p.id,
                            "message": f"宗地编号 {p.id} 在请求中重复"})
            continue
        geom, errs = parse_polygon(p.geometry, parcel_id=p.id, label=f"宗地 {p.id}")
        reasons.extend(errs)
        if geom is not None:
            geoms[p.id] = geom
            geojson_by_id[p.id] = p.geometry
    return geoms, geojson_by_id


# ---------------------------------------------------------------- 首次提交

@app.post("/v1/init", status_code=201)
def initialize(req: InitRequest):
    reasons: list[dict] = []

    region_geom, errs = parse_polygon(req.region, label="区域")
    reasons.extend(errs)

    parcel_geoms, parcel_gj = _parse_parcels(req.parcels, reasons)

    well_geoms: dict[str, Any] = {}
    well_gj: dict[str, dict] = {}
    for w in req.wells:
        if w.id in well_gj:
            reasons.append({"code": "duplicate_well_id", "well_id": w.id,
                            "message": f"井编号 {w.id} 在请求中重复"})
            continue
        geom, errs = parse_point(w.geometry, well_id=w.id, label=f"井 {w.id}")
        reasons.extend(errs)
        if geom is not None:
            well_geoms[w.id] = geom
            well_gj[w.id] = w.geometry

    tiling = None
    if region_geom is not None and len(parcel_geoms) == len(req.parcels):
        tiling = validate_tiling(parcel_geoms, region_geom)
        reasons.extend(tiling.reasons)
    if reasons:
        return _validation_error(
            reasons,
            gap_area=tiling.gap_area if tiling else None,
            overlaps=tiling.overlaps if tiling else None,
            overlap_parcel_ids=tiling.overlap_parcel_ids if tiling else None,
        )

    attributions = attribute_wells(well_geoms, parcel_geoms)

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _current_revision(conn) is not None:
                conn.execute("ROLLBACK")
                return _err(409, {"error": "already_initialized",
                                  "message": "数据集已完成首次提交，不能重复初始化"})
            _insert_version(conn, 1, parcel_gj, attributions,
                            region=req.region, wells=well_gj)
            conn.execute("INSERT INTO meta (id, current_revision) VALUES (1, 1)")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {"revision": 1, "parcel_count": len(parcel_gj),
            "well_count": len(well_gj), "well_summary": _well_summary(attributions)}


# ---------------------------------------------------------------- 整批替换

@app.put("/v1/parcels")
def replace_parcels(req: ReplaceRequest):
    # 1) 读取 base 版本快照（读事务，不阻塞他人）
    conn = connect()
    try:
        current = _current_revision(conn)
        if current is None:
            return _err(404, {"error": "not_initialized",
                              "message": "尚未首次提交，请先调用 POST /v1/init"})
        if req.base_revision != current:
            return _err(409, {
                "error": "revision_conflict",
                "message": f"所见修订号 {req.base_revision} 已过期，当前为 {current}，"
                           "请重新读取后再提交",
                "current_revision": current,
                "base_revision": req.base_revision,
            })
        region_gj = _load_region(conn)
        old_parcel_gj = _load_parcels(conn, current)
        well_gj = _load_well_geoms(conn)
    finally:
        conn.close()

    # 2) 应用整批替换：只允许替换当前版本中存在的宗地
    reasons: list[dict] = []
    region_geom, _ = parse_polygon(region_gj, label="区域")
    old_geoms = {}
    for pid, gj in old_parcel_gj.items():
        geom, _ = parse_polygon(gj, parcel_id=pid, label=f"宗地 {pid}")
        old_geoms[pid] = geom

    new_geoms = dict(old_geoms)
    new_parcel_gj = dict(old_parcel_gj)
    seen: set[str] = set()
    for p in req.parcels:
        if p.id in seen:
            reasons.append({"code": "duplicate_parcel_id", "parcel_id": p.id,
                            "message": f"宗地编号 {p.id} 在请求中重复"})
            continue
        seen.add(p.id)
        if p.id not in old_parcel_gj:
            reasons.append({"code": "unknown_parcel_id", "parcel_id": p.id,
                            "message": f"宗地 {p.id} 在当前版本中不存在，"
                                       "只能整批替换已有宗地"})
            continue
        geom, errs = parse_polygon(p.geometry, parcel_id=p.id, label=f"宗地 {p.id}")
        reasons.extend(errs)
        if geom is not None:
            new_geoms[p.id] = geom
            new_parcel_gj[p.id] = p.geometry
    if reasons:
        return _validation_error(reasons)

    # 3) 相邻宗地联改按整批新边界核查
    tiling = validate_tiling(new_geoms, region_geom)
    if not tiling.ok:
        return _validation_error(
            tiling.reasons,
            gap_area=tiling.gap_area,
            overlaps=tiling.overlaps,
            overlap_parcel_ids=tiling.overlap_parcel_ids,
        )

    # 4) 依新版边界重算全部井归属
    well_geoms = {}
    for wid, gj in well_gj.items():
        geom, _ = parse_point(gj, well_id=wid, label=f"井 {wid}")
        well_geoms[wid] = geom
    attributions = attribute_wells(well_geoms, new_geoms)

    # 5) 乐观并发提交：同版并发修订只成功一份
    new_revision = current + 1
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _current_revision(conn) != req.base_revision:
                conn.execute("ROLLBACK")
                return _err(409, {
                    "error": "revision_conflict",
                    "message": "提交期间已有其他修订发布，本次修订作废，请基于新版重试",
                    "current_revision": _current_revision(conn),
                    "base_revision": req.base_revision,
                })
            _insert_version(conn, new_revision, new_parcel_gj, attributions,
                            region=None, wells=None)
            cur = conn.execute(
                "UPDATE meta SET current_revision = ? WHERE id = 1 AND current_revision = ?",
                (new_revision, req.base_revision),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                return _err(409, {"error": "revision_conflict",
                                  "message": "版本指针已被其他修订更新",
                                  "base_revision": req.base_revision})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {"revision": new_revision, "parcel_count": len(new_parcel_gj),
            "well_count": len(well_gj), "well_summary": _well_summary(attributions)}


# ---------------------------------------------------------------- 查询

def _wells_payload(well_gj: dict[str, dict], attributions: dict[str, dict]) -> list[dict]:
    return [
        {"id": wid, "geometry": well_gj[wid], **attributions[wid]}
        for wid in sorted(well_gj)
    ]


@app.get("/v1/current")
def get_current():
    """当前完整版本：区域 + 边界 + 井归属，同一读事务取出，保证同版一致。"""
    conn = connect()
    try:
        conn.execute("BEGIN")
        try:
            revision = _current_revision(conn)
            if revision is None:
                conn.execute("ROLLBACK")
                return _err(404, {"error": "not_initialized",
                                  "message": "尚未首次提交"})
            region = _load_region(conn)
            parcels = _load_parcels(conn, revision)
            well_gj = _load_well_geoms(conn)
            attributions = _load_attributions(conn, revision)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {
        "revision": revision,
        "region": region,
        "parcels": [{"id": pid, "geometry": parcels[pid]} for pid in sorted(parcels)],
        "wells": _wells_payload(well_gj, attributions),
    }


@app.get("/v1/parcels")
def get_parcels():
    """当前宗地边界（响应携带所属修订号）。"""
    conn = connect()
    try:
        revision = _current_revision(conn)
        if revision is None:
            return _err(404, {"error": "not_initialized", "message": "尚未首次提交"})
        parcels = _load_parcels(conn, revision)
    finally:
        conn.close()
    return {"revision": revision,
            "parcels": [{"id": pid, "geometry": parcels[pid]} for pid in sorted(parcels)]}


@app.get("/v1/wells")
def get_wells():
    """当前井归属（响应携带所属修订号，与 /v1/parcels 对号即可验证同版）。"""
    conn = connect()
    try:
        revision = _current_revision(conn)
        if revision is None:
            return _err(404, {"error": "not_initialized", "message": "尚未首次提交"})
        well_gj = _load_well_geoms(conn)
        attributions = _load_attributions(conn, revision)
    finally:
        conn.close()
    return {"revision": revision, "wells": _wells_payload(well_gj, attributions)}


@app.get("/v1/revisions")
def list_revisions():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT revision, created_at, parcel_count, well_count "
            "FROM versions ORDER BY revision"
        ).fetchall()
    finally:
        conn.close()
    return {"revisions": [dict(r) for r in rows]}


@app.get("/v1/revisions/{revision}")
def get_revision(revision: int):
    """读取指定历史版本（含被替换前的旧版）。"""
    conn = connect()
    try:
        conn.execute("BEGIN")
        try:
            row = conn.execute(
                "SELECT revision, created_at, parcel_count, well_count "
                "FROM versions WHERE revision = ?", (revision,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return _err(404, {"error": "revision_not_found",
                                  "message": f"版本 {revision} 不存在"})
            region = _load_region(conn)
            parcels = _load_parcels(conn, revision)
            well_gj = _load_well_geoms(conn)
            attributions = _load_attributions(conn, revision)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {
        "revision": row[0],
        "created_at": row[1],
        "region": region,
        "parcels": [{"id": pid, "geometry": parcels[pid]} for pid in sorted(parcels)],
        "wells": _wells_payload(well_gj, attributions),
    }


# ---------------------------------------------------------------- 污染覆盖分析

def _coverage_conflict(base_revision: int, current: int) -> JSONResponse:
    return _err(409, {
        "error": "revision_conflict",
        "message": f"分析请求基于修订号 {base_revision}，当前边界已更新到 "
                   f"{current}，请求过期，未生成报告，请基于新版重试",
        "current_revision": current,
        "base_revision": base_revision,
    })


@app.post("/v1/coverage/analyze", status_code=201)
def analyze_coverage(req: CoverageAnalysisRequest):
    """提交污染覆盖分析：按井位圆形范围拆分每块宗地的超限/达标/未采样面积。

    - 未知井号（或请求内井号重复）整批 422 拒绝，不落报告；
    - base_revision 与当前边界修订号不一致 → 409，不留报告；
    - 输入、修订号与结果在同一事务落库，返回 report_id 供回查；
    - 已完成报告不可变，之后边界更新不影响历史报告。
    """
    # 1) 快照读取当前边界与井位（读事务，不阻塞写者）
    conn = connect()
    try:
        current = _current_revision(conn)
        if current is None:
            return _err(404, {"error": "not_initialized",
                              "message": "尚未首次提交，请先调用 POST /v1/init"})
        if req.base_revision != current:
            return _coverage_conflict(req.base_revision, current)
        region_gj = _load_region(conn)
        parcel_gj = _load_parcels(conn, current)
        well_gj = _load_well_geoms(conn)
    finally:
        conn.close()

    # 2) 语义校验：井号唯一、必须全部已知，否则整批拒绝
    reasons: list[dict] = []
    seen: set[str] = set()
    samples: list[tuple[str, float]] = []
    for s in req.samples:
        if s.well_id in seen:
            reasons.append({"code": "duplicate_well_id", "well_id": s.well_id,
                            "message": f"井编号 {s.well_id} 在分析请求中重复"})
            continue
        seen.add(s.well_id)
        if s.well_id not in well_gj:
            reasons.append({"code": "unknown_well_id", "well_id": s.well_id,
                            "message": f"井 {s.well_id} 不存在，分析请求整批拒绝"})
            continue
        samples.append((s.well_id, s.concentration))
    if reasons:
        return _err(422, {
            "error": "validation_failed",
            "message": "分析请求未通过校验，未生成报告",
            "reasons": reasons,
            "unknown_well_ids": sorted(
                {r["well_id"] for r in reasons if r["code"] == "unknown_well_id"}),
        })

    # 3) 解析几何并基于快照计算覆盖面积（重活在写事务外完成）
    region_geom, _ = parse_polygon(region_gj, label="区域")
    parcel_geoms: dict[str, Any] = {}
    for pid, gj in parcel_gj.items():
        geom, _ = parse_polygon(gj, parcel_id=pid, label=f"宗地 {pid}")
        parcel_geoms[pid] = geom
    well_points: dict[str, Any] = {}
    for wid, gj in well_gj.items():
        pt, _ = parse_point(gj, well_id=wid, label=f"井 {wid}")
        well_points[wid] = pt

    coverage = compute_coverage(
        parcels=parcel_geoms, region=region_geom, well_points=well_points,
        samples=samples, radius=req.radius, limit=req.limit,
    )

    # 4) 乐观并发提交：计算期间若边界已更新，拒绝且不留报告
    report_id = uuid.uuid4().hex
    payload = {
        "report_id": report_id,
        "base_revision": req.base_revision,
        "radius": req.radius,
        "limit": req.limit,
        "samples": [{"well_id": wid, "concentration": conc}
                    for wid, conc in samples],
        "created_at": _now(),
        **coverage,
    }
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current_now = _current_revision(conn)
            if current_now != req.base_revision:
                conn.execute("ROLLBACK")
                return _coverage_conflict(req.base_revision, current_now)
            conn.execute(
                "INSERT INTO coverage_reports "
                "(report_id, base_revision, radius, limit_value, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (report_id, req.base_revision, req.radius, req.limit,
                 json.dumps(payload), payload["created_at"]),
            )
            conn.executemany(
                "INSERT INTO coverage_samples (report_id, well_id, concentration) "
                "VALUES (?, ?, ?)",
                [(report_id, wid, conc) for wid, conc in samples],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return payload


@app.get("/v1/coverage/reports/{report_id}")
def get_coverage_report(report_id: str):
    """按报告 ID 回查：返回保存的输入、修订号与原始计算结果（永不被后续修订改写）。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT payload FROM coverage_reports WHERE report_id = ?",
            (report_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return _err(404, {"error": "report_not_found",
                          "message": f"覆盖分析报告 {report_id} 不存在"})
    return json.loads(row[0])


@app.get("/healthz")
def healthz():
    return {"status": "ok"}