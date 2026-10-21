# 两台昇腾 A2：P 端代理 + P 端压测，128K 输入 / 1K 输出完整操作手册

本版从“两个容器已经启动，P/D 模型服务已经运行”开始。所有需要的 Python 和 Shell 文件均在本文用完整的 `cat > 文件名 <<'EOF'` 命令给出。目标机器不需要 Git，不需要从代码仓库下载文件。依次复制本文代码块即可。

适用配置：前文使用的 vLLM-Ascend v0.23.0、原版 `DeepSeek-V4-Flash-w8a8-mtp` 权重、每台 8 张 NPU、TP=8、DP=1、MTP=1。若镜像版本或权重换成其他版本，先核对兼容性；补丁遇到源码不匹配时停止执行。

本文件包含长脚本，代理脚本保留上游 Apache-2.0 版权头。本文是执行步骤，未在你的 A2 实机上运行。

## 0. 本版调整清单

| 项目 | 本版执行位置 / 调整 |
|---|---|
| P 模型服务 | P 机器容器，8100 |
| D 模型服务 | D 机器容器，8200 |
| PD 代理 | P 机器容器，1999 |
| 压测客户端、token 文件、结果分析 | P 机器容器 |
| 压测请求地址 | P 容器内使用 `http://127.0.0.1:1999`；其他机器使用 `http://P_IP:1999` |
| D 容器内的额外工作 | 模型服务、诊断采样、结果文件；不运行代理和压测客户端 |
| 跨机器窗口核对 | 先确认两台宿主机时钟同步；使用相同 RUN_ID |
| D 采样结果传回 P | `docker cp` 导出到 D 宿主机，再从 P 宿主机用 `scp` 拉取，最后 `docker cp` 送入 P 容器 |
| 诊断与正式测试 | 诊断时启用 D 采样；正式测试关闭采样并重新预热。两阶段分别保存 |
| Batch Size | 从 D 的 8 个 worker 采样确认。客户端并发 C 单独记录 |

不要同时保留原先 D 机器上的旧代理和旧压测任务。若之前启动过，通过原终端停止，或核对其保存的 PID 后停止该任务。不要执行 `pkill python`、`killall python` 或批量终止模型进程。

## 1. 两台宿主机：确认容器、网络和时钟

分别在 P、D 宿主机执行：

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
ip -br addr
timedatectl show -p NTPSynchronized --value
date -u +'%Y-%m-%dT%H:%M:%S.%NZ'
if command -v chronyc >/dev/null 2>&1; then
  chronyc tracking
fi
```

记下两个容器名、P/D 通信 IP、对应网卡名。时钟未同步时，先让管理员完成同步，再做跨机器纯 D 窗口核对。检查容器网络模式，下面的 CONTAINER_NAME 换成当前机器真实容器名：

```bash
docker inspect -f '{{.HostConfig.NetworkMode}}' CONTAINER_NAME
```

本文后续按容器使用 host 网络编写。若不是 host 网络，先完成服务端口和 KV 通信地址的映射核对，再继续。

## 2. 两个容器：分别创建测试配置

在 P 容器、D 容器各执行一次。修改下面 IP、网卡、模型路径。两边 `RUN_ID` 必须相同。模型路径均指容器内路径，P_MODEL 和 D_MODEL 可以不同。

```bash
mkdir -p /tmp/pdtest
cat > /tmp/pdtest/test.env <<'PD_ENV_EOF'
export WORK=/tmp/pdtest
export RUN_ID=pd128k_p_proxy_001
export RUN="$WORK/runs/$RUN_ID"
export P_IP=10.10.0.11
export D_IP=10.10.0.12
export P_NIC=eth0
export D_NIC=eth0
export P_MODEL=/models/DeepSeek-V4-Flash-w8a8-mtp
export D_MODEL=/models/DeepSeek-V4-Flash-w8a8-mtp
export SERVED_MODEL=dsv4
export MAX_MODEL_LEN=133120
export MAX_SEQS=32
export MEM_UTIL=0.90
export P_TOKEN_BUDGET=8192
export D_TOKEN_BUDGET=512
export NO_PROXY="${NO_PROXY:-},127.0.0.1,localhost,$P_IP,$D_IP"
export no_proxy="$NO_PROXY"
PD_ENV_EOF

source /tmp/pdtest/test.env
test ! -e "$RUN" || { echo 'RUN_ID 已存在，修改 test.env 中 RUN_ID 后重新执行本步骤'; exit 1; }
mkdir -p "$RUN"
cp /tmp/pdtest/test.env "$RUN/test.env"
python - <<'PY'
from importlib.metadata import version
for package in ('vllm', 'vllm-ascend', 'torch', 'torch-npu'):
    print(package, version(package))
PY
```

后续每次新开容器终端，先执行下面这条恢复变量，不要重新执行 RUN_ID 存在性检查：

```bash
source /tmp/pdtest/test.env
```

## 3. 两个容器：创建完整的 P/D 启动 Shell

在 P、D 容器各执行以下两个代码块。两个 Shell 各自独立，不依赖其他公共 Shell 文件。P 模型已经运行时，不要重复启动 P；D 的 Shell 用于后面的诊断重启和正式重启。

创建 `start_p.sh`：

```bash
cat > "$WORK/start_p.sh" <<'PD_FILE_EOF'
#!/usr/bin/env bash
# Run inside the Prefill container: vLLM-Ascend v0.23.0, 8 NPUs.
# Example: LOCAL_IP=10.10.0.11 NIC=eth0 MODEL=/models/DeepSeek-V4-Flash-w8a8-mtp bash start_p.sh
set -euo pipefail

MODEL="${MODEL:-/models/DeepSeek-V4-Flash-w8a8-mtp}"
NIC="${NIC:-eth0}"
: "${LOCAL_IP:?Set LOCAL_IP to the communication IP of this Prefill machine}"

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

if [[ "${PROFILE:-0}" == 1 ]]; then
  profile_dir="${PROFILE_DIR:-/tmp/pdtest/profiles/prefill}"
  mkdir -p "$profile_dir"
  profile_json=$(python -c 'import json,sys; print(json.dumps(dict(profiler="torch", torch_profiler_dir=sys.argv[1], torch_profiler_with_stack=False)))' "$profile_dir")
  extra+=(--profiler-config "$profile_json")
fi

exec vllm serve "$MODEL" \
  --host 0.0.0.0 \
  --port "${P_PORT:-8100}" \
  --served-model-name "${SERVED_MODEL:-dsv4}" \
  --tensor-parallel-size 8 \
  --data-parallel-size 1 \
  --enable-expert-parallel \
  --max-model-len "${MAX_MODEL_LEN:-133120}" \
  --max-num-seqs "${MAX_SEQS:-32}" \
  --max-num-batched-tokens "${P_TOKEN_BUDGET:-8192}" \
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
  --enforce-eager \
  --speculative-config '{"method":"mtp","num_speculative_tokens":1,"enforce_eager":true}' \
  --additional-config '{"enable_cpu_binding":true,"enable_dsa_cp":false}' \
  --kv-transfer-config '{"kv_connector":"MooncakeHybridConnector","kv_role":"kv_producer","kv_port":"30000","engine_id":"0","kv_connector_extra_config":{"prefill":{"dp_size":1,"tp_size":8},"decode":{"dp_size":1,"tp_size":8}}}' \
  "${extra[@]}"
PD_FILE_EOF
```

创建 `start_d.sh`：

```bash
cat > "$WORK/start_d.sh" <<'PD_FILE_EOF'
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

if [[ "${PROFILE:-0}" == 1 ]]; then
  profile_dir="${PROFILE_DIR:-/tmp/pdtest/profiles/decode}"
  mkdir -p "$profile_dir"
  profile_json=$(python -c 'import json,sys; print(json.dumps(dict(profiler="torch", torch_profiler_dir=sys.argv[1], torch_profiler_with_stack=False)))' "$profile_dir")
  extra+=(--profiler-config "$profile_json")
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
PD_FILE_EOF
```


如果 P 服务尚未运行，可在 P 容器执行：

```bash
source /tmp/pdtest/test.env
LOCAL_IP="$P_IP" NIC="$P_NIC" MODEL="$P_MODEL" bash "$WORK/start_p.sh"
```

## 4. P 容器：创建全部代理和测试脚本

以下所有代码块只在 P 容器执行。逐个完整复制，直到各自的 `PD_FILE_EOF` 结束行；不要仅复制 Python 内容的一部分。

创建 `proxy.py`：

```bash
cat > "$WORK/proxy.py" <<'PD_FILE_EOF'
# Adapted from https://github.com/vllm-project/vllm/tests/v1/kv_connector/nixl_integration/toy_proxy_server.py

# SPDX-License-Identifier: Apache-2.0
#
# Tutorial: Using the Load Balance Proxy Server Example
#
# This proxy server is designed to distribute requests between multiple
# "prefiller" and "decoder" backend servers for large language model inference.
# It is useful for scaling out inference workloads and balancing load across
# multiple backend instances.
#
# Features:
# - Load balances requests to multiple prefiller and decoder servers.
# - Supports OpenAI-compatible /v1/completions and /v1/chat/completions endpoints.
# - Streams responses from backend servers to clients.
#
# Prerequisites:
# - Python 3.10+
# - Install dependencies:
#     pip install fastapi<0.124.0 httpx uvicorn vllm
#
# Step 1: Start Your Backend Servers
# ----------------------------------
# You need to have at least one prefiller and one decoder backend running.
# These can be mock servers or actual vLLM servers.
#
# For testing, you can use the provided mock server:
#
#   vllm serve --host 0.0.0.0 --port 8100 ... # Prefiller 1
#   vllm serve --host 0.0.0.0 --port 8101 ... # Prefiller 2
#   vllm serve --host 0.0.0.0 --port 8200 ... # Decoder 1
#   vllm serve --host 0.0.0.0 --port 8201 ... # Decoder 2
#
# Step 2: Start the Proxy Server
# ------------------------------
# Run the proxy server, specifying the host/port for each prefiller and decoder:
#
#   python load_balance_proxy_server_example.py \
#     --host 0.0.0.0 --port 9000 --workers 2 \
#     --prefiller-hosts 127.0.0.1 127.0.0.1 \
#     --prefiller-ports 8100 8101 \
#     --decoder-hosts 127.0.0.1 127.0.0.1 \
#     --decoder-ports 8200 8201
#
# This will start the proxy on port 9000, load balancing between two prefiller
# and two decoder servers.
#
# Step 3: Send a Request to the Proxy
# -----------------------------------
# You can now send OpenAI-compatible requests to the proxy. For example:
#
#   curl -X POST http://localhost:9000/v1/completions \
#     -H "Content-Type: application/json" \
#     -d '{
#           "model": "your-model",
#           "prompt": "The quick brown fox jumps over the lazy dog",
#           "max_tokens": 16
#         }'
#
# Or for chat completions:
#
#   curl -X POST http://localhost:9000/v1/chat/completions \
#     -H "Content-Type: application/json" \
#     -d '{
#           "model": "your-model",
#           "messages": [{"role": "user", "content": "Hello!"}],
#           "max_tokens": 16
#         }'
#
# Step 4: Health Check
# --------------------
# To check if the proxy is running and see how many backend instances are
# connected, use:
#
#   curl http://localhost:9000/healthcheck
#
# This will return a JSON object with the status and the number of prefiller
# and decoder instances.
#
# Step 5: Add or Remove Prefiller or Decoder Instances (Optional)
# ---------------------------------------------------------------
# You can add or remove prefiller or decoder instances after the proxy is started.
# For example, add 2 prefiller instances:
#
#   curl -X POST http://localhost:9000/instances/add \
#     -H "Content-Type: application/json" \
#     -d '{
#           "type": "prefill",
#           "instances": ["127.0.0.1:8102", "127.0.0.1:8103"]
#         }'
#
# or remove 1 decoder instance:
#
#   curl -X POST http://localhost:9000/instances/remove \
#     -H "Content-Type: application/json" \
#     -d '{
#           "type": "decode",
#           "instances": "127.0.0.1:8201"
#         }'
#
# This will return a JSON object with the adding or removing info
# and the current prefiller and decoder instances.
#
# When adding instances, if the instances are not started,
# the proxy will wait and try until the instances to be started
# or exceeding the number of attempts
#
# Notes:
# - You can scale the number of prefiller and decoder servers as needed.
# - The proxy will round-robin requests to balance load.
# - For production, ensure your backend servers are robust and secure.
#
# For more details, see the code and comments in this file.

import argparse
import asyncio
import base64
import functools
import heapq
import ipaddress
import json
import logging
import os
import sys
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from multiprocessing.managers import BaseManager
from pathlib import Path
from typing import Any, cast

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

logger = logging.getLogger(__name__)

try:
    import uvloop  # type: ignore[import-not-found]

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass


class ServerRole(str, Enum):
    PREFILL = "prefill"
    DECODE = "decode"


@dataclass
class InstanceInfo:
    request_id: str
    prefiller_key: str
    prefiller_score: float
    decoder_key: str
    decoder_score: float
    decoder_host: str
    decoder_port: int


TAINT_PRIORITY = 1e15

global_args: argparse.Namespace | None = None
shared_scheduler: "SharedProxyScheduler | None" = None
runtime: "WorkerRuntime | None" = None


@dataclass
class BackendServer:
    host: str
    port: int
    ordinal: int
    active_tokens: float = 0.0
    active_kv_cache: float = 0.0
    heap_seq: int = 0


@dataclass
class RolePools:
    """Per-role scheduling state: live servers, priority heap, and drain-isolated keys."""

    servers: dict[str, BackendServer] = field(default_factory=dict)
    heap: list[tuple[float, int, int, str]] = field(default_factory=list)
    tainted: set[str] = field(default_factory=set)


def setup_logging(log_level: str) -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        force=True,
    )
    logger.setLevel(getattr(logging, log_level.upper()))


def next_req_id() -> str:
    return str(uuid.uuid4())


def calculate_prefill_score(request_length: int) -> float:
    length_score = request_length / 4.0
    return length_score * 0.0345 + 120.0745


def calculate_decode_score(request_length: int) -> float:
    return request_length


def normalize_host(host: str) -> str:
    return host.replace("localhost", "0.0.0.0").replace("127.0.0.1", "0.0.0.0")


def server_key(host: str, port: int) -> str:
    return f"{normalize_host(host)}:{int(port)}"


def build_server_url(host: str, port: int) -> str:
    url = f"http://{host}:{port}"
    try:
        ip = ipaddress.ip_address(host)
        if isinstance(ip, ipaddress.IPv6Address):
            url = f"http://[{host}]:{port}"
    except Exception:
        pass
    return url


def build_base_url(host: str, port: int) -> str:
    return f"{build_server_url(host, port)}/v1"


class SharedProxyScheduler:
    """Centralized mutable scheduling state shared by all uvicorn workers.

    Uses lazy-deletion min-heap: on priority change, push a new entry and
    bump the server's ``heap_seq`` counter; stale entries (whose seq does
    not match) are skipped on pop.
    """

    def __init__(self, prefiller_instances, decoder_instances):
        self._lock = threading.RLock()
        self.request_num = 0
        self.waiting_nodes: dict[str, tuple[str, tuple[str, int], int]] = {}
        self._pools: dict[ServerRole, RolePools] = {
            ServerRole.PREFILL: RolePools(),
            ServerRole.DECODE: RolePools(),
        }
        self._ordinal = 0

        for host, port in prefiller_instances:
            self._add_server_no_lock(ServerRole.PREFILL, host, port)
        for host, port in decoder_instances:
            self._add_server_no_lock(ServerRole.DECODE, host, port)

    def _pool(self, role: ServerRole) -> RolePools:
        return self._pools[role]

    @property
    def prefillers(self) -> dict[str, BackendServer]:
        return self._pool(ServerRole.PREFILL).servers

    @property
    def decoders(self) -> dict[str, BackendServer]:
        return self._pool(ServerRole.DECODE).servers

    def _next_ordinal(self) -> int:
        ordinal = self._ordinal
        self._ordinal += 1
        return ordinal

    def _priority(self, role: ServerRole, entry: BackendServer, key: str) -> float:
        if key in self._pool(role).tainted:
            return TAINT_PRIORITY
        if role is ServerRole.PREFILL:
            return entry.active_tokens + entry.active_kv_cache * 0.3
        return entry.active_tokens

    def _push_heap(self, role: ServerRole, key: str) -> None:
        pool = self._pool(role)
        entry = pool.servers[key]
        entry.heap_seq += 1
        heapq.heappush(pool.heap, (self._priority(role, entry, key), entry.ordinal, entry.heap_seq, key))
        if len(pool.heap) > 2 * len(pool.servers):
            self._reset_heap(role)

    def _pop_valid(self, role: ServerRole) -> str:
        pool = self._pool(role)
        while pool.heap:
            _, _, seq, key = heapq.heappop(pool.heap)
            if key not in pool.servers:
                continue
            entry = pool.servers[key]
            if entry.heap_seq == seq:
                return key
        raise RuntimeError(f"No available {role.value} servers")

    def _reset_heap(self, role: ServerRole, *, bump_seq: bool = False) -> None:
        pool = self._pool(role)
        heap = []
        for key, entry in pool.servers.items():
            if bump_seq:
                entry.heap_seq += 1
            heap.append((self._priority(role, entry, key), entry.ordinal, entry.heap_seq, key))
        heapq.heapify(heap)
        pool.heap = heap

    def _add_server_no_lock(self, role: ServerRole, host: str, port: int) -> bool:
        key = server_key(host, port)
        pool = self._pool(role)
        if key in pool.servers:
            return False
        pool.servers[key] = BackendServer(host, int(port), self._next_ordinal())
        self._push_heap(role, key)
        return True

    def get_snapshot(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            return {
                "prefill_instances": [
                    {"host": e.host, "port": e.port}
                    for _, e in sorted(self.prefillers.items(), key=lambda item: item[1].ordinal)
                ],
                "decode_instances": [
                    {"host": e.host, "port": e.port}
                    for _, e in sorted(self.decoders.items(), key=lambda item: item[1].ordinal)
                ],
            }

    def log_status(self, msg: str) -> None:
        snapshot = self.get_snapshot()
        logger.info(
            "%s prefill=%s decode=%s",
            msg,
            [f"{s['host']}:{s['port']}" for s in snapshot["prefill_instances"]],
            [f"{s['host']}:{s['port']}" for s in snapshot["decode_instances"]],
        )

    def healthcheck(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "ok",
                "prefill_instances": len(self.prefillers),
                "decode_instances": len(self.decoders),
                "request_num": self.request_num,
            }

    def _pick_server(
        self,
        role: ServerRole,
        load: float,
        *,
        active_tokens: bool = False,
        kv_cache: bool = False,
    ) -> dict[str, Any]:
        key = self._pop_valid(role)
        entry = self._pool(role).servers[key]
        if active_tokens:
            entry.active_tokens += load
        if kv_cache:
            entry.active_kv_cache += load
        self._push_heap(role, key)
        return {"key": key, "host": entry.host, "port": entry.port}

    def _release_load(
        self,
        role: ServerRole,
        key: str | None,
        load: float,
        *,
        active_tokens: bool = False,
        kv_cache: bool = False,
    ) -> None:
        if not key or key not in self._pool(role).servers:
            return
        entry = self._pool(role).servers[key]
        if active_tokens:
            entry.active_tokens -= load
        if kv_cache:
            entry.active_kv_cache = max(0.0, entry.active_kv_cache - load)
        self._push_heap(role, key)

    def begin_request(self, load: float) -> dict[str, Any]:
        """Pick a prefiller, reserve KV pressure, and count this as an active request."""
        with self._lock:
            picked = self._pick_server(ServerRole.PREFILL, load, kv_cache=True)
            self.request_num += 1
            return picked

    def reserve_prefill_kv(self, load: float) -> dict[str, Any]:
        """Pick a prefiller for recompute without bumping the active request count."""
        with self._lock:
            return self._pick_server(ServerRole.PREFILL, load, kv_cache=True)

    def pick_decoder(self, load: float) -> dict[str, Any]:
        with self._lock:
            return self._pick_server(ServerRole.DECODE, load, active_tokens=True)

    def release_prefill_kv(self, key: str, load: float) -> None:
        with self._lock:
            self._release_load(ServerRole.PREFILL, key, load, kv_cache=True)

    def release_decoder(self, key: str, load: float) -> None:
        with self._lock:
            self._release_load(ServerRole.DECODE, key, load, active_tokens=True)

    def finish_request(
        self,
        prefiller_key: str | None,
        prefiller_load: float,
        decoder_key: str | None,
        decoder_load: float,
        release_prefill_kv: bool,
    ) -> None:
        with self._lock:
            if release_prefill_kv:
                self._release_load(ServerRole.PREFILL, prefiller_key, prefiller_load, kv_cache=True)
            self._release_load(ServerRole.DECODE, decoder_key, decoder_load, active_tokens=True)
            self.request_num = max(0, self.request_num - 1)

    def get_waiting_nodes(self) -> dict[str, tuple[str, tuple[str, int], int]]:
        with self._lock:
            return dict(self.waiting_nodes)

    def add_instances(self, role: ServerRole, instances: list[tuple[str, int]]) -> list[str]:
        waiting_nodes: list[str] = []
        with self._lock:
            servers = self._pool(role).servers
            for host, port in instances:
                key = server_key(host, port)
                if key in servers or key in self.waiting_nodes:
                    continue
                self.waiting_nodes[key] = (role.value, (host, int(port)), 0)
                waiting_nodes.append(f"{host}:{port}")
        return waiting_nodes

    def mark_waiting_retry(self, key: str, retry_count: int) -> None:
        with self._lock:
            if key not in self.waiting_nodes:
                return
            instance_type, server, _ = self.waiting_nodes[key]
            self.waiting_nodes[key] = (instance_type, server, retry_count)

    def activate_waiting_instance(self, role: ServerRole, host: str, port: int) -> None:
        with self._lock:
            key = server_key(host, port)
            self.waiting_nodes.pop(key, None)
            pool = self._pool(role)
            if key in pool.tainted:
                pool.tainted.discard(key)
                self._push_heap(role, key)
                return
            if self._add_server_no_lock(role, host, port):
                self.log_status(f"Add {role.value} instance: {host}:{port}.")

    def drop_waiting_instance(self, key: str) -> None:
        with self._lock:
            self.waiting_nodes.pop(key, None)

    def remove_instances(self, role: ServerRole, instances: list[tuple[str, int]]) -> bool:
        if not instances:
            return False
        keys = {server_key(host, port) for host, port in instances}
        with self._lock:
            pool = self._pool(role)
            if self.request_num > 0:
                pool.tainted.update(keys)
                self._reset_heap(role, bump_seq=True)
                logger.warning("Start to taint %s instances %s.", role.value, sorted(keys))
                return True

            removed = False
            for key in keys:
                removed = pool.servers.pop(key, None) is not None or removed
                self.waiting_nodes.pop(key, None)
            pool.tainted.difference_update(keys)
            if removed:
                self._reset_heap(role, bump_seq=True)
                self.log_status(f"Remove {role.value} instances: {sorted(keys)}.")
            return False

    def finalize_tainted_instances(self) -> None:
        with self._lock:
            if self.request_num != 0:
                return
            for role in ServerRole:
                pool = self._pool(role)
                if not pool.tainted:
                    continue
                keys = list(pool.tainted)
                for key in keys:
                    pool.servers.pop(key, None)
                pool.tainted.clear()
                self._reset_heap(role, bump_seq=True)
                self.log_status(f"Remove {role.value} instances after drain: {keys}.")


class SchedulerManager(BaseManager):
    """Multiprocessing RPC bridge; body is empty but required by BaseManager."""


def _shared_scheduler_proxy() -> "SharedProxyScheduler":
    if shared_scheduler is None:
        raise RuntimeError("shared scheduler is not initialized")
    return shared_scheduler


SchedulerManager.register("get_scheduler", callable=_shared_scheduler_proxy)


class WorkerRuntime:
    def __init__(self, scheduler: Any):
        self.scheduler = scheduler
        self._clients: dict[ServerRole, dict[str, httpx.AsyncClient]] = {
            ServerRole.PREFILL: {},
            ServerRole.DECODE: {},
        }
        self._async_lock = asyncio.Lock()

    async def schedule(self, method: str, /, *args, **kwargs) -> Any:
        async with self._async_lock:
            return getattr(self.scheduler, method)(*args, **kwargs)

    async def get_client(self, role: ServerRole, key: str) -> httpx.AsyncClient:
        clients = self._clients[role]
        if key not in clients:
            await self.sync_clients()
        return clients[key]

    async def sync_clients(self) -> None:
        snapshot = self.scheduler.get_snapshot()
        role_targets = {
            ServerRole.PREFILL: {
                server_key(s["host"], s["port"]): (s["host"], s["port"]) for s in snapshot["prefill_instances"]
            },
            ServerRole.DECODE: {
                server_key(s["host"], s["port"]): (s["host"], s["port"]) for s in snapshot["decode_instances"]
            },
        }
        for role, targets in role_targets.items():
            await self._sync_clients(role, targets)

    async def _sync_clients(self, role: ServerRole, targets: dict[str, tuple[str, int]]) -> None:
        clients = self._clients[role]
        for key in [key for key in clients if key not in targets]:
            await clients.pop(key).aclose()
        for key, (host, port) in targets.items():
            if key in clients:
                continue
            clients[key] = httpx.AsyncClient(
                timeout=None,
                base_url=build_base_url(host, port),
                limits=httpx.Limits(max_connections=100000, max_keepalive_connections=100000),
            )

    async def close(self) -> None:
        for role in ServerRole:
            for client in list(self._clients[role].values()):
                await client.aclose()
            self._clients[role].clear()


def get_runtime() -> WorkerRuntime:
    if runtime is None:
        raise RuntimeError("worker runtime is not initialized")
    return runtime


class NodeListener:
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while True:
            args = get_global_args()
            for key, (instance_type, server, retries) in list(self.scheduler.get_waiting_nodes().items()):
                host, port = server
                is_valid = asyncio.run(self.check_instance_status(host, port))
                print(f"Checking instance {key}...")
                retries += 1
                if is_valid:
                    self.scheduler.activate_waiting_instance(ServerRole(instance_type), host, port)
                elif retries >= args.max_waiting_retries:
                    print(f"Instance {key} was not added to the proxy.")
                    self.scheduler.drop_waiting_instance(key)
                else:
                    self.scheduler.mark_waiting_retry(key, retries)

            self.scheduler.finalize_tainted_instances()
            time.sleep(args.waiting_retry_interval)

    @staticmethod
    async def check_instance_status(host: str, port: int) -> bool:
        endpoint = "/models"
        headers = {"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY')}"}
        try:
            async with httpx.AsyncClient(timeout=5.0, base_url=build_base_url(host, port)) as client:
                response = await client.get(endpoint, headers=headers)
                response.raise_for_status()
                return True
        except (httpx.RequestError, httpx.HTTPStatusError):
            return False


def manager_config_path(proxy_port: int) -> Path:
    return Path(tempfile.gettempdir()) / f"vllm_lb_proxy_manager_{proxy_port}.json"


def write_manager_config(proxy_port: int, host: str, manager_port: int, authkey: bytes) -> None:
    manager_config_path(proxy_port).write_text(
        json.dumps(
            {
                "host": host,
                "port": manager_port,
                "authkey": base64.b64encode(authkey).decode("ascii"),
            }
        ),
        encoding="utf-8",
    )


def read_manager_config(proxy_port: int) -> dict[str, Any]:
    path = manager_config_path(proxy_port)
    if not path.is_file():
        raise RuntimeError(
            f"Manager config not found at {path}. "
            "Start the proxy from __main__ with --workers > 1 before worker processes connect."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def cleanup_manager_config(proxy_port: int) -> None:
    manager_config_path(proxy_port).unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--prefiller-hosts", type=str, nargs="+", default=["localhost"])
    parser.add_argument("--prefiller-ports", type=int, nargs="+", default=[8001])
    parser.add_argument("--decoder-hosts", type=str, nargs="+", default=["localhost"])
    parser.add_argument("--decoder-ports", type=int, nargs="+", default=[8002])
    parser.add_argument("--max-retries", type=int, default=3, help="Maximum number of retries for HTTP requests")
    parser.add_argument(
        "--retry-delay", type=float, default=0.001, help="Base delay (seconds) for exponential backoff retries"
    )
    parser.add_argument(
        "--max-waiting-retries", type=int, default=3, help="Maximum number of retries for waiting nodes to be started"
    )
    parser.add_argument(
        "--waiting-retry-interval",
        type=float,
        default=10,
        help="Check interval (seconds) for waiting nodes to be started",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of uvicorn worker processes. Scheduling state is shared across workers.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Log level for the proxy server.",
    )
    args = parser.parse_args()
    if len(args.prefiller_hosts) != len(args.prefiller_ports):
        raise ValueError("Number of prefiller hosts must match number of prefiller ports")
    if len(args.decoder_hosts) != len(args.decoder_ports):
        raise ValueError("Number of decoder hosts must match number of decoder ports")
    args.prefiller_instances = list(zip(args.prefiller_hosts, args.prefiller_ports))
    args.decoder_instances = list(zip(args.decoder_hosts, args.decoder_ports))
    return args


def get_global_args() -> argparse.Namespace:
    global global_args
    if global_args is None:
        global_args = parse_args()
    return global_args


def connect_shared_scheduler(proxy_port: int):
    manager_cfg = read_manager_config(proxy_port)
    manager = SchedulerManager(
        address=(manager_cfg["host"], manager_cfg["port"]),
        authkey=base64.b64decode(manager_cfg["authkey"]),
    )
    manager.connect()
    return manager.get_scheduler()  # type: ignore[attr-defined]


def bootstrap_parent_process(args: argparse.Namespace) -> None:
    """Initialize cross-worker shared state in the parent process before uvicorn spawns workers."""
    global shared_scheduler
    if args.workers <= 1:
        return

    shared_scheduler = SharedProxyScheduler(args.prefiller_instances, args.decoder_instances)
    NodeListener(shared_scheduler)

    authkey = os.urandom(16)
    manager = SchedulerManager(address=("127.0.0.1", 0), authkey=authkey)
    server = manager.get_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.address)
    write_manager_config(args.port, host, port, authkey)


def _ensure_scheduler(args) -> SharedProxyScheduler:
    global shared_scheduler
    if shared_scheduler is not None:
        return shared_scheduler
    shared_scheduler = SharedProxyScheduler(args.prefiller_instances, args.decoder_instances)
    NodeListener(shared_scheduler)
    return shared_scheduler


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global runtime
    args = get_global_args()
    if args.workers > 1:
        scheduler = connect_shared_scheduler(args.port)
    else:
        scheduler = _ensure_scheduler(args)
    runtime = WorkerRuntime(scheduler)
    await runtime.sync_clients()
    snapshot = scheduler.get_snapshot()
    logger.info(
        "Initialized %s prefill clients and %s decode clients in worker %s.",
        len(snapshot["prefill_instances"]),
        len(snapshot["decode_instances"]),
        os.getpid(),
    )
    yield
    await runtime.close()
    runtime = None


app = FastAPI(lifespan=lifespan)


def create_app():
    setup_logging(get_global_args().log_level)
    return app


async def listen_for_disconnect(request: Request) -> None:
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            break


def with_cancellation(handler_func):
    @functools.wraps(handler_func)
    async def wrapper(*args, **kwargs):
        request = kwargs["request"]
        handler_task = asyncio.create_task(handler_func(*args, **kwargs))
        cancellation_task = asyncio.create_task(listen_for_disconnect(request))
        done, pending = await asyncio.wait([handler_task, cancellation_task], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if handler_task in done:
            return handler_task.result()
        return None

    return wrapper


def auth_headers(request_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY')}",
        "X-Request-Id": request_id,
    }


def build_prefill_request(req_data: dict) -> dict:
    payload = req_data.copy()
    payload["kv_transfer_params"] = {
        "do_remote_decode": True,
        "do_remote_prefill": False,
        "remote_engine_id": None,
        "remote_block_ids": None,
        "remote_host": None,
        "remote_port": None,
    }
    payload["stream"] = False
    payload["max_tokens"] = 1
    payload["min_tokens"] = 1
    if "max_completion_tokens" in payload:
        payload["max_completion_tokens"] = 1
    payload.pop("stream_options", None)
    return payload


async def send_request_to_service(
    client: httpx.AsyncClient,
    endpoint: str,
    req_data: dict,
    request_id: str,
    max_retries: int = 3,
    base_delay: float = 0.2,
):
    req_data = build_prefill_request(req_data)
    headers = auth_headers(request_id)
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            response = await client.post(endpoint, json=req_data, headers=headers)
            response.raise_for_status()
            return response
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            logger.warning("Attempt %s failed for %s: %s", attempt, endpoint, exc)
            last_exc = exc
            if attempt < max_retries:
                await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
            else:
                logger.error("All %s attempts failed for %s.", max_retries, endpoint)
                raise last_exc


async def stream_service_response_with_retry(
    client: httpx.AsyncClient,
    endpoint: str,
    req_data: dict,
    request_id: str,
    max_retries: int = 3,
    base_delay: float = 0.2,
):
    headers = auth_headers(request_id)
    for attempt in range(1, max_retries + 1):
        try:
            async with client.stream("POST", endpoint, json=req_data, headers=headers) as response:
                response.raise_for_status()
                first_chunk_sent = False
                async for chunk in response.aiter_bytes():
                    first_chunk_sent = True
                    yield chunk
                return
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            if attempt < max_retries:
                logger.warning("Attempt %s failed for streaming %s: %s", attempt, endpoint, exc)
                await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
            else:
                logger.error("All %s attempts failed for streaming %s.", max_retries, endpoint)
                raise exc
        except Exception as exc:
            if "first_chunk_sent" in locals() and first_chunk_sent:
                logger.error("Streaming to client interrupted after response started: %s", exc)
                return
            if attempt < max_retries:
                logger.warning("Attempt %s failed for streaming %s: %s", attempt, endpoint, exc)
                await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
            else:
                logger.error("All %s attempts failed for streaming %s.", max_retries, endpoint)
                raise exc


async def _abort_prefill_selection(
    runtime: WorkerRuntime,
    prefiller_key: str,
    prefiller_score: float,
    *,
    is_initial_request: bool,
) -> None:
    if is_initial_request:
        await runtime.schedule("finish_request", prefiller_key, prefiller_score, None, 0.0, release_prefill_kv=True)
    else:
        await runtime.schedule("release_prefill_kv", prefiller_key, prefiller_score)


async def _finish_instance(runtime: WorkerRuntime, info: InstanceInfo, *, release_prefill_kv: bool) -> None:
    await runtime.schedule(
        "finish_request",
        info.prefiller_key,
        info.prefiller_score,
        info.decoder_key,
        info.decoder_score,
        release_prefill_kv,
    )


async def assign_instances(
    api: str,
    req_data: Any,
    request_length: int,
    *,
    is_initial_request: bool,
) -> InstanceInfo:
    runtime = get_runtime()
    args = get_global_args()
    prefiller_score = calculate_prefill_score(request_length)
    decoder_score = calculate_decode_score(request_length)
    request_id = next_req_id()
    pick_prefill = "begin_request" if is_initial_request else "reserve_prefill_kv"
    prefiller = await runtime.schedule(pick_prefill, prefiller_score)
    prefiller_key = prefiller["key"]

    try:
        response = await send_request_to_service(
            await runtime.get_client(ServerRole.PREFILL, prefiller_key),
            api,
            req_data,
            request_id,
            max_retries=args.max_retries,
            base_delay=args.retry_delay,
        )
    except Exception:
        await _abort_prefill_selection(runtime, prefiller_key, prefiller_score, is_initial_request=is_initial_request)
        raise

    kv_transfer_params = response.json().get("kv_transfer_params", {})
    if kv_transfer_params:
        req_data["kv_transfer_params"] = kv_transfer_params

    try:
        decoder = await runtime.schedule("pick_decoder", decoder_score)
    except Exception:
        await _abort_prefill_selection(runtime, prefiller_key, prefiller_score, is_initial_request=is_initial_request)
        raise

    prefiller_client = await runtime.get_client(ServerRole.PREFILL, prefiller_key)
    decoder_client = await runtime.get_client(ServerRole.DECODE, decoder["key"])
    logger.debug("Using %s %s", prefiller_client.base_url, decoder_client.base_url)
    return InstanceInfo(
        request_id=request_id,
        prefiller_key=prefiller_key,
        prefiller_score=prefiller_score,
        decoder_key=decoder["key"],
        decoder_score=decoder_score,
        decoder_host=decoder["host"],
        decoder_port=decoder["port"],
    )


async def reassign_instances(
    api: str,
    req_data: Any,
    request_length: int,
    previous_instance: InstanceInfo,
) -> InstanceInfo:
    runtime = get_runtime()
    await runtime.schedule("release_prefill_kv", previous_instance.prefiller_key, previous_instance.prefiller_score)
    await runtime.schedule("release_decoder", previous_instance.decoder_key, previous_instance.decoder_score)
    return await assign_instances(api, req_data, request_length, is_initial_request=False)


async def handle_completions_impl(api: str, request: Request):
    runtime = get_runtime()
    args = get_global_args()
    request_released = False
    try:
        req_data = await request.json()
        req_body = await request.body()
        request_length = len(req_body)
        instance_info = await assign_instances(api, req_data, request_length, is_initial_request=True)
        stream_flag = bool(req_data.get("stream", False))
        chat_flag = "messages" in req_data

        if "prompt" in req_data:
            origin_prompt = req_data["prompt"]
        elif chat_flag:
            messages = req_data["messages"]
            origin_prompt = messages[0].get("content", "")
        else:
            origin_prompt = ""
        origin_max_tokens = req_data.get("max_tokens", 16)

        async def generate_stream():
            nonlocal instance_info
            nonlocal request_released
            generated_token = ""
            released_kv = False
            retry_count = 0
            retry = True
            completion_tokens = 0

            async def release_prefill_kv_once() -> None:
                nonlocal released_kv
                if not released_kv:
                    await runtime.schedule(
                        "release_prefill_kv", instance_info.prefiller_key, instance_info.prefiller_score
                    )
                    released_kv = True

            try:
                while retry:
                    retry = False
                    decoder_client = await runtime.get_client(ServerRole.DECODE, instance_info.decoder_key)
                    async for chunk in stream_service_response_with_retry(
                        decoder_client,
                        api,
                        req_data,
                        request_id=instance_info.request_id,
                        max_retries=args.max_retries,
                        base_delay=args.retry_delay,
                    ):
                        if not released_kv and chunk:
                            await release_prefill_kv_once()
                        try:
                            chunk_str = chunk.decode("utf-8").strip()
                        except UnicodeDecodeError:
                            logger.debug("Skipping chunk: %s", chunk)
                            yield chunk
                            continue
                        if not chunk_str:
                            continue
                        if chunk_str.startswith("data: "):
                            chunk_str = chunk_str[len("data: ") :]
                        try:
                            chunk_json = json.loads(chunk_str)
                        except json.JSONDecodeError:
                            logger.debug("Skipping chunk: %s", chunk_str)
                            yield chunk
                            continue
                        choices = chunk_json.get("choices", [])
                        if not choices:
                            yield chunk
                            continue

                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        message = choice.get("message") or {}
                        content = delta.get("content") or message.get("content") or choice.get("text") or ""
                        generated_token += content

                        stop_reason = choice.get("stop_reason")
                        usage = chunk_json.get("usage", {})
                        completion_tokens = (
                            (completion_tokens + 1)
                            if stream_flag
                            else (completion_tokens + usage.get("completion_tokens", 0))
                        )
                        if stop_reason == "recomputed":
                            retry = True
                            retry_count += 1
                            if chat_flag:
                                messages[0]["content"] = origin_prompt + generated_token
                            else:
                                req_data["prompt"] = origin_prompt + generated_token
                            req_data["max_tokens"] = origin_max_tokens - completion_tokens + retry_count
                            tmp_request_length = len(json.dumps(req_data).encode("utf-8"))
                            instance_info = await reassign_instances(api, req_data, tmp_request_length, instance_info)
                            released_kv = False
                            break
                        if retry_count > 0 and not stream_flag:
                            if chat_flag:
                                choice["message"]["content"] = generated_token
                            else:
                                choice["text"] = generated_token
                            chunk = json.dumps(chunk_json).encode("utf-8")
                        yield chunk
            except asyncio.CancelledError:
                logger.warning(
                    "Streaming from decoder %s:%s was cancelled; releasing request %s resources",
                    instance_info.decoder_host,
                    instance_info.decoder_port,
                    instance_info.request_id,
                )
                raise
            except Exception as exc:
                logger.error(
                    "Error during streaming from decoder %s:%s: %s while handling request %s; releasing prefiller KV",
                    instance_info.decoder_host,
                    instance_info.decoder_port,
                    exc,
                    instance_info.request_id,
                )
            finally:
                await _finish_instance(runtime, instance_info, release_prefill_kv=not released_kv)
                released_kv = True
                request_released = True

        media_type = "text/event-stream; charset=utf-8" if stream_flag else "application/json"
        return StreamingResponse(generate_stream(), media_type=media_type)
    except Exception:
        import traceback

        exc_info = sys.exc_info()
        print(f"Error occurred in disagg prefill proxy server - {api} endpoint")
        print("".join(traceback.format_exception(*exc_info)))
        if not request_released and "instance_info" in locals():
            await _finish_instance(runtime, instance_info, release_prefill_kv=True)
            request_released = True
        raise


async def adjust_instances_impl(adjust_mode: str, request: Request):
    req_data = await request.json()
    instance_type = req_data.get("type", "")
    instances = req_data.get("instances", [])
    if isinstance(instances, str):
        instances = [instances]
    parsed_instances = parse_server_addresses(instances)
    all_msg = f"{adjust_mode} {instance_type} instances: {[f'{host}:{port}' for host, port in parsed_instances]}."

    try:
        role = ServerRole(instance_type)
    except ValueError:
        return {
            "error": (
                f"Instance type {instance_type!r} is not supported. "
                f"Only '{ServerRole.PREFILL.value}' and '{ServerRole.DECODE.value}' are allowed."
            )
        }

    scheduler = get_runtime().scheduler

    if adjust_mode == "add":
        waiting_nodes = scheduler.add_instances(role, parsed_instances)
        if waiting_nodes:
            all_msg = f"Instances {waiting_nodes} are waiting to be added."
    elif adjust_mode == "remove":
        need_waiting = scheduler.remove_instances(role, parsed_instances)
        if need_waiting:
            all_msg = (
                f"Instances {[f'{host}:{port}' for host, port in parsed_instances]} "
                "are isolated and waiting to be removed."
            )

    snapshot = scheduler.get_snapshot()
    return {
        "message": all_msg,
        "current_prefill_instances": [f"{server['host']}:{server['port']}" for server in snapshot["prefill_instances"]],
        "current_decode_instances": [f"{server['host']}:{server['port']}" for server in snapshot["decode_instances"]],
    }


def parse_server_addresses(instances: list[str]) -> list[tuple[str, int]]:
    return [(host, int(port)) for host, port in (instance.split(":") for instance in instances)]


@app.post("/v1/completions")
@with_cancellation
async def handle_completions(request: Request):
    return await handle_completions_impl("/completions", request)


@app.post("/v1/chat/completions")
@with_cancellation
async def handle_chat_completions(request: Request):
    return await handle_completions_impl("/chat/completions", request)


@app.post("/reset_prefix_cache")
async def reset_prefix_cache(request: Request):
    params = dict(request.query_params)
    runtime = get_runtime()
    await runtime.sync_clients()
    snapshot = runtime.scheduler.get_snapshot()
    backend_instances = [(ServerRole.PREFILL, server) for server in snapshot["prefill_instances"]] + [
        (ServerRole.DECODE, server) for server in snapshot["decode_instances"]
    ]
    failures: list[str] = []
    for role, server in backend_instances:
        base_url = build_server_url(server["host"], server["port"])
        try:
            client = await runtime.get_client(role, server_key(server["host"], server["port"]))
            resp = await client.post(f"{base_url}/reset_prefix_cache", params=params)
            resp.raise_for_status()
        except Exception as e:
            logger.error("reset_prefix_cache failed for %s: %s", base_url, e)
            failures.append(base_url)
    if failures:
        return JSONResponse(status_code=500, content={"failed": failures})
    return Response(status_code=200)


@app.get("/healthcheck")
async def healthcheck():
    return get_runtime().scheduler.healthcheck()


@app.post("/instances/add")
async def handle_add_instances(request: Request):
    return await adjust_instances_impl("add", request)


@app.post("/instances/remove")
async def handle_remove_instances(request: Request):
    return await adjust_instances_impl("remove", request)


if __name__ == "__main__":
    global_args = parse_args()
    setup_logging(global_args.log_level)
    bootstrap_parent_process(global_args)
    import uvicorn

    module_name = Path(__file__).stem
    try:
        uvicorn.run(
            f"{module_name}:create_app",
            host=global_args.host,
            port=global_args.port,
            workers=global_args.workers,
            factory=True,
            app_dir=str(Path(__file__).resolve().parent),
        )
    finally:
        cleanup_manager_config(global_args.port)
PD_FILE_EOF
```

创建 `wait_ready.py`：

```bash
cat > "$WORK/wait_ready.py" <<'PD_FILE_EOF'
"""Wait for HTTP 200, bounded by one overall deadline, bypassing HTTP proxy."""
import argparse,time,urllib.request
p=argparse.ArgumentParser();p.add_argument('--url',action='append',required=True)
p.add_argument('--timeout',type=float,default=1800);a=p.parse_args()
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline=time.monotonic()+a.timeout
pending=set(a.url)
while pending and time.monotonic()<deadline:
    for url in list(pending):
        try:
            with opener.open(url,timeout=min(5,max(.1,deadline-time.monotonic()))) as r:
                if r.status==200:pending.remove(url);print('READY',url,flush=True)
        except Exception:pass
    if pending:time.sleep(min(5,max(0,deadline-time.monotonic())))
if pending:raise SystemExit('TIMEOUT: '+', '.join(sorted(pending)))
PD_FILE_EOF
```

创建 `generate_prompts.py`：

```bash
cat > "$WORK/generate_prompts.py" <<'PD_FILE_EOF'
"""Create an exact token-ID workload. This synthetic workload is not an accuracy set."""
import argparse
import hashlib
import json
from pathlib import Path
import random


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--model', required=True)
    p.add_argument('--length', type=int, required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    from vllm.tokenizers import get_tokenizer
    tok = get_tokenizer(a.model, tokenizer_mode='deepseek_v4', trust_remote_code=True)
    text = ('Analyze data structures and explain system performance with clear examples. '
            'The server receives a request and processes the context before generating output. '
            'Compute memory capacity latency bandwidth queue scheduler token sequence attention. '
            'def example(value): return value * 2\n'
            '0123456789 abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ ')
    special = set(getattr(tok, 'all_special_ids', []) or [])
    pool = sorted(set(tok.encode(text, add_special_tokens=False))-special)
    if len(pool) < 16: raise RuntimeError('Tokenizer produced too few ordinary token IDs')
    ids = random.Random(20261021+a.length).choices(pool, k=a.length)
    data = dict(length=a.length, prompt_token_ids=ids, pool=pool, seed=20261021,
                scheme='random ordinary token IDs, unique 32-token header per request')
    payload = json.dumps(data, separators=(',', ':'))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(payload, encoding='utf-8')
    print(json.dumps(dict(file=str(a.out), length=len(ids),
                         sha256=hashlib.sha256(payload.encode()).hexdigest())))


if __name__ == '__main__': main()
PD_FILE_EOF
```

创建 `bench_client.py`：

```bash
cat > "$WORK/bench_client.py" <<'PD_FILE_EOF'
"""Closed-loop sustained load or a finite wave, with exact input/output token checks.

TTFT-token measures first delivered output token ID. TTFT-text measures first
nonempty text delta. Streaming event gaps are never labelled individual-token ITL.
"""
import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
import time
import uuid


def percentile(values, q):
    values = sorted(values)
    if not values: return None
    idx = (len(values)-1)*q
    lo, hi = math.floor(idx), math.ceil(idx)
    return values[lo]+(values[hi]-values[lo])*(idx-lo)


def stats(values):
    if not values: return dict(mean=None, p50=None, p95=None, p99=None)
    return dict(mean=statistics.mean(values), p50=percentile(values, .5),
                p95=percentile(values, .95), p99=percentile(values, .99))


async def main(a):
    import aiohttp
    data = json.loads(a.prompts.read_text(encoding='utf-8'))
    length = data['length']; base = data['prompt_token_ids']; pool = data['pool']
    assert len(base) == length and length >= 32
    a.out.parent.mkdir(exist_ok=True, parents=True)
    raw = a.out.with_suffix('.requests.jsonl').open('w', encoding='utf-8')
    run_id = uuid.uuid4().hex[:12]
    epoch = time.perf_counter()
    start_wall_ns = time.time_ns()
    measure_start = a.burnin if a.mode == 'steady' else 0
    measure_end = a.burnin+a.duration if a.mode == 'steady' else float('inf')
    records = []; issued = 0
    timeout = aiohttp.ClientTimeout(total=a.timeout)
    connector = aiohttp.TCPConnector(limit=a.concurrency, limit_per_host=a.concurrency)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, read_bufsize=8*1024*1024,
                                    trust_env=False) as session:
        async def request_one(index):
            rid = f'bench-{run_id}-{index}'
            ids = list(base)
            seed = int.from_bytes(hashlib.sha256(rid.encode()).digest()[:8], 'big')
            ids[:32] = random.Random(seed).choices(pool, k=32)
            body = dict(model=a.model, prompt=ids, add_special_tokens=False,
                        max_tokens=a.output_length, min_tokens=a.output_length,
                        ignore_eos=True, temperature=0, stream=True,
                        stream_options={'include_usage':True},
                        return_token_ids=a.return_token_ids, request_id=rid)
            encoded = json.dumps(body, separators=(',', ':')).encode()
            t0 = time.perf_counter()-epoch
            first, first_text, last = None, None, None
            events, usage, observed = [], None, 0
            error, finish, done = None, None, False
            try:
                async with session.post(a.base_url.rstrip('/')+'/v1/completions',
                        data=encoded, headers={'Content-Type':'application/json',
                                             'X-Request-Id':rid}) as response:
                    if response.status != 200:
                        raise RuntimeError(f'HTTP {response.status}: {(await response.text())[:1000]}')
                    async for line in response.content:
                        if not line.startswith(b'data:'): continue
                        value = line[len(b'data:'):].strip()
                        if value == b'[DONE]': done=True; break
                        message = json.loads(value)
                        if message.get('error'): raise RuntimeError(str(message['error']))
                        now = time.perf_counter()-epoch
                        if message.get('usage'): usage=message['usage']
                        for choice in message.get('choices', []):
                            token_ids = choice.get('token_ids') or []
                            text = choice.get('text') or ''
                            if token_ids or text:
                                if first is None: first=now
                                last=now
                                events.append({'t_s':now, 'tokens':len(token_ids)})
                            observed+=len(token_ids)
                            if text and first_text is None: first_text=now
                            if choice.get('finish_reason'): finish=choice['finish_reason']
                            if choice.get('stop_reason') == 'recomputed':
                                raise RuntimeError('KV recomputation invalidates the baseline')
                if not done: raise RuntimeError('Missing SSE [DONE] marker')
                if usage is None: raise RuntimeError('Missing usage; cannot validate exact lengths')
                if usage.get('prompt_tokens') != length:
                    raise RuntimeError(f'Input length mismatch: {usage}')
                if usage.get('completion_tokens') != a.output_length or (a.return_token_ids and observed != a.output_length):
                    raise RuntimeError(f'Output mismatch: usage={usage}, observed_token_ids={observed}')
                if first is None or last is None: raise RuntimeError('No output payload received')
            except Exception as exc:
                error = repr(exc)
            end = time.perf_counter()-epoch
            count = (usage or {}).get('completion_tokens', 0)
            row = dict(request_id=rid, start_s=t0, end_s=end, error=error, usage=usage,
                       output_ids=observed, finish_reason=finish, token_events=events,
                       ttft_token_ms=None if not a.return_token_ids or first is None else (first-t0)*1000,
                       ttft_text_ms=None if first_text is None else (first_text-t0)*1000,
                       tpot_request_ms=None if first is None or count<2 else (end-first)*1000/(count-1),
                       tpot_delivery_ms=None if first is None or count<2 else (last-first)*1000/(count-1),
                       e2e_ms=(end-t0)*1000,
                       first_event_tokens=events[0]['tokens'] if events else 0,
                       in_measure_window=t0>=measure_start and end<=measure_end)
            raw.write(json.dumps(row)+'\n'); raw.flush()
            records.append(row)
            if error: print(f'{rid}: {error}', flush=True)

        async def worker():
            nonlocal issued
            while True:
                if a.mode == 'wave':
                    if issued >= a.requests: return
                elif time.perf_counter()-epoch >= measure_end:
                    return
                index=issued; issued+=1
                await request_one(index)

        await asyncio.gather(*(worker() for _ in range(a.concurrency)))
    raw.close()
    elapsed = time.perf_counter()-epoch
    window_end = elapsed if a.mode == 'wave' else measure_end
    included = [r for r in records if r['in_measure_window']]
    valid = [r for r in included if not r['error']]
    failures = [r for r in included if r['error']]
    start_cohort = [r for r in records if measure_start<=r['start_s']<window_end]
    censored = [r for r in start_cohort if r['end_s']>window_end]
    delivered = sum(e['tokens'] for r in records for e in r['token_events']
                    if measure_start<=e['t_s']<window_end)
    summary = dict(run_id=run_id, mode=a.mode, base_url=a.base_url,
        input_tokens=length, output_tokens=a.output_length, client_concurrency=a.concurrency,
        start_wall_ns=start_wall_ns, window_start_s=measure_start, window_end_s=window_end,
        wall_elapsed_s=elapsed, measured_requests=len(included), valid_requests=len(valid),
        measured_failures=len(failures), all_phase_failures=sum(bool(r['error']) for r in records),
        start_window_requests=len(start_cohort), end_censored_requests=len(censored),
        end_censored_fraction=len(censored)/len(start_cohort) if start_cohort else None,
        start_cohort_tpot_ms=stats([r['tpot_request_ms'] for r in start_cohort
                                    if not r['error'] and r['tpot_request_ms'] is not None]),
        sufficient_samples=len(valid)>=a.min_samples,
        return_token_ids=a.return_token_ids,
        delivered_token_id_throughput=delivered/(window_end-measure_start) if a.return_token_ids else None,
        completed_output_token_throughput=sum((r['usage'] or {}).get('completion_tokens',0)
                for r in records if measure_start<=r['end_s']<window_end and not r['error'])/(window_end-measure_start),
        request_completed_throughput=sum(measure_start<=r['end_s']<window_end and not r['error']
                                         for r in records)/(window_end-measure_start),
        **{key:stats([r[key] for r in valid if r[key] is not None]) for key in
           ('ttft_token_ms','ttft_text_ms','tpot_request_ms','tpot_delivery_ms','e2e_ms')})
    a.out.write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))
    return 0 if not failures and not summary['all_phase_failures'] and summary['sufficient_samples'] else 2


if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--base-url', required=True); p.add_argument('--model', default='dsv4')
    p.add_argument('--prompts', type=Path, required=True); p.add_argument('--out', type=Path, required=True)
    p.add_argument('--concurrency', type=int, default=1)
    p.add_argument('--mode', choices=['steady','wave'], default='steady')
    p.add_argument('--requests', type=int, default=1)
    p.add_argument('--burnin', type=float, default=120); p.add_argument('--duration', type=float, default=600)
    p.add_argument('--timeout', type=float, default=1800)
    p.add_argument('--output-length', type=int, default=1024)
    p.add_argument('--return-token-ids', action='store_true', help='Length-calibration ONLY; echoes prompt IDs in the response')
    p.add_argument('--min-samples', type=int, default=30)
    args=p.parse_args()
    if args.concurrency<1 or args.requests<1 or args.duration<=0 or args.burnin<0:
        p.error('Invalid concurrency, requests, duration or burnin')
    raise SystemExit(asyncio.run(main(args)))
PD_FILE_EOF
```

创建 `pd_plateau.py`：

```bash
cat > "$WORK/pd_plateau.py" <<'PD_FILE_EOF'
"""Build a candidate pure-D window from a finite PD wave with token ID events.
Worker audit and absence of KV transfer must still be checked separately.
"""
import argparse,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--summary',type=Path,required=True)
p.add_argument('--out',type=Path,required=True);a=p.parse_args()
s=json.loads(a.summary.read_text());assert s['mode']=='wave' and s['return_token_ids']
rows=[json.loads(x) for x in a.summary.with_suffix('.requests.jsonl').read_text().splitlines()]
assert len(rows)==s['client_concurrency'] and all(not r['error'] for r in rows)
events=sorted((e['t_s'],i,e['tokens']) for i,r in enumerate(rows) for e in r['token_events'])
counts=[0]*len(rows);groups=[];win=[]
for t,i,n in events:
    counts[i]+=n
    if all(32<=k<=896 for k in counts): win.append((t,list(counts)))
    else:
        if win:groups.append(win)
        win=[]
if win:groups.append(win)
best=max(groups,key=lambda g:g[-1][0]-g[0][0],default=[])
result=dict(run=0,batch_requested=len(rows),plateau_valid=False,source=str(a.summary))
if len(best)>=2:
    dt=best[-1][0]-best[0][0];tokens=sum(best[-1][1])-sum(best[0][1])
    if dt>0 and tokens>0:
        result.update(plateau_valid=dt>=5,plateau_s=dt,
            plateau_start_ns=s['start_wall_ns']+round(best[0][0]*1e9),
            plateau_end_ns=s['start_wall_ns']+round(best[-1][0]*1e9),
            decode_tokens_per_s=tokens/dt,tpot_equivalent_ms=1000*len(rows)*dt/tokens)
a.out.write_text(json.dumps([result],indent=2));print(json.dumps(result,indent=2))
raise SystemExit(0 if result['plateau_valid'] else 2)
PD_FILE_EOF
```

创建 `audit_window.py`：

```bash
cat > "$WORK/audit_window.py" <<'PD_FILE_EOF'
"""Check all NPU workers in a chosen wall-clock interval. Does not infer throughput."""
import argparse,collections,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--steps',type=Path,required=True)
p.add_argument('--summary',type=Path,required=True);p.add_argument('--run',type=int,default=1)
p.add_argument('--transfer',type=Path);p.add_argument('--expected-workers',type=int,default=8)
a=p.parse_args();s=next(r for r in json.loads(a.summary.read_text()) if r['run']==a.run)
if not s.get('plateau_valid') or 'plateau_start_ns' not in s:
    print(json.dumps(dict(valid=False,reason='No eligible full-overlap decode window')))
    raise SystemExit(2)
start,end=s['plateau_start_ns'],s['plateau_end_ns'];groups=collections.defaultdict(list)
for path in a.steps.glob('*.jsonl'):
    for line in path.read_text().splitlines():
        r=json.loads(line)
        if start<=r['time_ns']<=end: groups[(r['host'],r['pid'],r['device'])].append(r)
report=[]
for key,rows in sorted(groups.items()):
    counts=collections.Counter(r['batch_size'] for r in rows)
    report.append(dict(worker=key,steps=len(rows),batch_distribution=dict(counts),
        all_decode=all(r['all_decode'] for r in rows),
        constant_target=all(r['batch_size']==s['batch_requested'] for r in rows),
        covered_s=(max(r['time_ns'] for r in rows)-min(r['time_ns'] for r in rows))/1e9))
transfers=[]
observed_reads=0
if a.transfer:
    for path in a.transfer.glob('*.jsonl'):
        for line in path.read_text().splitlines():
            r=json.loads(line)
            if r.get('kind')!='mooncake_sync_read': continue
            if r.get('error') is None and r.get('bytes',0)>0: observed_reads+=1
            t1=r['time_ns'];t0=t1-int(r.get('duration_ms',0)*1e6)
            if t0<end and t1>start:transfers.append(r)
valid=(len(report)==a.expected_workers and all(r['steps']>=100 and r['all_decode'] and
    r['constant_target'] and r['covered_s']>=4.5 for r in report) and observed_reads>0 and not transfers and s['plateau_valid'])
print(json.dumps(dict(valid=valid,window_s=(end-start)/1e9,workers=report,
    observed_successful_read_calls=observed_reads,overlapping_transfer_calls=len(transfers),note='Also inspect KV preemption/logs and transfer kernels in trace'),indent=2))
raise SystemExit(0 if valid else 2)
PD_FILE_EOF
```

创建 `write_bs.py`：

```bash
cat > "$WORK/write_bs.py" <<'PD_FILE_EOF'
"""Create selection.env only from three successful fixed-batch PD wave audits."""
import argparse,json,re,shlex
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True)
p.add_argument('--candidate',type=int,required=True);p.add_argument('--out',type=Path,required=True)
a=p.parse_args();b=a.candidate
for repeat in (1,2,3):
    prefix=a.directory/f'C{b}-r{repeat}'
    audit=json.loads(prefix.with_suffix('.audit.json').read_text())
    window=json.loads(prefix.with_suffix('.window.json').read_text())[0]
    assert audit['valid'] is True, f'Worker audit not valid: repeat {repeat}'
    assert window['plateau_valid'] is True and window['batch_requested']==b
    assert len(audit['workers'])==8
    assert all(r['constant_target'] and r['all_decode'] for r in audit['workers'])
if a.out.exists():raise SystemExit('selection.env exists; use a fresh run directory or archive it first')
a.out.write_text(f'export BS={b}\n',encoding='utf-8')
print(f'CONFIRMED BS={b}; saved {a.out}')
PD_FILE_EOF
```

创建 `compare_ttft.py`：

```bash
cat > "$WORK/compare_ttft.py" <<'PD_FILE_EOF'
"""Compare equal-size simultaneous waves to idle-server single-request repetitions."""
import argparse,json,statistics
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--single',type=Path,nargs='+',required=True)
p.add_argument('--batch',type=Path,nargs='+',required=True);p.add_argument('--bs',type=int,required=True)
p.add_argument('--tolerance',type=float,default=.10);a=p.parse_args()
def read(paths):
    runs=[]
    for path in paths:
        summary=json.loads(path.read_text());raw=path.with_suffix('.requests.jsonl')
        rows=[json.loads(line) for line in raw.read_text().splitlines()]
        assert summary['mode']=='wave' and not summary['all_phase_failures']
        assert all(not r['error'] for r in rows)
        assert all(r['usage']['prompt_tokens']==131072 and r['usage']['completion_tokens']==1024 for r in rows)
        runs.append(rows)
    return runs
def ttft(r):
    value=r['ttft_token_ms'] if r['ttft_token_ms'] is not None else r['ttft_text_ms']
    assert value is not None
    return value
single=read(a.single);batch=read(a.batch)
assert all(len(r)==1 for r in single), 'Use separate idle-server single-request runs'
assert all(len(r)==a.bs for r in batch)
t1=statistics.mean(ttft(run[0]) for run in single)
means=[statistics.mean(ttft(r) for r in run) for run in batch]
lasts=[max(ttft(r) for r in run) for run in batch]
spreads=[1000*(max(r['start_s'] for r in run)-min(r['start_s'] for r in run)) for run in batch]
makespans=[max(r['start_s']*1000+ttft(r) for r in run)-min(r['start_s']*1000 for r in run) for run in batch]
mean=statistics.mean(means);last=statistics.mean(lasts)
print(json.dumps(dict(bs=a.bs,single_repetitions=len(single),batch_repetitions=len(batch),
    single_mean_ttft_ms=t1,batch_mean_ttft_ms=mean,batch_mean_last_ttft_ms=last,
    request_start_spread_ms=spreads,wave_first_token_makespan_mean_ms=statistics.mean(makespans),
    batch_to_single=mean/t1,last_to_single=last/t1,
    mentor_prediction_ms=a.bs*t1,relative_error_to_mentor_prediction=mean/(a.bs*t1)-1,
    within_declared_tolerance=abs(mean/(a.bs*t1)-1)<=a.tolerance,
    ideal_serial_equal_service_mean_prediction_ms=(a.bs+1)/2*t1,
    tolerance=a.tolerance,note='Descriptive comparison; no causal conclusion from free HBM alone'),indent=2))
PD_FILE_EOF
```

创建 `check_runs.py`：

```bash
cat > "$WORK/check_runs.py" <<'PD_FILE_EOF'
"""Check a completed run set before summarizing; no hardware calls."""
import argparse,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True)
p.add_argument('--runs',type=int,required=True);p.add_argument('--concurrency',type=int,required=True)
p.add_argument('--mode',choices=['wave','steady'],default='wave');a=p.parse_args()
for i in range(1,a.runs+1):
    path=a.directory/f'r{i}.json'
    s=json.loads(path.read_text())
    assert s['mode']==a.mode and s['client_concurrency']==a.concurrency, f'Config mismatch: {path}'
    assert s['input_tokens']==131072 and s['output_tokens']==1024
    assert s['all_phase_failures']==0 and s['sufficient_samples'], f'Failed samples: {path}'
    rows=[json.loads(x) for x in path.with_suffix('.requests.jsonl').read_text().splitlines()]
    assert rows and all(not r['error'] for r in rows), f'Request failure: {path}'
    assert all(r['usage']['prompt_tokens']==131072 and r['usage']['completion_tokens']==1024 for r in rows)
    if a.mode=='wave':assert len(rows)==a.concurrency, f'Wave size mismatch: {path}'
    else:assert s['end_censored_fraction'] is not None and s['end_censored_fraction']<=.10, f'Extend duration: {path}'
print(f'PASS: {a.runs} {a.mode} runs, concurrency={a.concurrency}, all length/failure checks passed')
PD_FILE_EOF
```

创建 `metrics_monitor.py`：

```bash
cat > "$WORK/metrics_monitor.py" <<'PD_FILE_EOF'
"""Save raw Prometheus scrapes without assuming metric names or summing DP duplicates."""
import argparse
import concurrent.futures
import json
from pathlib import Path
import time
import urllib.request

p=argparse.ArgumentParser()
p.add_argument('--endpoints',type=Path,required=True)
p.add_argument('--out',type=Path,required=True)
p.add_argument('--interval',type=float,default=2)
a=p.parse_args()
rows=json.loads(a.endpoints.read_text())
a.out.parent.mkdir(parents=True,exist_ok=True)

def scrape(row):
    try:
        with urllib.request.urlopen(row['url']+'/metrics',timeout=5) as r:
            return dict(**row,time_ns=time.time_ns(),status=r.status,text=r.read().decode())
    except Exception as e: return dict(**row,time_ns=time.time_ns(),error=repr(e))

with a.out.open('a',encoding='utf-8') as f, concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
    try:
        while True:
            start=time.monotonic()
            for row in pool.map(scrape,rows): f.write(json.dumps(row)+'\n')
            f.flush()
            time.sleep(max(0,a.interval-(time.monotonic()-start)))
    except KeyboardInterrupt: pass
PD_FILE_EOF
```

创建 `report_batches.py`：

```bash
cat > "$WORK/report_batches.py" <<'PD_FILE_EOF'
"""Actual worker scheduling-batch distribution during one client measurement window.
Idle time has no execute_model event; use running/waiting metrics for occupancy.
"""
import argparse,collections,json,statistics
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--steps',type=Path,required=True)
p.add_argument('--summary',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
a=p.parse_args();s=json.loads(a.summary.read_text())
start=s['start_wall_ns']+int(s['window_start_s']*1e9)
end=s['start_wall_ns']+int(s['window_end_s']*1e9)
groups=collections.defaultdict(list)
for path in a.steps.glob('*.jsonl'):
    for line in path.read_text().splitlines():
        r=json.loads(line)
        if start<=r['time_ns']<end:groups[(r['host'],r['pid'],r['device'])].append(r)
def q(xs,p):
    xs=sorted(xs);return xs[round((len(xs)-1)*p)]
report=[]
for key,rows in sorted(groups.items()):
    sizes=[r['batch_size'] for r in rows]
    report.append(dict(worker=key,scheduled_iterations=len(rows),min=min(sizes),p50=q(sizes,.5),
        p95=q(sizes,.95),max=max(sizes),iteration_weighted_mean=statistics.mean(sizes),
        distribution=dict(collections.Counter(sizes)),all_decode_fraction=sum(r['all_decode'] for r in rows)/len(rows)))
a.out.write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
if len(report)!=8: raise SystemExit('Expected 8 NPU worker series; check window and source logs')
PD_FILE_EOF
```

创建 `capacity_table.py`：

```bash
cat > "$WORK/capacity_table.py" <<'PD_FILE_EOF'
"""Export all startup capacity snapshots as CSV; no runtime-peak claim."""
import argparse,csv,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True)
p.add_argument('--out',type=Path,required=True);a=p.parse_args()
fields=['host','pid','device','time_ns','num_blocks','kv_pool_gib',
        'model_storage_gib','startup_reserved_gib','startup_free_gib','total_gib']
with a.out.open('w',newline='',encoding='utf-8-sig') as f:
    w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
    for path in sorted(a.directory.glob('*.jsonl')):
        for line in path.read_text().splitlines():
            r=json.loads(line)
            if r.get('kind')!='capacity_post_kv_before_graph':continue
            row={k:r.get(k) for k in fields[:5]}
            for dst,src in [('kv_pool_gib','actual_unique_cache_storage_bytes'),
                ('model_storage_gib','model_parameter_buffer_storage_bytes'),
                ('startup_reserved_gib','reserved_bytes'),('startup_free_gib','free_bytes'),('total_gib','total_bytes')]:
                row[dst]=r[src]/1024**3
            w.writerow(row)
print(a.out)
PD_FILE_EOF
```


检查 P 容器依赖：

```bash
python -c 'import aiohttp,fastapi,httpx,uvicorn; print("代理和压测依赖可用")'
```

缺少某个包时，用你们批准的软件源或离线 wheel 安装缺少的包，保留现有 vLLM、vLLM-Ascend、torch 和 torch-npu 版本。

## 5. D 容器：创建完整的采样脚本

以下代码块只在 D 容器执行。诊断修改只适用于本文件固定的 v0.23.0 源码，脚本会备份原文件。

创建 `audit_patch.py`：

```bash
cat > "$WORK/audit_patch.py" <<'PD_FILE_EOF'
"""Apply guarded diagnostic hooks to the installed v0.23 runner/connector. Back up originals."""
import argparse
import ast
import hashlib
import importlib.util
import json
from pathlib import Path


def patch(path, anchor, replacement, expected):
    marker = '# ASCEND_BENCH_AUDIT_V1'
    source = path.read_text(encoding='utf-8')
    if marker in source:
        print('Already patched:', path)
        return
    if source.count(anchor) != expected:
        raise RuntimeError(f'{path}: expected {expected} anchors, got {source.count(anchor)}; stop, inspect installed source')
    modified = source.replace(anchor, replacement)
    ast.parse(modified)
    backup = Path(str(path)+'.bench.bak')
    if backup.exists():
        raise RuntimeError(f'Backup exists without marker: {backup}; inspect instead of overwriting')
    backup.write_text(source, encoding='utf-8')
    path.write_text(modified, encoding='utf-8')
    print(json.dumps(dict(file=str(path), original_sha256=hashlib.sha256(source.encode()).hexdigest(),
                         patched_sha256=hashlib.sha256(modified.encode()).hexdigest())))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, help='Optional source root, for offline validation')
    p.add_argument('--restore', action='store_true')
    a = p.parse_args()
    root = a.root
    if root is None:
        spec = importlib.util.find_spec('vllm_ascend')
        if spec is None or not spec.origin: raise RuntimeError('vllm_ascend not found')
        root = Path(spec.origin).parent
    paths = [root/'worker/model_runner_v1.py',
             root/'distributed/kv_transfer/kv_p2p/mooncake_hybrid_connector.py']
    if a.restore:
        for path in paths:
            backup = Path(str(path)+'.bench.bak')
            if backup.exists():
                path.write_text(backup.read_text(encoding='utf-8'), encoding='utf-8')
                backup.unlink()
                print('Restored:', path)
        return
    anchor = '        kv_caches = self.initialize_kv_cache_tensors(kv_cache_config)'
    patch(paths[0], anchor, anchor+'\n        # ASCEND_BENCH_AUDIT_V1\n        from capacity_probe import dump_capacity\n        dump_capacity(self, kv_caches)', 1)
    anchor = 'ret = self.engine.batch_transfer_sync_read(session_id, src_list, dst_list, length_list)'
    replacement = 'from capacity_probe import measured_read  # ASCEND_BENCH_AUDIT_V1\n        ret = measured_read(self.engine, session_id, src_list, dst_list, length_list, req_meta)'
    patch(paths[1], anchor, replacement, 2)


if __name__ == '__main__': main()
PD_FILE_EOF
```

创建 `patch_steps.py`：

```bash
cat > "$WORK/patch_steps.py" <<'PD_FILE_EOF'
"""Guarded v0.23.0 diagnostic patch; run AFTER audit_patch.py; restore in reverse order."""
import argparse,ast,importlib.util
from pathlib import Path

def main():
    p=argparse.ArgumentParser(); p.add_argument('--restore',action='store_true'); a=p.parse_args()
    path=Path(importlib.util.find_spec('vllm_ascend').origin).parent/'worker/model_runner_v1.py'
    backup=Path(str(path)+'.steps.bak')
    if a.restore:
        if backup.exists():
            path.write_text(backup.read_text(encoding='utf-8'),encoding='utf-8'); backup.unlink()
        return
    src=path.read_text(encoding='utf-8')
    if '# V4_2NODE_STEP_AUDIT' in src: return
    anchor="""                deferred_state_corrections_fn = self._update_states(
                    scheduler_output
                )"""
    assert src.count(anchor)==1, 'Source mismatch: stop and inspect installed runner'
    replacement=anchor+'\n                # V4_2NODE_STEP_AUDIT\n                from step_probe import snapshot\n                snapshot(self, scheduler_output)'
    modified=src.replace(anchor,replacement); ast.parse(modified)
    assert not backup.exists(), 'Existing backup: inspect first'
    backup.write_text(src,encoding='utf-8'); path.write_text(modified,encoding='utf-8')
    print(path)
if __name__=='__main__': main()
PD_FILE_EOF
```

创建 `capacity_probe.py`：

```bash
cat > "$WORK/capacity_probe.py" <<'PD_FILE_EOF'
"""Diagnostic-only helpers. Startup snapshots count underlying NPU storages once."""
import dataclasses
import json
import os
from pathlib import Path
import socket
import threading
import time

_lock = threading.Lock()


def write_record(directory, kind, record):
    if not directory:
        return
    root = Path(directory); root.mkdir(parents=True, exist_ok=True)
    row = dict(kind=kind, host=socket.gethostname(), pid=os.getpid(),
               time_ns=time.time_ns(), monotonic_ns=time.perf_counter_ns(), **record)
    path = root / f'{socket.gethostname()}-{os.getpid()}.jsonl'
    with _lock, path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(row, default=str) + '\n')


def _storages(value):
    import torch
    found = {}
    def visit(obj):
        if isinstance(obj, torch.Tensor):
            if obj.device.type != 'npu':
                return
            storage = obj.untyped_storage()
            key = (str(obj.device), storage.data_ptr(), storage.nbytes())
            found[key] = storage.nbytes()
        elif isinstance(obj, dict):
            for item in obj.values(): visit(item)
        elif isinstance(obj, (tuple, list)):
            for item in obj: visit(item)
    visit(value)
    return sum(found.values()), len(found)


def dump_capacity(runner, kv_caches):
    directory = os.getenv('AUDIT_CAPACITY_DIR')
    if not directory:
        return
    import torch
    torch.npu.synchronize()
    cfg = runner.kv_cache_config
    groups = []
    for i, group in enumerate(cfg.kv_cache_groups):
        spec = group.kv_cache_spec
        row = dict(index=i, spec_type=type(spec).__name__, layers=list(group.layer_names),
                   spec_repr=repr(spec))
        for name in ('block_size', 'page_size_bytes', 'real_page_size_bytes'):
            try: row[name] = getattr(spec, name)
            except (AttributeError, NotImplementedError): pass
        if dataclasses.is_dataclass(spec): row['fields'] = dataclasses.asdict(spec)
        # An individual spec bound; do not multiply/group-sum it blindly.
        try:
            row['spec_max_memory_usage_bytes'] = spec.max_memory_usage_bytes(runner.vllm_config)
        except (AttributeError, NotImplementedError, TypeError) as e:
            row['bound_unavailable'] = str(e)
        groups.append(row)
    kv_bytes, count = _storages(kv_caches)
    model_items = list(runner.model.parameters()) + list(runner.model.buffers())
    model_bytes, _ = _storages(model_items)
    parallel = runner.vllm_config.parallel_config
    free, total = torch.npu.mem_get_info()
    write_record(directory, 'capacity_post_kv_before_graph', dict(
        dp_rank=parallel.data_parallel_rank, tp_size=parallel.tensor_parallel_size,
        device=str(runner.device), num_blocks=cfg.num_blocks,
        planned_cache_bytes=sum(t.size for t in cfg.kv_cache_tensors),
        actual_unique_cache_storage_bytes=kv_bytes, unique_cache_storages=count,
        model_parameter_buffer_storage_bytes=model_bytes,
        model_load_memory_usage_bytes=getattr(runner, 'model_memory_usage', None),
        allocated_bytes=torch.npu.memory_allocated(), reserved_bytes=torch.npu.memory_reserved(),
        free_bytes=free, total_bytes=total,
        tensors=[dict(size=t.size, shared_by=t.shared_by) for t in cfg.kv_cache_tensors],
        groups=groups))


def measured_read(engine, session_id, src_list, dst_list, length_list, req_meta):
    directory = os.getenv('AUDIT_TRANSFER_DIR')
    if not directory:
        return engine.batch_transfer_sync_read(session_id, src_list, dst_list, length_list)
    start = time.perf_counter_ns()
    result, error = None, None
    try:
        result = engine.batch_transfer_sync_read(session_id, src_list, dst_list, length_list)
        return result
    except BaseException as exc:
        error = repr(exc)
        raise
    finally:
        stop = time.perf_counter_ns()
        write_record(directory, 'mooncake_sync_read', dict(
            request_id=req_meta.get('request_id'),
            remote_request_id=req_meta.get('remote_request_id'),
            duration_ms=(stop-start)/1e6, bytes=sum(int(n) for n in length_list),
            segments=len(length_list), result=result, error=error))
PD_FILE_EOF
```

创建 `step_probe.py`：

```bash
cat > "$WORK/step_probe.py" <<'PD_FILE_EOF'
"""CPU-only worker snapshots; opt in for diagnostic runs, not device timing."""
import atexit
import json
import os
from pathlib import Path
import socket
import time
_buffers={}
_last_flush=0.0

def flush():
    for path,rows in _buffers.items():
        if rows:
            with path.open('a',encoding='utf-8') as f: f.write(''.join(rows))
            rows.clear()
atexit.register(flush)

def snapshot(runner,out):
    directory=os.getenv('AUDIT_STEP_DIR')
    if not directory: return
    root=Path(directory); root.mkdir(parents=True,exist_ok=True)
    rows=[]
    for rid,n in out.num_scheduled_tokens.items():
        state=runner.requests.get(rid)
        prompt=getattr(state,'prompt_token_ids',None)
        plen=getattr(state,'num_prompt_tokens',None)
        if plen is None and prompt is not None: plen=len(prompt)
        computed=getattr(state,'num_computed_tokens',None)
        rows.append(dict(request_id=rid,scheduled_tokens=int(n),prompt_tokens=plen,
            computed_tokens=computed,
            decode=bool(computed>=plen) if computed is not None and plen is not None else None))
    row=dict(kind='worker_step',time_ns=time.time_ns(),pid=os.getpid(),
        host=socket.gethostname(),device=str(runner.device),
        batch_size=len(rows),scheduled_tokens=int(out.total_num_scheduled_tokens),
        all_decode=bool(rows) and all(x['decode'] is True for x in rows),requests=rows)
    path=root/f'{socket.gethostname()}-{os.getpid()}.jsonl'
    buf=_buffers.setdefault(path,[]); buf.append(json.dumps(row,default=str)+'\n')
    global _last_flush
    now=time.monotonic()
    if len(buf)>=32 or now-_last_flush>=0.25:
        flush(); _last_flush=now
PD_FILE_EOF
```


## 6. D 容器：停止旧 D，启用诊断后重新启动

在原 D 启动终端按 Ctrl+C，等待旧 D 服务退出。若 D 原本由后台或管理系统启动，先通过该系统正常停止。确认旧 D 的 NPU 进程退出、资源释放后继续。P 模型保持运行。

在 D 容器的新终端执行：

```bash
source /tmp/pdtest/test.env
python "$WORK/audit_patch.py"
python "$WORK/patch_steps.py"
mkdir -p "$RUN/d-steps" "$RUN/d-transfer" "$RUN/d-capacity" "$RUN/d-profiles"

nohup env \
  LOCAL_IP="$D_IP" NIC="$D_NIC" MODEL="$D_MODEL" \
  PYTHONPATH="$WORK${PYTHONPATH:+:$PYTHONPATH}" \
  AUDIT_STEP_DIR="$RUN/d-steps" \
  AUDIT_TRANSFER_DIR="$RUN/d-transfer" \
  AUDIT_CAPACITY_DIR="$RUN/d-capacity" \
  PROFILE=1 PROFILE_DIR="$RUN/d-profiles" \
  bash "$WORK/start_d.sh" \
  > "$RUN/decode-diagnostic.log" 2>&1 &
echo $! > "$RUN/decode-diagnostic.pid"
```

遇到 `Source mismatch`、anchor 不匹配或补丁报错时，停止执行，保留报错和备份文件，不要继续启动。

## 7. P 容器：检查 P/D，然后只在 P 启动代理

```bash
source /tmp/pdtest/test.env
python "$WORK/wait_ready.py" \
  --url "http://$P_IP:8100/health" \
  --url "http://$D_IP:8200/health" \
  --timeout 3600

nohup python "$WORK/proxy.py" \
  --host 0.0.0.0 --port 1999 --workers 1 --max-retries 1 \
  --prefiller-hosts "$P_IP" --prefiller-ports 8100 \
  --decoder-hosts "$D_IP" --decoder-ports 8200 \
  > "$RUN/proxy.log" 2>&1 &
echo $! > "$RUN/proxy.pid"

python "$WORK/wait_ready.py" \
  --url http://127.0.0.1:1999/healthcheck --timeout 120
curl --noproxy '*' -fsS http://127.0.0.1:1999/healthcheck
```

确认代理健康响应中 P/D 后端已经就绪。启动失败时查看：

```bash
tail -n 100 "$RUN/proxy.log"
```

## 8. 两台宿主机：启动 HBM、CPU、网卡采样

在每台宿主机各执行下面代码。P 宿主机设置 `ROLE=P`，D 宿主机设置 `ROLE=D`。RUN_ID 与容器中一致。

```bash
export RUN_ID=pd128k_p_proxy_001
export ROLE=P
export HOST_OUT="/data/pdtest-results/$RUN_ID/$ROLE"
mkdir -p "$HOST_OUT"

cat > "$HOST_OUT/monitor.sh" <<'PD_MONITOR_EOF'
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
PD_MONITOR_EOF

nohup bash "$HOST_OUT/monitor.sh" > "$HOST_OUT/host.log" 2>&1 &
echo $! > "$HOST_OUT/monitor.pid"
```

P 宿主机如果有 `pidstat`，再开启每秒的进程 CPU 采样；D 宿主机也可以同样执行：

```bash
if command -v pidstat >/dev/null 2>&1; then
  nohup pidstat -u -r -p ALL 1 > "$HOST_OUT/pidstat.log" 2>&1 &
  echo $! > "$HOST_OUT/pidstat.pid"
fi
```

记录 P 上代理、压测客户端与 P 模型进程的 CPU 使用情况。若发生 CPU 排队、代理响应异常或通信网卡饱和，先标记该轮无效，调整资源后新建 RUN_ID 重跑。两秒 HBM 采样的最大值只填写为“采样最大值”。

## 9. P 容器：启动 P/D 指标采集

```bash
source /tmp/pdtest/test.env
cat > "$RUN/endpoints.json" <<PD_ENDPOINTS_EOF
[
  {"role":"P","url":"http://$P_IP:8100"},
  {"role":"D","url":"http://$D_IP:8200"}
]
PD_ENDPOINTS_EOF

nohup python "$WORK/metrics_monitor.py" \
  --endpoints "$RUN/endpoints.json" \
  --out "$RUN/metrics.jsonl" --interval 2 \
  > "$RUN/metrics-monitor.log" 2>&1 &
echo $! > "$RUN/metrics-monitor.pid"
```

## 10. P 容器：短请求检查完整 PD 链路

```bash
curl --noproxy '*' -N --fail-with-body \
  http://127.0.0.1:1999/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"dsv4","prompt":"Explain CPU caches.","max_tokens":64,"min_tokens":64,"ignore_eos":true,"temperature":0,"stream":true,"stream_options":{"include_usage":true}}'
```

看到持续输出和最终 `[DONE]` 后继续。D 容器同时检查采样文件已经产生：

```bash
source /tmp/pdtest/test.env
ls -l "$RUN/d-steps" "$RUN/d-transfer" "$RUN/d-capacity"
```

D transfer 目录应出现非空 JSONL。纯 D 审计还要求存在成功的非零字节 KV read 记录；文件为空时先检查当前 connector 路径和日志，不要直接接受纯 D 结果。

## 11. P 容器：生成精确 128K 输入并校验 1K 输出

```bash
source /tmp/pdtest/test.env
python "$WORK/generate_prompts.py" \
  --model "$P_MODEL" --length 131072 --out "$RUN/prompt-128k.json"

python "$WORK/bench_client.py" \
  --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
  --prompts "$RUN/prompt-128k.json" --mode wave \
  --concurrency 1 --requests 1 --output-length 1024 \
  --return-token-ids --min-samples 1 --timeout 7200 \
  --out "$RUN/length-check.json"
```

确认 `input_tokens=131072`、`output_tokens=1024`、`valid_requests=1`、`all_phase_failures=0`。长度校验失败时停止。

## 12. P 容器：预热三次

```bash
for r in 1 2 3; do
  python "$WORK/bench_client.py" \
    --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
    --prompts "$RUN/prompt-128k.json" --mode wave \
    --concurrency 1 --requests 1 --output-length 1024 \
    --min-samples 1 --timeout 7200 --out "$RUN/warmup-r${r}.json" || break
done
```

三次全部成功后继续。

## 13. P 容器：扫描并发，寻找实际固定 D Batch Size

下面每个 C 只同时发 C 个请求，每个候选重复三次。失败时脚本停止，不继续增加并发。候选区间不成立会留下说明，仍然允许后续候选继续。`C` 在本步骤始终记录为客户端并发。

```bash
cat > "$WORK/scan_waves.sh" <<'PD_SCAN_EOF'
#!/usr/bin/env bash
set -euo pipefail
source /tmp/pdtest/test.env
mkdir -p "$RUN/waves"
for C in 1 2 4 8 16 24 32; do
  for r in 1 2 3; do
    prefix="$RUN/waves/C${C}-r${r}"
    python "$WORK/bench_client.py" \
      --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
      --prompts "$RUN/prompt-128k.json" --mode wave \
      --concurrency "$C" --requests "$C" --output-length 1024 \
      --return-token-ids --min-samples "$C" --timeout 7200 \
      --out "${prefix}.json"
    if python "$WORK/pd_plateau.py" \
        --summary "${prefix}.json" --out "${prefix}.window.json"; then
      echo "候选窗口成立 C=$C repeat=$r；仍需检查 D worker 和 transfer"
    else
      echo "候选窗口未成立 C=$C repeat=$r；不能据此确认 BS"
    fi
  done
done
PD_SCAN_EOF
bash "$WORK/scan_waves.sh" > "$RUN/scan.log" 2>&1
```

查看进度：

```bash
tail -n 100 "$RUN/scan.log"
```

若没有任意候选形成满足要求的固定 D 区间，报告“本次 128K/1K 未形成有效纯 D 稳态窗口”。保持输出 1024，不要改成 8K 后沿用本实验名称。

## 14. D 宿主机：导出第一份 D 采样快照

先确认第 13 步结束，P 不再发请求。保持 D 模型运行。下面在 D 宿主机执行，修改容器名；本步骤之后不要再运行诊断压测，直到第 16 步完成。

```bash
export RUN_ID=pd128k_p_proxy_001
export D_CONTAINER=填写D容器名
export D_EXPORT="/data/pdtest-results/$RUN_ID/D/discovery"
mkdir -p "$D_EXPORT"
docker cp "$D_CONTAINER:/tmp/pdtest/runs/$RUN_ID/." "$D_EXPORT/"
```

这里审核的是已结束请求的中段区间。最终完整 worker 日志在第 20 步停止 D 后再次导出，避免将尚未刷出的末尾记录当成丢失数据。

## 15. P 宿主机：把 D 采样快照传到 P 容器

下面在 P 宿主机执行。`D_SSH_HOST` 填 D 的 SSH 管理地址，可与通信 D_IP 不同。P_CONTAINER 填 P 容器实际名称。

```bash
export RUN_ID=pd128k_p_proxy_001
export P_CONTAINER=填写P容器名
export SSH_USER=填写SSH用户名
export D_SSH_HOST=填写D宿主机SSH地址
export P_IMPORT="/data/pdtest-results/$RUN_ID/import-discovery"
mkdir -p "$P_IMPORT"
scp -r "$SSH_USER@$D_SSH_HOST:/data/pdtest-results/$RUN_ID/D/discovery/." "$P_IMPORT/"
docker exec "$P_CONTAINER" mkdir -p "/tmp/pdtest/runs/$RUN_ID/d-export-discovery"
docker cp "$P_IMPORT/." "$P_CONTAINER:/tmp/pdtest/runs/$RUN_ID/d-export-discovery/"
```

不使用跨机器 SSH 时，通过你们批准的文件传输工具传递同一目录；保持目录层级，最后一条 `docker cp` 仍需执行。不得假设容器内路径能直接通过宿主机 SSH 读取。

## 16. P 容器：核对真实 BS 并保存选择

```bash
source /tmp/pdtest/test.env
for C in 1 2 4 8 16 24 32; do
  passed=0
  for r in 1 2 3; do
    prefix="$RUN/waves/C${C}-r${r}"
    if [[ ! -f "${prefix}.window.json" ]]; then
      continue
    fi
    if python "$WORK/audit_window.py" \
      --steps "$RUN/d-export-discovery/d-steps" \
      --transfer "$RUN/d-export-discovery/d-transfer" \
      --summary "${prefix}.window.json" --run 0 --expected-workers 8 \
      > "${prefix}.audit.json"; then
      passed=$((passed+1))
    fi
  done
  printf 'C=%s，通过 %s/3 次\n' "$C" "$passed"
done
```

只有三次均通过的候选，才允许作为本次确认的 BS。查看对应 audit 文件，确认 8 个 worker、固定目标 Batch Size、全部 decode、至少 100 个 worker step、有效窗口长度、存在成功 KV read、窗口内无记录到的 KV read。

以下 `8` 只是示例，必须换成上面三次通过的候选：

```bash
export CANDIDATE=8
python "$WORK/write_bs.py" --directory "$RUN/waves" \
  --candidate "$CANDIDATE" --out "$RUN/selection.env"
source "$RUN/selection.env"
echo "本轮确认的 D Batch Size：$BS"
```

若任意一次未通过，选择脚本会拒绝生成 selection.env。仍需通过第 19 步的 trace 核对传输算子和实际重算。纯 D 窗口的吞吐和等效 TPOT 在对应 `.window.json` 中；这是按客户端 token 事件得到的诊断估计，不能替代 NPU 算子计时。

## 17. P 容器：诊断阶段单并发和 C=BS 的 TTFT 对比

下面同一诊断配置下执行单并发 30 次和 C=BS 的请求波次 10 次。全部通过后才汇总。

```bash
cat > "$WORK/run_ttft.sh" <<'PD_TTFT_EOF'
#!/usr/bin/env bash
set -euo pipefail
source /tmp/pdtest/test.env
source "$RUN/selection.env"
phase=${1:?填写阶段目录名，例如 diagnostic 或 formal}
base="$RUN/$phase"
test ! -e "$base" || { echo "目录已存在：$base；请使用新的阶段名"; exit 1; }
mkdir -p "$base/single" "$base/batch"
for r in 1 2 3; do
  python "$WORK/bench_client.py" \
    --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
    --prompts "$RUN/prompt-128k.json" --mode wave \
    --concurrency 1 --requests 1 --output-length 1024 \
    --min-samples 1 --timeout 7200 --out "$base/warmup-r${r}.json"
done
for r in $(seq 1 30); do
  python "$WORK/bench_client.py" \
    --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
    --prompts "$RUN/prompt-128k.json" --mode wave \
    --concurrency 1 --requests 1 --output-length 1024 \
    --min-samples 1 --timeout 7200 --out "$base/single/r${r}.json"
done
for r in $(seq 1 10); do
  python "$WORK/bench_client.py" \
    --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
    --prompts "$RUN/prompt-128k.json" --mode wave \
    --concurrency "$BS" --requests "$BS" --output-length 1024 \
    --min-samples "$BS" --timeout 7200 --out "$base/batch/r${r}.json"
done
python "$WORK/check_runs.py" --directory "$base/single" --runs 30 --concurrency 1 --mode wave
python "$WORK/check_runs.py" --directory "$base/batch" --runs 10 --concurrency "$BS" --mode wave
python "$WORK/compare_ttft.py" \
  --single "$base"/single/r*.json --batch "$base"/batch/r*.json --bs "$BS" \
  > "$base/ttft-comparison.json"
cat "$base/ttft-comparison.json"
PD_TTFT_EOF
bash "$WORK/run_ttft.sh" diagnostic > "$RUN/ttft-diagnostic.log" 2>&1
```

这里记录的是“客户端并发数设置成已确认 BS 的数值”。本轮请求仍要查看 D 的实际 batch 分布，不能直接给 TTFT 结论加上“P 或 D 实际 batch 一定等于 BS”的前提。

## 18. P 容器：诊断阶段持续稳态，记录实际 D Batch Size 分布

```bash
cat > "$WORK/run_steady.sh" <<'PD_STEADY_EOF'
#!/usr/bin/env bash
set -euo pipefail
source /tmp/pdtest/test.env
source "$RUN/selection.env"
phase=${1:?填写阶段名}
base="$RUN/$phase/steady"
test ! -e "$base" || { echo "目录已存在：$base"; exit 1; }
mkdir -p "$base"
for r in 1 2 3; do
  python "$WORK/bench_client.py" \
    --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
    --prompts "$RUN/prompt-128k.json" --mode steady \
    --concurrency "$BS" --output-length 1024 \
    --burnin 300 --duration "${DURATION:-1800}" --min-samples 30 --timeout 7200 \
    --out "$base/r${r}.json"
done
python "$WORK/check_runs.py" --directory "$base" \
  --runs 3 --concurrency "$BS" --mode steady
PD_STEADY_EOF
bash "$WORK/run_steady.sh" diagnostic > "$RUN/steady-diagnostic.log" 2>&1
```

要求三次全部 PASS。若提示样本不足或窗口末尾截断比例超过 10%，将 DURATION 延长，例如下面命令，并使用新的阶段名保存重测结果：

```bash
DURATION=3600 bash "$WORK/run_steady.sh" diagnostic-long \
  > "$RUN/steady-diagnostic-long.log" 2>&1
```

持续 PD 压测的 TTFT/TPOT 填为 PD 端到端稳态结果；纯 D 固定窗口结果单独从第 16 步填写。

## 19. P 容器：采集一段 D trace

先等待其他压测结束。下面开启 D profiler，只发一组 C=BS 的诊断请求，完成后立即关闭。即使请求失败，也会发送停止 profiling 请求。

```bash
source /tmp/pdtest/test.env
source "$RUN/selection.env"
curl --noproxy '*' --fail-with-body -X POST "http://$D_IP:8200/start_profile"
if python "$WORK/bench_client.py" \
    --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
    --prompts "$RUN/prompt-128k.json" --mode wave \
    --concurrency "$BS" --requests "$BS" --output-length 1024 \
    --return-token-ids --min-samples "$BS" --timeout 7200 \
    --out "$RUN/profile-wave.json"; then
  profile_request_status=0
else
  profile_request_status=$?
fi
curl --noproxy '*' --fail-with-body -X POST "http://$D_IP:8200/stop_profile"
echo "profile request exit=$profile_request_status"
```

用 MindStudio Insight 打开导出的 D trace。定位请求的共同解码中段，检查实际 KV read/传输、重算和抢占活动；记录开始/结束位置和截图。若 trace 有这些活动，不能把相应区间写成纯 D。profiler 执行失败则填写“trace 未完成”，不要只凭 worker 审计写“已完成全部纯 D 验证”。带 profiler 的请求不用于正式 TTFT/TPOT 成绩。

## 20. D 容器和两台宿主机：停止诊断 D，导出最终采样

确认所有请求完成后，在 D 容器执行，只停止本手册保存的诊断 D PID：

```bash
source /tmp/pdtest/test.env
ps -p "$(cat "$RUN/decode-diagnostic.pid")" -o pid,args
kill -TERM "$(cat "$RUN/decode-diagnostic.pid")"
```

等待 D 完全退出并释放 NPU，保留 P 模型和 P 代理。然后在 D 宿主机导出：

```bash
export RUN_ID=pd128k_p_proxy_001
export D_CONTAINER=填写D容器名
export D_EXPORT="/data/pdtest-results/$RUN_ID/D/final"
mkdir -p "$D_EXPORT"
docker cp "$D_CONTAINER:/tmp/pdtest/runs/$RUN_ID/." "$D_EXPORT/"
```

在 P 宿主机执行：

```bash
export RUN_ID=pd128k_p_proxy_001
export P_CONTAINER=填写P容器名
export SSH_USER=填写SSH用户名
export D_SSH_HOST=填写D宿主机SSH地址
export P_IMPORT="/data/pdtest-results/$RUN_ID/import-final"
mkdir -p "$P_IMPORT"
scp -r "$SSH_USER@$D_SSH_HOST:/data/pdtest-results/$RUN_ID/D/final/." "$P_IMPORT/"
docker exec "$P_CONTAINER" mkdir -p "/tmp/pdtest/runs/$RUN_ID/d-export-final"
docker cp "$P_IMPORT/." "$P_CONTAINER:/tmp/pdtest/runs/$RUN_ID/d-export-final/"
```

## 21. P 容器：汇总最终 Batch Size、HBM 池和诊断结果

```bash
source /tmp/pdtest/test.env
source "$RUN/selection.env"
for r in 1 2 3; do
  python "$WORK/report_batches.py" \
    --steps "$RUN/d-export-final/d-steps" \
    --summary "$RUN/diagnostic/steady/r${r}.json" \
    --out "$RUN/diagnostic/steady/r${r}.batches.json"
done
python "$WORK/capacity_table.py" \
  --directory "$RUN/d-export-final/d-capacity" \
  --out "$RUN/d-capacity.csv"
```

如果第 18 步最终采用 diagnostic-long，以上路径中的 diagnostic 必须统一改成 diagnostic-long。

使用最终导出日志重新核对选中的三次发现窗口：

```bash
for r in 1 2 3; do
  python "$WORK/audit_window.py" \
    --steps "$RUN/d-export-final/d-steps" \
    --transfer "$RUN/d-export-final/d-transfer" \
    --summary "$RUN/waves/C${BS}-r${r}.window.json" \
    --run 0 --expected-workers 8 \
    > "$RUN/waves/C${BS}-r${r}.audit-final.json" || break
done
```

三次最终审计都通过后保留 BS。审计失败则停止正式 C=BS 测试，先处理原因。

同时整理：8 张 NPU 的 HBM 总量/采样已用量/剩余量、启动 KV 池容量、同一测试窗口内带原始 labels 的 KV 使用率指标、等待请求与抢占计数。`d-capacity.csv` 是 graph capture 前的启动快照，运行占用以对应时间的宿主机采样为准。预留 KV 池不等于其中 token block 已用满；不要将不同指标盲目相加。缺少可核实 KV 使用率时，标记“KV 池占用待确认”。

## 22. D 容器：恢复源码，关闭采样，启动正式 D

确认第 20 步的 D 已完全退出，按相反顺序恢复补丁：

```bash
source /tmp/pdtest/test.env
python "$WORK/patch_steps.py" --restore
python "$WORK/audit_patch.py" --restore
unset AUDIT_STEP_DIR AUDIT_TRANSFER_DIR AUDIT_CAPACITY_DIR

nohup env \
  LOCAL_IP="$D_IP" NIC="$D_NIC" MODEL="$D_MODEL" PROFILE=0 \
  AUDIT_STEP_DIR= AUDIT_TRANSFER_DIR= AUDIT_CAPACITY_DIR= \
  bash "$WORK/start_d.sh" \
  > "$RUN/decode-formal.log" 2>&1 &
echo $! > "$RUN/decode-formal.pid"
```

保持与诊断阶段相同的模型、TP、DP、MTP、max-model-len、max-num-seqs、token budget 和 HBM 配置。正式阶段不宣称直接采到了实际 BS；实际 BS 证据来自诊断阶段。

## 23. P 容器：正式 TTFT 和稳态性能

```bash
source /tmp/pdtest/test.env
python "$WORK/wait_ready.py" --url "http://$D_IP:8200/health" --timeout 3600
bash "$WORK/run_ttft.sh" formal > "$RUN/ttft-formal.log" 2>&1
bash "$WORK/run_steady.sh" formal > "$RUN/steady-formal.log" 2>&1
cat "$RUN/formal/ttft-comparison.json"
```

仅在所有检查 PASS、CPU/网络未成为额外瓶颈、没有实际重算/抢占/服务错误时填写正式成绩。不要把诊断单并发和正式并发混在一次比例计算中。

## 24. P 容器：填写实验记录

```bash
cat > "$RUN/RESULT.md" <<'PD_RESULT_EOF'
# 实验记录

- RUN_ID：
- P/D 型号、每卡 HBM、驱动/CANN/镜像版本：
- 模型权重名称/校验信息：
- P/D IP 与通信网卡；代理位置=P；压测客户端位置=P：
- 两台宿主机时钟同步记录：
- 输入=131072；输出=1024；MTP=1；TP=8；DP=1：
- D 实际固定 Batch Size BS；三次诊断窗口/审计路径：
- trace 核对结果（完成/未完成、证据路径）：
- 纯 D 窗口吞吐和等效 TPOT（客户端事件诊断估计）：
- 8 个 worker 持续压测的 BS 最小值/P50/P95/最大值：
- 8 张 D NPU 的 HBM 总量/采样已用量/剩余量：
- KV 池容量、KV 使用率指标名称及 labels、是否可确认接近用满：
- P CPU/代理/客户端/网卡负载；D CPU/网卡负载：
- 正式单并发平均 TTFT：
- 正式 C=BS 的平均 TTFT、最慢请求 TTFT：
- 实测平均 TTFT 倍数、相对于 BS 倍预测的偏差：
- 三次正式持续压测 TTFT/TPOT/吞吐、样本数、截断比例：
- 失败、重算、抢占记录：
- 本轮未完成项目：
PD_RESULT_EOF
```

HBM 有余量时也按实测填写 TTFT 倍数，不要预填“等于 BS 倍”。本实验未采集 P worker 的实际 prefill batch；需要把 TTFT 变化归因于 P 的批处理或队列时，另行采集 P worker/调度和 trace 证据。

## 25. 停止采集并归档

P 容器停止本文启动的指标采集；代理若继续使用则保留：

```bash
source /tmp/pdtest/test.env
kill -TERM "$(cat "$RUN/metrics-monitor.pid")"
# 不再使用代理时再执行下一条：
# kill -TERM "$(cat "$RUN/proxy.pid")"
```

两台宿主机各执行，ROLE 按当前机器填写 P 或 D：

```bash
export RUN_ID=pd128k_p_proxy_001
export ROLE=P
export HOST_OUT="/data/pdtest-results/$RUN_ID/$ROLE"
kill -TERM "$(cat "$HOST_OUT/monitor.pid")"
if [[ -f "$HOST_OUT/pidstat.pid" ]]; then
  kill -TERM "$(cat "$HOST_OUT/pidstat.pid")"
fi
```

P 宿主机导出最终 P 结果和全部脚本：

```bash
export RUN_ID=pd128k_p_proxy_001
export P_CONTAINER=填写P容器名
export ARCHIVE_ROOT="/data/pdtest-results/$RUN_ID"
mkdir -p "$ARCHIVE_ROOT/P/container-work"
docker cp "$P_CONTAINER:/tmp/pdtest/." "$ARCHIVE_ROOT/P/container-work/"
tar -C /data/pdtest-results -czf "/data/${RUN_ID}-P-results.tar.gz" "$RUN_ID"
```

D 宿主机同样导出，包括正式 D 日志与完整脚本：

```bash
export RUN_ID=pd128k_p_proxy_001
export D_CONTAINER=填写D容器名
export ARCHIVE_ROOT="/data/pdtest-results/$RUN_ID"
mkdir -p "$ARCHIVE_ROOT/D/container-work"
docker cp "$D_CONTAINER:/tmp/pdtest/." "$ARCHIVE_ROOT/D/container-work/"
tar -C /data/pdtest-results -czf "/data/${RUN_ID}-D-results.tar.gz" "$RUN_ID"
```

如果正式 D 服务继续运行，注明其服务日志导出时间；不要删除容器。只有在不再需要服务且已保存结果时，才通过对应 PID/管理系统正常停止服务。

## 26. 交付前逐项确认

- [ ] 代理只在 P；客户端只在 P；D 没有遗留代理或压测任务。
- [ ] 机器上没有执行 Git 或源码下载命令；全部脚本均来自本文完整内容。
- [ ] P/D 版本、权重、并行、MTP、HBM 配置已经保存。
- [ ] 128K 输入和 1K 输出长度校验通过。
- [ ] 客户端 C 与实际 D Batch Size 分别记录。
- [ ] 选择的 BS 通过三次发现窗口及最终 8-worker 审计。
- [ ] 纯 D trace 已核对；未完成时明确填写未完成。
- [ ] HBM 采样占用、KV 池预留量和逻辑使用率分别填写。
- [ ] 正式阶段关闭诊断采样并重新预热，单并发/并发使用同一阶段结果。
- [ ] P CPU 和网络负载没有被忽略，失败/重算/抢占记录已核对。
- [ ] P/D 两份宿主机记录、完整脚本、请求原始数据、trace 和结果已归档。

## 来源

代理脚本来自 [vLLM-Ascend v0.23.0 官方 PD 示例](https://github.com/vllm-project/vllm-ascend/blob/v0.23.0/examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py)，本文已给出完整内容，目标机器不需要访问该页面。模型和部署参数沿用 [v0.23.0 DeepSeek-V4-Flash 教程](https://github.com/vllm-project/vllm-ascend/blob/v0.23.0/docs/source/tutorials/models/DeepSeek-V4-Flash.md)。测试与诊断脚本为本次工程辅助代码；静态检查不等于 A2 实机验证。
