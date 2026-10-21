# 两台 A2 的 P/D 独立启动命令

以下命令配合完整手册及其 env.sh、common.sh 使用。

### 8.0 分别启动：三份独立进程，先按前台方式跑通

上一版把 `vllm serve` 命令收在 `serve.sh prefill|decode` 中。下面将两端命令展开，明确每一步在哪里执行。

| 终端 | 所在机器 | 进程 | 占用的 NPU | HTTP 端口 |
|---|---|---|---|---|
| 终端 1 | P 机器（示例 10.10.0.11） | Prefill / kv_producer | 本机 0～7，TP8 | 8100 |
| 终端 2 | D 机器（示例 10.10.0.12） | Decode / kv_consumer | 本机 0～7，TP8 | 8200 |
| 终端 3 | P 机器的另一个 SSH 终端 | Proxy | CPU 进程 | 1999 |

前提：第 3～5 节已经完成，两台都有模型、运行中的 `v4flash-2node` 容器、相同 `env.sh` 和各自的 `env.local.sh`。P、D 是两份独立的模型进程，分别加载权重，模型路径必须相同且内容一致。两台各用 8 卡，没有将两台拼成一个 TP16 实例。

**本节前台方式与 8.1～8.3 节后台方式二选一。跑通前台之后，要先停止前台服务，再改用后台启动；不能对同一个角色重复启动。** 下列前台命令用于 `PROFILE=0` 的基础运行；需要 Profiling 时使用第 11 节的脚本入口。

#### 终端 1：P 宿主机，进入容器

```bash
# 在 P 宿主机执行
cd /data/v4flash-2node
source env.sh
docker exec -it "$CONTAINER" bash
```

进入 P 容器后执行完整 Prefill 命令；保持此终端运行：

```bash
export AUDIT_STEP_DIR=/bench/results/steps/pd-P
source /bench/scripts/common.sh
KV_ARGS=()
if [[ -n "$KV_BYTES" ]]; then KV_ARGS+=(--kv-cache-memory-bytes "$KV_BYTES"); fi
vllm serve "$MODEL" \
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
 --kv-transfer-config '{
   "kv_connector":"MooncakeHybridConnector",
   "kv_role":"kv_producer","kv_port":"30000","engine_id":"0",
   "kv_connector_extra_config":{
     "prefill":{"dp_size":1,"tp_size":8},
     "decode":{"dp_size":1,"tp_size":8}
   }
 }' "${KV_ARGS[@]}"

```

P 的角色由 `kv_role=kv_producer` 指定。P HTTP 端口是 8100；30000 是这里配置的连接器端口，不是客户端请求端口。

#### 终端 2：D 宿主机，进入容器

```bash
# 在 D 宿主机执行
cd /data/v4flash-2node
source env.sh
docker exec -it "$CONTAINER" bash
```

进入 D 容器后执行完整 Decode 命令；保持此终端运行：

```bash
export AUDIT_STEP_DIR=/bench/results/steps/pd-D
export AUDIT_TRANSFER_DIR=/bench/results/transfer/pd-D
source /bench/scripts/common.sh
KV_ARGS=()
if [[ -n "$KV_BYTES" ]]; then KV_ARGS+=(--kv-cache-memory-bytes "$KV_BYTES"); fi
vllm serve "$MODEL" \
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
 --additional-config '{
   "ascend_compilation_config":{"enable_npugraph_ex":true,"enable_static_kernel":false},
   "enable_cpu_binding":true,"enable_dsa_cp":false,
   "multistream_overlap_shared_expert":true,"recompute_scheduler_enable":true
 }' \
 --kv-transfer-config '{
   "kv_connector":"MooncakeHybridConnector",
   "kv_role":"kv_consumer","kv_port":"30100","engine_id":"1",
   "kv_connector_extra_config":{
     "prefill":{"dp_size":1,"tp_size":8},
     "decode":{"dp_size":1,"tp_size":8}
   }
 }' "${KV_ARGS[@]}"

```

D 的角色由 `kv_role=kv_consumer` 指定。D HTTP 端口是 8200；30100 是这里配置的连接器端口。两个 JSON 中的 prefill/decode 并行元数据都必须填写实际布局 DP1/TP8。这里只启动 D 服务不会产生纯 D 工作负载，必须经代理完成 P→KV→D 请求流程。

#### 终端 3：P 宿主机，检查两端，再启动代理

等终端 1、2 的启动日志确认 API ready 后执行：

```bash
# 在 P 宿主机执行
cd /data/v4flash-2node
source env.sh
curl --noproxy '*' -fsS "http://$P_IP:8100/health"
curl --noproxy '*' -fsS "http://$D_IP:8200/health"
docker exec -it "$CONTAINER" bash
```

进入 P 容器后，使用固定版本实验代理：

```bash
source /bench/scripts/common.sh
python /bench/upstream/load_balance_proxy_server_example.py \
  --host 0.0.0.0 --port "$PROXY_PORT" --workers 1 --max-retries 1 \
  --prefiller-hosts "$P_IP" --prefiller-ports 8100 \
  --decoder-hosts "$D_IP" --decoder-ports 8200
```

这里是基础代理进程。若需要 P HTTP/D 首字节诊断，改用本节基础代理的同等入口 `PROXY_AUDIT=1 bash /bench/scripts/start_proxy.sh`，二者不能同时占用 1999 端口。

#### 第四个终端：发送请求，只连代理端口

```bash
# 可在 P 宿主机执行；替换示例 IP
curl --noproxy '*' -fsS http://10.10.0.11:1999/healthcheck
curl --noproxy '*' -fsS http://10.10.0.11:1999/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"dsv4","messages":[{"role":"user","content":"请计算 2 加 3。"}],"temperature":0,"max_tokens":256,"stream":false}'
```

后续 128K/1K 压测的 `--base-url` 填 `http://P_IP:1999`。直接请求 8100/8200 只用于健康检查或明确标注的局部诊断，不能当成完整 PD 端到端测试。健康检查成功之后，还要通过第 8.4 节的精确 token 计数、外部 KV 日志和正确性闸门。

#### 同样配置的最短启动入口

在模型和容器准备好以后，三个服务也可以分别用下面的命令启动。每行仍须在标注的机器执行：

```bash
# P 宿主机，前台保持运行
docker exec -it v4flash-2node bash /bench/scripts/serve.sh prefill
```

```bash
# D 宿主机，前台保持运行
docker exec -it v4flash-2node bash /bench/scripts/serve.sh decode
```

```bash
# P 宿主机另一个终端，等两端模型 ready 后执行
docker exec -it -e PROXY_AUDIT=1 v4flash-2node bash /bench/scripts/start_proxy.sh
```

下面 8.1～8.3 节提供同一启动入口的 nohup 后台方式，便于长时间测量与保存日志。

