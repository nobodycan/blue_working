#!/usr/bin/env bash
set -euo pipefail
source /tmp/pdtest/test.env
source "$RUN/selection.env"
phase=${1:?填写阶段目录名，例如 diagnostic 或 formal}
base="$RUN/$phase"
test ! -e "$base" || { echo "目录已存在：$base；请使用新的阶段名"; exit 1; }
mkdir -p "$base/single" "$base/batch"
for r in 1 2 3; do
  python "$WORK/bench_client.py" \
    --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
    --prompts "$RUN/prompt-128k.json" --mode wave \
    --concurrency 1 --requests 1 --output-length 1024 \
    --min-samples 1 --timeout 7200 --out "$base/warmup-r${r}.json"
done
for r in $(seq 1 30); do
  python "$WORK/bench_client.py" \
    --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
    --prompts "$RUN/prompt-128k.json" --mode wave \
    --concurrency 1 --requests 1 --output-length 1024 \
    --min-samples 1 --timeout 7200 --out "$base/single/r${r}.json"
done
for r in $(seq 1 10); do
  python "$WORK/bench_client.py" \
    --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
    --prompts "$RUN/prompt-128k.json" --mode wave \
    --concurrency "$BS" --requests "$BS" --output-length 1024 \
    --min-samples "$BS" --timeout 7200 --out "$base/batch/r${r}.json"
done
python "$WORK/check_runs.py" --directory "$base/single" --runs 30 --concurrency 1 --mode wave
python "$WORK/check_runs.py" --directory "$base/batch" --runs 10 --concurrency "$BS" --mode wave
python "$WORK/compare_ttft.py" \
  --single "$base"/single/r*.json --batch "$base"/batch/r*.json --bs "$BS" \
  > "$base/ttft-comparison.json"
cat "$base/ttft-comparison.json"
