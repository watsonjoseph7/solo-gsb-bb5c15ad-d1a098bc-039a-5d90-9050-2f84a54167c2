# 文件交接留痕服务（Handoff Chain）

基于 **FastAPI + SQLite** 的多人文件交接留痕服务。每次交接形成一条只可追加的
**SHA-256 哈希链**，可查询、可审计、可完整性校验；并发移交与随机数重放由服务端
原子拒绝，**链条不会分叉**。

## 核心规则

- **创建链条**：提交文件 `SHA-256` 摘要与首位持有人，服务端写入序号 `0` 的创世事件：
  ```json
  {"file_hash": "<文件摘要>", "holder": "<首位持有人>", "seq": 0}
  ```
  对其键名排序的**紧凑 JSON**（无空白）计算 SHA-256，即创世哈希（初始链尾）。
- **执行移交**：每次请求须携带
  - `tail_hash`：客户端认定的**当前链尾哈希**；
  - `receiver`：接收方；
  - `nonce`：一次性随机数（同一链内用过即作废，防重放）。

  交出方（`giver`）由服务端依据当前链尾持有人确定，客户端无法伪造。
  新事件哈希按以下紧凑 JSON（`sort_keys` 后）计算：
  ```json
  {"giver":"...","nonce":"...","prev_hash":"<前序哈希>","receiver":"...","seq":<序号>}
  ```
  服务端在**同一个 SQLite 立即事务**中完成「校验链尾 → 查重随机数 → 追加事件 →
  推进链尾」，全程串行。链尾不匹配（过期/竞争请求）或随机数重放一律返回
  **`409 Conflict`**，不写入任何数据，因而不可能产生分叉。
- **待接收移交（邀约两步确认）**：当交出方与接收方无法同时在线时，可先发起邀约：
  - `POST /offers`：以**最新链尾** `tail_hash` 发起邀约，指定接收方 `receiver`、
    有效期 `ttl_seconds`（1 ~ 604800 秒，即最长 7 天）与**一次性确认码**
    `confirm_code`。服务端**只保存确认码的加盐 SHA-256 摘要**
    （每个邀约使用独立随机盐），绝不保存明文，返回 `offer_id` 与 `expires_at`；
  - 接收方通过线下渠道拿到确认码后，`POST /offers/{offer_id}/accept` 完成移交。
    服务端在**同一个 SQLite `BEGIN IMMEDIATE` 事务**中依次校验：
    邀约仍为 `pending` → 未过期 → 确认码摘要一致 → **邀约锁定的链尾仍是当前链尾**，
    全部通过才追加唯一移交事件（移交随机数由服务端在发起邀约时生成）并把邀约置为
    `accepted`、回写事件序号；
  - 以下情况一律返回 **`409 Conflict` 且不写入任何事件**：邀约已过期
    （`offer_expired`）、重复确认（`offer_already_accepted`）、确认码错误
    （`offer_code_mismatch`，邀约仍可凭正确确认码继续确认）、链尾已被其他移交推进
    （`tail_mismatch`）。可用 `GET /offers/{offer_id}` 查询邀约状态
    （`pending` / `accepted` / `expired`，响应中不含任何确认码信息）。
- **完整性校验**：服务端从创世记录起逐条重算哈希、校验序号连续性与 `prev_hash`
  指针，并比对链尾缓存；校验结果给出 **首个断裂序号 `first_broken_seq`**。

## 一条命令启动（依赖安装 + 数据库初始化 + 启动）

需要已安装 Docker（含 Compose 插件）。在项目根目录执行：

```bash
docker compose up --build
```

该命令会：构建镜像（其中 `pip install -r requirements.txt` 完成依赖安装）→
容器启动时自动初始化 SQLite 数据库（自动建表，数据位于命名卷 `handoff-data`，
库文件 `/data/handoff.db`）→ 启动 API 服务。

### 访问地址与端口

| 用途 | 地址 |
| --- | --- |
| API 服务根地址 | <http://localhost:8000> |
| 健康检查 | <http://localhost:8000/health> |
| 交互式文档（Swagger UI） | <http://localhost:8000/docs> |
| OpenAPI 描述 | <http://localhost:8000/openapi.json> |

后台运行：`docker compose up --build -d`；停止：`docker compose down`
（数据保留在卷中；如需连数据一并清除：`docker compose down -v`）。

### 不用 Docker 的本地启动方式

```bash
pip install -r requirements.txt
DATA_DIR=./data python -m app.main        # 自动建库建表后启动
# 或显式初始化：
python -c "from app.database import init_db; init_db()"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/chains` | 创建交接链（文件摘要 + 首位持有人） |
| `POST` | `/handovers` | 执行一次移交（链尾哈希 + 接收方 + 一次性随机数） |
| `POST` | `/offers` | 发起待接收移交邀约（链尾哈希 + 接收方 + 有效期 + 一次性确认码） |
| `POST` | `/offers/{offer_id}/accept` | 凭确认码确认邀约，原子追加移交事件 |
| `GET`  | `/offers/{offer_id}` | 查询单个邀约状态（`pending` / `accepted` / `expired`） |
| `GET`  | `/chains` | 列出全部链条 |
| `GET`  | `/chains/{chain_id}` | 查询链条与全部事件 |
| `GET`  | `/chains/{chain_id}/verify` | 完整性校验，指出首个断裂序号 |
| `GET`  | `/health` | 健康检查 |

错误响应统一为：`{"error": "<code>", "detail": "<说明>"}`。

| 状态码 | error 代码 | 触发场景 |
| --- | --- | --- |
| **404** | `not_found` | 链条 / 邀约不存在 |
| **409** | `tail_mismatch` | 直接移交或确认邀约时链尾哈希已被推进 |
| **409** | `offer_tail_mismatch` | 发起邀约时携带的链尾已过期 |
| **409** | `nonce_replay` | 直接移交的一次性随机数在同一链内重放 |
| **409** | `offer_expired` | 邀约超过 `expires_at` 后才确认（不写事件） |
| **409** | `offer_already_accepted` | 邀约已被使用，重复（含并发）确认（不写事件） |
| **409** | `offer_code_mismatch` | 一次性确认码不正确（邀约保持 `pending`，不写事件） |
| 422 | —（参数校验） | 请求体格式 / 取值范围不合法 |

## 使用示例

```bash
# 1) 计算文件摘要并创建链条
FILE_HASH=$(sha256sum some-file.bin | awk '{print $1}')
curl -s -X POST http://localhost:8000/chains \
  -H 'Content-Type: application/json' \
  -d "{\"file_hash\": \"$FILE_HASH\", \"first_holder\": \"alice\"}"

# 响应中含 chain_id 与 tail_hash（创世哈希），例如：
# {"chain_id":"ab12...","tail_hash":"9f0c...","current_holder":"alice", ...}

# 2) alice 移交给 bob（必须带上当前 tail_hash 和全新 nonce）
curl -s -X POST http://localhost:8000/handovers \
  -H 'Content-Type: application/json' \
  -d '{"chain_id":"ab12...","tail_hash":"9f0c...","receiver":"bob","nonce":"n-0001"}'
# 返回新的 event_hash（即新链尾），下一次移交必须携带它

# 3) 用旧链尾再交一次 → 409 tail_mismatch（拒绝，不分叉）
curl -i -X POST http://localhost:8000/handovers \
  -H 'Content-Type: application/json' \
  -d '{"chain_id":"ab12...","tail_hash":"9f0c...","receiver":"carol","nonce":"n-0002"}'

# 4) 随机数重放 → 409 nonce_replay
curl -i -X POST http://localhost:8000/handovers \
  -H 'Content-Type: application/json' \
  -d '{"chain_id":"ab12...","tail_hash":"<新链尾>","receiver":"carol","nonce":"n-0001"}'

# 5) 查询链条
curl -s http://localhost:8000/chains/ab12...

# 6) 完整性校验（正常：valid=true）
curl -s http://localhost:8000/chains/ab12.../verify

# 7) 待接收移交：carol 先发起给 dave 的邀约（锁定当前链尾，有效期 300 秒）
curl -s -X POST http://localhost:8000/offers \
  -H 'Content-Type: application/json' \
  -d '{"chain_id":"ab12...","tail_hash":"<当前链尾>","receiver":"dave","ttl_seconds":300,"confirm_code":"4242-XYZ"}'
# -> {"offer_id":"cd34...","status":"pending","expires_at":"...", ...}
#    （确认码只在创建响应中回显一次，服务端仅留存其加盐 SHA-256 摘要）

# 8) dave 凭 offer_id 与确认码确认（同一立即事务内校验并追加事件）
curl -s -X POST http://localhost:8000/offers/cd34.../accept \
  -H 'Content-Type: application/json' \
  -d '{"confirm_code":"4242-XYZ"}'
# -> {"offer_id":"cd34...","status":"accepted","seq":4,"giver":"carol","receiver":"dave", ...}

# 9) 查询邀约状态（任何时候都不会返回确认码）
curl -s http://localhost:8000/offers/cd34...
# -> {"offer_id":"cd34...","status":"accepted","event_seq":4,"accepted_at":"...", ...}

# 10) 异常情形（均为 409，且不写入事件）
#   - 过期确认      {"error":"offer_expired", ...}
#   - 重复/并发确认  {"error":"offer_already_accepted", ...}
#   - 确认码错误     {"error":"offer_code_mismatch", ...}
#   - 邀约期间链尾已被普通移交推进：{"error":"tail_mismatch", ...}
```

校验响应示例（正常）：

```json
{
  "chain_id": "ab12...",
  "valid": true,
  "length": 3,
  "expected_next_seq": 3,
  "first_broken_seq": null,
  "reason": null,
  "recomputed_tail_hash": "…",
  "stored_tail_hash": "…"
}
```

若中途某条事件被篡改，响应形如
`{"valid": false, "first_broken_seq": 2, "reason": "事件哈希不匹配：…"}`。

## 防分叉与并发保证

1. 每次追加都在 `BEGIN IMMEDIATE` 事务内完成，且进程内写操作由互斥锁串行化；
   「读链尾 → 比对 → 插入 → 推进」是一个原子步骤。
2. 数据库层设有 `UNIQUE(chain_id, seq)` 与 `UNIQUE(chain_id, nonce)` 唯一约束
   作为兜底，任何竞争写入都无法产生重号事件或重放随机数。
3. 邀约确认复用上述立即事务：过期/重复/确认码错误/链尾推进的检查与事件追加、
   邀约置位在同一事务内提交；并以
   `UPDATE offers SET status='accepted' WHERE status='pending'` 的条件更新作为
   最终防线——并发重复确认最多一方成功，失败方整体回滚、不写事件。
4. 一次性确认码明文不落库：仅保存 `SHA-256(随机盐 : 确认码)`，每个邀约使用
   独立盐；摘要比对使用恒定时间比较。
5. WAL 日志模式 + busy timeout，兼顾读写并发与单写入者语义。

## 数据库初始化与旧库兼容

容器启动（`docker-entrypoint.sh`）与应用 lifespan 均会执行 `init_db()`，
全部建表语句使用 `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`，
因此：

- 全新环境自动创建 `chains`、`events`、`offers` 三张表；
- **升级已有部署时无需迁移脚本**：旧版本的 `chains` / `events` 数据原样保留，
  新表 `offers` 在首次启动时自动补建，旧链条可直接发起邀约。

## 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与启动
│   ├── service.py     # 哈希链核心逻辑（创建 / 移交 / 邀约 / 查询 / 校验）
│   ├── database.py    # SQLite 连接、建表与串行写事务
│   ├── hashing.py     # 紧凑 JSON 规范与 SHA-256
│   ├── schemas.py     # Pydantic 请求/响应模型
│   └── config.py      # 路径与端口配置
├── Dockerfile
├── docker-compose.yml
├── docker-entrypoint.sh
├── requirements.txt
└── README.md
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATA_DIR` | `/data` | 数据目录 |
| `DB_PATH` | `$DATA_DIR/handoff.db` | SQLite 文件路径 |
| `PORT` | `8000` | 监听端口（容器内，Compose 映射同为 8000） |
| `HOST` | `0.0.0.0` | 监听地址 |
