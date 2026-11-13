#!/usr/bin/env bash
# 归并演示脚本：服务启动后（docker compose up）运行。
#   bash scripts/merge_demo.sh
# 覆盖：连续归并（链式）、旧号改报、拒绝旧版本确认、幂等重复归并。
set -euo pipefail
B="${BASE_URL:-http://localhost:8000}"
J() { python3 -m json.tool --no-ensure-ascii 2>/dev/null || cat; }
# 提取 JSON 字段（避免依赖 jq）
field() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(d'"$1"')'; }

echo "== 0. 演示种子（含重复号 DEMO-DUP-001/002/003）=="
curl -s -X POST "$B/demo/seed" >/dev/null
curl -s -X POST "$B/migration/run-once" >/dev/null
for n in DEMO-DUP-001 DEMO-DUP-002 DEMO-DUP-003; do
  curl -s "$B/samples/$n" | field "['version'], d['status'], d['utc_time']" \
    | xargs echo "$n:"
done

V1=$(curl -s "$B/samples/DEMO-DUP-001" | field "['version']")
V2=$(curl -s "$B/samples/DEMO-DUP-002" | field "['version']")
V3=$(curl -s "$B/samples/DEMO-DUP-003" | field "['version']")

echo; echo "== 1. 第一次归并：DEMO-DUP-002 → DEMO-DUP-001（保留已有确认）=="
curl -s -X POST "$B/samples/merge" -H 'Content-Type: application/json' \
  -d "{\"source_no\":\"DEMO-DUP-002\",\"primary_no\":\"DEMO-DUP-001\",\"source_version\":$V2,\"primary_version\":$V1}" | J
NEWV=$(curl -s "$B/samples/DEMO-DUP-001" | field "['version']")

echo; echo "== 2. 第二次归并（链式）：DEMO-DUP-001 → DEMO-DUP-003 =="
V3=$(curl -s "$B/samples/DEMO-DUP-003" | field "['version']")
curl -s -X POST "$B/samples/merge" -H 'Content-Type: application/json' \
  -d "{\"source_no\":\"DEMO-DUP-001\",\"primary_no\":\"DEMO-DUP-003\",\"source_version\":$NEWV,\"primary_version\":$V3}" | J

echo; echo "== 3. 任一旧号查询：都解析到最终主号 DEMO-DUP-003 =="
for n in DEMO-DUP-001 DEMO-DUP-002 DEMO-DUP-003; do
  curl -s "$B/samples/$n" | field "['requested_no'], d['sample_no'], d['final_primary_no'], d['version']" \
    | xargs echo "$n →"
done

echo; echo "== 4. 旧号 DEMO-DUP-002 改报：落到最终主记录 =="
curl -s -X PUT "$B/samples/DEMO-DUP-002" -H 'Content-Type: application/json' \
  -d '{"tz":"Asia/Shanghai","year":2026,"month":6,"day":16,"hour":8,"minute":0,"second":0}' | J
sleep 3
curl -s "$B/samples/DEMO-DUP-003" | field "['sample_no'], d['version'], d['status'], d['utc_time']" \
  | xargs echo "主记录:"

echo; echo "== 5. 拒绝旧版本确认（归并前读到的版本，两边相同也不可用）=="
FINALV=$(curl -s "$B/samples/DEMO-DUP-003" | field "['version']")
curl -s -o /dev/null -w 'version=1 旧版本确认 → HTTP %{http_code}\n' \
  -X POST "$B/samples/DEMO-DUP-002/confirm" -H 'Content-Type: application/json' \
  -d '{"version":1,"utc_time":"2026-06-16T00:00:00Z"}'
echo "-- 按当前主记录版本确认 →"
curl -s -X POST "$B/samples/DEMO-DUP-002/confirm" -H 'Content-Type: application/json' \
  -d "{\"version\":$FINALV,\"utc_time\":\"2026-06-16T00:00:00Z\"}" | J

echo; echo "== 6. 重复归并（已指向同一主号）：幂等 no-op，版本不变 =="
BEFORE=$(curl -s "$B/samples/DEMO-DUP-003" | field "['version']")
curl -s -X POST "$B/samples/merge" -H 'Content-Type: application/json' \
  -d "{\"source_no\":\"DEMO-DUP-002\",\"primary_no\":\"DEMO-DUP-003\",\"source_version\":1,\"primary_version\":1}" | J
AFTER=$(curl -s "$B/samples/DEMO-DUP-003" | field "['version']")
echo "version before=$BEFORE after=$AFTER（应相同）"

echo; echo "== 7. 归并条件不满足被拒：DEMO-UNIQUE-001 与主号本地时刻不同 =="
UV=$(curl -s "$B/samples/DEMO-UNIQUE-001" | field "['version']")
FV=$(curl -s "$B/samples/DEMO-DUP-003" | field "['version']")
curl -s -X POST "$B/samples/merge" -H 'Content-Type: application/json' \
  -d "{\"source_no\":\"DEMO-UNIQUE-001\",\"primary_no\":\"DEMO-DUP-003\",\"source_version\":$UV,\"primary_version\":$FV}" | J

echo; echo "== 8. 研究取数：被归并样本只留一条主记录 =="
curl -s "$B/research/samples" | J
