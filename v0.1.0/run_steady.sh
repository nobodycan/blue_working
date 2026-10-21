#!/usr/bin/env bash
set -euo pipefail
source /tmp/pdtest/test.env
source "$RUN/selection.env"
phase=${1:?填写阶段名}
base="$RUN/$phase/steady"
test ! -e "$base" || { echo "目录已存在：$base"; exit 1; }
mkdir -p "$base"
for r in 1 2 3; do
  python "$WORK/bench_client.py" \
    --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
    --prompts "$RUN/prompt-128k.json" --mode steady \
    --concurrency "$BS" --output-length 1024 \
    --burnin 300 --duration "${DURATION:-1800}" --min-samples 30 --timeout 7200 \
    --out "$base/r${r}.json"
done
python "$WORK/check_runs.py" --directory "$base" \
  --runs 3 --concurrency "$BS" --mode steady
