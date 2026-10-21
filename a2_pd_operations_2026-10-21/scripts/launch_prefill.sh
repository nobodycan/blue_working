#!/usr/bin/env bash
set -euo pipefail
source /bench/scripts/common.sh
mkdir -p /bench/logs /bench/results /bench/profiles
extra=()
if [[ -n "$KV_BYTES" ]]; then extra+=(--kv-cache-memory-bytes "$KV_BYTES"); fi
if [[ "$PROFILE" == 1 ]]; then
  extra+=(--profiler-config '{"profiler":"torch","torch_profiler_dir":"/bench/profiles/prefill","torch_profiler_with_stack":false}')
fi
exec vllm serve "$MODEL" \
  --host 0.0.0.0 --served-model-name "$SERVED_MODEL" \
  --tensor-parallel-size 8 --data-parallel-size 1 --enable-expert-parallel \
  --max-model-len "$MAX_MODEL_LEN" --max-num-seqs "$MAX_SEQS" \
  --gpu-memory-utilization "$MEM_UTIL" --seed 1024 \
  --quantization ascend --tokenizer-mode deepseek_v4 --trust-remote-code \
  --block-size 128 --no-enable-prefix-caching --enable-chunked-prefill \
  --no-disable-hybrid-kv-cache-manager --async-scheduling \
  --safetensors-load-strategy prefetch \
  --speculative-config '{"method":"mtp","num_speculative_tokens":1,"enforce_eager":true}' \
  --port 8100 --max-num-batched-tokens "$P_TOKEN_BUDGET" --enforce-eager \
  --additional-config '{"enable_cpu_binding":true,"enable_dsa_cp":false}' \
  --kv-transfer-config '{"kv_connector":"MooncakeHybridConnector","kv_role":"kv_producer","kv_port":"30000","engine_id":"0","kv_connector_extra_config":{"prefill":{"dp_size":1,"tp_size":8},"decode":{"dp_size":1,"tp_size":8}}}' \
  "${extra[@]}"
