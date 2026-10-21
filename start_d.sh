#!/usr/bin/env bash
# Run inside the Decode container: vLLM-Ascend v0.23.0, 8 NPUs.
# Example: LOCAL_IP=10.10.0.12 NIC=eth0 MODEL=/models/DeepSeek-V4-Flash-w8a8-mtp bash start_d.sh
set -euo pipefail

MODEL="${MODEL:-/models/DeepSeek-V4-Flash-w8a8-mtp}"
NIC="${NIC:-eth0}"
: "${LOCAL_IP:?Set LOCAL_IP to the communication IP of this Decode machine}"

command -v vllm >/dev/null 2>&1 || { echo "ERROR: vllm is not on PATH" >&2; exit 1; }
[[ -f "$MODEL/config.json" ]] || { echo "ERROR: model config not found: $MODEL/config.json" >&2; exit 1; }
[[ -d "/sys/class/net/$NIC" ]] || { echo "ERROR: network interface not found: $NIC" >&2; exit 1; }

export ASCEND_RT_VISIBLE_DEVICES="${ASCEND_RT_VISIBLE_DEVICES:-0,1,2,3,4,5,6,7}"
export OMP_PROC_BIND=false
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-10}"
export PYTORCH_NPU_ALLOC_CONF="${PYTORCH_NPU_ALLOC_CONF:-expandable_segments:True}"
export HCCL_BUFFSIZE="${HCCL_BUFFSIZE:-1024}"
export TASK_QUEUE_ENABLE=1
export HCCL_OP_EXPANSION_MODE=AIV
export VLLM_RPC_TIMEOUT=3600000
export VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=30000
export HCCL_EXEC_TIMEOUT=204
export HCCL_CONNECT_TIMEOUT=1200
export HCCL_IF_IP="$LOCAL_IP"
export GLOO_SOCKET_IFNAME="$NIC"
export HCCL_SOCKET_IFNAME="$NIC"
export TP_SOCKET_IFNAME="$NIC"
export NO_PROXY="${NO_PROXY:-},127.0.0.1,localhost,$LOCAL_IP${P_IP:+,$P_IP}${D_IP:+,$D_IP}"
export no_proxy="$NO_PROXY"

if [[ -f /usr/lib/aarch64-linux-gnu/libjemalloc.so.2 ]]; then
  export LD_PRELOAD="/usr/lib/aarch64-linux-gnu/libjemalloc.so.2${LD_PRELOAD:+:$LD_PRELOAD}"
fi

extra=()
if [[ -n "${KV_BYTES:-}" ]]; then
  extra+=(--kv-cache-memory-bytes "$KV_BYTES")
fi

exec vllm serve "$MODEL" \
  --host 0.0.0.0 \
  --port "${D_PORT:-8200}" \
  --served-model-name "${SERVED_MODEL:-dsv4}" \
  --tensor-parallel-size 8 \
  --data-parallel-size 1 \
  --enable-expert-parallel \
  --max-model-len "${MAX_MODEL_LEN:-133120}" \
  --max-num-seqs "${MAX_SEQS:-32}" \
  --max-num-batched-tokens "${D_TOKEN_BUDGET:-512}" \
  --gpu-memory-utilization "${MEM_UTIL:-0.90}" \
  --seed 1024 \
  --quantization ascend \
  --tokenizer-mode deepseek_v4 \
  --trust-remote-code \
  --block-size 128 \
  --no-enable-prefix-caching \
  --enable-chunked-prefill \
  --no-disable-hybrid-kv-cache-manager \
  --async-scheduling \
  --safetensors-load-strategy prefetch \
  --speculative-config '{"method":"mtp","num_speculative_tokens":1,"enforce_eager":true}' \
  --compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY"}' \
  --additional-config '{"ascend_compilation_config":{"enable_npugraph_ex":true,"enable_static_kernel":false},"enable_cpu_binding":true,"enable_dsa_cp":false,"multistream_overlap_shared_expert":true,"recompute_scheduler_enable":true}' \
  --kv-transfer-config '{"kv_connector":"MooncakeHybridConnector","kv_role":"kv_consumer","kv_port":"30100","engine_id":"1","kv_connector_extra_config":{"prefill":{"dp_size":1,"tp_size":8},"decode":{"dp_size":1,"tp_size":8}}}' \
  "${extra[@]}"
