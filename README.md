# 采样时间 UTC 迁移服务

公共卫生跨站研究数据迁移：把旧库的**本地采样时间**（样本号 + IANA 时区 + 本地年月日时分秒）
迁移为 UTC。迁移期间旧采集端照常**补报/改报**，后台分批迁移不中断录入。

旧端曾误把同一条采样建成多个样本号；服务提供**归并 API**，把重复号并到保留的主号：
旧号永续解析到最终主记录（研究取数只留一条），旧端仍可沿用原号改报/确认。

技术栈：FastAPI + SQLite（WAL）+ zoneinfo，交付 Docker Compose 一键启动，Swagger 可演示全部场景。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

启动即写入 6 个演示样本（`SEED_DEMO=true`，仅空库播种），后台 worker 每 2 秒迁一批：

| 样本号 | 时区 | 本地时刻 | 结果 |
|---|---|---|---|
| `DEMO-UNIQUE-001` | Asia/Shanghai | 2026-06-15 08:30:00 | 唯一换算 → 自动确认 |
| `DEMO-DST-AMBIGUOUS-001` | America/New_York | 2026-11-01 01:30:00 | 夏令时回拨重复 → 待人工选候选 |
| `DEMO-DST-NONEXISTENT-001` | America/New_York | 2026-03-08 02:30:00 | 夏令时跳过不存在 → 须改原值 |
| `DEMO-DUP-001/002/003` | Asia/Shanghai | 2026-06-16 07:15:00 | 旧端误建的重复号，用于归并演示 |

本地开发（不用 Docker）：

```bash
pip install -r requirements.txt
SEED_DEMO=true uvicorn app.main:app --reload
pytest tests/ -v     # 40 个测试：分类、改报语义、归并、并发守卫、重启续迁等
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

## 样本号归并

旧端误建的重复样本（同一条采样、多个样本号）用 `POST /samples/merge` 归并。
请求必须带**源号、保留的主号、读取时两边的版本**：

```json
{"source_no": "DEMO-DUP-002", "primary_no": "DEMO-DUP-001",
 "source_version": 1, "primary_version": 1}
```

**归并条件**
- 两边**时区与本地时刻完全相同**，否则 409 `MERGE_CONDITION_MISMATCH`；
- 两边**都已确认**时，UTC 也必须相同，否则 409 `UTC_MISMATCH`；
- 任一边版本与读取时不同 → 409 `VERSION_CONFLICT`；
- 源号已归并到别的主号 → 409 `SOURCE_ALREADY_MERGED`（不静默改指）；
- 源号 = 主号 → 400 `SAME_SAMPLE`。
版本不符、条件不满足或写入失败时**整笔回滚**：记录内容、版本、编号关系都不变。

**归并结果**
- **保留已有确认**：主号已确认则保留主号 UTC；否则源号已确认则采用源号 UTC；
  两边都未确认 → 清空迁移结果，回到 `pending` 按合并后原值重新迁移。
- 主记录版本 = `max(两边版本) + 1`，严格大于归并前任一版本 →
  **归并前读到的两个版本（即使相同）都不能再用于确认**。
- 被归并的旧号记录删除，`sample_aliases` 记 "旧号 → 归并当时的主号"。
- 已指向同一最终主号的重复归并是幂等 no-op：不改记录、不改版本（返回 `duplicate=true`）。

**旧号永续**
- 关系可链式（A→B 后 B→C）：解析时沿链走到最终主号，任一旧号都能找到同一条主记录；
- `GET /samples/{旧号}` 返回主记录，并注明 `final_primary_no`（最终主号）与
  `requested_no`（本次请求的号）、`aliases`（所有旧号）；
- `PUT /samples/{旧号}`、`POST /samples/{旧号}/confirm` 都在 `BEGIN IMMEDIATE`
  事务内先解析再写入，作用于最终主记录，绝不按旧号重建第二条记录。

**迟到迁移/确认**
迁移结果写回是 `WHERE sample_no=? AND version=? AND status='pending'` 条件更新：
被归并的旧号已删除，迟到结果命中 0 行；主号版本已变大，旧版本的迁移或确认同样命中 0 行。
因此迟到迁移不能恢复旧记录、不能覆盖主记录，旧版本确认一律 409。

**并发**
归并、改报、确认都走"解析 + 校验 + 写入"的单个 `BEGIN IMMEDIATE` 事务，由 SQLite
串行化；结果是这两种之一，不会出现重复记录或断链：
- 归并先提交 → 改报经旧号解析落到主记录；
- 改报/确认先提交 → 归并版本校验失败 409，两条记录原样保留。

**持久化**：`sample_aliases` 与 `samples` 同库，位于 compose 命名卷 `/data`，
容器重启后关系仍有效。

## API 一览

| 方法/路径 | 说明 |
|---|---|
| `PUT /samples/{sample_no}` | 旧端写原值（补报/改报同入口）；旧号自动落到主记录。返回 `created/updated/unchanged` |
| `GET /samples/{sample_no}` | 查原值、版本、状态、UTC，注明 `final_primary_no`；重复时刻附候选及 UTC 偏移 |
| `GET /samples/{sample_no}/aliases` | 列出最终指向该主号的全部旧号 |
| `GET /samples?status=...` | 列表（每主记录一条，附 aliases）；`ambiguous`/`nonexistent` 即待核对队列 |
| `POST /samples/merge` | 归并重复样本号：`{source_no, primary_no, source_version, primary_version}` |
| `POST /samples/{sample_no}/confirm` | 人工确认 `{version, utc_time}`；旧号确认作用于主记录 |
| `GET /research/samples?utc_from=&utc_to=` | 研究取数，仅已确认；主记录只出现一次，单语句快照 |
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

**E. 归并与旧号**

> 片段中 `version` 均取自上一步 `GET` 的响应，不要硬编码。

1. `POST /demo/seed`，约 2 秒后三个 `DEMO-DUP-*` 均 `confirmed`，记下各自 `version`。
2. `POST /samples/merge`，body
   `{"source_no":"DEMO-DUP-002","primary_no":"DEMO-DUP-001","source_version":1,"primary_version":1}`
   → 200，`merged=true, new_version=2`，主记录保留原 UTC，`aliases=["DEMO-DUP-002"]`。
3. 再归并一次（链式）：`{"source_no":"DEMO-DUP-001","primary_no":"DEMO-DUP-003","source_version":2,"primary_version":1}`
   → 200，最终主号 `DEMO-DUP-003`，`aliases=["DEMO-DUP-001","DEMO-DUP-002"]`。
4. `GET /samples/DEMO-DUP-002` → `sample_no=DEMO-DUP-003, final_primary_no=DEMO-DUP-003, requested_no=DEMO-DUP-002`。
5. 旧号改报：`PUT /samples/DEMO-DUP-002` 改为 `08:00:00` → 落到 `DEMO-DUP-003`（`version+1, pending`），约 2 秒后自动确认。
6. 拒绝旧确认：`POST /samples/DEMO-DUP-002/confirm` body `{"version":1,...}` → 409 `VERSION_CONFLICT`；
   按当前主记录版本确认 → 200，返回的主记录是 `DEMO-DUP-003`。
7. 幂等重复归并：再次 `POST /samples/merge` 同一对号（任意版本）→ 200 `duplicate=true`，`new_version` 不变。
8. 条件不满足：`DEMO-UNIQUE-001` 归并进 `DEMO-DUP-003` → 409 `MERGE_CONDITION_MISMATCH`，两边记录不变。
9. `GET /research/samples` → 该组只出现 `DEMO-DUP-003` 一条。

也可直接跑脚本版归并演示（需服务已启动）：`bash scripts/merge_demo.sh`

**F. 重启续迁**
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
  merging.py    样本号归并：链式解析 + 原子归并
tests/test_app.py     18 个测试（迁移/确认/取数）
tests/test_merge.py   22 个测试（归并/旧号/迟到结果/并发/持久化）
Dockerfile / docker-compose.yml / requirements.txt
scripts/demo.sh        curl 版演示脚本（迁移与确认）
scripts/merge_demo.sh  curl 版归并演示（连续归并/旧号改报/拒绝旧确认）
```
