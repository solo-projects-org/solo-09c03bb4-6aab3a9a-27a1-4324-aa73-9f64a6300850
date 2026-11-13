"""端到端与并发安全测试。运行：pytest tests/ -v"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import app
from app.migrator import apply_result, migrate_batch
from app.timeutil import classify_local

NY = "America/New_York"
SH = "Asia/Shanghai"


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """每个测试独立的数据库文件。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("MIGRATION_INTERVAL_SECONDS", "0.1")
    yield


@pytest.fixture
def client():
    # 不进入上下文 → 不启动后台 worker，迁移用 migrate_batch 显式触发，确定性高
    return TestClient(app, raise_server_exceptions=False)


def put(client, sample_no, tz=SH, y=2026, mo=6, d=15, h=8, mi=30, s=0):
    return client.put(f"/samples/{sample_no}", json={
        "tz": tz, "year": y, "month": mo, "day": d,
        "hour": h, "minute": mi, "second": s})


# ---------------------------------------------------------------- 时间分类

def test_classify_unique():
    c = classify_local(SH, datetime(2026, 6, 15, 8, 30, 0))
    assert c.kind == "unique"
    assert c.utc_a.isoformat() == "2026-06-15T00:30:00+00:00"


def test_classify_ambiguous():
    # 2026-11-01 02:00 EDT 回拨 01:00 EST，01:30 出现两次
    c = classify_local(NY, datetime(2026, 11, 1, 1, 30, 0))
    assert c.kind == "ambiguous"
    assert c.utc_a.isoformat() == "2026-11-01T05:30:00+00:00"  # fold=0, EDT
    assert c.utc_b.isoformat() == "2026-11-01T06:30:00+00:00"  # fold=1, EST


def test_classify_nonexistent():
    # 2026-03-08 02:00 跳到 03:00，02:30 不存在
    c = classify_local(NY, datetime(2026, 3, 8, 2, 30, 0))
    assert c.kind == "nonexistent"


# ---------------------------------------------------------------- 录入与自动迁移

def test_unique_auto_confirmed(client):
    r = put(client, "S1")
    assert r.status_code == 200 and r.json()["change"] == "created"
    assert r.json()["record"]["status"] == "pending"
    migrate_batch()
    rec = client.get("/samples/S1").json()
    assert rec["status"] == "confirmed"
    assert rec["utc_time"] == "2026-06-15T00:30:00Z"
    assert rec["version"] == 1 and rec["migrated_version"] == 1


def test_ambiguous_flow(client):
    put(client, "S2", tz=NY, mo=11, d=1, h=1)
    migrate_batch()
    rec = client.get("/samples/S2").json()
    assert rec["status"] == "ambiguous"
    cands = [c["utc_time"] for c in rec["candidates"]]
    assert cands == ["2026-11-01T05:30:00Z", "2026-11-01T06:30:00Z"]
    assert rec["candidates"][0]["offset"] == "UTC-04:00"
    assert rec["candidates"][1]["offset"] == "UTC-05:00"

    # 非候选 UTC → 409
    r = client.post("/samples/S2/confirm",
                    json={"version": 1, "utc_time": "2026-11-01T07:30:00Z"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "INVALID_CANDIDATE"
    # 错误版本 → 409
    r = client.post("/samples/S2/confirm",
                    json={"version": 99, "utc_time": cands[1]})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "VERSION_CONFLICT"
    # 按读取时的版本选候选 → 确认成功
    r = client.post("/samples/S2/confirm", json={"version": 1, "utc_time": cands[1]})
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"
    assert r.json()["utc_time"] == "2026-11-01T06:30:00Z"
    # 幂等重试 → 200
    r = client.post("/samples/S2/confirm", json={"version": 1, "utc_time": cands[1]})
    assert r.status_code == 200
    # 已确认后想换候选 → 409
    r = client.post("/samples/S2/confirm", json={"version": 1, "utc_time": cands[0]})
    assert r.status_code == 409


def test_nonexistent_must_correct(client):
    put(client, "S3", tz=NY, mo=3, d=8, h=2)
    migrate_batch()
    rec = client.get("/samples/S3").json()
    assert rec["status"] == "nonexistent"
    # 不存在时刻不能确认，必须改原值
    r = client.post("/samples/S3/confirm",
                    json={"version": 1, "utc_time": "2026-03-08T07:30:00Z"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "LOCAL_TIME_NONEXISTENT"
    # 旧端改原值 → 版本 +1，重迁后自动确认
    r = put(client, "S3", tz=NY, mo=3, d=8, h=3)  # 03:30 存在
    assert r.json()["change"] == "updated"
    assert r.json()["record"]["version"] == 2
    assert r.json()["record"]["status"] == "pending"
    migrate_batch()
    rec = client.get("/samples/S3").json()
    assert rec["status"] == "confirmed"
    assert rec["utc_time"] == "2026-03-08T07:30:00Z"
    assert rec["migrated_version"] == 2


# ---------------------------------------------------------------- 补报/改报语义

def test_unchanged_repost_keeps_confirmation(client):
    put(client, "S4")
    migrate_batch()
    assert client.get("/samples/S4").json()["status"] == "confirmed"
    r = put(client, "S4")  # 原值未变的重报
    assert r.json()["change"] == "unchanged"
    rec = client.get("/samples/S4").json()
    assert rec["status"] == "confirmed" and rec["version"] == 1
    assert rec["utc_time"] == "2026-06-15T00:30:00Z"


def test_correction_revokes_and_remigrates(client):
    put(client, "S5")
    migrate_batch()
    assert client.get("/samples/S5").json()["status"] == "confirmed"
    r = put(client, "S5", h=9, mi=45)  # 改报：时间变了
    assert r.json()["change"] == "updated"
    rec = r.json()["record"]
    assert rec["version"] == 2 and rec["status"] == "pending"
    assert rec["utc_time"] is None and rec["migrated_version"] is None
    migrate_batch()
    rec = client.get("/samples/S5").json()
    assert rec["status"] == "confirmed"
    assert rec["utc_time"] == "2026-06-15T01:45:00Z"


def test_timezone_change_also_remigrates(client):
    put(client, "S5B", tz=SH)
    migrate_batch()
    assert client.get("/samples/S5B").json()["utc_time"] == "2026-06-15T00:30:00Z"
    r = put(client, "S5B", tz="UTC")  # 只改时区
    assert r.json()["change"] == "updated"
    migrate_batch()
    assert client.get("/samples/S5B").json()["utc_time"] == "2026-06-15T08:30:00Z"


def test_invalid_input_rejected(client):
    r = client.put("/samples/BAD1", json={
        "tz": "Not/AZone", "year": 2026, "month": 1, "day": 1,
        "hour": 0, "minute": 0, "second": 0})
    assert r.status_code == 400
    r = client.put("/samples/BAD2", json={
        "tz": SH, "year": 2026, "month": 2, "day": 30,
        "hour": 0, "minute": 0, "second": 0})
    assert r.status_code == 400
    assert client.get("/samples/BAD1").status_code == 404


# ---------------------------------------------------------------- 迟到结果不覆盖

def test_late_migration_cannot_overwrite_correction():
    """迁移读到 v1 后旧端改报为 v2，v1 的迁移结果必须被丢弃。"""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO samples VALUES ('S6','Asia/Shanghai','2026-06-15 08:30:00',"
            "1,'pending',NULL,NULL,NULL,NULL,'2026-01-01T00:00:00Z')")
        # 模拟迁移已读出 v1，期间旧端改报 → version=2
        conn.execute(
            "UPDATE samples SET local_time='2026-06-15 09:45:00', version=2,"
            " status='pending' WHERE sample_no='S6'")
        ok = apply_result(conn, "S6", 1, "confirmed",
                          "2026-06-15T00:30:00Z", None, None)
        assert ok is False  # 迟到结果被拒绝
        row = conn.execute("SELECT * FROM samples WHERE sample_no='S6'").fetchone()
        assert row["version"] == 2 and row["status"] == "pending"
        assert row["utc_time"] is None  # 未被旧结果污染
    finally:
        conn.close()


def test_late_migration_cannot_overwrite_manual_confirm():
    """迁移读到 pending v1 后记录被人工确认，迁移结果不得覆盖确认。"""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO samples VALUES ('S7','Asia/Shanghai','2026-06-15 08:30:00',"
            "1,'pending',NULL,NULL,NULL,NULL,'2026-01-01T00:00:00Z')")
        conn.execute(
            "UPDATE samples SET status='confirmed', utc_time='2026-06-15T00:30:00Z',"
            " migrated_version=1 WHERE sample_no='S7'")  # 人工确认先到
        ok = apply_result(conn, "S7", 1, "ambiguous", None,
                          "2026-06-15T00:30:00Z", "2026-06-15T01:30:00Z")
        assert ok is False
        row = conn.execute("SELECT * FROM samples WHERE sample_no='S7'").fetchone()
        assert row["status"] == "confirmed"
        assert row["utc_time"] == "2026-06-15T00:30:00Z"
    finally:
        conn.close()


def test_manual_confirm_while_pending(client):
    """pending 记录可直接人工确认（服务端按当前原值校验），此后迁移不再动它。"""
    put(client, "S8")
    r = client.post("/samples/S8/confirm",
                    json={"version": 1, "utc_time": "2026-06-15T00:30:00Z"})
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    stats = migrate_batch()  # 已确认，不再被迁移拾取
    assert stats["picked"] == 0
    assert client.get("/samples/S8").json()["status"] == "confirmed"


# ---------------------------------------------------------------- 研究取数

def test_research_only_confirmed(client):
    put(client, "R1")                                    # → confirmed
    put(client, "R2", tz=NY, mo=11, d=1, h=1)            # → ambiguous
    put(client, "R3", tz=NY, mo=3, d=8, h=2)             # → nonexistent
    put(client, "R4", h=10)                              # → confirmed（另一时刻）
    migrate_batch()
    res = client.get("/research/samples").json()
    nos = [i["sample_no"] for i in res["items"]]
    assert nos == ["R1", "R4"]  # 只有已确认记录，按 UTC 排序
    item = res["items"][0]
    assert item["utc_time"] == "2026-06-15T00:30:00Z"
    assert item["version"] == 1
    assert res["items"][1]["utc_time"] == "2026-06-15T02:30:00Z"
    # UTC 范围过滤
    res = client.get("/research/samples",
                     params={"utc_from": "2026-06-15T01:00:00Z"}).json()
    assert res["count"] == 1
    res = client.get("/research/samples",
                     params={"utc_to": "2026-06-15T01:00:00Z"}).json()
    assert res["count"] == 1
    # 朴素时间被拒绝
    assert client.get("/research/samples",
                      params={"utc_from": "2026-06-15 01:00:00"}).status_code == 422


def test_research_never_sees_mismatched_version(client):
    """确认行恒满足 migrated_version = version（改报即撤销确认）。"""
    put(client, "R5")
    migrate_batch()
    put(client, "R5", h=12)  # 改报 → 确认被撤销
    res = client.get("/research/samples").json()
    assert res["count"] == 0  # 新原值未确认前，研究侧看不到该样本


# ---------------------------------------------------------------- 重启续迁

def test_restart_resumes_migration(tmp_path):
    """进程重启后（新连接、同一库文件），pending 记录继续被迁移。"""
    conn = connect()
    conn.execute(
        "INSERT INTO samples VALUES ('S9','Asia/Shanghai','2026-06-15 08:30:00',"
        "1,'pending',NULL,NULL,NULL,NULL,'2026-01-01T00:00:00Z')")
    conn.close()
    # 模拟重启：全新连接，迁移状态全在库里
    stats = migrate_batch()
    assert stats["confirmed"] == 1
    conn2 = connect()
    row = conn2.execute("SELECT * FROM samples WHERE sample_no='S9'").fetchone()
    assert row["status"] == "confirmed"
    conn2.close()


def test_background_worker_processes_pending(tmp_path, monkeypatch):
    """后台 worker（随应用启动）自动迁移；等价于容器重启后继续工作。"""
    monkeypatch.setenv("MIGRATION_INTERVAL_SECONDS", "0.1")
    with TestClient(app) as c:
        put(c, "S10")
        deadline = time.time() + 5
        rec = {}
        while time.time() < deadline:
            rec = c.get("/samples/S10").json()
            if rec["status"] == "confirmed":
                break
            time.sleep(0.1)
        assert rec["status"] == "confirmed"
        assert rec["utc_time"] == "2026-06-15T00:30:00Z"
        st = c.get("/migration/status").json()
        assert st["counts"]["confirmed"] == 1
        assert st["worker"]["batches"] >= 1


# ---------------------------------------------------------------- 演示种子

def test_demo_seed(client):
    r = client.post("/demo/seed")
    assert r.status_code == 200
    migrate_batch()
    assert client.get("/samples/DEMO-UNIQUE-001").json()["status"] == "confirmed"
    assert client.get("/samples/DEMO-DST-AMBIGUOUS-001").json()["status"] == "ambiguous"
    assert client.get("/samples/DEMO-DST-NONEXISTENT-001").json()["status"] == "nonexistent"
    # 幂等：再次 seed 不破坏状态
    client.post("/demo/seed")
    assert client.get("/samples/DEMO-UNIQUE-001").json()["status"] == "confirmed"
