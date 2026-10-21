#!/usr/bin/env bash
set -euo pipefail
source /tmp/pdtest/test.env
mkdir -p "$RUN/waves"
for C in 1 2 4 8 16 24 32; do
  for r in 1 2 3; do
    prefix="$RUN/waves/C${C}-r${r}"
    python "$WORK/bench_client.py" \
      --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
      --prompts "$RUN/prompt-128k.json" --mode wave \
      --concurrency "$C" --requests "$C" --output-length 1024 \
      --return-token-ids --min-samples "$C" --timeout 7200 \
      --out "${prefix}.json"
    if python "$WORK/pd_plateau.py" \
        --summary "${prefix}.json" --out "${prefix}.window.json"; then
      echo "候选窗口成立 C=$C repeat=$r；仍需检查 D worker 和 transfer"
    else
      echo "候选窗口未成立 C=$C repeat=$r；不能据此确认 BS"
    fi
  done
done
