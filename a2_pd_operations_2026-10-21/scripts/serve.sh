#!/usr/bin/env bash
set -euo pipefail
source /bench/scripts/common.sh
role=${1:?prefill_or_decode}
args=( "$MODEL" --host 0.0.0.0 --served-model-name "$SERVED_MODEL"
 --data-parallel-size 1 --tensor-parallel-size 8 --enable-expert-parallel
 --max-model-len "$MAX_MODEL_LEN" --max-num-seqs "$MAX_SEQS"
 --gpu-memory-utilization "$MEM_UTIL" --seed 1024 --quantization ascend
 --tokenizer-mode deepseek_v4 --trust-remote-code --block-size 128
 --no-enable-prefix-caching --enable-chunked-prefill --no-disable-hybrid-kv-cache-manager
 --async-scheduling --safetensors-load-strategy prefetch
 --speculative-config '{"method":"mtp","num_speculative_tokens":1,"enforce_eager":true}'
)
if [[ "$role" == prefill ]]; then
  args+=(--port 8100 --max-num-batched-tokens "$P_TOKEN_BUDGET" --enforce-eager
    --additional-config '{"enable_cpu_binding":true,"enable_dsa_cp":false}')
  kv_role=kv_producer; kv_port=30000; engine_id=0
elif [[ "$role" == decode ]]; then
  args+=(--port 8200 --max-num-batched-tokens "$D_TOKEN_BUDGET"
    --compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY"}'
    --additional-config '{"ascend_compilation_config":{"enable_npugraph_ex":true,"enable_static_kernel":false},"enable_cpu_binding":true,"enable_dsa_cp":false,"multistream_overlap_shared_expert":true,"recompute_scheduler_enable":true}')
  kv_role=kv_consumer; kv_port=30100; engine_id=1
else
  echo 'Usage: serve.sh prefill|decode'; exit 2
fi
transfer=$(python - "$kv_role" "$kv_port" "$engine_id" <<'PYKV'
import json,sys
print(json.dumps(dict(kv_connector='MooncakeHybridConnector',kv_role=sys.argv[1],
 kv_port=sys.argv[2],engine_id=sys.argv[3],kv_connector_extra_config={
 'prefill':{'dp_size':1,'tp_size':8},'decode':{'dp_size':1,'tp_size':8}})))
PYKV
)
args+=(--kv-transfer-config "$transfer")
if [[ -n "$KV_BYTES" ]]; then args+=(--kv-cache-memory-bytes "$KV_BYTES"); fi
if [[ "$PROFILE" == 1 ]]; then
  args+=(--profiler-config "{\"profiler\":\"torch\",\"torch_profiler_dir\":\"/bench/profiles/$role\",\"torch_profiler_with_stack\":false}")
fi
mkdir -p /bench/results /bench/logs /bench/profiles
printf '%q ' vllm serve "${args[@]}" > "/bench/results/$role-command.txt"
printf '\n' >> "/bench/results/$role-command.txt"
exec vllm serve "${args[@]}"
