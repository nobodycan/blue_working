#!/usr/bin/env bash
set -euo pipefail
command -v npu-smi >/dev/null
while true; do
  date -u +'%Y-%m-%dT%H:%M:%S.%NZ'
  echo 'PROC_STAT'
  cat /proc/stat
  echo 'PROC_NET_DEV'
  cat /proc/net/dev
  echo 'NPU_SMI'
  npu-smi info
  sleep 2
done
