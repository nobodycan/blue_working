#!/usr/bin/env bash
source /bench/env.sh
export OMP_PROC_BIND=false OMP_NUM_THREADS=10
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export HCCL_BUFFSIZE=1024 TASK_QUEUE_ENABLE=1 HCCL_OP_EXPANSION_MODE=AIV
export VLLM_RPC_TIMEOUT=3600000 VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=30000
export HCCL_EXEC_TIMEOUT=204 HCCL_CONNECT_TIMEOUT=1200
export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7
export PYTHONPATH="/bench/scripts${PYTHONPATH:+:$PYTHONPATH}"
: "${LOCAL_IP:?set env.local.sh}" "${NIC:?set env.local.sh}"
export HCCL_IF_IP="$LOCAL_IP" GLOO_SOCKET_IFNAME="$NIC"
export HCCL_SOCKET_IFNAME="$NIC" TP_SOCKET_IFNAME="$NIC"
if [[ -f /usr/lib/aarch64-linux-gnu/libjemalloc.so.2 ]]; then
  export LD_PRELOAD="/usr/lib/aarch64-linux-gnu/libjemalloc.so.2${LD_PRELOAD:+:$LD_PRELOAD}"
fi
export NO_PROXY="${NO_PROXY:-},127.0.0.1,localhost,$P_IP,$D_IP"
export no_proxy="$NO_PROXY"
