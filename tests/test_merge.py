"""样本号归并的端到端与并发安全测试。运行：pytest tests/ -v"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import app
from app.merging import MergeFailure, merge_samples, resolve_view
from app.migrator import apply_result, migrate_batch

SH = "Asia/Shanghai"
NY = "America/New_York"
T = dict(tz=SH, year=2026, month=6, day=16, hour=7, minute=15, second=0)


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("MIGRATION_INTERVAL_SECONDS", "0.1")
    yield


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


_ALIAS = {"y": "year", "mo": "month", "d": "day", "h": "hour",
          "mi": "minute", "s": "second"}


def put(client, sample_no, **kw):
    body = {**T, **{_ALIAS.get(k, k): v for k, v in kw.items()}}
    return client.put(f"/samples/{sample_no}", json=body)


def merge(client, source, primary, sv, pv):
    return client.post("/samples/merge", json={
        "source_no": source, "primary_no": primary,
        "source_version": sv, "primary_version": pv})


def count_rows():
    conn = connect()
    try:
        return conn.execute("SELECT COUNT(*) c FROM samples").fetchone()["c"]
    finally:
        conn.close()


def read_row(no):
    conn = connect()
    try:
        row, final_no, depth = resolve_view(conn, no)
        aliases = conn.execute(
            "SELECT sample_no FROM sample_aliases WHERE primary_no=?",
            (final_no,)).fetchall() if row["sample_no"] else []
        return dict(row) if row["sample_no"] else None, final_no, depth, [
            a["sample_no"] for a in aliases]
    finally:
        conn.close()


# ---------------------------------------------------------------- 基本归并

def test_merge_both_confirmed_keeps_confirmation(client):
    assert put(client, "P1").json()["record"]["status"] == "pending"
    assert put(client, "S1").json()["record"]["status"] == "pending"
    migrate_batch()
    p = client.get("/samples/P1").json()
    s = client.get("/samples/S1").json()
    assert p["status"] == s["status"] == "confirmed"
    utc = p["utc_time"]

    r = merge(client, "S1", "P1", s["version"], p["version"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["merged"] is True and out["duplicate"] is False
    assert out["final_primary_no"] == "P1"
    assert out["new_version"] == max(s["version"], p["version"]) + 1
    assert out["record"]["sample_no"] == "P1"
    assert out["record"]["utc_time"] == utc
    assert out["record"]["status"] == "confirmed"
    assert out["record"]["migrated_version"] == out["new_version"]
    assert out["aliases"] == ["S1"]

    # 研究取数只留一条
    items = client.get("/research/samples").json()["items"]
    assert [i["sample_no"] for i in items] == ["P1"]

    # 旧号查询 → 同一条主记录，注明最终主号
    g = client.get("/samples/S1").json()
    assert (g["sample_no"], g["final_primary_no"], g["requested_no"]) == \
        ("P1", "P1", "S1")
    assert g["version"] == out["new_version"]


def test_merge_keeps_source_confirmation_when_primary_unconfirmed(client):
    put(client, "P2", tz=NY, mo=11, d=1, h=1)   # ambiguous
    put(client, "S2", tz=NY, mo=11, d=1, h=1)   # ambiguous
    migrate_batch()
    s = client.get("/samples/S2").json()
    assert s["status"] == "ambiguous"
    cand = s["candidates"][1]["utc_time"]
    r = client.post("/samples/S2/confirm",
                    json={"version": s["version"], "utc_time": cand})
    assert r.status_code == 200
    p = client.get("/samples/P2").json()
    assert p["status"] == "ambiguous"

    out = merge(client, "S2", "P2", s["version"], p["version"]).json()
    assert out["merged"] is True
    assert out["record"]["status"] == "confirmed"
    assert out["record"]["utc_time"] == cand
    assert out["record"]["migrated_version"] == out["new_version"]


def test_merge_both_unconfirmed_remigrates(client):
    put(client, "P3", tz=NY, mo=11, d=1, h=1)
    put(client, "S3", tz=NY, mo=11, d=1, h=1)
    # 都不迁移 → pending
    out = merge(client, "S3", "P3", 1, 1).json()
    assert out["merged"] is True
    assert out["record"]["status"] == "pending"
    assert out["record"]["utc_time"] is None
    assert out["record"]["migrated_version"] is None
    migrate_batch()
    rec = client.get("/samples/P3").json()
    assert rec["status"] == "ambiguous"  # 按合并后的原值重新迁移
    assert rec["version"] == 2


# ---------------------------------------------------------------- 条件校验

def test_tz_or_local_mismatch_rejected_without_changes(client):
    put(client, "P4", tz=SH)
    put(client, "S4", tz=SH, hour=9)
    migrate_batch()
    before_p = client.get("/samples/P4").json()
    before_s = client.get("/samples/S4").json()
    r = merge(client, "S4", "P4", 1, 1)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "MERGE_CONDITION_MISMATCH"
    assert client.get("/samples/P4").json() == before_p
    assert client.get("/samples/S4").json() == before_s
    assert count_rows() == 2


def test_tz_mismatch_even_same_local_rejected(client):
    put(client, "P4B", tz=SH)
    put(client, "S4B", tz="UTC", hour=7)  # 同一本地时刻，不同时区
    migrate_batch()
    r = merge(client, "S4B", "P4B", 1, 1)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "MERGE_CONDITION_MISMATCH"


def test_both_confirmed_but_utc_differs_rejected(client):
    put(client, "P5", tz=NY, mo=11, d=1, h=1)
    put(client, "S5", tz=NY, mo=11, d=1, h=1)
    migrate_batch()
    rec = client.get("/samples/S5").json()
    assert client.post("/samples/S5/confirm", json={
        "version": 1, "utc_time": rec["candidates"][0]["utc_time"]}).status_code == 200
    rec = client.get("/samples/P5").json()
    assert client.post("/samples/P5/confirm", json={
        "version": 1, "utc_time": rec["candidates"][1]["utc_time"]}).status_code == 200
    r = merge(client, "S5", "P5", 1, 1)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "UTC_MISMATCH"
    assert client.get("/samples/P5").json()["status"] == "confirmed"
    assert client.get("/samples/S5").json()["status"] == "confirmed"


def test_version_conflict_either_side_rejected(client):
    put(client, "P6")
    put(client, "S6")
    migrate_batch()
    r = merge(client, "S6", "P6", 99, 1)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "VERSION_CONFLICT"
    r = merge(client, "S6", "P6", 1, 99)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "VERSION_CONFLICT"
    assert count_rows() == 2
    assert client.get("/samples/S6").json()["sample_no"] == "S6"


def test_primary_not_found(client):
    put(client, "S7")
    r = merge(client, "S7", "NOPE", 1, 1)
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "PRIMARY_NOT_FOUND"
    assert count_rows() == 1


# ---------------------------------------------------------------- 版本失效

def test_pre_merge_versions_cannot_confirm_even_if_equal(client):
    """两边归并前版本都是 1；归并后 1 不能再用于确认。"""
    put(client, "P8")   # 不迁移，保持 pending v1
    put(client, "S8")
    out = merge(client, "S8", "P8", 1, 1).json()
    assert out["new_version"] == 2
    for no in ("P8", "S8"):
        r = client.post(f"/samples/{no}/confirm",
                        json={"version": 1, "utc_time": "2026-06-15T23:15:00Z"})
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "VERSION_CONFLICT"
        assert r.json()["detail"]["current_version"] == 2
    # 主记录的新版本可确认
    r = client.post("/samples/S8/confirm",
                    json={"version": 2, "utc_time": "2026-06-15T23:15:00Z"})
    assert r.status_code == 200 and r.json()["sample_no"] == "P8"


# ---------------------------------------------------------------- 旧号改报/确认

def test_old_no_repost_updates_primary(client):
    put(client, "P9")
    put(client, "S9")
    migrate_batch()
    out = merge(client, "S9", "P9", 1, 1).json()
    v = out["new_version"]
    # 旧号沿用原号改报 → 落到主记录
    r = put(client, "S9", hour=8, minute=0)
    assert r.status_code == 200
    rec = r.json()["record"]
    assert r.json()["change"] == "updated"
    assert (rec["sample_no"], rec["requested_no"]) == ("P9", "S9")
    assert rec["version"] == v + 1 and rec["status"] == "pending"
    assert client.get("/samples/P9").json()["version"] == v + 1
    # 旧号原样补报 → 幂等，保留主记录确认
    migrate_batch()
    assert client.get("/samples/P9").json()["status"] == "confirmed"
    r = put(client, "S9", hour=8, minute=0)
    assert r.json()["change"] == "unchanged"
    assert r.json()["record"]["status"] == "confirmed"
    assert client.get("/samples/P9").json()["utc_time"] == "2026-06-16T00:00:00Z"


def test_old_no_confirm_confirms_primary(client):
    put(client, "P10", tz=NY, mo=11, d=1, h=1)
    put(client, "S10", tz=NY, mo=11, d=1, h=1)
    migrate_batch()
    out = merge(client, "S10", "P10", 1, 1).json()
    # 两边都未确认 → 按合并后原值重新迁移
    assert out["record"]["status"] == "pending"
    migrate_batch()
    rec = client.get("/samples/P10").json()
    assert rec["status"] == "ambiguous"
    cand = rec["candidates"][0]["utc_time"]
    r = client.post("/samples/S10/confirm",
                    json={"version": rec["version"], "utc_time": cand})
    assert r.status_code == 200
    assert r.json()["sample_no"] == "P10"
    assert client.get("/samples/P10").json()["utc_time"] == cand


# ---------------------------------------------------------------- 链式归并

def test_chained_merge_resolves_to_final_primary(client):
    for no in ("A1", "B1", "C1"):
        put(client, no)
    migrate_batch()
    assert merge(client, "A1", "B1", 1, 1).status_code == 200
    out = merge(client, "B1", "C1", 2, 1).json()   # B1 归并后版本是 2
    assert out["final_primary_no"] == "C1"
    assert out["aliases"] == ["A1", "B1"]
    assert out["new_version"] == out["record"]["version"] == 3

    # 任一旧号都解析到最终主号
    for old in ("A1", "B1"):
        g = client.get(f"/samples/{old}").json()
        assert (g["sample_no"], g["final_primary_no"], g["requested_no"]) == \
            ("C1", "C1", old)
        assert g["version"] == 3
    # 旧号仍可沿用原号改报，只改主记录
    r = put(client, "A1", hour=9)
    assert r.json()["record"]["sample_no"] == "C1"
    assert count_rows() == 1
    items = client.get("/research/samples").json()["items"]
    assert [i["sample_no"] for i in items] == []  # 改报撤销确认，待重迁
    # 确认旧号 → 主记录（按主记录当前版本）
    rec = client.get("/samples/C1").json()
    assert rec["version"] == 4 and rec["status"] == "pending"
    r = client.post("/samples/A1/confirm", json={
        "version": 4, "utc_time": "2026-06-16T01:15:00Z"})
    assert r.status_code == 200
    assert r.json()["sample_no"] == "C1"
    assert client.get("/samples/C1").json()["status"] == "confirmed"
    items = client.get("/research/samples").json()["items"]
    assert [i["sample_no"] for i in items] == ["C1"]


def test_chained_merge_old_versions_all_invalid(client):
    for no in ("A2", "B2", "C2"):
        put(client, no)
    out1 = merge(client, "A2", "B2", 1, 1).json()           # B2 → v2
    out2 = merge(client, "B2", "C2", out1["new_version"], 1).json()  # C2 → v3
    assert out2["new_version"] == 3
    for ver in (1, 2):
        r = client.post("/samples/C2/confirm",
                        json={"version": ver, "utc_time": "2026-06-15T23:15:00Z"})
        assert r.status_code == 409
        assert r.json()["detail"]["current_version"] == 3


# ---------------------------------------------------------------- 重复归并幂等

def test_remerge_to_same_primary_is_noop(client):
    put(client, "P11")
    put(client, "S11")
    merge(client, "S11", "P11", 1, 1)
    migrate_batch()
    rec = client.get("/samples/P11").json()
    v = rec["version"]
    r = merge(client, "S11", "P11", 1, 1)       # 旧版本 + 已指向同一主号
    assert r.status_code == 200
    assert r.json()["duplicate"] is True and r.json()["merged"] is False
    assert r.json()["new_version"] == v
    assert client.get("/samples/P11").json()["version"] == v
    assert count_rows() == 1


def test_cannot_merge_self(client):
    put(client, "X1")
    r = merge(client, "X1", "X1", 1, 1)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "SAME_SAMPLE"
    assert client.get("/samples/X1").json()["version"] == 1


def test_cannot_repoint_already_merged_source(client):
    put(client, "A3"); put(client, "B3"); put(client, "D3")
    merge(client, "A3", "B3", 1, 1)
    r = merge(client, "A3", "D3", 1, 1)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "SOURCE_ALREADY_MERGED"
    # 关系未被改指
    assert client.get("/samples/A3").json()["final_primary_no"] == "B3"


# ---------------------------------------------------------------- 迟到迁移

def test_late_migration_cannot_restore_source_or_overwrite_primary(client):
    put(client, "P12")
    put(client, "S12")
    migrate_batch()
    # 模拟迁移已读出两边的旧快照，之后才归并
    s_old = client.get("/samples/S12").json()
    p_old = client.get("/samples/P12").json()
    out = merge(client, "S12", "P12", 1, 1).json()
    v = out["new_version"]

    conn = connect()
    try:
        # v1 的迟到结果写回已删除的旧号 → 不恢复记录
        assert apply_result(conn, "S12", 1, "confirm", None,
                            "2026-06-16T00:00:00Z", None) is False
        # v1 的迟到结果写回主号 → 不覆盖归并后的主记录
        assert apply_result(conn, "P12", 1, "ambiguous", None, None, None) is False
        row = conn.execute("SELECT * FROM samples WHERE sample_no='P12'").fetchone()
        assert row["version"] == v and row["status"] == "confirmed"
        assert row["utc_time"] == out["record"]["utc_time"]
        assert conn.execute("SELECT COUNT(*) c FROM samples").fetchone()["c"] == 1
        # 旧号关系仍在，解析到主记录
        row2, final_no, depth = resolve_view(conn, "S12")
        assert final_no == "P12" and depth == 1 and row2["sample_no"] == "P12"
    finally:
        conn.close()
    # 手工断言未被使用（避免 lint 警告）
    assert s_old["version"] == p_old["version"] == 1


def test_late_confirm_after_merge_rejected(client):
    """归并前读到的版本，归并后（即使两边版本相同）确认一律 409。"""
    put(client, "P13", tz=NY, mo=11, d=1, h=1)
    put(client, "S13", tz=NY, mo=11, d=1, h=1)
    migrate_batch()
    cand = client.get("/samples/S13").json()["candidates"][1]["utc_time"]
    out = merge(client, "S13", "P13", 1, 1).json()
    r = client.post("/samples/S13/confirm", json={"version": 1, "utc_time": cand})
    assert r.status_code == 409


# ---------------------------------------------------------------- 并发

def test_concurrent_put_during_merge_keeps_invariants(client):
    """归并与旧端改报并发：两个事务串行化，结果必为二者之一。

    - 改报先提交 → 归并读到的 version 已变 → 409，两条记录原样保留；
    - 归并先提交 → 改报经旧号解析落到主记录，最终只有一条记录。
    两种情况下都不得出现重复记录、断链或被静默覆盖的版本。
    """
    put(client, "PC", tz=NY, mo=11, d=1, h=1)
    put(client, "SC", tz=NY, mo=11, d=1, h=1)
    migrate_batch()
    results = {}
    barrier = threading.Barrier(2)

    def do_merge():
        barrier.wait()
        r = merge(client, "SC", "PC", 1, 1)
        results["merge"] = r

    def do_repost():
        barrier.wait()
        results["put"] = put(client, "SC", hour=1, minute=45)

    ts = [threading.Thread(target=do_merge), threading.Thread(target=do_repost)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert results["merge"].status_code in (200, 409)
    assert results["put"].status_code == 200
    conn = connect()
    try:
        rows = {r["sample_no"]: dict(r) for r in conn.execute(
            "SELECT * FROM samples")}
        alias = [dict(r) for r in conn.execute("SELECT * FROM sample_aliases")]
        final_no = resolve_view(conn, "SC")[1]
        if results["merge"].status_code == 200:
            # 归并先赢：只剩主记录，旧号永续解析到主记录
            assert list(rows) == ["PC"]
            assert final_no == "PC"
            assert [a["sample_no"] for a in alias] == ["SC"]
            assert results["put"].json()["record"]["sample_no"] == "PC"
            assert results["put"].json()["record"]["requested_no"] == "SC"
            # 主记录版本 = 归并版本 + 改报再 +1，且改报落的是新原值
            assert rows["PC"]["version"] >= 3
            assert rows["PC"]["local_time"] == "2026-06-16 01:45:00"
        else:
            # 改报先赢：版本冲突拒绝归并，关系与记录均未改变
            assert set(rows) == {"PC", "SC"}
            assert alias == []
            assert final_no == "SC"
            assert results["merge"].json()["detail"]["error"] == "VERSION_CONFLICT"
            assert rows["SC"]["version"] == 2
            assert rows["PC"]["version"] == 1
    finally:
        conn.close()


def test_merge_failure_is_atomic(client):
    put(client, "PA")
    put(client, "SA", hour=9)  # 条件不满足
    migrate_batch()
    conn = connect()
    try:
        before = dict(conn.execute("SELECT * FROM samples WHERE sample_no='PA'").fetchone())
        sbefore = dict(conn.execute("SELECT * FROM samples WHERE sample_no='SA'").fetchone())
        with pytest.raises(MergeFailure):
            merge_samples(conn, "SA", "PA", 1, 1)
        after = dict(conn.execute("SELECT * FROM samples WHERE sample_no='PA'").fetchone())
        safter = dict(conn.execute("SELECT * FROM samples WHERE sample_no='SA'").fetchone())
        assert conn.execute("SELECT COUNT(*) c FROM sample_aliases").fetchone()["c"] == 0
    finally:
        conn.close()
    assert before == after and sbefore == safter


# ---------------------------------------------------------------- 持久化

def test_aliases_persist_across_reconnect(tmp_path, monkeypatch):
    """关系存库，新连接（等价容器重启）后仍可解析旧号。"""
    db = str(tmp_path / "persist.db")
    monkeypatch.setenv("DB_PATH", db)
    c = TestClient(app, raise_server_exceptions=False)
    put(c, "PK")
    put(c, "SK")
    out = merge(c, "SK", "PK", 1, 1).json()
    # 全新连接读取
    conn = connect(db)
    try:
        row, final_no, depth = resolve_view(conn, "SK")
        assert final_no == "PK" and depth == 1
        assert row["sample_no"] == "PK" and row["version"] == out["new_version"]
    finally:
        conn.close()


# ---------------------------------------------------------------- 研究取数 & 列表

def test_research_and_list_after_merge(client):
    put(client, "R0")                 # 独立
    put(client, "M1"); put(client, "M2")
    migrate_batch()
    merge(client, "M2", "M1", 1, 1)
    res = client.get("/research/samples").json()
    assert [i["sample_no"] for i in res["items"]] == ["M1", "R0"]
    lst = client.get("/samples").json()
    assert lst["total"] == 2
    item = [i for i in lst["items"] if i["sample_no"] == "M1"][0]
    assert item["aliases"] == ["M2"]
    al = client.get("/samples/M2/aliases").json()
    assert al == {"final_primary_no": "M1", "aliases": ["M2"]}
    assert client.get("/samples/UNKNOWN/aliases").status_code == 404
