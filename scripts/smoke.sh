#!/usr/bin/env bash
# End-to-end smoke test of the Compose stack against the fake PACS.
# Runs a gpu flow with RADFETCH_DEBUG_FAKE_GPU, so it works on hosts without a GPU.
# Usage: scripts/smoke.sh        (set SMOKE_NO_BUILD=1 to reuse an existing radfetch:local image)
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -e .env ] || [ -e data ]; then
  echo "refusing to run: .env or ./data exists, and this test would overwrite them" >&2
  exit 2
fi

compose() { docker compose --profile dev "$@"; }
cleanup() {
  status=$?
  if [ $status -ne 0 ]; then compose logs --tail 60 gateway executor sender api || true; fi
  compose down -v >/dev/null 2>&1 || true
  rm -f .env flows/smoke.yaml
  # data/ is written by root inside the containers
  docker run --rm -v "$PWD:/w" busybox:1.36 rm -rf /w/data >/dev/null 2>&1 || rm -rf data || true
  exit $status
}
trap cleanup EXIT

sed -e "s#^RADFETCH_HOST_DATA_DIR=.*#RADFETCH_HOST_DATA_DIR=$PWD/data#" \
    -e "s#^RADFETCH_BATCH_WINDOWS=.*#RADFETCH_BATCH_WINDOWS=#" .env.example > .env
echo "RADFETCH_DEBUG_FAKE_GPU=true" >> .env

cat > flows/smoke.yaml <<'FLOW'
name: smoke
select:
  - modality: CT
    min_instances: 5
image: busybox:1.36
command: ["sh", "-c", "test \"$RADFETCH_FAKE_GPU\" = 1 && find /input -type f | wc -l > /output/count.txt"]
gpu: true
destinations:
  - type: directory
    path: /data/results
FLOW

if [ "${SMOKE_NO_BUILD:-}" = 1 ]; then compose up -d --no-build; else compose up -d --build; fi

api() { curl -sf --noproxy '*' -H 'content-type: application/json' "$@"; }
timeout 90 sh -c 'until curl -sf --noproxy "*" http://localhost:8000/healthz >/dev/null; do sleep 1; done'

api -X POST http://localhost:8000/jobs -d '{"accession_number": "ACC0001", "flow": "smoke"}' >/dev/null
api -X POST http://localhost:8000/batches \
  -d '{"name": "smoke", "flow": "smoke", "accession_numbers": ["ACC0002", "ACC0003", "ACC0004"]}' >/dev/null

for _ in $(seq 60); do
  jobs=$(api http://localhost:8000/healthz)
  case "$jobs" in *'"done":4'*) break ;; *'"failed"'*) echo "a job failed: $jobs" >&2; exit 1 ;; esac
  sleep 3
done
echo "$jobs"
case "$jobs" in *'"done":4'*) ;; *) echo "timed out waiting for 4 done jobs" >&2; exit 1 ;; esac

results=$(docker run --rm -v "$PWD/data:/data:ro" busybox:1.36 sh -c 'cat /data/results/*/count.txt' | sort | uniq -c | xargs)
[ "$results" = "4 12" ] || { echo "unexpected results: $results" >&2; exit 1; }
echo "smoke test passed: 4 jobs done, each container saw 12 instances with the fake GPU"
