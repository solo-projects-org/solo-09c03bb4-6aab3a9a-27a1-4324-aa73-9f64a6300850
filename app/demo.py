"""演示种子数据：覆盖唯一换算、夏令时重复、夏令时不存在三种情形，
外加一对"旧端误建"的重复样本号，用于演示归并。

- DEMO-UNIQUE-001          Asia/Shanghai 2026-06-15 08:30:00  普通时刻，自动确认
- DEMO-DST-AMBIGUOUS-001   America/New_York 2026-11-01 01:30:00
                           2026-11-01 02:00 EDT 回拨到 01:00 EST，01:30 出现两次
- DEMO-DST-NONEXISTENT-001 America/New_York 2026-03-08 02:30:00
                           2026-03-08 02:00 跳到 03:00，02:30 不存在
- DEMO-DUP-001 / 002       同一条采样被旧端误建为两个号（时区、本地时刻相同），
                           均唯一换算 → 归并时保留已有确认
- DEMO-DUP-003             另一个号，归并后再被归并（演示链式解析）
- DEMO-DUP-LATE-001        模拟"归并前读出、归并后才写回"的旧迁移（测试用）
"""
from __future__ import annotations

DEMO_SAMPLES = [
    {
        "sample_no": "DEMO-UNIQUE-001",
        "tz": "Asia/Shanghai",
        "year": 2026, "month": 6, "day": 15,
        "hour": 8, "minute": 30, "second": 0,
    },
    {
        "sample_no": "DEMO-DST-AMBIGUOUS-001",
        "tz": "America/New_York",
        "year": 2026, "month": 11, "day": 1,
        "hour": 1, "minute": 30, "second": 0,
    },
    {
        "sample_no": "DEMO-DST-NONEXISTENT-001",
        "tz": "America/New_York",
        "year": 2026, "month": 3, "day": 8,
        "hour": 2, "minute": 30, "second": 0,
    },
    {
        "sample_no": "DEMO-DUP-001",
        "tz": "Asia/Shanghai",
        "year": 2026, "month": 6, "day": 16,
        "hour": 7, "minute": 15, "second": 0,
    },
    {
        "sample_no": "DEMO-DUP-002",
        "tz": "Asia/Shanghai",
        "year": 2026, "month": 6, "day": 16,
        "hour": 7, "minute": 15, "second": 0,
    },
    {
        "sample_no": "DEMO-DUP-003",
        "tz": "Asia/Shanghai",
        "year": 2026, "month": 6, "day": 16,
        "hour": 7, "minute": 15, "second": 0,
    },
]
