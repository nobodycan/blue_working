#!/usr/bin/env bash
set -euo pipefail
source /bench/scripts/common.sh
mkdir -p /bench/logs /bench/results /bench/profiles
extra=()
if [[ -n "$KV_BYTES" ]]; then extra+=(--kv-cache-memory-bytes "$KV_BYTES"); fi
if [[ "$PROFILE" == 1 ]]; then
  extra+=(--profiler-config '{"profiler":"torch","torch_profiler_dir":"/bench/profiles/decode","torch_profiler_with_stack":false}')
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
  --port 8200 --max-num-batched-tokens "$D_TOKEN_BUDGET" \
  --compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY"}' \
  --additional-config '{"ascend_compilation_config":{"enable_npugraph_ex":true,"enable_static_kernel":false},"enable_cpu_binding":true,"enable_dsa_cp":false,"multistream_overlap_shared_expert":true,"recompute_scheduler_enable":true}' \
  --kv-transfer-config '{"kv_connector":"MooncakeHybridConnector","kv_role":"kv_consumer","kv_port":"30100","engine_id":"1","kv_connector_extra_config":{"prefill":{"dp_size":1,"tp_size":8},"decode":{"dp_size":1,"tp_size":8}}}' \
  "${extra[@]}"
