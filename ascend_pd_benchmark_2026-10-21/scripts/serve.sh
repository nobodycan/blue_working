#!/usr/bin/env bash
set -euo pipefail
source /bench/env.sh
role=${1:?expected_single_prefill_or_decode}
export OMP_PROC_BIND=false OMP_NUM_THREADS=10
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export HCCL_BUFFSIZE=1024 TASK_QUEUE_ENABLE=1 HCCL_OP_EXPANSION_MODE=AIV
export VLLM_RPC_TIMEOUT=3600000 VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=30000
export HCCL_EXEC_TIMEOUT=204 HCCL_CONNECT_TIMEOUT=1200
export PYTHONPATH="/bench/scripts${PYTHONPATH:+:$PYTHONPATH}"
if [[ -f /usr/lib/aarch64-linux-gnu/libjemalloc.so.2 ]]; then
  export LD_PRELOAD="/usr/lib/aarch64-linux-gnu/libjemalloc.so.2${LD_PRELOAD:+:$LD_PRELOAD}"
fi
args=( "$MODEL" --host 0.0.0.0 --served-model-name "$SERVED_MODEL"
  --max-model-len "$MAX_MODEL_LEN" --gpu-memory-utilization "$MEM_UTIL"
  --enable-expert-parallel --quantization ascend --seed 1024
  --tokenizer-mode deepseek_v4 --tool-call-parser deepseek_v4
  --enable-auto-tool-choice --reasoning-parser deepseek_v4 --trust-remote-code
  --block-size 128 --no-enable-prefix-caching --async-scheduling
  --safetensors-load-strategy prefetch
  --speculative-config '{"num_speculative_tokens":1,"method":"mtp","enforce_eager":true}'
)
case "$role" in
 single)
  args+=(--port 8000 --data-parallel-size 1 --tensor-parallel-size 8
    --max-num-seqs "$SINGLE_MAX_SEQS" --max-num-batched-tokens 8192
    --compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY"}'
    --additional-config '{"ascend_compilation_config":{"enable_npugraph_ex":true,"enable_static_kernel":false},"enable_cpu_binding":true,"enable_dsa_cp":true,"multistream_overlap_shared_expert":true}')
  kv_bytes=$SINGLE_KV_BYTES
  export VLLM_ASCEND_ENABLE_FLASHCOMM1=1
  ;;
 prefill|decode)
  : "${NODE_ID:?set_NODE_ID_0_to_3}"
  : "${DP_RANK:?launch_dp_must_set_DP_RANK}"
  : "${DP_MASTER:?launch_dp_must_set_DP_MASTER}"
  : "${API_PORT:?launch_dp_must_set_API_PORT}"
  [[ "$LOCAL_IP" != REPLACE* && "$NIC" != REPLACE* ]] || { echo 'Set LOCAL_IP and NIC'; exit 1; }
  export HCCL_IF_IP="$LOCAL_IP" GLOO_SOCKET_IFNAME="$NIC"
  export TP_SOCKET_IFNAME="$NIC" HCCL_SOCKET_IFNAME="$NIC"
  args+=(--port "$API_PORT" --tensor-parallel-size 1
    --data-parallel-rank "$DP_RANK" --data-parallel-address "$DP_MASTER"
    --data-parallel-rpc-port 12321 --no-disable-hybrid-kv-cache-manager)
  if [[ "$role" == prefill ]]; then
    args+=(--data-parallel-size 8 --max-num-batched-tokens "$P_TOKEN_BUDGET"
      --max-num-seqs "$P_MAX_SEQS" --enforce-eager
      --additional-config '{"enable_cpu_binding":true,"enable_shared_expert_dp":true}')
    kv_role=kv_producer; engine_id=$NODE_ID; kv_port=$((30000 + 100 * NODE_ID))
    kv_bytes=$P_KV_BYTES
  else
    args+=(--data-parallel-size 32 --max-num-batched-tokens "$D_TOKEN_BUDGET"
      --max-num-seqs "$D_MAX_SEQS"
      --compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY"}'
      --additional-config '{"ascend_compilation_config":{"enable_npugraph_ex":true,"enable_static_kernel":false},"enable_cpu_binding":true,"multistream_overlap_shared_expert":true,"recompute_scheduler_enable":true}')
    kv_role=kv_consumer; engine_id=4; kv_port=30400
    kv_bytes=$D_KV_BYTES
  fi
  transfer=$(python - "$kv_role" "$engine_id" "$kv_port" <<'PY'
import json, sys
print(json.dumps({'kv_connector':'MooncakeHybridConnector', 'kv_role':sys.argv[1],
 'engine_id':sys.argv[2], 'kv_port':sys.argv[3], 'kv_connector_extra_config':{
 'prefill':{'dp_size':8,'tp_size':1}, 'decode':{'dp_size':32,'tp_size':1}}}))
PY
)
  args+=(--kv-transfer-config "$transfer")
  ;;
 *) echo 'Usage: serve.sh single|prefill|decode'; exit 2 ;;
esac
if [[ -n "$kv_bytes" ]]; then args+=(--kv-cache-memory-bytes "$kv_bytes"); fi
if [[ "$PROFILE" == 1 ]]; then
  prof=$(python - "$role" "${NODE_ID:-single}" "${DP_RANK:-0}" <<'PY'
import json,sys
print(json.dumps({'profiler':'torch','torch_profiler_dir':'/bench/profiles/'+'-'.join(sys.argv[1:]),'torch_profiler_with_stack':False}))
PY
)
  args+=(--profiler-config "$prof")
fi
printf '%q ' vllm serve "${args[@]}"; printf '\n'
exec vllm serve "${args[@]}"
