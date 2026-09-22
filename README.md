# Cleanroom Ventilation Minimum-Cut Service

洁净厂房局部污染发生时，设施工程师需要关闭一组**有方向**的通风管段，使所有
污染源区域都无法到达任何保护区域，同时把总停产代价降到最低。本服务用
FastAPI + PostgreSQL 实现方案的保存、最小割计算、采用与已采用结果查询，
并自行实现支持 64 位整数容量的多源多汇最大流 / 最小割算法。

## 1. 核心语义

- **区域（area）** 与 **管段（segment）** 的 ID 均匹配
  `^[A-Za-z0-9_-]{1,32}$`，且在各自命名空间内唯一。
- 每条管段有 `from`、`to` 与整数关闭费用 `cost ∈ [0, 10^9]`；管段**有方向，
  不可反转**。区域至多 300 个、管段至多 2000 条。
- 污染源集合 `sources` 与保护区集合 `sinks` 均非空、互不相交，且只能引用
  已声明的区域。
- 求解器（`app/maxflow.py`，自实现 Dinic 阻塞流）加入容量为
  `Σcost + 1` 的超级源 / 超级汇边后求最大流；最小割总费用等于最大流值。
  Python 整数无溢出，算法额外校验总容量不超过有符号 64 位范围
  （本例上限 2000 × 10^9 = 2×10^12，天然满足）。
- **并列割的唯一选择**：最大流结束后，在残量网络中从超级源 BFS，得到的
  可达集合恰好是所有最小割源侧的交集，即按集合包含关系最小的源侧。该集合
  只取决于网络本身，与管段提交顺序和增广路的内部选择无关。
- 返回三项结果：源侧区域（字典序升序）、被切断的管段 ID（字典序升序）、
  总关闭费用。被切断管段 = 尾端在源侧、头端在汇侧的全部管段。
- 方案在存储前被规范化（各列表排序）并计算规范化 JSON 的 SHA-256；相同
  语义内容无论提交顺序如何，哈希与计算结果都相同。

## 2. 运行（Docker Compose）

```bash
docker compose up --build
```

启动两个容器：

| 服务 | 端口 / 存储 | 说明 |
| ---- | ----------- | ---- |
| `api` | `http://localhost:8000` | FastAPI / uvicorn |
| `db`  | 命名卷 `pgdata` 挂载到 `/var/lib/postgresql/data` | PostgreSQL 16，数据在容器重建后保留 |

API 会在启动时自动建表（`plans` / `computations` / `adopted`），并等待数据库
健康检查通过。数据库连接串由环境变量 `DATABASE_URL` 指定（compose 已配置）。
不设置 `DATABASE_URL` 直接 `uvicorn app.api:app` 时使用内存仓储，仅用于本地
测试，不持久化。

停止后数据仍在卷中：

```bash
docker compose down          # 保留 pgdata 卷
docker compose down -v       # 同时删除数据卷
```

健康检查：

```bash
curl -s http://localhost:8000/healthz
# {"status":"ok","storage":"postgresql"}
```

## 3. 接口说明与调用示例

所有请求 / 响应均为普通 JSON（`Content-Type: application/json`）。
动作类 POST（compute / adopt）允许空请求体，或发送 `{}`；若携带任何字段
会返回 `UNEXPECTED_FIELDS`。错误响应统一形如：

```json
{ "error": { "code": "STABLE_CODE", "message": "human readable" } }
```

### 3.1 保存（整版替换）方案 — `PUT /api/plans/{plan_id}`

请求体必须恰好包含 `areas`、`segments`、`sources`、`sinks` 四个字段。
校验**先于**任何数据库写入：非法整版不会创建或改动既有方案及其版本号。

```bash
curl -sS -X PUT http://localhost:8000/api/plans/zone-a \
  -H 'Content-Type: application/json' \
  -d '{
    "areas": ["src", "a", "b", "sink"],
    "segments": [
      {"id": "e1", "from": "src", "to": "a",    "cost": 3},
      {"id": "e2", "from": "a",   "to": "b",    "cost": 4},
      {"id": "e3", "from": "b",   "to": "sink", "cost": 5},
      {"id": "e4", "from": "src", "to": "sink", "cost": 9}
    ],
    "sources": ["src"],
    "sinks": ["sink"]
  }'
```

响应（列表已规范化排序；再次整版替换合法内容会让 `version` 递增）：

```json
{
  "plan_id": "zone-a",
  "version": 1,
  "content_hash": "9f2c3a……（64 个十六进制字符的 SHA-256）",
  "plan": {
    "areas": ["a", "b", "sink", "src"],
    "segments": ["……按 id 排序……"],
    "sources": ["src"],
    "sinks": ["sink"]
  }
}
```

### 3.2 查询已保存方案 — `GET /api/plans/{plan_id}`

```bash
curl -sS http://localhost:8000/api/plans/zone-a
```

### 3.3 计算最小割 — `POST /api/plans/{plan_id}/compute`

```bash
curl -sS -X POST http://localhost:8000/api/plans/zone-a/compute
```

```json
{
  "computation_id": "7c8b4f0e-9b3a-4f2a-9d5c-1a2b3c4d5e6f",
  "plan_id": "zone-a",
  "content_hash": "9f2c3a……",
  "result": {
    "source_side": ["src"],
    "cut_segments": ["e1", "e4"],
    "total_cost": 12
  }
}
```

对同一 `content_hash` 重复计算会返回**同一个** `computation_id`（内容去重），
方案内容变化后再计算才会产生新的计算记录。计算结果连同当时的规范方案与
版本号一起冻结保存。

### 3.4 采用计算结果 — `POST /api/results/{computation_id}/adopt`

采用只能引用一条**已成功的计算记录**；成功后以完整快照原子地替换当前已
采用结果。采用不存在的结果、非法 UUID、非法整版或计算失败都不会改动既有
方案或已采用结果。

```bash
curl -sS -X POST \
  http://localhost:8000/api/results/7c8b4f0e-9b3a-4f2a-9d5c-1a2b3c4d5e6f/adopt
```

```json
{
  "computation_id": "7c8b4f0e-9b3a-4f2a-9d5c-1a2b3c4d5e6f",
  "adopted_at": "2026-09-22T08:30:00.123456+00:00",
  "snapshot": {
    "plan_id": "zone-a",
    "plan_version": 1,
    "content_hash": "9f2c3a……",
    "computation_id": "7c8b4f0e-9b3a-4f2a-9d5c-1a2b3c4d5e6f",
    "plan": { "……计算时冻结的完整规范方案……": "" },
    "result": {
      "source_side": ["src"],
      "cut_segments": ["e1", "e4"],
      "total_cost": 12
    }
  }
}
```

快照中的方案取自计算时刻的冻结副本；之后再整版替换 `zone-a` 也不会改变
该快照，工程师拿到的是一份可直接执行、确实隔断全部污染路径的最低费用清单。

### 3.5 查询已采用结果 — `GET /api/adopted`

```bash
curl -sS http://localhost:8000/api/adopted
```

从未有结果被采用时返回 `404 ADOPTED_RESULT_NOT_FOUND`。

## 4. 稳定错误码

| HTTP | code | 触发条件 |
| ---- | ---- | -------- |
| 400 | `INVALID_JSON` | 非 JSON、非 UTF-8、顶层不是对象 |
| 422 | `DUPLICATE_JSON_KEY` | JSON 对象中出现重复键 |
| 422 | `MISSING_FIELDS` / `UNEXPECTED_FIELDS` | 顶层缺字段 / 多字段（动作端点传参同样报后者） |
| 422 | `INVALID_AREAS` / `INVALID_SEGMENTS` / `INVALID_SOURCES` / `INVALID_SINKS` | 对应字段不是列表 |
| 422 | `INVALID_ID` | 任何 ID 不匹配 `[A-Za-z0-9_-]{1,32}` |
| 422 | `DUPLICATE_AREA` / `DUPLICATE_SEGMENT` / `DUPLICATE_ZONE_AREA` | 区域、管段、源 / 汇列表内重复 |
| 422 | `UNKNOWN_AREA` | 管段端点或源 / 汇引用未声明区域 |
| 422 | `INVALID_COST` | 费用不是 `[0, 10^9]` 内的整数（布尔值拒绝） |
| 422 | `EMPTY_SOURCES` / `EMPTY_SINKS` | 污染源或保护区为空 |
| 422 | `SOURCE_SINK_OVERLAP` | 同一区域同时出现在源与汇中 |
| 422 | `TOO_MANY_AREAS` / `TOO_MANY_SEGMENTS` | 超过 300 / 2000 |
| 422 | `COMPUTATION_FAILED` | 求解器拒绝的非法网络（正常校验已覆盖，兜底） |
| 404 | `PLAN_NOT_FOUND` | 读取 / 计算不存在的方案 |
| 404 | `RESULT_NOT_FOUND` | 采用不存在的 `computation_id`（含非法 UUID 形态） |
| 404 | `ADOPTED_RESULT_NOT_FOUND` | 尚无任何已采用结果 |
| 400 | `INVALID_REQUEST` | 路径 / 查询形态错误 |
| 405 | `METHOD_NOT_ALLOWED` | 方法不允许 |
| 500 | `INTERNAL_ERROR` | 未预期内部错误（统一 JSON 兜底，正常输入不会触发） |

## 5. 测试

```bash
pip install -r requirements-dev.txt
python -m pytest
```

测试包含：

- **穷举对拍**：对不超过 8 个区域的网络，枚举全部合法（源、汇）划分
  （8 区域时共 6050 种），与暴力枚举每个合法割的参考实现逐一比较；覆盖
  完全图、全零图、随机稀疏图、含平行边 / 自环图。
- **并列割**：构造多条同值最小割，验证返回包含关系最小的唯一源侧。
- **多源多汇**、**零费用割 / 零容量边**、**平行边与自环**。
- **超过 32 位的总费用**：5000 条 10^9 的平行边，总费用 5×10^12。
- **顺序无关**：打乱管段 / 区域提交顺序，结果字节级一致。
- **可执行性验证**：删除结果中的切断管段后 BFS，确认没有任何源能到达汇。
- **API 端到端**：版本递增、内容去重、非法整版不覆盖、采用不存在结果不
  改写当前方案 / 已采用结果、快照在方案后续编辑后仍冻结计算时版本。
- **规模测试**：300 区域 / 2000 管段随机网络在数秒内完成。

## 6. 项目结构

```
app/
  api.py            # FastAPI 路由、严格 JSON 解析、稳定错误码、生命周期
  service.py        # 规范化哈希、计算编排、快照构造
  validation.py     # 方案文档的严格校验与规范化
  maxflow.py        # 自实现 64 位 Dinic 最大流 / 包含最小最小割
  repository/
    base.py         # 仓储协议与数据类
    memory.py       # 内存实现（测试 / 本地无 DB 运行）
    postgres.py     # psycopg3 异步连接池 + 事务化 SQL 实现
tests/
  brute.py          # 暴力枚举最小割的参考实现
  test_maxflow.py   # 穷举对拍与算法性质
  test_scale.py     # 300 / 2000 规模
  test_api.py       # HTTP 端到端
  test_repository.py# 仓储语义
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
