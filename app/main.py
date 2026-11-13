"""旧库本地采样时间 → UTC 迁移服务。

端点总览：
- PUT    /samples/{sample_no}          旧端入口：按样本号写原值（补报/改报）
- GET    /samples/{sample_no}          查原值、版本、状态、UTC（含重复时刻候选）
- GET    /samples                      记录列表（可按状态过滤，核对队列）
- POST   /samples/{sample_no}/confirm  人工确认（重复时刻按读取版本选候选 UTC）
- GET    /research/samples             研究取数：仅已确认记录，单次查询同一已提交快照
- GET    /migration/status             迁移进度与 worker 状态
- POST   /migration/run-once           手动触发一批迁移（演示/运维用）
- POST   /demo/seed                    写入演示样本（唯一/重复/不存在各一）
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator
from zoneinfo import ZoneInfo

from .db import connect, db_path
from .demo import DEMO_SAMPLES
from .migrator import (AMBIGUOUS, CONFIRMED, NONEXISTENT, PENDING,
                       migrate_batch)
from .timeutil import (classify_local, fmt_local, iso_utc, offset_label,
                       parse_local, parse_utc, utcnow_iso)

logger = logging.getLogger("utc-migrator")
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------- 配置

def cfg() -> dict:
    """每次调用动态读取环境变量，便于测试与容器调参。"""
    return {
        "interval": float(os.environ.get("MIGRATION_INTERVAL_SECONDS", "2")),
        "batch": int(os.environ.get("MIGRATION_BATCH_SIZE", "50")),
        "delay_ms": int(os.environ.get("MIGRATION_RECORD_DELAY_MS", "0")),
        "seed_demo": os.environ.get("SEED_DEMO", "false").lower()
                     in ("1", "true", "yes"),
    }

WORKER_STATE: dict[str, Any] = {"last_run_at": None, "last_stats": None,
                                "batches": 0}


# ---------------------------------------------------------------- 模型

class LocalTimeIn(BaseModel):
    """旧端上报的原值：IANA 时区 + 本地年月日时分秒。"""
    tz: str = Field(examples=["America/New_York"], description="IANA 时区名")
    year: int = Field(ge=1, le=9999, examples=[2026])
    month: int = Field(ge=1, le=12, examples=[11])
    day: int = Field(ge=1, le=31, examples=[1])
    hour: int = Field(ge=0, le=23, examples=[1])
    minute: int = Field(ge=0, le=59, examples=[30])
    second: int = Field(ge=0, le=59, examples=[0])


class ConfirmIn(BaseModel):
    """人工确认：必须带上读取时看到的记录版本，防止基于过期视图确认。"""
    version: int = Field(ge=1, description="读取记录时看到的 version")
    utc_time: datetime = Field(description="选定的 UTC（重复时刻须为候选之一）",
                               examples=["2026-11-01T06:30:00Z"])

    @field_validator("utc_time")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("utc_time 必须带时区（如 ...Z 或 ...+00:00）")
        return v


class CandidateOut(BaseModel):
    utc_time: str
    fold: int
    offset: str


class SampleOut(BaseModel):
    sample_no: str
    tz: str
    local_time: str
    version: int
    status: str
    utc_time: str | None
    candidates: list[CandidateOut]
    migrated_version: int | None
    updated_at: str


class SampleWriteOut(BaseModel):
    change: str  # created / updated / unchanged
    record: SampleOut


# ---------------------------------------------------------------- 行 → 输出

def row_to_out(row) -> SampleOut:
    candidates: list[CandidateOut] = []
    if row["status"] == AMBIGUOUS:
        # 依据当前原值实时重算候选（与迁移时存储的 cand_a/cand_b 一致，
        # 因为原值一变版本就会 +1 并回到 pending）
        naive = parse_local(row["local_time"])
        c = classify_local(row["tz"], naive)
        if c.kind == "ambiguous":
            candidates = [
                CandidateOut(utc_time=iso_utc(c.utc_a), fold=0,
                             offset=offset_label(row["tz"], naive, 0)),
                CandidateOut(utc_time=iso_utc(c.utc_b), fold=1,
                             offset=offset_label(row["tz"], naive, 1)),
            ]
    return SampleOut(
        sample_no=row["sample_no"], tz=row["tz"], local_time=row["local_time"],
        version=row["version"], status=row["status"], utc_time=row["utc_time"],
        candidates=candidates, migrated_version=row["migrated_version"],
        updated_at=row["updated_at"],
    )


def get_row_or_404(conn, sample_no: str):
    row = conn.execute("SELECT * FROM samples WHERE sample_no=?",
                       (sample_no,)).fetchone()
    if row is None:
        raise HTTPException(404, detail={"error": "SAMPLE_NOT_FOUND",
                                         "sample_no": sample_no})
    return row


# ---------------------------------------------------------------- 旧端写入（补报/改报）

def upsert_sample(conn, sample_no: str, tz: str, naive: datetime) -> tuple[Any, str]:
    """按样本号写原值。返回 (行, change)。

    - 不存在 → 新建 version=1, pending
    - 原值（时区+本地时刻）未变 → 什么都不动，保留既有确认（幂等重报）
    - 原值变了 → version+1，撤销原确认与候选，回到 pending 等待重迁
    """
    local = fmt_local(naive)
    now = utcnow_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM samples WHERE sample_no=?",
                           (sample_no,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO samples (sample_no, tz, local_time, version, status,"
                " utc_time, cand_a, cand_b, migrated_version, updated_at)"
                " VALUES (?,?,?,1,'pending',NULL,NULL,NULL,NULL,?)",
                (sample_no, tz, local, now))
            change = "created"
        elif row["tz"] == tz and row["local_time"] == local:
            change = "unchanged"  # 原值未变的重报：保留确认
        else:
            conn.execute(
                "UPDATE samples SET tz=?, local_time=?, version=version+1,"
                " status='pending', utc_time=NULL, cand_a=NULL, cand_b=NULL,"
                " migrated_version=NULL, updated_at=? WHERE sample_no=?",
                (tz, local, now, sample_no))
            change = "updated"  # 改报：撤销原确认并重迁
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    row = conn.execute("SELECT * FROM samples WHERE sample_no=?",
                       (sample_no,)).fetchone()
    return row, change


def validate_local_time(body: LocalTimeIn) -> datetime:
    try:
        ZoneInfo(body.tz)
    except Exception:
        raise HTTPException(400, detail={"error": "UNKNOWN_TIME_ZONE",
                                         "tz": body.tz})
    try:
        return datetime(body.year, body.month, body.day,
                        body.hour, body.minute, body.second)
    except ValueError as e:
        raise HTTPException(400, detail={"error": "INVALID_LOCAL_TIME",
                                         "message": str(e)})


# ---------------------------------------------------------------- 应用与后台 worker

async def migration_worker(stop: asyncio.Event, interval: float,
                           batch_size: int, delay_ms: int) -> None:
    while not stop.is_set():
        try:
            stats = await asyncio.to_thread(
                migrate_batch, db_path(), batch_size, delay_ms)
            WORKER_STATE["last_run_at"] = utcnow_iso()
            WORKER_STATE["last_stats"] = stats
            WORKER_STATE["batches"] += 1
        except Exception:
            logger.exception("migration batch failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    c = cfg()
    conn = connect()
    try:
        empty = conn.execute("SELECT COUNT(*) n FROM samples").fetchone()["n"] == 0
    finally:
        conn.close()
    if c["seed_demo"] and empty:
        # 仅空库播种：重启不会把已修正/已确认的演示样本改回原值
        seed_demo_samples()
    stop = asyncio.Event()
    task = asyncio.create_task(
        migration_worker(stop, c["interval"], c["batch"], c["delay_ms"]))
    logger.info("migration worker started: interval=%ss batch=%s db=%s",
                c["interval"], c["batch"], db_path())
    yield
    stop.set()
    await task


app = FastAPI(
    title="采样时间 UTC 迁移服务",
    version="1.0.0",
    description=("旧库本地采样时间迁移为 UTC。迁移期间旧端可照常补报/改报；"
                 "唯一换算自动确认，夏令时重复/不存在时刻留待核对。"),
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.put("/samples/{sample_no}", response_model=SampleWriteOut,
         tags=["旧端录入"])
def put_sample(sample_no: str, body: LocalTimeIn):
    """旧端按样本号写原值（补报/改报同入口）。

    - 首次上报 → 建档待迁移
    - 时区或本地时刻变化 → 撤销原确认，版本 +1，重新迁移
    - 原值未变的重报 → 保留既有确认，幂等
    """
    naive = validate_local_time(body)
    conn = connect()
    try:
        row, change = upsert_sample(conn, sample_no, body.tz, naive)
        return SampleWriteOut(change=change, record=row_to_out(row))
    finally:
        conn.close()


@app.get("/samples/{sample_no}", response_model=SampleOut, tags=["查询"])
def get_sample(sample_no: str):
    """查某样本的原值、版本、状态、UTC（重复时刻附候选及偏移）。"""
    conn = connect()
    try:
        return row_to_out(get_row_or_404(conn, sample_no))
    finally:
        conn.close()


@app.get("/samples", response_model=dict, tags=["查询"])
def list_samples(status: str | None = Query(
                     None, description="按状态过滤：pending/confirmed/"
                                       "ambiguous/nonexistent"),
                 limit: int = Query(100, ge=1, le=1000),
                 offset: int = Query(0, ge=0)):
    """记录列表。`status=ambiguous` / `nonexistent` 即待人工核对队列。"""
    if status is not None and status not in (PENDING, CONFIRMED, AMBIGUOUS,
                                             NONEXISTENT):
        raise HTTPException(422, detail={"error": "INVALID_STATUS",
                                         "status": status})
    conn = connect()
    try:
        where = "WHERE status=?" if status else ""
        args: list[Any] = [status] if status else []
        total = conn.execute(f"SELECT COUNT(*) c FROM samples {where}",
                             args).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM samples {where} ORDER BY updated_at DESC, sample_no"
            " LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return {"total": total, "items": [row_to_out(r) for r in rows]}
    finally:
        conn.close()


@app.post("/samples/{sample_no}/confirm", response_model=SampleOut,
          tags=["人工核对"])
def confirm_sample(sample_no: str, body: ConfirmIn):
    """人工确认 UTC。

    - 重复时刻（ambiguous）：须提交读取时版本 + 两个候选之一
    - 待迁移（pending）记录也可直接确认（服务端按当前原值实时校验）
    - 不存在时刻（nonexistent）：拒绝确认，必须先改原值
    - 版本不匹配或候选不合法 → 409
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = get_row_or_404(conn, sample_no)
            provided = iso_utc(body.utc_time)

            # 幂等重试：同样的版本同样的 UTC，直接返回现状
            if (row["status"] == CONFIRMED and row["version"] == body.version
                    and row["utc_time"] == provided):
                conn.execute("COMMIT")
                return row_to_out(row)

            if row["version"] != body.version:
                raise HTTPException(409, detail={
                    "error": "VERSION_CONFLICT",
                    "message": "记录已被改报或确认，请重新读取后再试",
                    "current_version": row["version"],
                    "current_status": row["status"]})
            if row["status"] == CONFIRMED:
                raise HTTPException(409, detail={
                    "error": "ALREADY_CONFIRMED",
                    "message": "该版本已确认，如需变更请走改报",
                    "utc_time": row["utc_time"]})

            # 按当前原值实时分类，校验提交的 UTC 是否合法
            c = classify_local(row["tz"], parse_local(row["local_time"]))
            if c.kind == "nonexistent":
                raise HTTPException(409, detail={
                    "error": "LOCAL_TIME_NONEXISTENT",
                    "message": "该本地时刻在此时区不存在（夏令时跳过），"
                               "不能确认，必须先由旧端改原值"})
            valid = {iso_utc(c.utc_a)} | ({iso_utc(c.utc_b)} if c.utc_b else set())
            if provided not in valid:
                raise HTTPException(409, detail={
                    "error": "INVALID_CANDIDATE",
                    "message": "提交的 UTC 不是该原值的合法候选",
                    "valid_candidates": sorted(valid)})

            cur = conn.execute(
                "UPDATE samples SET status='confirmed', utc_time=?,"
                " migrated_version=?, updated_at=?"
                " WHERE sample_no=? AND version=?"
                " AND status IN ('pending','ambiguous')",
                (provided, body.version, utcnow_iso(), sample_no, body.version))
            if cur.rowcount != 1:
                raise HTTPException(409, detail={"error": "CONFIRM_RACE",
                                                 "message": "确认与并发变更冲突，请重读"})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return row_to_out(get_row_or_404(conn, sample_no))
    finally:
        conn.close()


@app.get("/research/samples", response_model=dict, tags=["研究取数"])
def research_samples(
        utc_from: datetime | None = Query(None, description="UTC 下界（含）"),
        utc_to: datetime | None = Query(None, description="UTC 上界（含）"),
        limit: int = Query(1000, ge=1, le=10000)):
    """研究取数：只返回已确认记录。

    一致性保证：整个查询是单条 SELECT，SQLite 在 WAL 下为单语句提供
    同一已提交快照——本次返回的所有原值与确认结果来自同一状态；
    且确认行恒满足 migrated_version = version（确认结果对应的就是
    同行的原值版本），改报会撤销确认，故不会读到"新原值配旧 UTC"。
    """
    for p in (utc_from, utc_to):
        if p is not None and p.tzinfo is None:
            raise HTTPException(422, detail={"error": "NAIVE_DATETIME",
                                             "message": "utc_from/utc_to 必须带时区"})
    lo = iso_utc(utc_from) if utc_from else None
    hi = iso_utc(utc_to) if utc_to else None
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT sample_no, tz, local_time, version, utc_time FROM samples"
            " WHERE status='confirmed' AND migrated_version = version"
            " AND (? IS NULL OR utc_time >= ?) AND (? IS NULL OR utc_time <= ?)"
            " ORDER BY utc_time, sample_no LIMIT ?",
            (lo, lo, hi, hi, limit)).fetchall()
        return {"count": len(rows), "snapshot": "single-statement",
                "items": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/migration/status", response_model=dict, tags=["迁移"])
def migration_status():
    """迁移进度：各状态数量 + 后台 worker 状态。"""
    conn = connect()
    try:
        counts = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) c FROM samples GROUP BY status")}
        return {
            "counts": {s: counts.get(s, 0)
                       for s in (PENDING, CONFIRMED, AMBIGUOUS, NONEXISTENT)},
            "worker": {"interval_seconds": cfg()["interval"],
                       "batch_size": cfg()["batch"],
                       **WORKER_STATE},
        }
    finally:
        conn.close()


@app.post("/migration/run-once", response_model=dict, tags=["迁移"])
def migration_run_once():
    """立即同步迁移一批（后台 worker 之外的手动触发，便于演示与运维）。"""
    return migrate_batch(db_path(), cfg()["batch"], cfg()["delay_ms"])


def seed_demo_samples() -> list[dict]:
    conn = connect()
    try:
        out = []
        for s in DEMO_SAMPLES:
            naive = datetime(s["year"], s["month"], s["day"],
                             s["hour"], s["minute"], s["second"])
            row, change = upsert_sample(conn, s["sample_no"], s["tz"], naive)
            out.append({"change": change, "record": row_to_out(row).model_dump()})
        return out
    finally:
        conn.close()


@app.post("/demo/seed", response_model=list[dict], tags=["演示"])
def demo_seed():
    """写入三个演示样本：唯一换算 / 夏令时重复 / 夏令时不存在。幂等。"""
    return seed_demo_samples()
