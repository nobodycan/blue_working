# v0.2.0：昇腾 A2 双机 PD 测试，纯 D 平均 TPOT ≤30 ms / ≤50 ms

本 README 已合并《v030rc2_采样适配_全部文件与修改内容.md》，替换旧的 v0.23.0 采样安装方式。下面从“两个 A2 容器已创建”开始，包含 P/D 启动、代理、全部 Python/Shell 文件内容、两档 TPOT 筛选、HBM、TTFT 对比和正式测试步骤。目标机器不需要 Git。所有脚本都用本文中的 `cat > 文件名 <<'EOF'` 创建，不依赖其他文档中未给出的脚本。

本版本只需这一份 README.md：涉及的 Python、Shell 脚本全文和创建命令均已内嵌。按对应执行位置复制代码块即可，无需下载脚本或压缩包。

第10步若出现 `Failed to connect to target`、`103900 / E19999` 或 `MatchEndpoints failed timeout`，先按 [network-diagnostics.md](network-diagnostics.md) 排查，确认 KV 传输成功后再继续性能测试。

本机没有你的 A2；已核对适配稿、相关版本源码和文档脚本的静态一致性，未执行本轮 A2 测试，不预填任何性能结果。

## 0. 先固定这次测试的口径和执行位置

| 项目 | 本次固定设置 |
|---|---|
| 软件 | P、D 均为 vLLM 0.30.0 + vLLM-Ascend 0.30.0rc2，核对同一镜像构建 |
| 模型 | 原版 `DeepSeek-V4-Flash-w8a8-mtp`；0731/DSpark 权重不能仅改模型路径后套用 |
| 硬件/并行 | 每台 8 张 A2 NPU；每实例 TP=8、DP=1、EP 开启 |
| 输入/输出 | 精确输入 131072 token，精确输出 1024 token |
| Runner | 本套采样只适配 MRV1；P、D 显式设置 `VLLM_USE_V2_MODEL_RUNNER=0` |
| MTP | 1 个 speculative token；按实际接受的输出 token 计数 |
| Prefix cache / offload | 本轮关闭 prefix cache，不增加 CPU KV/权重 offload |
| P 服务 / D 服务 | P 容器 8100 / D 容器 8200 |
| 代理 / 客户端 | 都在 P 容器；测试请求发 `http://127.0.0.1:1999` |
| 诊断改源码 | 只修改 D 容器的 runner 和 Mooncake connector；正式前统一恢复 |
| 目标档位 | **纯 D 实例平均 TPOT ≤30 ms**、**≤50 ms** 两档 |

两档是上限，不要求实测必须等于 30/50，不通过 sleep 或限速将延迟凑成目标。每个候选 B 重复三次；本 README 要求三次各自的窗口平均 TPOT 均不超过该档上限。

本 README 用经过 D worker/传输审计的共同解码窗口做筛选：`实例窗口等效平均 TPOT(ms) = 1000 × B × 窗口秒数 ÷ 窗口实际输出 token 数`。这是整个 TP8 实例的有效输出口径；输出只计一次，不把八张卡当成八个独立实例相加或再次求平均。开启 MTP 时不能用迭代数代替输出 token 数。

该时间来自 P 客户端的输出事件，须同时检查 CPU/网络和 NPU trace。逐卡算子时间、NPU 忙碌时间、客户端完整请求 TPOT另列，不替代这里的筛选值。`step_probe.py` 记录的 CPU 入口时间不等于 NPU 迭代执行耗时。

最终分别得到 `BS_30`、`BS_50`，含义是：在本轮固定配置、已扫描候选中，满足对应上限且证据通过的最大已验证 D Batch Size。只做粗扫时不能宣称全局最大；MAX_SEQS=32 也不能外推 BS>32。B30 与 B50 可能相同；某档没有合格候选时记录“未测得”，不借用另一档或其他输入长度的数据。

**MRV1 是本轮实验配置选择。**MRV1 诊断与正式结果不能冒充默认 MRV2 性能。如果你需要保留 MRV2，先不要运行本 README 的采样安装与启动脚本。

## 1. 两台宿主机：记录容器、网络和时钟

P、D 宿主机分别执行并保留输出：

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
ip -br addr
timedatectl show -p NTPSynchronized --value
date -u +'%Y-%m-%dT%H:%M:%S.%NZ'
if command -v chronyc >/dev/null 2>&1; then chronyc tracking; fi
```

确认时钟同步后再做跨机器窗口审计；记录 chrony 的 offset/误差，不能只用日期字符串判断同步。检查各自容器网络，替换容器名：

```bash
docker inspect -f '{{.HostConfig.NetworkMode}}' 容器名
docker inspect -f '{{.Config.Image}} {{.Image}}' 容器名
```

后续按 host 网络编写。核对 P/D 的通信 IP、对应网卡、实际 NPU 型号及每卡 HBM、驱动/CANN/NNAL 兼容信息。两侧模型权重和量化配置也须一致。不要只凭包版本字符串判断镜像完全相同。

## 2. 两个容器：创建本轮唯一配置

在两个容器各执行一次。修改 IP、网卡和容器内模型路径。两边 RUN_ID 相同，使用新的 WORK 目录避免旧脚本混入。

```bash
mkdir -p /tmp/pdtest030_slo
cat > /tmp/pdtest030_slo/test.env <<'PD_ENV_EOF'
export WORK=/tmp/pdtest030_slo
export RUN_ID=pd030_tpot30_50_001
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
export KV_BYTES=
export NO_PROXY="${NO_PROXY:-},127.0.0.1,localhost,$P_IP,$D_IP"
export no_proxy="$NO_PROXY"
PD_ENV_EOF
source /tmp/pdtest030_slo/test.env
test ! -e "$RUN" || { echo 'RUN_ID 已存在，请换新 RUN_ID'; exit 1; }
mkdir -p "$RUN"
cp "$WORK/test.env" "$RUN/test.env"
python - <<'PY'
from importlib.metadata import version
for name in ('vllm', 'vllm-ascend', 'torch', 'torch-npu'):
    print(name, version(name))
assert version('vllm').split('+')[0] == '0.30.0'
assert version('vllm-ascend').split('+')[0] == '0.30.0rc2'
PY
```

每次新开容器终端只恢复变量：

```bash
source /tmp/pdtest030_slo/test.env
```

不要重复执行 RUN_ID 存在性检查。在生成正式结果前，保留镜像 ID、模型配置和权重校验记录、所有启动参数。参数、权重、KV dtype、MTP、图模式或 HBM 配置变更后，使用新 RUN_ID 重新筛选两档 BS。

## 3. 两个容器：创建完整启动 Shell

每个容器都可以创建两个 Shell；P 只执行 start_p，D 只执行 start_d。此处只写文件，暂不启动。

### start_p.sh

```bash
cat > "$WORK/start_p.sh" <<'PD_FILE_START_P_SH_EOF'
#!/usr/bin/env bash
# Run inside the Prefill container: vLLM 0.30.0 + vLLM-Ascend 0.30.0rc2, MRV1, 8 NPUs.
# Example: LOCAL_IP=10.10.0.11 NIC=eth0 MODEL=/models/DeepSeek-V4-Flash-w8a8-mtp bash start_p.sh
set -euo pipefail

# Fixed MRV1 diagnostic baseline; also retain for formal comparisons.
export VLLM_USE_V2_MODEL_RUNNER=0

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
PD_FILE_START_P_SH_EOF
```

### start_d.sh

```bash
cat > "$WORK/start_d.sh" <<'PD_FILE_START_D_SH_EOF'
#!/usr/bin/env bash
# Run inside the Decode container: vLLM 0.30.0 + vLLM-Ascend 0.30.0rc2, MRV1, 8 NPUs.
# Example: LOCAL_IP=10.10.0.12 NIC=eth0 MODEL=/models/DeepSeek-V4-Flash-w8a8-mtp bash start_d.sh
set -euo pipefail

# Fixed MRV1 diagnostic baseline; also retain for formal comparisons.
export VLLM_USE_V2_MODEL_RUNNER=0

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
PD_FILE_START_D_SH_EOF
```


## 4. P 容器：创建代理、压测和分析脚本的全部内容

下面均在 P 容器执行。代理基于官方 v0.30.0rc2 示例，并做了三项测试约束：P 侧不回显 128K prompt token IDs；P 不返回 KV 元信息时请求失败；遇到 recomputed 时让客户端拒绝该请求。保留原 Apache-2.0 标记。D 仍按诊断请求返回实际输出 token IDs。

### proxy.py

```bash
cat > "$WORK/proxy.py" <<'PD_FILE_PROXY_PY_EOF'
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
    prefiller_cached_tokens: int | None = None
    decoder_released: bool = False


TAINT_PRIORITY = 1e15


def extract_cached_tokens(response_json: dict) -> int | None:
    usage = response_json.get("usage") or {}
    prompt_tokens_details = usage.get("prompt_tokens_details") or {}
    cached_tokens = prompt_tokens_details.get("cached_tokens")
    return cached_tokens if isinstance(cached_tokens, int) else 0


def update_cached_tokens_in_chunk(chunk_json: dict, cached_tokens: int | None) -> bool:
    if cached_tokens is None:
        return False
    usage = chunk_json.get("usage")
    if not isinstance(usage, dict):
        return False
    prompt_tokens_details = usage.get("prompt_tokens_details")
    if not isinstance(prompt_tokens_details, dict):
        prompt_tokens_details = {}
    usage["prompt_tokens_details"] = prompt_tokens_details
    prompt_tokens_details["cached_tokens"] = cached_tokens
    return True


def encode_response_chunk(chunk_json: dict, is_sse: bool) -> bytes:
    chunk = json.dumps(chunk_json, ensure_ascii=False).encode("utf-8")
    return b"data: " + chunk + b"\n\n" if is_sse else chunk


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
    # PD_BENCH: only D returns token IDs; avoid echoing 128K prompt IDs from P.
    payload["return_token_ids"] = False
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
    max_attempts = max(1, max_retries)
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.post(endpoint, json=req_data, headers=headers)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400 or attempt == max_attempts:
                raise
            logger.warning("Attempt %s failed for %s: %s", attempt, endpoint, exc)
        except httpx.RequestError as exc:
            if attempt == max_attempts:
                raise
            logger.warning("Attempt %s failed for %s: %s", attempt, endpoint, exc)
        await asyncio.sleep(base_delay * (2 ** (attempt - 1)))


async def stream_service_response(
    client: httpx.AsyncClient,
    endpoint: str,
    req_data: dict,
    request_id: str,
    max_retries: int = 3,
    base_delay: float = 0.2,
):
    headers = auth_headers(request_id)
    max_attempts = max(1, max_retries)
    for attempt in range(1, max_attempts + 1):
        first_chunk_sent = False
        try:
            async with client.stream("POST", endpoint, json=req_data, headers=headers) as response:
                if response.status_code >= 400:
                    # Buffer the upstream error body so callers can forward the real
                    # cause (e.g. vLLM's "maximum context length" message) instead of
                    # dropping it into an empty 200. The body is unreadable once this
                    # streaming context exits, so read it before raising.
                    await response.aread()
                    response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    first_chunk_sent = True
                    yield chunk
                return
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400 or attempt == max_attempts:
                raise
            logger.warning("Attempt %s failed for streaming %s: %s", attempt, endpoint, exc)
        except httpx.RequestError as exc:
            if first_chunk_sent:
                logger.error("Streaming to client interrupted after response started: %s", exc)
                return
            if attempt == max_attempts:
                raise
            logger.warning("Attempt %s failed for streaming %s: %s", attempt, endpoint, exc)
        except Exception as exc:
            if first_chunk_sent:
                logger.error("Streaming to client interrupted after response started: %s", exc)
                return
            if attempt == max_attempts:
                raise
            logger.warning("Attempt %s failed for streaming %s: %s", attempt, endpoint, exc)
        await asyncio.sleep(base_delay * (2 ** (attempt - 1)))


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
        None if info.decoder_released else info.decoder_key,
        info.decoder_score,
        release_prefill_kv,
    )
    info.decoder_released = True


async def _release_decoder_once(runtime: WorkerRuntime, info: InstanceInfo) -> None:
    if info.decoder_released:
        return
    await runtime.schedule("release_decoder", info.decoder_key, info.decoder_score)
    info.decoder_released = True


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
    request_id = req_data.get("request_id") or next_req_id()
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
    except (Exception, asyncio.CancelledError):
        await _abort_prefill_selection(runtime, prefiller_key, prefiller_score, is_initial_request=is_initial_request)
        raise

    response_json = response.json()
    kv_transfer_params = response_json.get("kv_transfer_params", {})
    # PD_BENCH: fail rather than silently execute a local prefill on D.
    if not isinstance(kv_transfer_params, dict) or not kv_transfer_params:
        await _abort_prefill_selection(runtime, prefiller_key, prefiller_score, is_initial_request=is_initial_request)
        raise RuntimeError("Missing P kv_transfer_params; invalid PD benchmark")
    req_data["kv_transfer_params"] = kv_transfer_params
    prefiller_cached_tokens = extract_cached_tokens(response_json)

    decoder_key = None
    try:
        decoder = await runtime.schedule("pick_decoder", decoder_score)
        decoder_key = decoder["key"]
        prefiller_client = await runtime.get_client(ServerRole.PREFILL, prefiller_key)
        decoder_client = await runtime.get_client(ServerRole.DECODE, decoder_key)
    except (Exception, asyncio.CancelledError):
        await _abort_prefill_selection(runtime, prefiller_key, prefiller_score, is_initial_request=is_initial_request)
        if decoder_key is not None:
            await runtime.schedule("release_decoder", decoder_key, decoder_score)
        raise

    logger.debug("Using %s %s", prefiller_client.base_url, decoder_client.base_url)
    return InstanceInfo(
        request_id=request_id,
        prefiller_key=prefiller_key,
        prefiller_score=prefiller_score,
        decoder_key=decoder_key,
        decoder_score=decoder_score,
        decoder_host=decoder["host"],
        decoder_port=decoder["port"],
        prefiller_cached_tokens=prefiller_cached_tokens,
    )


async def reassign_instances(
    api: str,
    req_data: Any,
    request_length: int,
    previous_instance: InstanceInfo,
    *,
    previous_prefiller_kv_released: bool,
) -> InstanceInfo:
    runtime = get_runtime()
    if not previous_prefiller_kv_released:
        await runtime.schedule("release_prefill_kv", previous_instance.prefiller_key, previous_instance.prefiller_score)
    await _release_decoder_once(runtime, previous_instance)
    return await assign_instances(api, req_data, request_length, is_initial_request=False)


async def _replay_first_chunk(first_chunk: bytes, gen: Any):
    """Replay a pre-read first chunk, then stream the rest of ``gen``.

    ``handle_completions_impl`` pre-reads the first chunk of the decode stream
    before committing the ASGI response head so an initial decode error can be
    returned with the real HTTP status; this replays that chunk so the client
    still receives the full stream.
    """
    if first_chunk:
        yield first_chunk
    async for chunk in gen:
        yield chunk


async def handle_completions_impl(api: str, request: Request):
    runtime = get_runtime()
    args = get_global_args()
    request_released = False
    try:
        req_data = await request.json()
        if request_id := request.headers.get("X-Request-Id"):
            req_data["request_id"] = request_id
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

        # Pre-open the decode response BEFORE committing the ASGI 200 head so an
        # initial decode 4xx/5xx/connection failure can be returned with the real
        # HTTP status code instead of an empty 200. On success the opened stream
        # (first chunk already read) is handed to generate_stream via these vars.
        preopened_gen = None
        preopened_first = b""
        decoder_client = await runtime.get_client(ServerRole.DECODE, instance_info.decoder_key)
        preopened_gen = stream_service_response(
            decoder_client,
            api,
            req_data,
            request_id=instance_info.request_id,
            max_retries=args.max_retries,
            base_delay=args.retry_delay,
        )
        try:
            preopened_first = await preopened_gen.__anext__()
        except asyncio.CancelledError:
            await _finish_instance(runtime, instance_info, release_prefill_kv=True)
            request_released = True
            raise
        except StopAsyncIteration:
            preopened_first = b""
        except httpx.HTTPStatusError as exc:
            await _finish_instance(runtime, instance_info, release_prefill_kv=True)
            request_released = True
            return Response(
                content=exc.response.content, status_code=exc.response.status_code, media_type="application/json"
            )
        except httpx.RequestError as exc:
            await _finish_instance(runtime, instance_info, release_prefill_kv=True)
            request_released = True
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "message": f"Decode backend request to {api} failed: {exc}",
                        "type": "upstream_error",
                        "code": "decode_backend_unavailable",
                    }
                },
            )

        async def generate_stream():
            nonlocal instance_info
            nonlocal request_released
            nonlocal preopened_gen, preopened_first
            generated_token = ""
            released_kv = False
            retry_count = 0
            retry = True
            completion_tokens = 0
            reported_prefiller_cached_tokens = instance_info.prefiller_cached_tokens

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
                    if preopened_gen is not None:
                        # First iteration: consume the decode stream pre-opened by the
                        # caller so initial 4xx/5xx could be returned with the real status.
                        gen = _replay_first_chunk(preopened_first, preopened_gen)
                        preopened_gen = None
                    else:
                        decoder_client = await runtime.get_client(ServerRole.DECODE, instance_info.decoder_key)
                        gen = stream_service_response(
                            decoder_client,
                            api,
                            req_data,
                            request_id=instance_info.request_id,
                            max_retries=args.max_retries,
                            base_delay=args.retry_delay,
                        )
                    async for chunk in gen:
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
                        is_sse = chunk_str.startswith("data: ")
                        if is_sse:
                            chunk_str = chunk_str[len("data: ") :]
                        try:
                            chunk_json = json.loads(chunk_str)
                        except json.JSONDecodeError:
                            logger.debug("Skipping chunk: %s", chunk_str)
                            yield chunk
                            continue
                        choices = chunk_json.get("choices", [])
                        if not choices or not stream_flag:
                            if update_cached_tokens_in_chunk(chunk_json, reported_prefiller_cached_tokens):
                                chunk = encode_response_chunk(chunk_json, is_sse)
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
                            # PD_BENCH: expose the marker; the benchmark rejects the request.
                            logger.error("KV recomputation invalidates this benchmark")
                            yield chunk
                            return
                        if retry_count > 0 and not stream_flag:
                            if chat_flag:
                                choice["message"]["content"] = generated_token
                            else:
                                choice["text"] = generated_token
                            chunk = encode_response_chunk(chunk_json, is_sse)
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
    except Exception as e:
        import traceback

        exc_info = sys.exc_info()
        print(f"Error occurred in disagg prefill proxy server - {api} endpoint")
        print("".join(traceback.format_exception(*exc_info)))
        if not request_released and "instance_info" in locals():
            await _finish_instance(runtime, instance_info, release_prefill_kv=True)
            request_released = True
        if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
            try:
                err_body = e.response.json()
            except Exception:
                err_body = {"error": {"message": e.response.text or "upstream error"}}
            return JSONResponse(err_body, status_code=e.response.status_code)
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


async def handle_models_impl():
    runtime = get_runtime()
    request_id = next_req_id()
    await runtime.sync_clients()
    snapshot = runtime.scheduler.get_snapshot()
    candidates = [(ServerRole.PREFILL, s) for s in snapshot["prefill_instances"]] + [
        (ServerRole.DECODE, s) for s in snapshot["decode_instances"]
    ]
    if not candidates:
        return JSONResponse(status_code=503, content={"error": "No available backend instances"})
    failures: list[str] = []
    for role, server in candidates:
        host, port = server["host"], server["port"]
        key = server_key(host, port)
        try:
            client = await runtime.get_client(role, key)
            response = await client.get("/models", headers=auth_headers(request_id), timeout=3.0)
            response.raise_for_status()
            return Response(content=response.content, media_type="application/json")
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            logger.warning("GET /models from %s:%s failed: %s", host, port, exc)
            failures.append(f"{host}:{port}")
    return JSONResponse(
        status_code=503,
        content={
            "error": "All backend instances failed to serve /v1/models",
            "failed": failures,
        },
    )


@app.get("/v1/models")
async def handle_models():
    return await handle_models_impl()


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
PD_FILE_PROXY_PY_EOF
```

### wait_ready.py

```bash
cat > "$WORK/wait_ready.py" <<'PD_FILE_WAIT_READY_PY_EOF'
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
PD_FILE_WAIT_READY_PY_EOF
```

### generate_prompts.py

```bash
cat > "$WORK/generate_prompts.py" <<'PD_FILE_GENERATE_PROMPTS_PY_EOF'
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
PD_FILE_GENERATE_PROMPTS_PY_EOF
```

### bench_client.py

```bash
cat > "$WORK/bench_client.py" <<'PD_FILE_BENCH_CLIENT_PY_EOF'
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
PD_FILE_BENCH_CLIENT_PY_EOF
```

### pd_plateau.py

```bash
cat > "$WORK/pd_plateau.py" <<'PD_FILE_PD_PLATEAU_PY_EOF'
"""Find an overlapping pure-D candidate and count actual accepted output IDs."""
import argparse
import json
import math
from pathlib import Path


def analyze(path):
    summary = json.loads(path.read_text(encoding="utf-8"))
    assert summary["mode"] == "wave" and summary["return_token_ids"]
    assert summary["input_tokens"] == 131072 and summary["output_tokens"] == 1024
    assert summary["all_phase_failures"] == 0
    rows = [json.loads(x) for x in path.with_suffix(".requests.jsonl").read_text(encoding="utf-8").splitlines()]
    batch = summary["client_concurrency"]
    assert len(rows) == batch and all(not r["error"] for r in rows)
    for row in rows:
        assert row["usage"]["prompt_tokens"] == 131072
        assert row["usage"]["completion_tokens"] == row["output_ids"] == 1024
        assert sum(e["tokens"] for e in row["token_events"]) == 1024
    events = sorted((e["t_s"], i, e["tokens"]) for i, r in enumerate(rows) for e in r["token_events"])
    counts, groups, current = [0] * batch, [], []
    for when, index, accepted in events:
        assert accepted >= 0 and math.isfinite(when)
        counts[index] += accepted
        if all(32 <= value <= 896 for value in counts):
            current.append((when, list(counts)))
        else:
            if current:
                groups.append(current)
            current = []
    if current:
        groups.append(current)
    best = max(groups, key=lambda g: g[-1][0] - g[0][0], default=[])
    result = dict(run=0, batch_requested=batch, plateau_valid=False, source=str(path),
                  input_tokens=131072, output_tokens=1024,
                  metric="instance_pure_decode_equivalent_mean_tpot_from_output_ids")
    if len(best) >= 2:
        seconds = best[-1][0] - best[0][0]
        deltas = [end - start for start, end in zip(best[0][1], best[-1][1])]
        tokens = sum(deltas)
        if seconds > 0 and all(n > 0 for n in deltas):
            result.update(plateau_valid=seconds >= 5, plateau_s=seconds,
                plateau_start_ns=summary["start_wall_ns"] + round(best[0][0] * 1e9),
                plateau_end_ns=summary["start_wall_ns"] + round(best[-1][0] * 1e9),
                accepted_output_tokens=tokens, accepted_tokens_per_sequence=deltas,
                decode_tokens_per_s=tokens / seconds,
                tpot_equivalent_ms=1000 * batch * seconds / tokens,
                note="No eight-card averaging; worker/transfer audit and trace/log review remain required")
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--summary", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    result = analyze(a.summary)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps([result], indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["plateau_valid"] else 2)
PD_FILE_PD_PLATEAU_PY_EOF
```

### audit_window.py

```bash
cat > "$WORK/audit_window.py" <<'PD_FILE_AUDIT_WINDOW_PY_EOF'
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
        runner_is_mrv1=all(r.get('runner_module')=='vllm_ascend.worker.model_runner_v1' for r in rows),
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
    r['constant_target'] and r['runner_is_mrv1'] and r['covered_s']>=4.5 for r in report) and observed_reads>0 and not transfers and s['plateau_valid'])
print(json.dumps(dict(valid=valid,window_s=(end-start)/1e9,workers=report,
    batch_requested=s['batch_requested'],window_start_ns=start,window_end_ns=end,
    observed_successful_read_calls=observed_reads,overlapping_transfer_calls=len(transfers),note='Also inspect KV preemption/logs and transfer kernels in trace'),indent=2))
raise SystemExit(0 if valid else 2)
PD_FILE_AUDIT_WINDOW_PY_EOF
```

### select_tpot.py

```bash
cat > "$WORK/select_tpot.py" <<'PD_FILE_SELECT_TPOT_PY_EOF'
"""Select separately under 30/50-ms pure-D instance mean-TPOT ceilings."""
import argparse
import json
import math
from pathlib import Path
import re


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def one_repeat(directory, batch, repeat):
    prefix = directory / f"C{batch}-r{repeat}"
    summary = read(prefix.with_suffix(".json"))
    window = read(prefix.with_suffix(".window.json"))[0]
    audit = read(prefix.with_suffix(".audit.json"))
    rows = [json.loads(line) for line in prefix.with_suffix(".requests.jsonl").read_text(encoding="utf-8").splitlines()]
    assert summary["mode"] == "wave" and summary["client_concurrency"] == batch
    assert summary["input_tokens"] == 131072 and summary["output_tokens"] == 1024
    assert summary["return_token_ids"] and summary["all_phase_failures"] == 0
    assert len(rows) == batch and all(not r["error"] for r in rows)
    assert all(r["usage"]["prompt_tokens"] == 131072 and
               r["usage"]["completion_tokens"] == r["output_ids"] == 1024 for r in rows)
    assert all(sum(e["tokens"] for e in r["token_events"]) == 1024 for r in rows)
    assert window["plateau_valid"] and window["batch_requested"] == batch
    seconds, tokens = float(window["plateau_s"]), int(window["accepted_output_tokens"])
    assert math.isfinite(seconds) and seconds >= 5 and tokens > 0
    value = 1000 * batch * seconds / tokens
    assert math.isfinite(value) and value > 0
    assert math.isclose(value, window["tpot_equivalent_ms"], rel_tol=1e-9)
    assert audit["valid"] is True and len(audit["workers"]) == 8
    assert audit["batch_requested"] == batch
    assert audit["window_start_ns"] == window["plateau_start_ns"]
    assert audit["window_end_ns"] == window["plateau_end_ns"]
    assert audit["observed_successful_read_calls"] > 0 and audit["overlapping_transfer_calls"] == 0
    assert all(w["all_decode"] and w["constant_target"] and w["steps"] >= 100
               and w["covered_s"] >= 4.5 and w["runner_is_mrv1"]
               and set(w["batch_distribution"]) == {str(batch)} for w in audit["workers"])
    return dict(repeat=repeat, tpot_ms=value, seconds=seconds, tokens=tokens,
                summary=str(prefix.with_suffix(".json")), audit=str(prefix.with_suffix(".audit.json")))


def reviewed(review, batch):
    row = review.get(str(batch), {})
    return (row.get("status") == "pass" and row.get("reviewed_repeats") == [1, 2, 3]
            and all(isinstance(row.get(name), str) and row[name].strip()
                    for name in ("trace_evidence", "no_recompute_preemption_evidence", "cpu_network_evidence")))


def analyze(directory, review=None, provisional=False):
    batches = sorted({int(m[1]) for path in directory.glob("C*-r*.json")
                      if (m := re.fullmatch(r"C(\d+)-r(\d+)\.json", path.name))})
    tested, rejected = [], []
    for batch in batches:
        try:
            repeats = [one_repeat(directory, batch, i) for i in (1, 2, 3)]
            tested.append(dict(batch=batch, repeats=repeats,
                worst_repeat_mean_tpot_ms=max(r["tpot_ms"] for r in repeats),
                combined_mean_tpot_ms=1000 * batch * sum(r["seconds"] for r in repeats) /
                                      sum(r["tokens"] for r in repeats)))
        except (AssertionError, KeyError, ValueError, TypeError, OSError, IndexError) as exc:
            rejected.append(dict(batch=batch, reason=f"{type(exc).__name__}: {exc}"))
    selections = {}
    for budget in (30, 50):
        passed = [row for row in tested if row["worst_repeat_mean_tpot_ms"] <= budget]
        if review is not None:
            if provisional:
                passed = [row for row in passed if review.get(str(row["batch"]), {}).get("status") != "fail"]
            else:
                passed = [row for row in passed if reviewed(review, row["batch"])]
        selected = max(passed, key=lambda row: row["batch"], default=None)
        selections[str(budget)] = dict(budget_ms=budget, selection=selected,
            status=("verified_in_tested_scope" if review is not None and not provisional else "candidate_pending_trace_and_log_review")
                   if selected else "no_qualified_batch",
            note="Maximum among validated candidates in this scan, not an extrapolated global maximum")
    return dict(metric="instance_pure_decode_equivalent_mean_tpot_from_output_ids",
                ceiling_rule="Each of three repeat means must be <= budget; no sleep/throttling",
                threshold_ms=[30, 50], tested_batch_sizes=batches,
                valid_three_repeat_batches=tested, rejected_batches=rejected,
                review_applied=review is not None, provisional=provisional, selections=selections)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--waves", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--env-out", type=Path, required=True)
    p.add_argument("--review", type=Path)
    p.add_argument("--provisional", action="store_true")
    a = p.parse_args()
    result = analyze(a.waves, read(a.review) if a.review else None, a.provisional)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    exports = []
    for budget, row in result["selections"].items():
        if row["selection"]:
            name = f"BS_{budget}" if a.review and not a.provisional else f"CANDIDATE_{budget}"
            exports.append(f"export {name}={row['selection']['batch']}")
    a.env_out.write_text("\n".join(exports) + "\n", encoding="utf-8")
    print(json.dumps(result["selections"], indent=2))
PD_FILE_SELECT_TPOT_PY_EOF
```

### check_window_tpot.py

```bash
cat > "$WORK/check_window_tpot.py" <<'PD_FILE_CHECK_WINDOW_TPOT_PY_EOF'
"""Check three output-window means against one ceiling; not a worker audit."""
import argparse
import json
import math
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--directory", type=Path, required=True)
p.add_argument("--bs", type=int, required=True)
p.add_argument("--budget", type=int, choices=[30, 50], required=True)
a = p.parse_args()
values = []
for i in (1, 2, 3):
    path = a.directory / f"C{a.bs}-r{i}.window.json"
    row = json.loads(path.read_text(encoding="utf-8"))[0]
    assert row["plateau_valid"] and row["batch_requested"] == a.bs
    value = 1000 * a.bs * row["plateau_s"] / row["accepted_output_tokens"]
    assert math.isfinite(value) and 0 < value <= a.budget, f"Repeat {i}: {value} ms exceeds {a.budget} ms"
    values.append(value)
print(json.dumps(dict(budget_ms=a.budget, bs=a.bs, repeat_window_mean_ms=values,
    pass_output_window_ceiling=True,
    note="Output-ID window recheck only; actual worker-BS and trace evidence come from diagnostic phase"), indent=2))
PD_FILE_CHECK_WINDOW_TPOT_PY_EOF
```

### check_diagnostic_case.py

```bash
cat > "$WORK/check_diagnostic_case.py" <<'PD_FILE_CHECK_DIAGNOSTIC_CASE_PY_EOF'
"""Require completed diagnostic evidence before running the uninstrumented case."""
import argparse
import json
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def check(run, budget):
    from select_tpot import one_repeat

    selected = read(run / "selection.json")["selections"][str(budget)]
    assert selected["status"] == "verified_in_tested_scope" and selected["selection"]
    batch = selected["selection"]["batch"]
    base = run / "diagnostic" / f"tpot{budget}"
    assert (base / "COMPLETED.json").is_file(), "Diagnostic measurement did not finish"
    completed = read(base / "COMPLETED.json")
    assert completed == dict(phase="diagnostic", budget_ms=budget, batch=batch)
    repeats = [one_repeat(base / "verification", batch, r) for r in (1, 2, 3)]
    assert all(row["tpot_ms"] <= budget for row in repeats)
    distributions = []
    for repeat in (1, 2, 3):
        rows = read(base / "steady" / f"r{repeat}.batches.json")
        assert len(rows) == 8 and len({tuple(row["worker"]) for row in rows}) == 8
        assert all(row["scheduled_iterations"] > 0 for row in rows)
        distributions.append(dict(repeat=repeat, workers=rows))
    return dict(valid=True, budget_ms=budget, batch=batch,
                verification_repeats=repeats, steady_batch_distributions=distributions,
                note="Fixed BS is verified in the three decode windows; steady PD distribution is reported separately")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--budget", type=int, choices=(30, 50), required=True)
    p.add_argument("--out", type=Path)
    a = p.parse_args()
    try:
        result = check(a.run, a.budget)
    except (AssertionError, KeyError, ValueError, TypeError, OSError, IndexError) as exc:
        print(json.dumps(dict(valid=False, reason=f"{type(exc).__name__}: {exc}")))
        raise SystemExit(2)
    if a.out:
        a.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(dict(valid=True, budget_ms=a.budget, batch=result["batch"])))
PD_FILE_CHECK_DIAGNOSTIC_CASE_PY_EOF
```

### compare_ttft.py

```bash
cat > "$WORK/compare_ttft.py" <<'PD_FILE_COMPARE_TTFT_PY_EOF'
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
PD_FILE_COMPARE_TTFT_PY_EOF
```

### check_runs.py

```bash
cat > "$WORK/check_runs.py" <<'PD_FILE_CHECK_RUNS_PY_EOF'
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
PD_FILE_CHECK_RUNS_PY_EOF
```

### metrics_monitor.py

```bash
cat > "$WORK/metrics_monitor.py" <<'PD_FILE_METRICS_MONITOR_PY_EOF'
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
PD_FILE_METRICS_MONITOR_PY_EOF
```

### report_batches.py

```bash
cat > "$WORK/report_batches.py" <<'PD_FILE_REPORT_BATCHES_PY_EOF'
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
PD_FILE_REPORT_BATCHES_PY_EOF
```

### capacity_table.py

```bash
cat > "$WORK/capacity_table.py" <<'PD_FILE_CAPACITY_TABLE_PY_EOF'
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
PD_FILE_CAPACITY_TABLE_PY_EOF
```

### run_waves.sh

```bash
cat > "$WORK/run_waves.sh" <<'PD_FILE_RUN_WAVES_SH_EOF'
#!/usr/bin/env bash
set -euo pipefail
source /tmp/pdtest030_slo/test.env
mkdir -p "$RUN/waves"
for C in ${C_LIST:-1 2 4 8 16 24 32}; do
  [[ "$C" =~ ^[0-9]+$ ]] && (( C >= 1 && C <= MAX_SEQS )) || { echo "Invalid C=$C"; exit 2; }
  for r in 1 2 3; do
    prefix="$RUN/waves/C${C}-r${r}"
    if [[ ! -f "${prefix}.json" ]]; then
      python "$WORK/bench_client.py" \
        --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
        --prompts "$RUN/prompt-128k.json" --mode wave \
        --concurrency "$C" --requests "$C" --output-length 1024 \
        --return-token-ids --min-samples "$C" --timeout 7200 --out "${prefix}.json"
    fi
    if [[ ! -f "${prefix}.window.json" ]]; then
      if python "$WORK/pd_plateau.py" --summary "${prefix}.json" --out "${prefix}.window.json"; then
        echo "Window candidate: C=$C repeat=$r; D audit still required"
      else
        echo "No eligible full-overlap window: C=$C repeat=$r"
      fi
    fi
  done
done
PD_FILE_RUN_WAVES_SH_EOF
```

### run_budget_case.sh

```bash
cat > "$WORK/run_budget_case.sh" <<'PD_FILE_RUN_BUDGET_CASE_SH_EOF'
#!/usr/bin/env bash
set -euo pipefail
source /tmp/pdtest030_slo/test.env
phase=${1:?diagnostic or formal}
budget=${2:?30 or 50}
[[ "$phase" == diagnostic || "$phase" == formal ]] || exit 2
[[ "$budget" == 30 || "$budget" == 50 ]] || exit 2
BS=$(python -c 'import json,sys; r=json.load(open(sys.argv[1]))["selections"][sys.argv[2]]; assert r["status"]=="verified_in_tested_scope" and r["selection"]; print(r["selection"]["batch"])' "$RUN/selection.json" "$budget")
if [[ "$phase" == formal ]]; then
  python "$WORK/check_diagnostic_case.py" --run "$RUN" --budget "$budget"
fi
base="$RUN/$phase/tpot${budget}"
test ! -e "$base" || { echo "Use a fresh RUN_ID: $base already exists"; exit 2; }
mkdir -p "$base/verification" "$base/single" "$base/batch" "$base/steady"
printf 'budget_ms=%s\nclient_concurrency=%s\n' "$budget" "$BS" > "$base/case.txt"
for r in 1 2 3; do
  python "$WORK/bench_client.py" --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
    --prompts "$RUN/prompt-128k.json" --mode wave --concurrency 1 --requests 1 \
    --output-length 1024 --min-samples 1 --timeout 7200 --out "$base/warmup-r${r}.json"
done
for r in 1 2 3; do
  prefix="$base/verification/C${BS}-r${r}"
  python "$WORK/bench_client.py" --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
    --prompts "$RUN/prompt-128k.json" --mode wave --concurrency "$BS" --requests "$BS" \
    --output-length 1024 --return-token-ids --min-samples "$BS" --timeout 7200 --out "${prefix}.json"
  python "$WORK/pd_plateau.py" --summary "${prefix}.json" --out "${prefix}.window.json"
done
python "$WORK/check_window_tpot.py" --directory "$base/verification" --bs "$BS" --budget "$budget" > "$base/window-slo-check.json"
for r in $(seq 1 30); do
  python "$WORK/bench_client.py" --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
    --prompts "$RUN/prompt-128k.json" --mode wave --concurrency 1 --requests 1 \
    --output-length 1024 --min-samples 1 --timeout 7200 --out "$base/single/r${r}.json"
done
for r in $(seq 1 10); do
  python "$WORK/bench_client.py" --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
    --prompts "$RUN/prompt-128k.json" --mode wave --concurrency "$BS" --requests "$BS" \
    --output-length 1024 --min-samples "$BS" --timeout 7200 --out "$base/batch/r${r}.json"
done
python "$WORK/check_runs.py" --directory "$base/single" --runs 30 --concurrency 1 --mode wave
python "$WORK/check_runs.py" --directory "$base/batch" --runs 10 --concurrency "$BS" --mode wave
python "$WORK/compare_ttft.py" --single "$base"/single/r*.json --batch "$base"/batch/r*.json --bs "$BS" > "$base/ttft-comparison.json"
for r in 1 2 3; do
  python "$WORK/bench_client.py" --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
    --prompts "$RUN/prompt-128k.json" --mode steady --concurrency "$BS" --output-length 1024 \
    --burnin 300 --duration "${DURATION:-1800}" --min-samples 30 --timeout 7200 --out "$base/steady/r${r}.json"
done
python "$WORK/check_runs.py" --directory "$base/steady" --runs 3 --concurrency "$BS" --mode steady
python - "$phase" "$budget" "$BS" "$base/COMPLETED.json" <<'PY'
import json,sys
from pathlib import Path
Path(sys.argv[4]).write_text(json.dumps(dict(phase=sys.argv[1],budget_ms=int(sys.argv[2]),batch=int(sys.argv[3]))),encoding='utf-8')
PY
echo "Completed $phase budget=${budget}ms C=$BS; PD request TPOT remains a separate reported metric"
PD_FILE_RUN_BUDGET_CASE_SH_EOF
```


检查现有镜像依赖：

```bash
python -c 'import aiohttp,fastapi,httpx,uvicorn; print("代理/压测依赖可用")'
```

缺包时按你们批准的软件源或离线 wheel 补缺包，保留现有 vLLM、Ascend、torch、torch-npu 版本；目标机器不需要 Git。

## 5. D 容器：创建合入后的完整采样文件

下面四个文件只在 D 容器创建。新的 audit_patch 合并容量、step 和两处 KV read；不再执行旧的独立 patch_steps 安装/恢复。

### audit_patch.py

```bash
cat > "$WORK/audit_patch.py" <<'PD_FILE_AUDIT_PATCH_PY_EOF'
#!/usr/bin/env python3
"""Diagnostic hooks for vLLM 0.30.0 + vLLM-Ascend 0.30.0rc2, MRV1 only."""
import argparse
import ast
import hashlib
from importlib.metadata import version
from importlib.util import find_spec
import json
import os
from pathlib import Path
import stat
import tempfile

MARK = "ASCEND_BENCH_V030RC2"
FILES = ("worker/model_runner_v1.py",
         "distributed/kv_transfer/kv_p2p/mooncake_hybrid_connector.py")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def method(tree, name):
    cls = [n for n in tree.body if isinstance(n, ast.ClassDef)
           and n.name == "NPUModelRunner"]
    matches = [n for c in cls for n in c.body
               if isinstance(n, ast.FunctionDef) and n.name == name]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one NPUModelRunner.{name}")
    return matches[0]


def assignment(scope, target, function):
    return [n for n in ast.walk(scope) if isinstance(n, ast.Assign)
            and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id == target and isinstance(n.value, ast.Call)
            and ast.unparse(n.value.func) == function]


def edit(source, changes):
    lines = source.splitlines(keepends=True)
    for node, code, insert in sorted(changes, key=lambda x: x[0].lineno, reverse=True):
        indent = " " * node.col_offset
        replacement = [indent + line + "\n" for line in code.splitlines()]
        if insert:
            lines[node.end_lineno:node.end_lineno] = replacement
        else:
            lines[node.lineno - 1:node.end_lineno] = replacement
    result = "".join(lines)
    ast.parse(result)
    return result.encode("utf-8")


def prepare(original):
    runner, connector = [data.decode("utf-8") for data in original]
    for source in (runner, connector):
        if any(m in source for m in (MARK, "ASCEND_BENCH_AUDIT_V1", "V4_2NODE_STEP_AUDIT")):
            raise RuntimeError("Existing diagnostic markers: restore previous patches first")
    tree = ast.parse(runner)
    caps = assignment(method(tree, "initialize_kv_cache"), "kv_caches",
                      "self.initialize_kv_cache_tensors")
    steps = assignment(method(tree, "execute_model"), "deferred_state_corrections_fn",
                       "self._update_states")
    if len(caps) != 1 or len(steps) != 1:
        raise RuntimeError("Runner AST mismatch; no files changed")
    keywords = {k.arg for k in caps[0].value.keywords}
    if not {"kernel_block_sizes", "kv_cache_allocation_context"} <= keywords:
        raise RuntimeError("Unexpected KV initialization signature; no files changed")
    runner_new = edit(runner, [
        (caps[0], f"# {MARK}:capacity\nfrom capacity_probe import dump_capacity\ndump_capacity(self, kv_caches)", True),
        (steps[0], f"# {MARK}:step\nfrom step_probe import snapshot\nsnapshot(self, scheduler_output)", True),
    ])
    reads = assignment(ast.parse(connector), "ret", "self.engine.batch_transfer_sync_read")
    expected = ["session_id", "src_list", "dst_list", "length_list"]
    if len(reads) != 2 or any([ast.unparse(x) for x in n.value.args] != expected
                              or n.value.keywords for n in reads):
        raise RuntimeError("Connector AST mismatch; expected two KV reads; no files changed")
    code = (f"# {MARK}:read\nfrom capacity_probe import measured_read\n"
            "ret = measured_read(self.engine, session_id, src_list, dst_list, length_list, req_meta)")
    connector_new = edit(connector, [(n, code, False) for n in reads])
    return (runner_new, connector_new)


def atomic_write(path, data):
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, name = tempfile.mkstemp(prefix=path.name + ".pd030.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--restore", action="store_true")
    p.add_argument("--root", type=Path, help="Offline source tree for validation only")
    a = p.parse_args()
    if a.root is None:
        spec = find_spec("vllm_ascend")
        if spec is None or spec.origin is None:
            raise SystemExit("vllm_ascend not found")
        root = Path(spec.origin).parent
    else:
        root = a.root
    paths = [root / name for name in FILES]
    backups = [Path(str(path) + ".pd030.bak") for path in paths]
    state_path = root / ".pd_audit_v030rc2.json"

    if a.restore:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        originals = [b.read_bytes() for b in backups]
        for path, data, row in zip(paths, originals, state):
            if digest(data) != row["original_sha256"]:
                raise RuntimeError(f"Backup checksum mismatch: {path}")
            if digest(path.read_bytes()) not in (row["original_sha256"], row["patched_sha256"]):
                raise RuntimeError(f"Source changed after patch; refusing overwrite: {path}")
        for path, data in zip(paths, originals):
            atomic_write(path, data)
        state_path.unlink()
        for backup in backups:
            backup.unlink()
        print("RESTORED: both source files match their original checksums")
        return

    if a.root is None:
        core, ascend = version("vllm"), version("vllm-ascend")
        if core.split("+")[0] != "0.30.0" or ascend.split("+")[0] != "0.30.0rc2":
            raise RuntimeError(f"Unsupported versions: vllm={core}, ascend={ascend}")
    if state_path.exists() or any(b.exists() for b in backups):
        raise RuntimeError("Patch state/backups already exist; inspect or --restore first")
    for path in paths:
        for suffix in (".bench.bak", ".steps.bak"):
            if Path(str(path) + suffix).exists():
                raise RuntimeError("Old backup exists; restore old patch first")
    for name in ("capacity_probe.py", "step_probe.py"):
        ast.parse(Path(__file__).with_name(name).read_text(encoding="utf-8"))
    original = [path.read_bytes() for path in paths]
    patched = prepare(original)
    state = [dict(file=name, original_sha256=digest(old), patched_sha256=digest(new))
             for name, old, new in zip(FILES, original, patched)]
    print(json.dumps(state, indent=2))
    if a.check:
        print("CHECK PASSED; no files changed; runtime MRV1/field checks still required")
        return
    for backup, data in zip(backups, original):
        with backup.open("xb") as f:
            f.write(data)
    with state_path.open("x", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    try:
        for path, data in zip(paths, patched):
            atomic_write(path, data)
    except Exception:
        for path, data in zip(paths, original):
            atomic_write(path, data)
        raise
    print("APPLIED: capacity + worker step + two KV read hooks. Restart D with MRV1.")


if __name__ == "__main__":
    main()
PD_FILE_AUDIT_PATCH_PY_EOF
```

### capacity_probe.py

```bash
cat > "$WORK/capacity_probe.py" <<'PD_FILE_CAPACITY_PROBE_PY_EOF'
"""Diagnostic startup storage snapshot and synchronous Mooncake read events."""
import json
import operator
import os
from pathlib import Path
import socket
import threading
import time

_lock = threading.Lock()


def write_record(directory, kind, record):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname()
    row = dict(kind=kind, host=host, pid=os.getpid(), time_ns=time.time_ns())
    row.update(record)
    with _lock, (root / f"{host}-{os.getpid()}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def storages(value):
    import torch
    found = {}
    def visit(obj):
        if isinstance(obj, torch.Tensor) and obj.device.type == "npu":
            storage = obj.untyped_storage()
            key = (str(obj.device), storage.data_ptr(), storage.nbytes())
            found[key] = storage.nbytes()
        elif isinstance(obj, dict):
            for child in obj.values():
                visit(child)
        elif isinstance(obj, (tuple, list)):
            for child in obj:
                visit(child)
    visit(value)
    return sum(found.values()), len(found)


def dump_capacity(runner, kv_caches):
    directory = os.getenv("AUDIT_CAPACITY_DIR")
    if not directory:
        return
    if runner.vllm_config.use_v2_model_runner:
        raise RuntimeError("This diagnostic supports Model Runner V1 only")
    import torch
    torch.npu.synchronize()
    cfg = runner.kv_cache_config
    tensors = []
    for item in cfg.kv_cache_tensors:
        tensors.append({name: getattr(item, name) for name in
                        ("size", "layers", "layer_stride", "block_stride", "offset", "host_resident")
                        if hasattr(item, name)})
    groups = []
    for index, group in enumerate(cfg.kv_cache_groups):
        spec = group.kv_cache_spec
        entry = dict(index=index, spec_type=type(spec).__name__,
                     layers=list(group.layer_names), spec_repr=repr(spec))
        for name in ("block_size", "page_size_bytes", "real_page_size_bytes"):
            try:
                entry[name] = getattr(spec, name)
            except (AttributeError, NotImplementedError, ValueError, AssertionError, TypeError) as exc:
                entry[name + "_unavailable"] = repr(exc)
        groups.append(entry)
    kv_bytes, count = storages(kv_caches)
    if kv_bytes <= 0:
        raise RuntimeError("No NPU KV backing storage found; diagnostic layout mismatch")
    model_bytes, _ = storages(list(runner.model.parameters()) + list(runner.model.buffers()))
    free, total = torch.npu.mem_get_info()
    write_record(directory, "capacity_post_kv_before_graph", dict(
        runner_module=type(runner).__module__, runner_class=type(runner).__name__,
        dp_rank=runner.vllm_config.parallel_config.data_parallel_rank,
        tp_size=runner.vllm_config.parallel_config.tensor_parallel_size,
        device=str(runner.device), num_blocks=cfg.num_blocks,
        actual_unique_cache_storage_bytes=kv_bytes, unique_cache_storages=count,
        model_parameter_buffer_storage_bytes=model_bytes,
        allocated_bytes=torch.npu.memory_allocated(), reserved_bytes=torch.npu.memory_reserved(),
        free_bytes=free, total_bytes=total, tensors=tensors, groups=groups,
        note="Before graph capture; descriptor sizes are metadata, not additive physical allocations"))


def measured_read(engine, session_id, src_list, dst_list, length_list, req_meta):
    directory = os.getenv("AUDIT_TRANSFER_DIR")
    if not directory:
        return engine.batch_transfer_sync_read(session_id, src_list, dst_list, length_list)
    start = time.perf_counter_ns()
    result, error = None, None
    try:
        result = engine.batch_transfer_sync_read(session_id, src_list, dst_list, length_list)
        try:
            code = operator.index(result)
            if code < 0:
                error = f"return_code={code}"
        except TypeError:
            error = f"unexpected_return_type={type(result).__name__}"
        return result
    except BaseException as exc:
        error = repr(exc)
        raise
    finally:
        stop = time.perf_counter_ns()
        end_wall = time.time_ns()
        write_record(directory, "mooncake_sync_read", dict(
            time_ns=end_wall, request_id=req_meta.get("request_id"),
            remote_request_id=req_meta.get("remote_request_id"),
            duration_ms=(stop-start)/1e6, bytes=sum(int(n) for n in length_list),
            segments=len(length_list), result=result, error=error,
            success=error is None))
PD_FILE_CAPACITY_PROBE_PY_EOF
```

### step_probe.py

```bash
cat > "$WORK/step_probe.py" <<'PD_FILE_STEP_PROBE_PY_EOF'
"""CPU request-state snapshots for MRV1; never synchronize NPU per step."""
import atexit
import json
import os
from pathlib import Path
import socket
import time

_buffers = {}
_last_flush = 0.0


def flush():
    for path, rows in _buffers.items():
        if rows:
            with path.open("a", encoding="utf-8") as f:
                f.write("".join(rows))
            rows.clear()


atexit.register(flush)


def snapshot(runner, out):
    directory = os.getenv("AUDIT_STEP_DIR")
    if not directory:
        return
    rows = []
    for rid, scheduled in out.num_scheduled_tokens.items():
        scheduled = int(scheduled)
        if scheduled <= 0:
            continue
        state = runner.requests.get(rid)
        plen = getattr(state, "num_prompt_tokens", None)
        computed = getattr(state, "num_computed_tokens", None)
        previous_drafts = getattr(state, "prev_num_draft_len", None)
        known = plen is not None and computed is not None and previous_drafts is not None
        lower_bound = int(computed) - max(0, int(previous_drafts)) if known else None
        rows.append(dict(request_id=rid, scheduled_tokens=scheduled,
                         prompt_tokens=plen, computed_tokens=computed,
                         previous_draft_tokens=previous_drafts,
                         conservative_computed_tokens=lower_bound,
                         decode=bool(lower_bound >= plen) if known else None))
    if sum(r["scheduled_tokens"] for r in rows) != int(out.total_num_scheduled_tokens):
        raise RuntimeError("Scheduler token counts disagree; diagnostic schema mismatch")
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname()
    row = dict(kind="worker_step", time_ns=time.time_ns(), host=host,
               pid=os.getpid(), device=str(runner.device),
               runner_module=type(runner).__module__, batch_size=len(rows),
               scheduled_tokens=int(out.total_num_scheduled_tokens),
               all_decode=bool(rows) and all(r["decode"] is True for r in rows),
               classification="conservative CPU state after _update_states", requests=rows)
    path = root / f"{host}-{os.getpid()}.jsonl"
    buffer = _buffers.setdefault(path, [])
    buffer.append(json.dumps(row, default=str) + "\n")
    global _last_flush
    now = time.monotonic()
    if len(buffer) >= 32 or now - _last_flush >= 0.25:
        flush()
        _last_flush = now
PD_FILE_STEP_PROBE_PY_EOF
```

### patch_steps.py

```bash
cat > "$WORK/patch_steps.py" <<'PD_FILE_PATCH_STEPS_PY_EOF'
#!/usr/bin/env python3
"""Old separate step installer is deliberately disabled for this diagnostic."""
raise SystemExit(
    "Do not run the old separate step patch. "
    "audit_patch.py now handles capacity, steps and KV reads together. "
    "Use: python audit_patch.py --check / --apply / --restore"
)
PD_FILE_PATCH_STEPS_PY_EOF
```


安装后实际修改两个框架文件：runner 的 KV 初始化返回后加容量采样、_update_states 返回后加实际请求 batch 采样；connector 的两处同步 read 由记录包装器调用。它保留原返回值和异常。新 KV 元信息读取 layers/stride/offset，容量按底层 NPU storage 去重。

## 6. 停止旧服务，检查补丁，按 MRV1 启动 P/D

先正常停止之前的 P、D 模型和旧代理/压测任务，等待旧 NPU 进程释放资源。使用原终端 Ctrl+C 或现有管理系统，不要批量终止 Python。保留原日志。

D 容器执行，只检查不写源码：

```bash
source /tmp/pdtest030_slo/test.env
python "$WORK/audit_patch.py" --check
```

必须看到 CHECK PASSED。旧标记、旧备份、版本或 AST 不匹配时停止；不手改检查条件。通过后统一安装：

```bash
python "$WORK/audit_patch.py" --apply
```

必须看到 APPLIED。保留 `.pd030.bak` 备份和 `.pd_audit_v030rc2.json`。不传用于离线验证的 `--root`。

P 容器启动模型；profiler 仅注册端点，此时不开始采样：

```bash
source /tmp/pdtest030_slo/test.env
mkdir -p "$RUN/p-profiles"
nohup env LOCAL_IP="$P_IP" NIC="$P_NIC" MODEL="$P_MODEL" \
  PROFILE=1 PROFILE_DIR="$RUN/p-profiles" \
  AUDIT_CAPACITY_DIR= AUDIT_TRANSFER_DIR= AUDIT_STEP_DIR= \
  bash "$WORK/start_p.sh" > "$RUN/prefill.log" 2>&1 &
echo $! > "$RUN/prefill.pid"
```

D 容器启动诊断模型：

```bash
source /tmp/pdtest030_slo/test.env
mkdir -p "$RUN/d-capacity" "$RUN/d-transfer" "$RUN/d-steps" "$RUN/d-profiles"
nohup env LOCAL_IP="$D_IP" NIC="$D_NIC" MODEL="$D_MODEL" \
  PYTHONPATH="$WORK${PYTHONPATH:+:$PYTHONPATH}" \
  AUDIT_CAPACITY_DIR="$RUN/d-capacity" AUDIT_TRANSFER_DIR="$RUN/d-transfer" \
  AUDIT_STEP_DIR="$RUN/d-steps" PROFILE=1 PROFILE_DIR="$RUN/d-profiles" \
  bash "$WORK/start_d.sh" > "$RUN/decode-diagnostic.log" 2>&1 &
echo $! > "$RUN/decode-diagnostic.pid"
```

MRV1 是两端启动 Shell 的显式设置。图模式、prefix cache、MTP、KV dtype 和量化信息以实际日志记录为准。W8A8 不自动等于 KV 全部为 8bit。

## 7. P 容器：等待模型就绪并启动 P 端代理

```bash
source /tmp/pdtest030_slo/test.env
python "$WORK/wait_ready.py" --url "http://$P_IP:8100/health" \
  --url "http://$D_IP:8200/health" --timeout 3600
nohup python "$WORK/proxy.py" --host 0.0.0.0 --port 1999 --workers 1 --max-retries 1 \
  --prefiller-hosts "$P_IP" --prefiller-ports 8100 \
  --decoder-hosts "$D_IP" --decoder-ports 8200 > "$RUN/proxy.log" 2>&1 &
echo $! > "$RUN/proxy.pid"
python "$WORK/wait_ready.py" --url http://127.0.0.1:1999/healthcheck --timeout 120
curl --noproxy '*' -fsS http://127.0.0.1:1999/healthcheck
```

必须两端就绪且短请求通过后再压测。代理失败时先查看 `tail -n 100 "$RUN/proxy.log"`。

## 8. 两台宿主机：持续采集 HBM、CPU、网卡

每台宿主机各执行一次；P 填 ROLE=P，D 填 ROLE=D，RUN_ID 与容器相同。

```bash
export RUN_ID=pd030_tpot30_50_001
export ROLE=P
export HOST_OUT="/data/pdtest-results/$RUN_ID/$ROLE"
mkdir -p "$HOST_OUT"
cat > "$HOST_OUT/monitor.sh" <<'PD_MONITOR_EOF'
#!/usr/bin/env bash
set -euo pipefail
command -v npu-smi >/dev/null
while true; do
  date -u +'%Y-%m-%dT%H:%M:%S.%NZ'
  echo PROC_STAT
  cat /proc/stat
  echo PROC_NET_DEV
  cat /proc/net/dev
  echo NPU_SMI
  npu-smi info
  sleep 2
done
PD_MONITOR_EOF
nohup bash "$HOST_OUT/monitor.sh" > "$HOST_OUT/host.log" 2>&1 &
echo $! > "$HOST_OUT/monitor.pid"
if command -v pidstat >/dev/null 2>&1; then
  nohup pidstat -u -r -p ALL 1 > "$HOST_OUT/pidstat.log" 2>&1 &
  echo $! > "$HOST_OUT/pidstat.pid"
fi
```

两个采样日志分别保留。P 上代理与客户端会用 CPU 和网卡；出现 CPU 排队或网卡饱和时本轮输出事件 TPOT不作为卡性能依据。HBM 每两秒记录一次，最终写“采样最大值”，不写瞬时峰值。

## 9. P 容器：保存两端原始指标

```bash
source /tmp/pdtest030_slo/test.env
cat > "$RUN/endpoints.json" <<PD_ENDPOINTS_EOF
[
 {"role":"P","url":"http://$P_IP:8100"},
 {"role":"D","url":"http://$D_IP:8200"}
]
PD_ENDPOINTS_EOF
nohup python "$WORK/metrics_monitor.py" --endpoints "$RUN/endpoints.json" \
  --out "$RUN/metrics.jsonl" --interval 2 > "$RUN/metrics-monitor.log" 2>&1 &
echo $! > "$RUN/metrics-monitor.pid"
```

保留原始 metric 名和 labels。记录每组测试窗口的等待请求、KV 使用率、实际抢占/重算计数及增量，不把 DP 重复标签直接相加。

## 10. P 容器：短 PD 请求与 D 采样检查

```bash
curl --noproxy '*' -N --fail-with-body http://127.0.0.1:1999/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"dsv4","prompt":"Explain CPU caches.","max_tokens":64,"min_tokens":64,"ignore_eos":true,"temperature":0,"stream":true,"stream_options":{"include_usage":true}}'
```

应看到输出和 `[DONE]`。在 D 容器新终端检查 8 个 worker 和 runner 模块：

```bash
source /tmp/pdtest030_slo/test.env
python - <<'PY'
import json,os
from pathlib import Path
root=Path(os.environ['RUN'])
rows=[json.loads(line) for p in (root/'d-capacity').glob('*.jsonl') for line in p.read_text().splitlines()]
rows=[r for r in rows if r.get('kind')=='capacity_post_kv_before_graph']
workers={(r['host'],r['pid'],r['device']) for r in rows}
print('capacity workers:',len(workers))
assert len(workers)==8
assert all(r['runner_module']=='vllm_ascend.worker.model_runner_v1' for r in rows)
reads=[json.loads(line) for p in (root/'d-transfer').glob('*.jsonl') for line in p.read_text().splitlines()]
assert any(r.get('success') is True and r.get('bytes',0)>0 for r in reads), 'No successful KV read'
print('8 workers, MRV1, and successful KV reads confirmed')
PY
```

未通过则先处理，不进入长请求。worker 状态未知、runner 不符或只有部分卡记录时不接受后续 BS 结果。

## 11. P 容器：精确长度校验

```bash
source /tmp/pdtest030_slo/test.env
python "$WORK/generate_prompts.py" --model "$P_MODEL" --length 131072 --out "$RUN/prompt-128k.json"
python "$WORK/bench_client.py" --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
  --prompts "$RUN/prompt-128k.json" --mode wave --concurrency 1 --requests 1 \
  --output-length 1024 --return-token-ids --min-samples 1 --timeout 7200 --out "$RUN/length-check.json"
```

确认 input_tokens=131072、output_tokens=1024、valid_requests=1、all_phase_failures=0。D 的 token_ids 必须真实返回1024个。return-token-ids 的第一块还会含输入 IDs，因此它仅用于诊断窗口计数；正式 TTFT/请求 TPOT不启用该回显。

## 12. P 容器：三次预热

```bash
for r in 1 2 3; do
  python "$WORK/bench_client.py" --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
    --prompts "$RUN/prompt-128k.json" --mode wave --concurrency 1 --requests 1 \
    --output-length 1024 --min-samples 1 --timeout 7200 --out "$RUN/warmup-r${r}.json" || break
done
```

三次全部成功后继续。

## 13. P 容器：并发扫描，每个候选三次

先做粗扫：

```bash
C_LIST='1 2 4 8 16 24 32' bash "$WORK/run_waves.sh" > "$RUN/waves-coarse.log" 2>&1
tail -n 50 "$RUN/waves-coarse.log"
```

要将“最大”限定为完整 1..MAX_SEQS 范围，需要补全整数候选；脚本保留已有记录，不重复覆盖：

```bash
C_LIST="$(seq -s ' ' 1 "$MAX_SEQS")" bash "$WORK/run_waves.sh" > "$RUN/waves-full.log" 2>&1
```

128K 输入、三次重复和多候选会使测试持续较长时间，执行前安排独占测试窗口。请求失败会停止本次扫描；修复环境后新建 RUN_ID。没有足够长公共窗口会保留 plateau_valid=false，不当作卡 TPOT超标。

只有候选的三个窗口都满足：至少5秒、所有请求已输出32..896 token、8 worker全部Decode且固定目标BS、每worker至少100步、窗口内无记录到的KV read，再进入30/50筛选。纯 D 窗口时长不足、P供应不足或无有效候选时记录“本次未形成纯 D 稳态”，不改输出长度凑结果。

## 14. D 宿主机：导出扫描采样快照

第13步全部结束，确认没有在途请求，暂停继续发请求。在 D 宿主机执行，替换容器名：

```bash
export RUN_ID=pd030_tpot30_50_001
export D_CONTAINER=填写D容器名
export D_EXPORT="/data/pdtest-results/$RUN_ID/D/discovery"
mkdir -p "$D_EXPORT"
docker cp "$D_CONTAINER:/tmp/pdtest030_slo/runs/$RUN_ID/." "$D_EXPORT/"
```

这里审计已结束波次的中段；D保持运行。最终停止 D 后还要再次导出完整日志并复核，避免尾部缓冲记录丢失。

## 15. P 宿主机：传入 P 容器

SSH 地址使用 D 的管理地址，不必与通信D_IP相同。无SSH时通过批准的文件传输工具传递同一目录，再执行 docker cp；不要求容器内有 SSH 服务。

```bash
export RUN_ID=pd030_tpot30_50_001
export P_CONTAINER=填写P容器名
export SSH_USER=填写SSH用户名
export D_SSH_HOST=填写D宿主机SSH地址
export P_IMPORT="/data/pdtest-results/$RUN_ID/import-discovery"
mkdir -p "$P_IMPORT"
scp -r "$SSH_USER@$D_SSH_HOST:/data/pdtest-results/$RUN_ID/D/discovery/." "$P_IMPORT/"
docker exec "$P_CONTAINER" mkdir -p "/tmp/pdtest030_slo/runs/$RUN_ID/d-export-discovery"
docker cp "$P_IMPORT/." "$P_CONTAINER:/tmp/pdtest030_slo/runs/$RUN_ID/d-export-discovery/"
```

## 16. P 容器：审计候选，分别计算30/50候选 BS

先保存每个已有窗口的审计结果：

```bash
source /tmp/pdtest030_slo/test.env
for window in "$RUN"/waves/C*-r*.window.json; do
  [[ -f "$window" ]] || continue
  prefix=${window%.window.json}
  if python "$WORK/audit_window.py" --steps "$RUN/d-export-discovery/d-steps" \
      --transfer "$RUN/d-export-discovery/d-transfer" --summary "$window" --run 0 --expected-workers 8 \
      > "${prefix}.audit.json"; then
    echo "PASS ${prefix##*/}"
  else
    echo "INVALID ${prefix##*/}"
  fi
done
python "$WORK/select_tpot.py" --waves "$RUN/waves" --out "$RUN/candidates.json" --env-out "$RUN/candidates.env"
```

输出中 `30`、`50` 分别查看 selection.batch 和三次 tpot_ms；`worst_repeat_mean_tpot_ms` 必须≤该档上限。状态此时为 candidate_pending_trace_and_log_review，不能直接作为已完成全部验证的卡性能结论。

如果某档 selection=null，记录未测得。先看 rejected_batches 是窗口/长度/审计缺失，还是有效窗口的 TPOT均高于上限。两类原因分别记录，不盲目升级并发。

## 17. P 容器：对候选做独立 trace 核对

从 candidates.json 中取两个候选 B 的去重列表。不要将 profiler 波次计入30/50筛选或正式性能。

```bash
mkdir -p "$RUN/profile-check"
for B in $(python -c 'import json,sys; r=json.load(open(sys.argv[1])); print(" ".join(map(str,sorted({x["selection"]["batch"] for x in r["selections"].values() if x["selection"]}))))' "$RUN/candidates.json"); do
  curl --noproxy '*' --fail-with-body -X POST "http://$D_IP:8200/start_profile" || break
  if python "$WORK/bench_client.py" --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
      --prompts "$RUN/prompt-128k.json" --mode wave --concurrency "$B" --requests "$B" \
      --output-length 1024 --return-token-ids --min-samples "$B" --timeout 7200 --out "$RUN/profile-check/B${B}.json"; then
    profile_status=0
  else
    profile_status=$?
  fi
  curl --noproxy '*' --fail-with-body -X POST "http://$D_IP:8200/stop_profile" || break
  echo "B=$B profile request exit=$profile_status"
done
```

用第14/15步相同的宿主机导出方法取 D 的 d-profiles 目录。用 MindStudio Insight 检查共同解码中段是否包含其他传输/变换、实际重算或抢占。结合发现阶段每次窗口和指标、D日志核对。代表性trace是补充证据，不把它的采样延迟当作未采样波次的精确NPU时长。记录文件与窗口位置。

Profiler可能产生较大文件或改变运行节奏；失败时记录未完成，不接受该候选的纯 D 验证。先完成当前采样和stop_profile，再开始下一组。

## 18. P 容器：登记证据并生成两档最终选择

先生成待填写记录，保留 pending 和空证据，不能自动当作通过：

```bash
python - <<'PY'
import json,os
from pathlib import Path
root=Path(os.environ['RUN'])
path=root/'review.json'
if path.exists(): raise SystemExit('review.json 已存在，请保留原记录并编辑')
rows=json.loads((root/'candidates.json').read_text())['valid_three_repeat_batches']
review={str(r['batch']):dict(status='pending',reviewed_repeats=[],trace_evidence='',
    no_recompute_preemption_evidence='',cpu_network_evidence='') for r in rows}
path.write_text(json.dumps(review,indent=2),encoding='utf-8')
print(path)
PY
vi "$RUN/review.json"
```

对实际已核对通过的候选，填写 status=pass、reviewed_repeats=[1,2,3] 和三个证据路径/窗口说明。trace证据说明窗口内无其他KV传输活动；日志/指标证据说明没有实际重算或抢占；CPU/网卡证据说明没有客户端或代理瓶颈。未核对保持pending，检查失败填fail并在证据栏写原因。不要仅为让脚本通过而填pass。

生成最终两档选择：

```bash
python "$WORK/select_tpot.py" --waves "$RUN/waves" --review "$RUN/review.json" \
  --out "$RUN/selection.json" --env-out "$RUN/selection.env"
cat "$RUN/selection.env"
```

通过的目标分别导出BS_30和BS_50；未通过的档位不导出。若最大候选被review否决，更新候选、对下一档重复第17/18步：

```bash
python "$WORK/select_tpot.py" --waves "$RUN/waves" --review "$RUN/review.json" --provisional \
  --out "$RUN/candidates.json" --env-out "$RUN/candidates.env"
```

新候选通过后再生成selection.json。未审查更大候选时，只报告“已验证集合内最大”，保留完整已测/被拒绝/待审查列表。不得由一个已通过BS外推整档所有BS均可用。

## 19. P 容器：两档诊断测试，分别记录TTFT、稳态和HBM

以下每档先预热，重新验证三次纯 D 输出窗口 TPOT上限，再做单并发30次、C=该档BS的波次10次和稳态三次。目录按档位分开。没有已验证BS的档位不运行。

```bash
for T in 30 50; do
  if python -c 'import json,sys; r=json.load(open(sys.argv[1]))["selections"][sys.argv[2]]; raise SystemExit(0 if r["status"]=="verified_in_tested_scope" and r["selection"] else 2)' "$RUN/selection.json" "$T"; then
    bash "$WORK/run_budget_case.sh" diagnostic "$T" > "$RUN/diagnostic-tpot${T}.log" 2>&1 || break
  else
    echo "TPOT<=$T ms：未测得已验证BS，跳过并保留原因"
  fi
done
```

本轮仍采集 D worker，最终按第21步核对各档验证波次的实际BS和TPOT。持续闭环PD中的请求级平均TPOT单列，它可能包含其他请求KV传输和调度影响，不能拿它冒充纯D窗口TPOT。

每次稳态默认预热300秒、统计1800秒、至少30个完整请求。样本不足或窗口截断比例>10%时整档不合格。延长DURATION并新建RUN_ID重跑，不覆盖原始结果。B30=B50也分别保留两个档位记录，不重复计算八张卡的输出。

## 20. D停止诊断后导出最终记录

所有请求结束后，D容器只停止本手册启动的D PID，等待完全退出并释放NPU：

```bash
source /tmp/pdtest030_slo/test.env
ps -p "$(cat "$RUN/decode-diagnostic.pid")" -o pid,args
d_pid=$(cat "$RUN/decode-diagnostic.pid")
kill -TERM "$d_pid"
deadline=$((SECONDS+300))
while kill -0 "$d_pid" 2>/dev/null; do
  if (( SECONDS >= deadline )); then echo 'D尚未退出：停止后续步骤，先检查日志和进程'; exit 2; fi
  sleep 2
done
npu-smi info
```

D宿主机执行：

先确认上述D进程已退出，npu-smi不再显示本轮D worker占用，再导出。若尚有本轮worker，先正常处理退出，不能在旧worker运行时恢复源码或启动第二个D。

```bash
export RUN_ID=pd030_tpot30_50_001
export D_CONTAINER=填写D容器名
export D_EXPORT="/data/pdtest-results/$RUN_ID/D/final"
mkdir -p "$D_EXPORT"
docker cp "$D_CONTAINER:/tmp/pdtest030_slo/runs/$RUN_ID/." "$D_EXPORT/"
```

P宿主机执行：

```bash
export RUN_ID=pd030_tpot30_50_001
export P_CONTAINER=填写P容器名
export SSH_USER=填写SSH用户名
export D_SSH_HOST=填写D宿主机SSH地址
export P_IMPORT="/data/pdtest-results/$RUN_ID/import-final"
mkdir -p "$P_IMPORT"
scp -r "$SSH_USER@$D_SSH_HOST:/data/pdtest-results/$RUN_ID/D/final/." "$P_IMPORT/"
docker exec "$P_CONTAINER" mkdir -p "/tmp/pdtest030_slo/runs/$RUN_ID/d-export-final"
docker cp "$P_IMPORT/." "$P_CONTAINER:/tmp/pdtest030_slo/runs/$RUN_ID/d-export-final/"
```

## 21. P容器：完整日志复核两档BS、TPOT和容量

先用最终日志复核发现阶段所有已有窗口，再重新生成selection。若发现阶段选择变化，不继续沿用原来对应的诊断TTFT记录，新建RUN_ID排查重测。

```bash
source /tmp/pdtest030_slo/test.env
for window in "$RUN"/waves/C*-r*.window.json; do
  [[ -f "$window" ]] || continue
  prefix=${window%.window.json}
  if python "$WORK/audit_window.py" --steps "$RUN/d-export-final/d-steps" \
      --transfer "$RUN/d-export-final/d-transfer" --summary "$window" --run 0 --expected-workers 8 \
      > "${prefix}.audit.json"; then echo "FINAL PASS ${prefix##*/}"; else echo "FINAL INVALID ${prefix##*/}"; fi
done
cp "$RUN/selection.json" "$RUN/selection-before-final-audit.json"
python "$WORK/select_tpot.py" --waves "$RUN/waves" --review "$RUN/review.json" \
  --out "$RUN/selection.json" --env-out "$RUN/selection.env"
python - <<'PY'
import json,os
from pathlib import Path
root=Path(os.environ['RUN'])
before=json.loads((root/'selection-before-final-audit.json').read_text())['selections']
after=json.loads((root/'selection.json').read_text())['selections']
for target in ('30','50'):
    old=before[target]['selection']; new=after[target]['selection']
    assert (old['batch'] if old else None)==(new['batch'] if new else None), 'BS changed after final audit'
print('两档选择与最终审计一致')
PY
python "$WORK/capacity_table.py" --directory "$RUN/d-export-final/d-capacity" --out "$RUN/d-capacity.csv"
```

分别复核两档的诊断验证波次和稳态实际batch分布：

```bash
for T in 30 50; do
  base="$RUN/diagnostic/tpot${T}"
  [[ -d "$base/verification" ]] || continue
  for window in "$base"/verification/*.window.json; do
    prefix=${window%.window.json}
    python "$WORK/audit_window.py" --steps "$RUN/d-export-final/d-steps" \
      --transfer "$RUN/d-export-final/d-transfer" --summary "$window" --run 0 --expected-workers 8 \
      > "${prefix}.audit.json" || break
  done
  for r in 1 2 3; do
    python "$WORK/report_batches.py" --steps "$RUN/d-export-final/d-steps" \
      --summary "$base/steady/r${r}.json" --out "$base/steady/r${r}.batches.json" || break
  done
  python "$WORK/check_diagnostic_case.py" --run "$RUN" --budget "$T" \
    --out "$base/diagnostic-evidence.json" || { echo "本档诊断未通过，禁止生成正式成绩"; break; }
done
```

验证波次必须三次audit.valid=true且各次窗口TPOT≤该档上限。check_diagnostic_case还要求整档诊断流程完成、三次稳态均有8个worker的batch分布。任意不通过则该档诊断结果不能标记已验证；正式脚本会再次检查并拒绝继续。稳态BS分布另记最小值、P50、P95、最大值和全部Decode比例；客户端C数值等于BS并不保证每个稳态迭代实际BS都相等。

## 22. D容器：统一恢复源码并重启正式D

确认D已完全退出、日志已导出后，执行新的统一恢复命令；不运行旧patch_steps --restore：

```bash
source /tmp/pdtest030_slo/test.env
python "$WORK/audit_patch.py" --restore
unset AUDIT_CAPACITY_DIR AUDIT_TRANSFER_DIR AUDIT_STEP_DIR
nohup env LOCAL_IP="$D_IP" NIC="$D_NIC" MODEL="$D_MODEL" PROFILE=0 \
  AUDIT_CAPACITY_DIR= AUDIT_TRANSFER_DIR= AUDIT_STEP_DIR= \
  bash "$WORK/start_d.sh" > "$RUN/decode-formal.log" 2>&1 &
echo $! > "$RUN/decode-formal.pid"
```

必须看到RESTORED。恢复发现其他源码编辑会拒绝覆盖。P模型、代理保持同一配置；D仍保留MRV1、相同MTP/并行/图模式/KV/HBM配置。

## 23. P容器：分别做30/50档正式测试

```bash
source /tmp/pdtest030_slo/test.env
python "$WORK/wait_ready.py" --url "http://$D_IP:8200/health" --timeout 3600
for T in 30 50; do
  if python -c 'import json,sys; r=json.load(open(sys.argv[1]))["selections"][sys.argv[2]]; raise SystemExit(0 if r["status"]=="verified_in_tested_scope" and r["selection"] else 2)' "$RUN/selection.json" "$T"; then
    bash "$WORK/run_budget_case.sh" formal "$T" > "$RUN/formal-tpot${T}.log" 2>&1 || break
  else
    echo "正式TPOT<=$T ms：无合格BS，不生成该档成绩"
  fi
done
```

每档先重新预热，再用实际输出ID的三个共同窗口复测上限；超标或窗口不成立会停止该档。正式阶段没有worker采样，输出窗口数值复测不冒充逐worker验证，实际BS证据来自诊断阶段。正式TTFT和请求级TPOT来自关闭token-ID回显的波次/持续负载，不能与诊断阶段混算。

查看两档结果：

```bash
for T in 30 50; do
  base="$RUN/formal/tpot${T}"
  [[ -f "$base/ttft-comparison.json" ]] || continue
  cat "$base/window-slo-check.json"
  cat "$base/ttft-comparison.json"
done
```

只有生成该档COMPLETED.json、完整跑完并通过长度/失败/样本/截断检查的档位才写正式成绩。run_budget_case 中的请求级 TPOT 不用于重新命名纯D档位；若其明显恶化，结合D传输/排队、P负载另行分析。

## 24. 按两档填写HBM与最终结果

每档都对齐其对应验证波次/稳态时间窗口，逐卡记录D的HBM总量、采样已用量、剩余量和对应KV池逻辑使用率。`d-capacity.csv` 是graph前启动快照，不能代替运行采样；TP8以最紧张一张卡约束容量，不将八张卡空余简单合并给一个rank。

固定MEM_UTIL时KV池可能预先保留，B30和B50的物理占用可能相近。必须分开填写“物理已分配”“KV池容量”“逻辑已用block比例”。缺少可核实指标时填待确认，不写“显存已满”。本流程测的是当前HBM配置下的SLO能力，不能直接把占用量称为最小所需HBM。

如果还要测每档最小可用KV池容量：在完成本轮结果后，另建RUN_ID，在该配置中填写`KV_BYTES`（单位为每NPU字节）、固定相应已验证B作为唯一候选，重新执行诊断/30或50ms上限/三次审计/正式复测。下调后出现OOM、重算、抢占或超过目标TPOT即不合格，保留上一个通过值。不要在本轮中途修改KV_BYTES或MAX_SEQS后沿用旧BS。

P容器创建结果表：

```bash
cat > "$RUN/RESULT.md" <<'PD_RESULT_EOF'
# 128K / 1K，TP8纯D实例平均TPOT约束测试结果

软件/镜像/驱动/CANN/NNAL/权重校验：
MRV1、MTP=1、TP=8、DP=1、Prefix cache关闭、无offload：
P/D通信与时钟同步记录：
MAX_SEQS、MEM_UTIL、KV_BYTES、KV dtype、图模式：
扫描范围（是否完整1..MAX_SEQS）、已测/失败/待核对候选：

| 指标 | TPOT≤30ms | TPOT≤50ms |
|---|---|---|
| 最大已验证D BS（限定已测范围） | | |
| 三次发现窗口平均TPOT及最差重复 | | |
| 三次worker审计 / trace证据 | | |
| 诊断阶段C=BS，真实D BS分布 | | |
| 正式阶段三次输出窗口TPOT复测 | | |
| 每卡KV池容量 / 逻辑使用率 | | |
| 每卡HBM采样已用 / 剩余 / 最紧张卡 | | |
| P/D CPU与网卡负载证据 | | |
| 正式单并发平均TTFT | | |
| 正式C=BS的平均/最慢请求TTFT | | |
| 平均TTFT倍数 / 相对BS倍假设偏差 | | |
| 三次持续PD请求级TPOT/TTFT/吞吐 | | |
| 失败、抢占、重算、样本/截断检查 | | |
| 本档结论：通过/未测得/待验证及原因 | | |

未完成事项与限制：
PD_RESULT_EOF
```

HBM未满不自动推出平均TTFT等于BS倍；按实测比例填写。未采集P worker的实际prefill batch时，不把客户端C当成P batch归因。

如果还要核对TTFT与P计算时间，在正式成绩完成后另做P profiler诊断。P在第6步已注册端点，在P容器执行：

```bash
curl --noproxy '*' --fail-with-body -X POST "http://$P_IP:8100/start_profile"
if python "$WORK/bench_client.py" --base-url http://127.0.0.1:1999 --model "$SERVED_MODEL" \
    --prompts "$RUN/prompt-128k.json" --mode wave --concurrency 1 --requests 1 \
    --output-length 1024 --min-samples 1 --timeout 7200 --out "$RUN/p-profile-single.json"; then
  p_profile_status=0
else
  p_profile_status=$?
fi
curl --noproxy '*' --fail-with-body -X POST "http://$P_IP:8100/stop_profile"
echo "P profiler request exit=$p_profile_status"
```

按第25步从P宿主机导出`$RUN/p-profiles`。该诊断结果不混入正式TTFT；在MindStudio Insight看8卡依赖关键路径，不能相加8卡算子耗时当TTFT。不要将D纯解码TPOT解释成P的TTFT。

## 25. 停止监控并归档两台结果

P容器只停止本文启动的指标采集；代理是否保留按后续使用安排：

```bash
source /tmp/pdtest030_slo/test.env
kill -TERM "$(cat "$RUN/metrics-monitor.pid")"
# 不再使用代理时执行：
# kill -TERM "$(cat "$RUN/proxy.pid")"
```

每台宿主机分别停止监控，ROLE按P/D填写：

```bash
export RUN_ID=pd030_tpot30_50_001
export ROLE=P
export HOST_OUT="/data/pdtest-results/$RUN_ID/$ROLE"
kill -TERM "$(cat "$HOST_OUT/monitor.pid")"
if [[ -f "$HOST_OUT/pidstat.pid" ]]; then kill -TERM "$(cat "$HOST_OUT/pidstat.pid")"; fi
```

P宿主机：

```bash
export RUN_ID=pd030_tpot30_50_001
export P_CONTAINER=填写P容器名
export ARCHIVE_ROOT="/data/pdtest-results/$RUN_ID"
mkdir -p "$ARCHIVE_ROOT/P/container-work"
docker cp "$P_CONTAINER:/tmp/pdtest030_slo/." "$ARCHIVE_ROOT/P/container-work/"
tar -C /data/pdtest-results -czf "/data/${RUN_ID}-P-results.tar.gz" "$RUN_ID"
```

D宿主机：

```bash
export RUN_ID=pd030_tpot30_50_001
export D_CONTAINER=填写D容器名
export ARCHIVE_ROOT="/data/pdtest-results/$RUN_ID"
mkdir -p "$ARCHIVE_ROOT/D/container-work"
docker cp "$D_CONTAINER:/tmp/pdtest030_slo/." "$ARCHIVE_ROOT/D/container-work/"
tar -C /data/pdtest-results -czf "/data/${RUN_ID}-D-results.tar.gz" "$RUN_ID"
```

保留配置、脚本、发现与验证原始请求数据、候选/选择JSON、8卡采样、trace、CPU网卡记录、正式两档结果。模型服务如继续运行，注明日志导出时刻，不删除容器。

## 26. 交付检查

- [ ] 软件栈一致；本轮明确MRV1；P端代理和客户端；D端采样。
- [ ] 新版统一安装/统一恢复，未重复执行旧patch_steps。
- [ ] 输入131072、输出1024真实校验；MTP实际接受token计数。
- [ ] 30ms/50ms是上限，三次各自窗口平均均满足，未用sleep凑值。
- [ ] 两档BS分开选择，C与实际BS分开记录，不重复计8卡输出。
- [ ] 候选8-worker审计、成功KV read证据、窗口无重叠read、trace/日志已核对。
- [ ] 时钟同步误差、P/D CPU与网络负载已保留。
- [ ] 未形成公共解码窗口/无合格BS的档位明确未测得，不外推。
- [ ] 正式测试关闭D采样、重新预热与三次输出窗口上限复测。
- [ ] HBM物理预留、池容量、逻辑使用率和最紧张卡分别填写。
- [ ] 30/50档TTFT对比和稳态PD成绩分开，原始数据已归档。

## 27. 版本依据与检查边界

本文合入的采样内容来自你指定的 [v030rc2适配稿](https://github.com/nobodycan/blue_working/blob/main/v030rc2_%E9%87%87%E6%A0%B7%E9%80%82%E9%85%8D_%E5%85%A8%E9%83%A8%E6%96%87%E4%BB%B6%E4%B8%8E%E4%BF%AE%E6%94%B9%E5%86%85%E5%AE%B9.md)，已核对远端与本地内容相同。

源码核对：[Ascend v0.30.0rc2 runner](https://github.com/vllm-project/vllm-ascend/blob/v0.30.0rc2/vllm_ascend/worker/model_runner_v1.py)、[Mooncake connector](https://github.com/vllm-project/vllm-ascend/blob/v0.30.0rc2/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_hybrid_connector.py)、[官方PD代理](https://github.com/vllm-project/vllm-ascend/blob/v0.30.0rc2/examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py)、[DeepSeek-V4教程](https://github.com/vllm-project/vllm-ascend/blob/v0.30.0rc2/docs/source/tutorials/models/DeepSeek-V4-Flash.md)、[vLLM v0.30.0请求级TPOT计算](https://github.com/vllm-project/vllm/blob/v0.30.0/vllm/benchmarks/serve.py)。链接用于追溯，目标机器不必访问。

本轮已检查18个Python文件、4个Shell文件、62个Bash命令块及5段内嵌Python的语法，并核对22个内嵌脚本与生成源一致、采样适配稿一致。以上均为静态检查，未运行新脚本的端到端或NPU性能测试。你的A2启动、正确性、128K/1K、30/50ms实际达标与HBM结果必须按本文实测。
