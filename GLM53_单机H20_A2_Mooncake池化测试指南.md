# GLM-5.3 单机 H20 / 单机 A2：Mooncake 池化测试指南

日期：2026-11-16。

## 0. 范围与前置条件

按两套独立设备编写：一台H20、一台昇腾A2，每台8张卡。每台一个推理实例：TP8、DP1、EP8、PP1，不开MTP，DCP=1，不主动开启PCP。

- P/D混合推理，不做P/D分离；Mooncake用于KV存储池，不是P→D直传。
- 本机CPU内存池，不用SSD，不连接其他机器上的存储池。
- 为减少网络命名空间问题，master与vLLM在同一容器的不同终端运行，使用127.0.0.1:50088。
- AISBench可在另一Python环境或客户端运行。示例客户端可通过127.0.0.1:8000访问服务；异机客户端需改为服务端IP。
- 配置结构依据vLLM/vLLM-Ascend v0.30.0文档。其他版本先核对接口。

**本文不是实测报告。启动示例是待验证模板，不代表GLM-5.3的当前量化、DSA/indexer、KV dtype与连接器组合已经验证通过。**

### 先确认模型能否在单机装下

1. 原定W4A4C8在H20/A2两边的支持尚未落实，不能仅凭镜像或参数推定可用。
2. H20公开部署路线可参考W4A8配合FP8 KV，但这不是W4A4C8。
3. A2公开GLM-5.3路线包含W8A8C8；不能直接使用950DT的MXFP4权重。
4. 如果A2为8×64GB，总HBM为512GB；若所选权重和运行开销超过此容量，固定TP8/DP1/PP1的单机实验不能成立。
5. 即使权重装得下，也要给长prefill、KV、通信、图捕获留空间。CPU权重卸载、增加PP/DP或换模型都会改变实验条件，不应静默采用。

先用已有可用命令启动无池模型。无法加载时，正式实验标记“容量/适配阻塞”，不要换成小模型后声称完成原实验。

## 1. 三组实验固定口径

| 场景 | KV池 | 本地prefix cache | 前缀数 | 正式输入/输出 | 并发 | 正式条数 |
|---|---|---|---:|---|---:|---:|
| S1 | 关闭 | 开启 | 1 | 131072 / 1 token | 10 | 30 |
| S2 | 关闭 | 开启 | 10 | 131072 / 1 token | 10 | 30 |
| S3 | 开启 | 开启 | 10 | 131072 / 1 token | 10 | 30 |

- 公共前缀构造比例90%；缓存命中长度还受tokenizer接缝、特殊token和block边界影响。
- 先单独预热，预热并发统一为1，条数1/10/10，不计入正式30条。
- S2/S3复用同一份p10数据；H20/A2复用同一整份数据和一致的tokenizer。
- 同平台三个场景保持量化、KV dtype、图模式、通信环境、batch budget等相同。
- 各场景开始前隔离旧缓存。主测不主动清预热后的HBM。
- 使用default_perf，不用stable_stage；输出1 token，不用TPOT评价。

**“开启池”不代表“请求实际读池”。S3仍可能全部命中本地HBM。**

## 2. 单机布局与目录

```text
终端1：Mooncake master，监听50088（S3使用）
终端2：vLLM，8张卡，监听8000
终端3：AISBench客户端/压测脚本
```

若master和vLLM不在同一容器/网络命名空间，127.0.0.1不能表示对方，必须另配可达地址。本文默认不采用这种布局。无需额外部署etcd。

示例目录：

```text
/opt/pool_test/                       主脚本、启动模板
/opt/aisbench_auto_tools_prefix/       已安装且已信任的工具
/models/GLM-5.3/                      客户端tokenizer目录
/models/实际量化权重/                  服务端权重
/data/glm53_pool/                     池配置、服务端日志
/data/glm53_pool_data_r1/              固定数据；prepare前必须不存在
/data/glm53_pool_runs/                 压测结果
```

本文示例用于无鉴权内部服务。文件若已存在，先检查内容，不盲目覆盖。每次实验使用新的服务日志目录。

## 3. 环境检查

### H20服务容器

```bash
nvidia-smi
python3 -m pip show vllm torch mooncake-transfer-engine
command -v mooncake_master
free -h
ulimit -l
vllm serve --help > /tmp/vllm_help_h20.txt
```

确认8张卡可见、驱动满足镜像CUDA要求。H20使用CUDA侧Mooncake包，不是NPU包。RDMA测试还需要验证GPU/NIC拓扑与RDMA环境；本文先用TCP做本机功能验证，不将其等同RDMA性能。

### A2服务容器

```bash
npu-smi info
python3 -m pip show vllm vllm-ascend torch torch-npu mooncake-transfer-engine-npu
command -v mooncake_master
ldd --version
free -h
ulimit -l
test -r /etc/hccn.conf
vllm serve --help > /tmp/vllm_help_a2.txt
```

检查镜像配套HDK、CANN、NPU Mooncake wheel及glibc兼容性。v0.30.0特性指南列有CANN 9.1.0+、推荐HDK 25.5+及wheel依赖；具体组合以镜像/软件兼容表为准，不只是版本号越大越好。

保留原来能正常启动模型的CANN/ATB环境与Mooncake动态库路径，不盲目升级依赖。A2不照搬A3 FabricMem设置，也不直接执行固定数量的大页分配命令；先核对实际页大小与内存预算。

## 4. H20本机池配置

在H20服务容器创建配置：

```bash
mkdir -p /data/glm53_pool/h20/config
cat > /data/glm53_pool/h20/config/mooncake.json <<'JSON'
{
  "mode": "embedded",
  "metadata_server": "P2PHANDSHAKE",
  "master_server_address": "127.0.0.1:50088",
  "global_segment_size": "1GB",
  "local_buffer_size": "1GB",
  "protocol": "tcp",
  "device_name": "",
  "enable_offload": false
}
JSON
```

- embedded模式由vLLM worker贡献CPU内存，master本身不是完整KV数据存储节点。
- 1GB是每worker池段，TP8约贡献8GB，另有每worker传输缓冲等内存。
- **1GB仅用于小请求验证，不是10条长前缀正式测试容量。** 正式容量见第8节。
- 首先不开SSD、不上独立存储节点。若正式测试改为rdma，需重新验证、归档配置，不与TCP结果混为一组。

### 4.1 终端1启动master

先确认端口未被其他程序使用，不直接kill占用进程：

```bash
ss -lntp | grep ':50088' || true
```

然后在独立终端保持运行：

```bash
mkdir -p /data/glm53_pool/h20/master_smoke
set -o pipefail
mooncake_master --port 50088 2>&1 | tee /data/glm53_pool/h20/master_smoke/master.log
```

自定义lease、淘汰等参数时，按当前版本的`mooncake_master --help`配置并留档。

### 4.2 H20启动模板

将以下代码保存为`/opt/pool_test/start_h20_pool_test.sh`。先补齐MODEL_ARGS，不要直接假定空数组能加载你的量化权重。

```bash
#!/usr/bin/env bash
# 装订版本，work by claude code，2026-11-16
set -Eeuo pipefail
MODE=${1:?用法：bash start_h20_pool_test.sh off或on}
[[ "$MODE" == off || "$MODE" == on ]] || exit 2
: "${MODEL_DIR:?先设置已验证的H20权重路径}"
: "${SERVER_RUN:?先设置本次新的日志目录}"
mkdir "$SERVER_RUN"
export PYTHONHASHSEED=0
export CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7

# 补入原来验证过的量化、KV dtype、图模式等参数，三个场景保持相同。
# fp8_ds_mla等dtype必须另行验证与GLM-5.3/Store连接器的兼容性。
MODEL_ARGS=()

CMD=(vllm serve "$MODEL_DIR"
  --served-model-name glm53 --host 0.0.0.0 --port 8000
  --tensor-parallel-size 8 --data-parallel-size 1
  --enable-expert-parallel --decode-context-parallel-size 1
  --enable-prefix-caching --enable-chunked-prefill
  --max-model-len 135168 --max-num-seqs 10
  --max-num-batched-tokens 16384
  "${MODEL_ARGS[@]}")
if [[ "$MODE" == on ]]; then
  export MOONCAKE_CONFIG_PATH=/data/glm53_pool/h20/config/mooncake.json
  CMD+=(--kv-transfer-config '{"kv_connector":"MooncakeStoreConnector","kv_role":"kv_both"}')
else
  unset MOONCAKE_CONFIG_PATH
fi
printf '%q ' "${CMD[@]}" > "$SERVER_RUN/actual_start_cmd.txt"
printf '\n' >> "$SERVER_RUN/actual_start_cmd.txt"
printf 'mode=%s\nPYTHONHASHSEED=%s\n' "$MODE" "$PYTHONHASHSEED" > "$SERVER_RUN/selected_environment.txt"
if [[ "$MODE" == on ]]; then cp "$MOONCAKE_CONFIG_PATH" "$SERVER_RUN/mooncake.json"; fi
"${CMD[@]}" 2>&1 | tee "$SERVER_RUN/server.log"
```

无MTP配置，不额外增加PP/CPU权重卸载。原启动命令若有speculative/MTP配置，改写时务必移除。图模式、量化、显存利用率等使用已验证值，不能只为S3单独调整。

## 5. A2本机池配置

在A2服务容器创建：

```bash
mkdir -p /data/glm53_pool/a2/config
cat > /data/glm53_pool/a2/config/mooncake.json <<'JSON'
{
  "metadata_server": "P2PHANDSHAKE",
  "protocol": "ascend",
  "device_name": "",
  "master_server_address": "127.0.0.1:50088",
  "global_segment_size": "1GB",
  "preferred_segment": false,
  "prefer_alloc_in_same_node": true,
  "enable_ssd_offload": false,
  "tenant_id": "glm53_a2_smoke_01"
}
JSON
```

1GB同样只是小请求验证。A2使用Ascend协议，不能直接用H20的wheel和通信配置。优先本机分配不保证所有版本的数据路径都绕开RoCE。

### 5.1 终端1启动master

```bash
mkdir -p /data/glm53_pool/a2/master_smoke
set -o pipefail
mooncake_master --port 50088 2>&1 | tee /data/glm53_pool/a2/master_smoke/master.log
```

H20和A2在不同机器，因此端口相同不冲突。不要与其他业务共享本实验master。

### 5.2 A2启动模板

保存为`/opt/pool_test/start_a2_pool_test.sh`，先补齐MODEL_ARGS：

```bash
#!/usr/bin/env bash
# 装订版本，work by claude code，2026-11-16
set -Eeuo pipefail
MODE=${1:?用法：bash start_a2_pool_test.sh off或on}
[[ "$MODE" == off || "$MODE" == on ]] || exit 2
: "${MODEL_DIR:?先设置已验证且单机能装下的A2权重路径}"
: "${SERVER_RUN:?先设置本次新的日志目录}"
mkdir "$SERVER_RUN"
export PYTHONHASHSEED=0
export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7

# 此前已加载镜像配套CANN/ATB环境及Mooncake库路径。
# 通信环境在off/on间保持一致，不只为on场景修改。
# ModelSlim通常需要--quantization ascend，其他KV/图模式参数按实际适配填写。
MODEL_ARGS=()

CMD=(vllm serve "$MODEL_DIR"
  --served-model-name glm53 --host 0.0.0.0 --port 8000
  --tensor-parallel-size 8 --data-parallel-size 1
  --enable-expert-parallel --decode-context-parallel-size 1
  --enable-prefix-caching --enable-chunked-prefill
  --max-model-len 135168 --max-num-seqs 10
  --max-num-batched-tokens 16384
  "${MODEL_ARGS[@]}")
if [[ "$MODE" == on ]]; then
  export MOONCAKE_CONFIG_PATH=/data/glm53_pool/a2/config/mooncake.json
  CMD+=(--kv-transfer-config '{"kv_connector":"AscendStoreConnector","kv_role":"kv_both","kv_connector_extra_config":{"backend":"mooncake","lookup_rpc_port":"0","use_layerwise":false}}')
else
  unset MOONCAKE_CONFIG_PATH
fi
printf '%q ' "${CMD[@]}" > "$SERVER_RUN/actual_start_cmd.txt"
printf '\n' >> "$SERVER_RUN/actual_start_cmd.txt"
printf 'mode=%s\nPYTHONHASHSEED=%s\n' "$MODE" "$PYTHONHASHSEED" > "$SERVER_RUN/selected_environment.txt"
if [[ "$MODE" == on ]]; then cp "$MOONCAKE_CONFIG_PATH" "$SERVER_RUN/mooncake.json"; fi
"${CMD[@]}" 2>&1 | tee "$SERVER_RUN/server.log"
```

如采用官方A2 RoCE路径，在正确Ascend环境中设置真实值：

```bash
export HCCL_IF_IP=本机实际业务IP
export GLOO_SOCKET_IFNAME=实际网卡名
export TP_SOCKET_IFNAME=实际网卡名
export HCCL_SOCKET_IFNAME=实际网卡名
export HCCL_INTRA_ROCE_ENABLE=1
```

这些环境同样用于off测试。保留已验证配置，冲突时先核对镜像/CANN指导，不机械叠加。

本场景只用AscendStoreConnector存储池，**不额外添加MooncakeConnectorV1/V2做P→D传输**。

## 6. 小请求验收

设置真实权重路径、新服务日志目录，在终端2启动对应on模板；例如H20：

```bash
export MODEL_DIR=/models/实际H20量化权重
export SERVER_RUN=/data/glm53_pool/h20/service_smoke_on
bash /opt/pool_test/start_h20_pool_test.sh on
```

A2对应使用其模板和日志路径。等待ready后：

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/v1/models
```

核对启动日志：8个worker、并行分组、MTP/DCP设置、连接器类名、地址和池段注册正确，无缺库、布局断言或注册/连接失败。特别核对GLM-5.3主KV及DSA indexer cache，而不只是通用模型能运行。

### 两条共享前缀、不同后缀的请求

以下前缀长度足以跨多个缓存块，不是正式128K数据。非流式仅用于功能验证：

```bash
mkdir -p /data/glm53_pool/smoke_requests
python3 -I - <<'PY'
import json
from pathlib import Path
root = Path('/data/glm53_pool/smoke_requests')
prefix = 'This is a repeated document for a cache functionality check. ' * 256
for i in (1, 2):
    body = {'model': 'glm53', 'prompt': prefix + f' Unique suffix number {i}.',
            'max_tokens': 1, 'min_tokens': 1, 'ignore_eos': True,
            'temperature': 0, 'stream': False}
    (root / f'request{i}.json').write_text(json.dumps(body), encoding='utf-8')
# 装订版本，work by claude code，2026-11-16
PY

curl --fail --silent --show-error http://127.0.0.1:8000/metrics \
  > /data/glm53_pool/smoke_requests/before.metrics

for i in 1 2; do
  curl --fail --silent --show-error http://127.0.0.1:8000/v1/completions \
    -H 'Content-Type: application/json' \
    --data-binary "@/data/glm53_pool/smoke_requests/request${i}.json" \
    > "/data/glm53_pool/smoke_requests/response${i}.json"
done

curl --fail --silent --show-error http://127.0.0.1:8000/metrics \
  > /data/glm53_pool/smoke_requests/after.metrics
```

确认请求成功、实际输出1 token，并检查写池完成。第二次更快或本地命中，只能证明前缀复用，不能证明回读。

### 真正的回读验收

1. 确认前缀对象已成功写入，异步写入结束、无失败。
2. 用当前版本已验证的“仅清本地缓存”方法，保留外部对象。
3. 再发相同前缀、不同后缀，确认真实外部读取字节、命中、重算量及输出正确性。
4. 未核实前不调用reset-prefix-cache；接口可能连带通知/失效外部缓存。
5. 不假设重启vLLM会保留数据。embedded/worker贡献的存储段可能随worker退出失效；master元数据还在也不代表数据有效。

不能完成第2步时，把强制回读验收标记未完成，不伪造结论。主测仍按热HBM口径进行。

## 7. 准备固定AISBench数据

将以下文件复制到Linux客户端的`/opt/pool_test`：

```text
glm53_pool_aisbench.sh
glm53_pool_aisbench_README.md
glm53_pool_aisbench_test.py       可选，只做离线逻辑检查
```

激活AISBench环境，在同一个客户端终端执行：

```bash
cd /opt/pool_test
COMMON=(
  --tools-dir /opt/aisbench_auto_tools_prefix
  --tokenizer /models/GLM-5.3
  --data-dir /data/glm53_pool_data_r1
  --out-root /data/glm53_pool_runs
)
bash glm53_pool_aisbench.sh check "${COMMON[@]}"
bash glm53_pool_aisbench.sh prepare "${COMMON[@]}"
```

只prepare一次，将整份数据目录连同manifest复制给另一平台。数据含p1/p10，每份full.jsonl为30条；p10每前缀3条。脚本验证行数、唯一性、循环分组、实际token长度、前缀匹配和哈希。

若生成长度不精确，run默认拒绝。严格128K应修正生成器、用新目录重生成；明确接受近似值才加`--allow-length-drift`，报告实际范围。最终仍需核对服务端usage和已安装AISBench数据模板。

## 8. 正式池容量：不能直接使用示例1GB

预算方法：

```text
P = 10条前缀
L = 每条实际可缓存前缀长度，约117964 token，需按block/tokenizer复核
B_rank = 当前模型、indexer、KV dtype和布局下每rank每逻辑token的实际存储字节
每rank所需池段 ≈ P × L × B_rank + 格式对齐/元数据/其他写入/淘汰余量
节点池段内存 ≈ 8 × 每rank池段
```

B_rank优先根据池对象/写入字节与实现布局测量。MLA/DSA在TP间可能复制，不能把逻辑KV简单除以8；主KV和indexer都要计入。

另计H20每worker local_buffer_size、A2实际注册/staging缓冲、通信、Python、操作系统和AISBench内存。后缀也可能写池，不能仅预算90%前缀。

池太小会淘汰，太大可能注册/锁页失败。没有适用于所有机器的固定GB值。调整后正常停止自己本次的worker/master，再按新配置启动、验收，归档正式配置。

**CPU池扩容不自动解决活动KV的HBM不足。** 10条128K若不能同时驻留，可能排队、抢占、重算；应记录为容量/调度现象，而非纯读取时延。

## 9. 正式执行：每个平台三组

先补齐两个启动模板的MODEL_ARGS，固定每个平台已验证的参数。正式S3前把池配置改为第8节确定的容量，并完成小请求验收。

### 9.1 选择平台

H20服务端终端：

```bash
export PLATFORM=H20
export START=/opt/pool_test/start_h20_pool_test.sh
export MODEL_DIR=/models/实际H20量化权重
export BASE=/data/glm53_pool/h20
```

A2服务端终端：

```bash
export PLATFORM=A2
export START=/opt/pool_test/start_a2_pool_test.sh
export MODEL_DIR=/models/实际A2量化权重
export BASE=/data/glm53_pool/a2
```

终端变量不会自动共享。客户端也设置PLATFORM/BASE，且COMMON数组应来自第7节；客户端不在同文件系统时，复制actual_start_cmd.txt到客户端后传本地路径。

### 9.2 S1：无池、1前缀

正常停止自己先前的smoke服务，确认旧worker退出、端口释放。日志目录必须是新的。

服务端终端：

```bash
export SERVER_RUN="$BASE/service_S1_r1"
bash "$START" off
```

客户端终端以H20为例（A2改两个变量）：

```bash
export PLATFORM=H20
export BASE=/data/glm53_pool/h20
export SERVER_RUN="$BASE/service_S1_r1"
bash glm53_pool_aisbench.sh run "${COMMON[@]}" \
  --platform "$PLATFORM" --scene 1 \
  --host 127.0.0.1 --port 8000 --model glm53 \
  --server-config "$SERVER_RUN/actual_start_cmd.txt" --cache-ready
```

脚本预热1条，正式30条。off仅移除池连接器，保留本地prefix cache。

### 9.3 S2：无池、10前缀

正常停止S1服务，确认worker退出，以新off服务隔离旧HBM缓存。

服务端终端：

```bash
export SERVER_RUN="$BASE/service_S2_r1"
bash "$START" off
```

客户端等待ready后：

```bash
export SERVER_RUN="$BASE/service_S2_r1"
bash glm53_pool_aisbench.sh run "${COMMON[@]}" \
  --platform "$PLATFORM" --scene 2 \
  --host 127.0.0.1 --port 8000 --model glm53 \
  --server-config "$SERVER_RUN/actual_start_cmd.txt" --cache-ready
```

### 9.4 S3：有池、10前缀

正常停止S2服务。正式S3使用独立、干净的master/worker生命周期，不延用smoke旧对象。终端1启动正式配置所对应的master，记录参数；不要动其他业务进程。

服务端终端：

```bash
export SERVER_RUN="$BASE/service_S3_r1"
bash "$START" on
```

客户端：

```bash
export SERVER_RUN="$BASE/service_S3_r1"
bash glm53_pool_aisbench.sh run "${COMMON[@]}" \
  --platform "$PLATFORM" --scene 3 \
  --host 127.0.0.1 --port 8000 --model glm53 \
  --server-config "$SERVER_RUN/actual_start_cmd.txt" --cache-ready
```

预热10条后脚本暂停。确认本轮写池完成、无失败/回退、引擎空闲，再输入yes，正式测30条。

- `--cache-ready`是操作者确认，不是清缓存指令。
- `--scene 3`不会自动开启池，必须先on启动服务。
- 无人值守需提供真实有效的`--pool-check-script`，接口见原脚本说明；不能用无条件返回0冒充验证。
- 主测不清预热后的HBM；强制回读应另设“HBM冷/池热”状态，独立标注。

分别完成H20的S1/S2/S3、A2的S1/S2/S3。复测使用新r2/r3服务目录，重新隔离缓存并预热，不直接重放旧完整请求，否则后缀也可能命中。

## 10. 结果验收

压测结果在`/data/glm53_pool_runs/run_时间_随机标识/`。只用measure统计正式30条；warmup另存。将服务端server.log、master.log及配置一起归档。

| 项目 | 要求/解释 |
|---|---|
| 请求数 | 正式成功30、失败0，无额外自动补发 |
| 长度 | 逐条核对实际输入/输出，不仅看目标值 |
| 性能 | TTFT均值/P50/P90/最大值、总完成时间；30条的P99不当稳定结论 |
| 本地命中 | 用正式阶段同进程metrics增量核对 |
| 外部命中 | 单独核对，不与本地计数盲目相加 |
| 外部读取字节 | 证明真实读池，master在线或lookup命中不够 |
| 失败/回退 | 读失败后的重新prefill不能算成功池回读 |
| 容量 | HBM KV块、池占用/淘汰、抢占和重算 |
| 配置 | 镜像/包版本、量化、KV dtype、图模式、网络后端、池容量 |

结果解释：

1. S2全留在HBM、S3不读池：只能说该工作集主要命中本地，不能评价回读性能。
2. S2频繁淘汰重算、S3成功读池且重算减少：可以讨论减少重算的收益，并计入读写成本。
3. S3池小、读失败或频繁抢占：先定位容量/适配/调度限制，不能简单解释为“池化更慢”。

脚本退出0只说明流程和客户端汇总计数通过，不代表所有服务端状态、长度和真实回读均已自动验证。

## 11. 常见问题

- **master在线但不写池**：检查连接器类、配置文件路径、worker池段注册。
- **A2 errno 12/大段主机内存注册失败**：先缩小池段复现，核对HDK/CANN/Mooncake ABI、注册/锁页资源；不要盲目申请几十GB或增加大页。已有问题报告没有通用已确认修复。
- **H20加入Store后DSA/FP8报布局错误**：保存最小复现，核对组合兼容性。切换dtype后必须重做对应基线。
- **S3快但外部读取为0**：可能只是HBM命中。
- **重启后池读不到**：worker提供的存储段可能失效，master元数据不代表数据存活。
- **AISBench计数解析失败**：保留原生报告，核对版本格式，不把未知当成功。
- **对照中调了图模式/通信/显存参数**：重新成组测试，不只调一个场景。

## 12. 参考与验证状态

- [vLLM v0.30.0 MooncakeStoreConnector](https://github.com/vllm-project/vllm/blob/v0.30.0/docs/features/mooncake_store_connector_usage.md)
- [vLLM-Ascend v0.30.0 AscendStore KV池](https://github.com/vllm-project/vllm-ascend/blob/v0.30.0/docs/source/user_guide/feature_guide/kv_cache_pool_ascend_store.md)
- [GLM-5.3 vLLM recipe；main会变动，实验时固定版本](https://github.com/vllm-project/recipes/blob/main/models/zai-org/GLM-5.3.yaml)
- [GLM-5.3 Ascend教程](https://docs.vllm.ai/projects/ascend/en/v0.30.0/tutorials/models/GLM5.2.html)
- [A2主机内存注册失败问题报告，仅供排障](https://github.com/vllm-project/vllm-ascend/issues/19721)

配套AISBench主脚本已做Bash语法检查及13项离线测试。本文启动模板只做静态检查，尚未在真实H20/A2、GLM-5.3、Mooncake环境联调。没有远程执行部署、安装依赖或修改服务器。

> 装订版本，work by claude code，2026-11-16
