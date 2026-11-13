"""时间换算工具：用 zoneinfo 判定本地时刻能否唯一换算为 UTC。

三种结果：
- unique      唯一换算（普通时刻）→ 可自动确认
- ambiguous   重复时刻（夏令时结束回拨，本地时刻出现两次）→ 留待人工选一个候选 UTC
- nonexistent 不存在时刻（夏令时开始跳过，本地时刻不存在）→ 必须改原值
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc

LOCAL_FMT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class Classification:
    kind: str  # "unique" | "ambiguous" | "nonexistent"
    utc_a: datetime | None  # unique: 唯一结果；ambiguous: fold=0 候选
    utc_b: datetime | None  # ambiguous: fold=1 候选


def classify_local(tz_name: str, naive: datetime) -> Classification:
    """对 (IANA 时区, 本地朴素时刻) 做分类。"""
    z = ZoneInfo(tz_name)
    u0 = naive.replace(tzinfo=z, fold=0).astimezone(UTC)
    u1 = naive.replace(tzinfo=z, fold=1).astimezone(UTC)
    if u0 == u1:
        return Classification("unique", u0, None)
    # 回环校验：UTC 反算回本地，能回到原时刻才是真实存在的本地时刻
    ok0 = u0.astimezone(z).replace(tzinfo=None) == naive
    ok1 = u1.astimezone(z).replace(tzinfo=None) == naive
    if ok0 and ok1:
        return Classification("ambiguous", u0, u1)
    if ok0 != ok1:  # 防御性分支：真实 tzdata 不会走到，但保证有确定行为
        return Classification("unique", u0 if ok0 else u1, None)
    return Classification("nonexistent", None, None)


def fmt_local(naive: datetime) -> str:
    """本地时刻的规范存储格式（朴素，不带时区）。"""
    return naive.strftime(LOCAL_FMT)


def parse_local(text: str) -> datetime:
    return datetime.strptime(text, LOCAL_FMT)


def iso_utc(dt: datetime) -> str:
    """UTC 的规范存储格式，如 2026-11-01T06:30:00Z。字典序即时间序，便于范围查询。"""
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(text: str) -> datetime:
    """解析 ISO 8601 UTC/带偏移时刻，统一归一到 UTC 感知对象。"""
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("must be timezone-aware")
    return dt.astimezone(UTC)


def utcnow_iso() -> str:
    return iso_utc(datetime.now(UTC))


def offset_label(tz_name: str, naive: datetime, fold: int) -> str:
    """候选时刻对应的 UTC 偏移，如 UTC-04:00，供人工核对时参考。"""
    z = ZoneInfo(tz_name)
    off = naive.replace(tzinfo=z, fold=fold).utcoffset()
    if off is None:
        return "UTC"
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"UTC{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"
