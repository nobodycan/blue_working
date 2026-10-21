#!/usr/bin/env bash
export BENCH_ROOT=/data/v4flash-2node
export MODEL_DIR=/data/models/DeepSeek-V4-Flash-w8a8-mtp
export MODEL=/models/DeepSeek-V4-Flash-w8a8-mtp
export IMAGE=quay.io/ascend/vllm-ascend:v0.23.0
export CONTAINER=v4flash-2node
export SERVED_MODEL=dsv4
export P_IP=10.10.0.11 D_IP=10.10.0.12
export PROXY_PORT=1999
export MAX_MODEL_LEN=133120 MAX_SEQS=32 MEM_UTIL=0.90
export P_TOKEN_BUDGET=8192 D_TOKEN_BUDGET=512
# Per-NPU KV pool budget in BYTES; empty means auto sizing.
export KV_BYTES=${KV_BYTES:-}
export PROFILE=${PROFILE:-0}
export AUDIT_CAPACITY_DIR=${AUDIT_CAPACITY_DIR:-/bench/results/capacity}
export AUDIT_TRANSFER_DIR=${AUDIT_TRANSFER_DIR:-}
export AUDIT_STEP_DIR=${AUDIT_STEP_DIR:-}
# Host-specific NIC and IP are set in env.local.sh, never guessed.
if [[ -f /bench/env.local.sh ]]; then source /bench/env.local.sh; fi
