# 采样时间 UTC 迁移服务

公共卫生跨站研究数据迁移：把旧库的**本地采样时间**（样本号 + IANA 时区 + 本地年月日时分秒）
迁移为 UTC。迁移期间旧采集端照常**补报/改报**，后台分批迁移不中断录入。

技术栈：FastAPI + SQLite（WAL）+ zoneinfo，交付 Docker Compose 一键启动，Swagger 可演示全部场景。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

启动即写入 3 个演示样本（`SEED_DEMO=true`，仅空库播种），后台 worker 每 2 秒迁一批：

| 样本号 | 时区 | 本地时刻 | 结果 |
|---|---|---|---|
| `DEMO-UNIQUE-001` | Asia/Shanghai | 2026-06-15 08:30:00 | 唯一换算 → 自动确认 |
| `DEMO-DST-AMBIGUOUS-001` | America/New_York | 2026-11-01 01:30:00 | 夏令时回拨重复 → 待人工选候选 |
| `DEMO-DST-NONEXISTENT-001` | America/New_York | 2026-03-08 02:30:00 | 夏令时跳过不存在 → 须改原值 |

本地开发（不用 Docker）：

```bash
pip install -r requirements.txt
SEED_DEMO=true uvicorn app.main:app --reload
pytest tests/ -v     # 18 个测试：分类、改报语义、并发守卫、重启续迁等
```

## 数据模型与状态机

`samples` 表：`sample_no`(主键)、`tz`、`local_time`（原值）、`version`、
`status`、`utc_time`、`cand_a/cand_b`（重复时刻候选，审计用）、
`migrated_version`（确认结果对应的原值版本）、`updated_at`。

```
                 唯一换算                人工选候选
pending  ──────────────────► confirmed ◄────────── ambiguous（重复时刻）
   │                              ▲
   │ 夏令时跳过                   │ 改原值后重迁
   └──────────► nonexistent ──────┘

任何状态下旧端改报（时区或本地时刻变化）：
  version+1 → 撤销确认(utc_time=NULL) → 回到 pending 重迁
原值未变的重报：什么都不动，保留确认（幂等）
```

## 关键设计

**1. 迟到的迁移结果不覆盖较新的改报/人工确认**
迁移写回一律条件更新：

```sql
UPDATE samples SET ... WHERE sample_no=? AND version=? AND status='pending'
```

迁移读到记录后，若旧端改报（version 已 +1）或人工已确认（status 已变），
`rowcount=0`，stale 结果直接丢弃；记录仍是 pending，下一批按新原值重算。

**2. 人工确认按读取版本进行**
`POST /samples/{no}/confirm` 必须带读取时看到的 `version`。服务端按**当前原值**
实时重算分类并校验：重复时刻只接受两个候选之一；不存在时刻直接 409 拒绝，
必须先由旧端改原值。版本不匹配 → 409，防止基于过期视图确认。

**3. 研究取数的一致性**
`GET /research/samples` 只取 `status='confirmed' AND migrated_version = version`
（确认结果与同行原值同版本；改报即撤销确认，不会读到"新原值配旧 UTC"）。
整个查询是**单条 SELECT**，SQLite WAL 下单语句即同一已提交快照——
一次查询中的原值与确认结果来自同一状态，且读写互不阻塞。

**4. 重启续迁**
待迁移队列就是 `status='pending'` 的记录，全在 SQLite 里（compose 命名卷持久化）。
进程/容器重启后 worker 继续捞 pending，无需恢复逻辑。

**5. 夏令时判定（zoneinfo）**
对本地朴素时刻分别按 `fold=0/1` 换算 UTC：两者相同 → 唯一；不同则回环校验
（UTC 反算回本地），都成立 → 重复（ambiguous），都不成立 → 不存在（nonexistent）。

## API 一览

| 方法/路径 | 说明 |
|---|---|
| `PUT /samples/{sample_no}` | 旧端写原值（补报/改报同入口）。返回 `created/updated/unchanged` |
| `GET /samples/{sample_no}` | 查原值、版本、状态、UTC；重复时刻附候选及 UTC 偏移 |
| `GET /samples?status=...` | 列表；`ambiguous`/`nonexistent` 即待核对队列 |
| `POST /samples/{sample_no}/confirm` | 人工确认 `{version, utc_time}` |
| `GET /research/samples?utc_from=&utc_to=` | 研究取数，仅已确认，单语句快照 |
| `GET /migration/status` | 各状态数量 + worker 状态 |
| `POST /migration/run-once` | 手动触发一批迁移 |
| `POST /demo/seed` | 写入/重置演示样本（幂等 upsert） |

## Swagger 演示脚本（/docs 里逐个 Try it out）

**A. 夏令时重复时刻 → 人工确认**
1. `GET /samples/DEMO-DST-AMBIGUOUS-001` → `status=ambiguous`，候选
   `2026-11-01T05:30:00Z`(UTC-04:00) 与 `2026-11-01T06:30:00Z`(UTC-05:00)，记下 `version`。
2. `POST /samples/DEMO-DST-AMBIGUOUS-001/confirm`，body
   `{"version": 1, "utc_time": "2026-11-01T06:30:00Z"}` → 200 已确认。
   改 `version` 或非候选时间重试 → 409。

**B. 夏令时不存在时刻 → 必须改原值**
1. `POST /samples/DEMO-DST-NONEXISTENT-001/confirm` 任意 UTC → 409 `LOCAL_TIME_NONEXISTENT`。
2. `PUT /samples/DEMO-DST-NONEXISTENT-001` 改为 `2026-03-08 03:30:00`
   → `version=2, pending`；约 2 秒后 `GET` → 已确认 `2026-03-08T07:30:00Z`。

**C. 迁移中改报（撤销确认并重迁）**
1. `PUT /samples/DEMO-UNIQUE-001` 把时刻改为 `09:45:00`
   → 响应里确认已被撤销（`version=2, pending, utc_time=null`）。
2. 约 2 秒后 `GET /samples/DEMO-UNIQUE-001` → 已按新原值确认 `2026-06-15T01:45:00Z`。
3. 原样再 PUT 一次 → `change=unchanged`，确认保留。

**D. 研究取数只看已确认**
- 演示 B 进行中（nonexistent 未修正时）调 `GET /research/samples`
  → 该样本不出现；修正确认后才出现。可加 `utc_from/utc_to` 做范围过滤。

**E. 重启续迁**
1. `PUT /samples/RESTART-001` 一条新样本，立刻 `docker compose restart api`。
2. 服务回来后在 `GET /samples/RESTART-001` 看到它已被接着迁移并确认；
   既有确认记录全部保留（数据在命名卷 `dbdata` 里）。

也可直接跑脚本版演示（需服务已启动）：`bash scripts/demo.sh`

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DB_PATH` | `/data/app.db`（容器） | SQLite 路径 |
| `MIGRATION_INTERVAL_SECONDS` | `2` | 后台批迁移间隔 |
| `MIGRATION_BATCH_SIZE` | `50` | 每批条数 |
| `MIGRATION_RECORD_DELAY_MS` | `0` | 每条人工延迟，演示"迁移进行中"窗口用 |
| `SEED_DEMO` | compose 里 `true` | 空库启动时写演示样本 |

## 目录结构

```
app/
  main.py       FastAPI 端点 + 后台迁移 worker
  migrator.py   分批迁移（条件更新守卫）
  timeutil.py   zoneinfo 夏令时分类
  db.py         SQLite(WAL) 连接与建表
  demo.py       演示种子数据
tests/test_app.py  18 个测试
Dockerfile / docker-compose.yml / requirements.txt
scripts/demo.sh    curl 版演示脚本
```
