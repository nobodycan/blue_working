#!/usr/bin/env bash
set -euo pipefail
out=${1:?output_path}
mkdir -p "$(dirname "$out")"
while true; do
  date -u +'%Y-%m-%dT%H:%M:%S.%NZ'
  npu-smi info
  sleep 2
done >> "$out" 2>&1
