#!/usr/bin/env bash
# Copy to env.sh. Replace these example addresses before running PD.
export BENCH_ROOT=/data/ascend-v4-bench
export MODEL_DIR=/data/models/DeepSeek-V4-Flash-w8a8-mtp
export IMAGE=quay.io/ascend/vllm-ascend:v0.23.0
export CONTAINER=ascend-v4-bench
export MODEL=/models/DeepSeek-V4-Flash-w8a8-mtp
export SERVED_MODEL=dsv4
export NIC=REPLACE_WITH_CONTROL_NIC
export LOCAL_IP=REPLACE_WITH_THIS_HOST_CONTROL_IP
P_IPS=(10.10.0.11 10.10.0.12 10.10.0.13 10.10.0.14)
D_IPS=(10.10.0.21 10.10.0.22 10.10.0.23 10.10.0.24)
export D_MASTER=${D_IPS[0]}
export PROXY_HOST=${P_IPS[0]}
export PROXY_PORT=1999
export MAX_MODEL_LEN=135000
export SINGLE_MAX_SEQS=32 P_MAX_SEQS=16 D_MAX_SEQS=30
export P_TOKEN_BUDGET=4096 D_TOKEN_BUDGET=60
export MEM_UTIL=0.90 PROFILE=0
# Optional per-NPU KV budgets in BYTES, NOT whole-card HBM.
export SINGLE_KV_BYTES= P_KV_BYTES= D_KV_BYTES=
export AUDIT_CAPACITY_DIR=/bench/results/capacity
# Set only during the optional transfer diagnostic run.
export AUDIT_TRANSFER_DIR=
