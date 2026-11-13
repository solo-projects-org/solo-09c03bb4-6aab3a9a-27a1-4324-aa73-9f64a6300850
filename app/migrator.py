"""后台分批迁移。

核心安全性质：所有写回都是条件更新
    WHERE sample_no=? AND version=? AND status='pending'
即"迟到的迁移结果不能覆盖较新的改报或人工确认"：
- 若迁移计算期间旧端改报（version 已 +1）→ 更新 0 行， stale 结果直接丢弃；
- 若期间被人工确认（status 已非 pending）→ 更新 0 行，确认结果保留。
被丢弃结果的记录仍是 pending（改报会重置为 pending），下一批会按新原值重算。

迁移状态全部落在数据库（status='pending' 即待迁移队列），进程重启后
worker 继续捞 pending 记录即可，无需额外恢复逻辑。
"""
from __future__ import annotations

import sqlite3
import time

from .db import connect
from .timeutil import classify_local, iso_utc, parse_local, utcnow_iso

# 状态常量
PENDING = "pending"
CONFIRMED = "confirmed"
AMBIGUOUS = "ambiguous"
NONEXISTENT = "nonexistent"


def fetch_pending_batch(conn: sqlite3.Connection, batch_size: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT sample_no, tz, local_time, version FROM samples "
        "WHERE status='pending' ORDER BY updated_at, sample_no LIMIT ?",
        (batch_size,),
    ).fetchall()


def apply_result(conn: sqlite3.Connection, sample_no: str, version: int,
                 status: str, utc_time: str | None,
                 cand_a: str | None, cand_b: str | None) -> bool:
    """把迁移结果写回，带版本与状态守卫。返回是否真正生效。"""
    cur = conn.execute(
        "UPDATE samples SET status=?, utc_time=?, cand_a=?, cand_b=?, "
        "migrated_version=?, updated_at=? "
        "WHERE sample_no=? AND version=? AND status='pending'",
        (status, utc_time, cand_a, cand_b, version, utcnow_iso(),
         sample_no, version),
    )
    return cur.rowcount == 1


def migrate_batch(db_path: str | None = None, batch_size: int = 50,
                  record_delay_ms: int = 0) -> dict:
    """迁移一批 pending 记录。返回统计信息。"""
    conn = connect(db_path)
    try:
        rows = fetch_pending_batch(conn, batch_size)
        stats = {"picked": len(rows), "confirmed": 0, "ambiguous": 0,
                 "nonexistent": 0, "stale": 0}
        for r in rows:
            if record_delay_ms > 0:
                # 演示用：放大"迁移进行中"窗口，便于在 Swagger 上演示迁移中改报
                time.sleep(record_delay_ms / 1000.0)
            c = classify_local(r["tz"], parse_local(r["local_time"]))
            if c.kind == "unique":
                ok = apply_result(conn, r["sample_no"], r["version"],
                                  CONFIRMED, iso_utc(c.utc_a), None, None)
                stats["confirmed" if ok else "stale"] += 1
            elif c.kind == "ambiguous":
                ok = apply_result(conn, r["sample_no"], r["version"],
                                  AMBIGUOUS, None,
                                  iso_utc(c.utc_a), iso_utc(c.utc_b))
                stats["ambiguous" if ok else "stale"] += 1
            else:
                ok = apply_result(conn, r["sample_no"], r["version"],
                                  NONEXISTENT, None, None, None)
                stats["nonexistent" if ok else "stale"] += 1
        return stats
    finally:
        conn.close()
