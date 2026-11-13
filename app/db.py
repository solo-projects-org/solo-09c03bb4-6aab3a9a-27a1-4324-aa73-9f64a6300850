"""SQLite 访问层：WAL 模式 + busy_timeout，支撑迁移期间录入照常使用。

- WAL：读写不互斥，研究查询不会被迁移/录入阻塞。
- busy_timeout：写-写竞争时等待而非立刻失败。
- 每次操作独立短事务，迁移按记录逐条提交，不长时间占锁。
"""
from __future__ import annotations

import os
import sqlite3

DEFAULT_DB_PATH = os.environ.get("DB_PATH", "./data/app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  sample_no        TEXT PRIMARY KEY,           -- 样本号（旧端主键）
  tz               TEXT NOT NULL,              -- IANA 时区，原值
  local_time       TEXT NOT NULL,              -- 本地年月日时分秒，原值，格式 YYYY-MM-DD HH:MM:SS
  version          INTEGER NOT NULL,           -- 原值版本：只有原值变化才 +1
  status           TEXT NOT NULL               -- pending/confirmed/ambiguous/nonexistent
                   CHECK (status IN ('pending','confirmed','ambiguous','nonexistent')),
  utc_time         TEXT,                       -- 已确认的 UTC（ISO 8601，Z 结尾）
  cand_a           TEXT,                       -- 重复时刻候选 UTC（fold=0），审计用
  cand_b           TEXT,                       -- 重复时刻候选 UTC（fold=1），审计用
  migrated_version INTEGER,                    -- 当前 utc_time/分类结果对应的原值版本
  updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_status ON samples(status);
CREATE INDEX IF NOT EXISTS idx_samples_utc ON samples(utc_time);

-- 样本号归并关系：sample_no 旧号 → primary_no 当时的主号。
-- 不设外键：主号之后可能再次被归并（链式），旧号解析时沿链走到最终主号。
-- 关系与 samples 同库，随 /data 命名卷持久化，重启后仍有效。
CREATE TABLE IF NOT EXISTS sample_aliases (
  sample_no        TEXT PRIMARY KEY,          -- 被归并掉的旧号
  primary_no       TEXT NOT NULL,             -- 归并当时的直接主号
  source_version   INTEGER,                   -- 归并前源号版本（审计）
  primary_version  INTEGER,                   -- 归并前主号版本（审计）
  merged_version   INTEGER,                   -- 归并后的主记录版本
  merged_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_aliases_primary ON sample_aliases(primary_no);
"""


def db_path() -> str:
    return os.environ.get("DB_PATH", DEFAULT_DB_PATH)


def connect(path: str | None = None) -> sqlite3.Connection:
    p = path or db_path()
    if p != ":memory:":
        d = os.path.dirname(os.path.abspath(p))
        if d:
            os.makedirs(d, exist_ok=True)
    # isolation_level=None → 自动提交，显式 BEGIN/COMMIT 控制事务边界
    conn = sqlite3.connect(p, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
