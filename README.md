# 宗地边界与地下水采样井归属发布 API

基于 **FastAPI + Shapely + SQLite** 的空间数据发布服务：宗地边界版本化发布，
每次边界修订通过核查后，自动按新版边界重算全部地下水采样井的归属；
下游读到的边界与井归属永远来自同一个完整版本。

## 快速启动

```bash
docker compose up --build
# 服务监听 http://localhost:8000 ，交互式文档见 http://localhost:8000/docs
```

本地开发（Python 3.11+）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest                 # 运行测试
```

数据默认存放在容器卷 `/data/app.db`（环境变量 `DB_PATH` 可改）。

## 数据规则

- **区域**：首次提交时确定，单环、无洞 GeoJSON `Polygon`，同一平面 2D **整数坐标**，几何必须有效；之后不可变。
- **宗地**：至多 40 块，编号唯一（字符串或整数均可，内部统一为字符串）；同样是无洞整数坐标 Polygon。
- **井**：至多 100 口，编号唯一，GeoJSON `Point`（2D 有限数值坐标）；首次提交后几何不可变。
- 宗地集合必须：**全部位于区域内、内部互不重叠（共边/共点允许）、合起来恰好覆盖区域**。
  整数顶点多边形面积是 0.5 的整数倍，因此用 1e-6 容差即可把浮点噪声与真实空缺/重叠可靠分开。

## 井归属规则（每次发布时全量重算）

| 井的位置 | status | 说明 |
|---|---|---|
| 严格位于唯一宗地内部 | `assigned` | `parcel_id` 为所属宗地 |
| 落在宗地边界上（共边/共点） | `pending`（待核） | `candidates` 列出**所有**候选宗地 |
| 不沾任何宗地（含区域外） | `uncovered`（未覆盖） | `candidates` 为空 |

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/v1/init` | 首次提交：区域 + 宗地 + 井 → 版本 1（仅允许一次） |
| `PUT` | `/v1/parcels` | 携带 `base_revision` 整批替换指定宗地，成功后发布新版本 |
| `GET` | `/v1/current` | 当前完整版本：区域 + 边界 + 井归属（同一读事务取出） |
| `GET` | `/v1/parcels` | 当前宗地边界（响应带 `revision`） |
| `GET` | `/v1/wells` | 当前井归属（响应带 `revision`，可与边界响应对号验证同版） |
| `GET` | `/v1/revisions` | 版本列表 |
| `GET` | `/v1/revisions/{n}` | 读取指定历史版本 |
| `GET` | `/healthz` | 健康检查 |

### 首次提交

```bash
curl -X POST http://localhost:8000/v1/init -H 'Content-Type: application/json' -d '{
  "region":  {"type":"Polygon","coordinates":[[[0,0],[100,0],[100,100],[0,100],[0,0]]]},
  "parcels": [
    {"id":"P1","geometry":{"type":"Polygon","coordinates":[[[0,0],[50,0],[50,100],[0,100],[0,0]]]}},
    {"id":"P2","geometry":{"type":"Polygon","coordinates":[[[50,0],[100,0],[100,100],[50,100],[50,0]]]}}
  ],
  "wells": [
    {"id":"W1","geometry":{"type":"Point","coordinates":[25,50]}},
    {"id":"W2","geometry":{"type":"Point","coordinates":[50,50]}},
    {"id":"W3","geometry":{"type":"Point","coordinates":[150,50]}}
  ]
}'
# => {"revision":1,"parcel_count":2,"well_count":3,
#     "well_summary":{"assigned":1,"pending":1,"uncovered":1}}
```

### 整批替换（相邻宗地联改一次提交）

把 P1/P2 的共边从 x=50 移到 x=60，两块一起改、一次核查：

```bash
curl -X PUT http://localhost:8000/v1/parcels -H 'Content-Type: application/json' -d '{
  "base_revision": 1,
  "parcels": [
    {"id":"P1","geometry":{"type":"Polygon","coordinates":[[[0,0],[60,0],[60,100],[0,100],[0,0]]]}},
    {"id":"P2","geometry":{"type":"Polygon","coordinates":[[[60,0],[100,0],[100,100],[60,100],[60,0]]]}}
  ]
}'
# => {"revision":2, ...}   全部井已按新边界重算归属
```

- 只能替换当前版本中**已存在**的宗地编号；未提交的宗地沿用旧边界。
- 核查按**整批新边界**（替换后完整集合）进行，任一不满足即整体拒绝。

### 核查失败（422）：返回空缺面积、重叠宗地编号及原因，原版继续可读

```json
{
  "detail": {
    "error": "validation_failed",
    "message": "整批新边界未通过核查，当前版本保持不变",
    "gap_area": 1000.0,
    "overlaps": [{"parcel_ids": ["P1", "P2"], "area": 500.0}],
    "overlap_parcel_ids": ["P1", "P2"],
    "reasons": [
      {"code": "gap", "message": "宗地合起来未覆盖区域，空缺 1000 平方单位", "area": 1000.0},
      {"code": "overlap", "parcel_ids": ["P1", "P2"], "area": 500.0, "message": "..."}
    ]
  }
}
```

`reasons[].code` 取值：`invalid_geometry` / `hole_not_allowed` /
`non_integer_coordinate` / `outside_region` / `overlap` / `gap` / `overflow` /
`unknown_parcel_id` / `duplicate_parcel_id` / `duplicate_well_id`。

### 修订号冲突（409）

`base_revision` 与当前版本不一致（含提交期间被他人抢先发布）时返回 409，
响应携带当前修订号，客户端重新读取后可基于新版重试。

## 并发与原子性设计

- **同版并发修订只成功一份**：几何核查与井归属重算在写事务外基于快照完成；
  提交时 `BEGIN IMMEDIATE` 串行化写者，进入事务先核对 `meta.current_revision`
  仍等于 `base_revision`，并以 `UPDATE ... WHERE current_revision = :base`
  作为最终闸门，后到者整体回滚返回 409。
- **失败或重启不留半版**：版本行、宗地行、井归属行、版本指针在同一个
  SQLite 事务中提交，中途异常或进程被杀即自动回滚；启动时还会防御性清理
  任何超出当前指针的版本残留。
- **同版一致性**：`/v1/current` 在单个读事务（WAL 快照）中取出边界与井归属；
  单独调用 `/v1/parcels` 与 `/v1/wells` 时比较两个响应的 `revision`
  即可确认是否同版。

## 项目结构

```
app/
  main.py      # FastAPI 入口与端点
  geo.py       # 几何校验、覆盖核查、井归属计算（Shapely）
  db.py        # SQLite 连接、建表、启动清理
  schemas.py   # 请求模型（Pydantic）
tests/test_api.py
Dockerfile / docker-compose.yml
```
