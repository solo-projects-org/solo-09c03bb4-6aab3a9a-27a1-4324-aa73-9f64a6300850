#!/usr/bin/env bash
# 演示脚本：服务启动后（docker compose up）运行，走完关键场景。
set -euo pipefail
B="${BASE_URL:-http://localhost:8000}"
J() { python3 -m json.tool --no-ensure-ascii 2>/dev/null || cat; }

echo "== 0. 迁移进度 =="
curl -s "$B/migration/status" | J

echo; echo "== A. 夏令时重复时刻：候选与版本 =="
curl -s "$B/samples/DEMO-DST-AMBIGUOUS-001" | J
echo "-- 按读取版本选候选 UTC 确认 --"
curl -s -X POST "$B/samples/DEMO-DST-AMBIGUOUS-001/confirm" \
  -H 'Content-Type: application/json' \
  -d '{"version":1,"utc_time":"2026-11-01T06:30:00Z"}' | J

echo; echo "== B. 夏令时不存在时刻：确认被拒 =="
curl -s -X POST "$B/samples/DEMO-DST-NONEXISTENT-001/confirm" \
  -H 'Content-Type: application/json' \
  -d '{"version":1,"utc_time":"2026-03-08T07:30:00Z"}' | J
echo "-- 旧端改原值为 03:30（存在的时刻）--"
curl -s -X PUT "$B/samples/DEMO-DST-NONEXISTENT-001" \
  -H 'Content-Type: application/json' \
  -d '{"tz":"America/New_York","year":2026,"month":3,"day":8,"hour":3,"minute":30,"second":0}' | J
sleep 3
echo "-- 重迁后 --"
curl -s "$B/samples/DEMO-DST-NONEXISTENT-001" | J

echo; echo "== C. 迁移中改报：撤销确认并重迁 =="
curl -s -X PUT "$B/samples/DEMO-UNIQUE-001" \
  -H 'Content-Type: application/json' \
  -d '{"tz":"Asia/Shanghai","year":2026,"month":6,"day":15,"hour":9,"minute":45,"second":0}' | J
sleep 3
curl -s "$B/samples/DEMO-UNIQUE-001" | J
echo "-- 原值未变的重报：保留确认 --"
curl -s -X PUT "$B/samples/DEMO-UNIQUE-001" \
  -H 'Content-Type: application/json' \
  -d '{"tz":"Asia/Shanghai","year":2026,"month":6,"day":15,"hour":9,"minute":45,"second":0}' | J

echo; echo "== D. 研究取数（仅已确认）=="
curl -s "$B/research/samples" | J
