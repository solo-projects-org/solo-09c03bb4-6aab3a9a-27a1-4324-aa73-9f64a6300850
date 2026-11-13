"""样本号归并：旧号 → 主号的持久关系、链式解析与原子归并。

设计要点
--------
* `sample_aliases` 只记录"旧号 → 归并当时的主号"这一跳。主号之后可能再次被
  归并，解析时沿链走到最终主号；因此任意旧号始终能找到同一条主记录。
* 归并、改报、确认都从同一个解析入口 `resolve_view` 出发，并在
  `BEGIN IMMEDIATE` 事务内完成"解析 + 校验 + 写入"，避免解析之后、写入之前
  被并发归并插入（否则旧号可能被重新建造成第二条记录）。
* 版本：归并后主记录版本取 `max(两边版本) + 1`。它严格大于归并前任一版本，
  所以归并前读到的两个版本（即使相同）都不能再用于确认。
* 已指向同一最终主号的重复归并是幂等 no-op：不改记录、不改版本、不新增关系。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .migrator import CONFIRMED, PENDING
from .timeutil import utcnow_iso

# 沿旧号链向下解析，最多 64 跳（防御异常数据造成的环）。
_RESOLVE_ROW_SQL = """
WITH RECURSIVE chain(no, depth) AS (
    SELECT ?, 0
    UNION ALL
    SELECT a.primary_no, c.depth + 1
      FROM sample_aliases a
      JOIN chain c ON a.sample_no = c.no
     WHERE c.depth < 64
)
SELECT s.*, c.no AS final_no, c.depth AS alias_depth
  FROM chain c
  LEFT JOIN samples s ON s.sample_no = c.no
 ORDER BY c.depth DESC
 LIMIT 1
"""

# 反向收集所有（直接或间接）指向某最终主号的旧号。
_ALIASES_SQL = """
WITH RECURSIVE up(no) AS (
    SELECT ?
    UNION
    SELECT a.sample_no
      FROM sample_aliases a
      JOIN up u ON a.primary_no = u.no
)
SELECT no FROM up WHERE no <> ? ORDER BY no
"""


class MergeFailure(Exception):
    """归并前置条件不满足。事务已回滚，调用方转为 HTTP 错误。"""

    def __init__(self, status_code: int, error: str, message: str | None = None,
                 **extra):
        super().__init__(message or error)
        self.status_code = status_code
        self.error = error
        self.message = message
        self.extra = extra


def _begin(conn: sqlite3.Connection) -> None:
    """开启写事务（BEGIN IMMEDIATE）。

    连接为自动提交模式；若上层已显式开事务则延用，避免嵌套 BEGIN。
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


@dataclass
class MergeResult:
    merged: bool
    duplicate: bool
    source_no: str
    primary_no: str
    final_primary_no: str
    source_version: int | None
    primary_version: int | None
    new_version: int
    aliases: list[str] = field(default_factory=list)
    row: sqlite3.Row | None = None


def resolve_view(conn: sqlite3.Connection, requested_no: str) -> tuple[sqlite3.Row, str, int]:
    """把任意样本号解析为最终主号，返回 (最终主记录行, 最终主号, 跳数)。

    最终主记录不存在时（正常样本号）行中的 samples 列为 NULL，可用
    `row["sample_no"] is None` 判断。若旧号的链断在缺失的主号上，同样为 NULL
    （调用方应按"记录不存在/关系损坏"处理，绝不能借旧号重建记录）。
    """
    row = conn.execute(_RESOLVE_ROW_SQL, (requested_no,)).fetchone()
    return row, row["final_no"], int(row["alias_depth"])


def list_aliases(conn: sqlite3.Connection, final_no: str) -> list[str]:
    """所有最终指向 final_no 的旧号（含链式旧号），按字典序。"""
    return [r["no"] for r in conn.execute(_ALIASES_SQL, (final_no, final_no))]


def alias_groups(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """一次性解析全部关系，得到 {最终主号: [旧号...]}，供列表接口使用。"""
    direct = {r["sample_no"]: r["primary_no"]
              for r in conn.execute("SELECT sample_no, primary_no FROM sample_aliases")}
    finals: dict[str, str] = {}

    def final_of(no: str) -> str:
        path: list[str] = []
        seen: set[str] = set()
        cur = no
        while cur in direct and cur not in seen and len(path) < 64:
            seen.add(cur)
            path.append(cur)
            cur = direct[cur]
        for p in path:
            finals[p] = cur
        return finals.get(no, cur)

    groups: dict[str, list[str]] = {}
    for source in direct:
        groups.setdefault(final_of(source), []).append(source)
    for values in groups.values():
        values.sort()
    return groups


def merge_samples(conn: sqlite3.Connection, source_no: str, primary_no: str,
                  source_version: int, primary_version: int) -> MergeResult:
    """把 source_no 归并进 primary_no（两者都先解析到最终主号）。

    整个操作在单个 `BEGIN IMMEDIATE` 事务内：任一校验失败、条件不满足或写入
    失败都会回滚，记录内容、版本与编号关系均保持原样。
    """
    now = utcnow_iso()
    _begin(conn)
    try:
        srow, s_final, s_depth = resolve_view(conn, source_no)
        prow, p_final, p_depth = resolve_view(conn, primary_no)

        # 源号已经属于另一个主号：不允许把既有关系改指，避免"复活"已归并数据。
        if s_depth > 0 and s_final != p_final:
            raise MergeFailure(
                409, "SOURCE_ALREADY_MERGED",
                "源号已归并到另一主号，不能再改指",
                source_no=source_no, current_primary_no=s_final)

        if prow["sample_no"] is None:
            raise MergeFailure(404, "PRIMARY_NOT_FOUND",
                               "保留的主号不存在", primary_no=primary_no)

        # 两边解析到同一最终主号：已是同一记录。
        if s_final == p_final:
            if source_no == primary_no and s_depth == 0:
                raise MergeFailure(400, "SAME_SAMPLE",
                                   "源号与主号是同一条记录，不能归并自身")
            # 重复归并（可能源号原本就是主号的旧号）：幂等成功，不改版本。
            aliases = list_aliases(conn, p_final)
            conn.execute("COMMIT")
            return MergeResult(
                merged=False, duplicate=True, source_no=source_no,
                primary_no=primary_no, final_primary_no=p_final,
                source_version=source_version,
                primary_version=primary_version,
                new_version=prow["version"], aliases=aliases, row=prow)

        # 至此源号必是一条现存普通记录（s_depth == 0）。
        if srow["sample_no"] is None:
            raise MergeFailure(404, "SOURCE_NOT_FOUND",
                               "源号不存在", source_no=source_no)

        if srow["version"] != source_version or prow["version"] != primary_version:
            raise MergeFailure(
                409, "VERSION_CONFLICT",
                "源号或主号版本与读取时不一致，请重新读取后再试",
                source_no=source_no, primary_no=primary_no,
                source_current_version=srow["version"],
                primary_current_version=prow["version"],
                current_primary_no=p_final)

        # 归并条件：两边时区与本地时刻必须完全相同。
        if srow["tz"] != prow["tz"] or srow["local_time"] != prow["local_time"]:
            raise MergeFailure(
                409, "MERGE_CONDITION_MISMATCH",
                "两边时区与本地时刻不一致，不能归并",
                source={"tz": srow["tz"], "local_time": srow["local_time"]},
                primary={"tz": prow["tz"], "local_time": prow["local_time"]})

        s_confirmed = srow["status"] == CONFIRMED
        p_confirmed = prow["status"] == CONFIRMED
        if s_confirmed and p_confirmed and srow["utc_time"] != prow["utc_time"]:
            raise MergeFailure(
                409, "UTC_MISMATCH",
                "两边均已确认但 UTC 不同，不能归并",
                source_utc_time=srow["utc_time"],
                primary_utc_time=prow["utc_time"])

        # 严格大于两边归并前版本 → 两个旧版本都失效（即使它们相同）。
        new_version = max(srow["version"], prow["version"]) + 1

        if p_confirmed:
            # 保留主记录已有的确认（两边都确认且 UTC 相同已在上面校验）。
            status = CONFIRMED
            utc_time = prow["utc_time"]
            cand_a, cand_b = prow["cand_a"], prow["cand_b"]
            migrated_version = new_version
        elif s_confirmed:
            # 主号尚未确认、源号已确认：归并保留"已有确认"。
            status = CONFIRMED
            utc_time = srow["utc_time"]
            cand_a, cand_b = srow["cand_a"], srow["cand_b"]
            migrated_version = new_version
        else:
            # 两边都未确认：清空迁移结果，按合并后的原值重新迁移。
            status = PENDING
            utc_time = cand_a = cand_b = migrated_version = None

        cur = conn.execute(
            "UPDATE samples SET version=?, status=?, utc_time=?, cand_a=?,"
            " cand_b=?, migrated_version=?, updated_at=?"
            " WHERE sample_no=? AND version=? AND status=?",
            (new_version, status, utc_time, cand_a, cand_b, migrated_version,
             now, p_final, prow["version"], prow["status"]))
        if cur.rowcount != 1:
            raise MergeFailure(409, "MERGE_RACE",
                               "归并与并发变更冲突，本次未做任何修改")

        # 删除被归并的旧记录；之后任何迟到迁移 UPDATE 都只会命中 0 行，
        # 旧端再 PUT 也会先经 alias 解析到主记录，不会重建旧记录。
        cur = conn.execute(
            "DELETE FROM samples WHERE sample_no=? AND version=? AND status=?",
            (source_no, srow["version"], srow["status"]))
        if cur.rowcount != 1:
            raise MergeFailure(409, "MERGE_RACE",
                               "源记录在归并过程中发生变化，本次未做任何修改")

        conn.execute(
            "INSERT INTO sample_aliases"
            " (sample_no, primary_no, source_version, primary_version,"
            " merged_version, merged_at) VALUES (?,?,?,?,?,?)",
            (source_no, p_final, srow["version"], prow["version"],
             new_version, now))

        merged_row = conn.execute(
            "SELECT * FROM samples WHERE sample_no=?", (p_final,)).fetchone()
        aliases = list_aliases(conn, p_final)
        conn.execute("COMMIT")
        return MergeResult(
            merged=True, duplicate=False, source_no=source_no,
            primary_no=primary_no, final_primary_no=p_final,
            source_version=srow["version"], primary_version=prow["version"],
            new_version=new_version, aliases=aliases, row=merged_row)
    except Exception:
        conn.execute("ROLLBACK")
        raise
