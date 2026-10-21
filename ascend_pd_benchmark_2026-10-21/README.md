# 昇腾 A2 / DeepSeek-V4-Flash W8A8：PD 分离、HBM 容量与 TTFT 工程测试手册

版本：2026-10-21。本文面向 Linux/Bash、Atlas 800 A2、每台 8 张 64GB NPU。设备实际容量以 `npu-smi` 为准。所有 IP、网卡名和目录示例必须替换。

**验证状态：已联网核对固定版本官方文档和关键源码；配套脚本做本地检查。本文没有在你的 A2 机器上执行，不包含任何虚构的实测性能数据。** 部署、模型正确性及性能结论，必须通过下面的现场验收。

本文选择 `quay.io/ascend/vllm-ascend:v0.23.0` 与 `Eco-Tech/DeepSeek-V4-Flash-w8a8-mtp`，因为可以找到对应 tag 的完整 A2 部署资料。该选择用于建立可复现基准，不表示它是当前最快版本。**不要把这个 checkpoint 与 `DeepSeek-V4-Flash-0731-w8a8`、DSpark 或其他镜像混用。** 官方给出前者的单台 A2 部署条件，且 A2/A3 的 V4-Flash 支持状态仍标有实验性质。[固定版本模型指南](https://docs.vllm.ai/projects/ascend/en/v0.23.0/tutorials/models/DeepSeek-V4-Flash.html) [支持矩阵](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/support_matrix/supported_models.html)

## 1. 要回答的工程问题

对于每个输入长度 `L ∈ {16384,65536,131072}`，固定输出 `O=1024`、模型、量化、并行布局、KV 格式和调度设置：

1. 在平均 TPOT ≤30ms、≤50ms 时，分别能承载多少**客户端并发**，对应多少实际活跃 Decode 序列？
2. 为这些序列提供服务，各个 NPU 至少需要多大的 KV pool？加上权重、工作区、图和通信后，整卡 HBM 需求是多少？
3. 低排队条件下，实际 TTFT 比 Prefill 的 NPU 执行时间多多少？这些时间来自框架、P/D 交接还是数据传输？
4. 改一个优化参数后，这些量有没有改善，且模型输出仍然正确？

最终产物应是 `L × C_client × 实际 B_decode × KV预算 × 并行布局` 的结果表和曲线。不要只输出一个“最大 batch”数字。

## 2. 必须先修正的测量口径

### 2.1 三种内存不是同一个量

| 名称 | 含义 | 获取方式 |
|---|---|---|
| 整卡 HBM 占用 | 权重、KV 预分配、激活、通信、图、分配器等 | `npu-smi` + 启动日志 + 分配器数据 |
| KV pool 已分配容量 | 引擎预先申请的物理 KV 缓冲区 | 本文容量探针的 planned/actual storage bytes |
| 活跃请求的逻辑 KV 使用量 | 请求正在占用哪些 cache blocks | KV manager 指标/服务 profiler；混合组需要分别解释 |

**不能用“压测时 HBM－服务空闲时 HBM”除以请求数，求每请求 KV。** KV pool 通常在初始化时已经分配，开始服务后整卡占用可以几乎不变。`--gpu-memory-utilization 0.9` 是执行器内存比例约束，也不表示“90% HBM 全部用于 KV”。[CacheConfig 定义](https://docs.vllm.ai/en/v0.23.0/configuration/engine_args/#cacheconfig)

DeepSeek-V4 有压缩/混合注意力状态。W8A8 描述权重和激活精度，**不等于 KV 是 INT8**。也不能未经验证就把 KV 除以 TP；部分状态可能在 TP ranks 间复制。本手册直接逐 rank 读取实际 storage 和 cache specs。[NPU KV 分配源码](https://github.com/vllm-project/vllm-ascend/blob/v0.23.0/vllm_ascend/worker/model_runner_v1.py) [KV specs 定义](https://github.com/vllm-project/vllm/blob/v0.23.0/vllm/v1/kv_cache_interface.py)

### 2.2 并发、batch 和 token budget 分别记录

- `C_client`：已经发出、尚未完成的客户端请求数。
- `B_decode`：某个 D engine/DP rank 在某个实际 step 中处理的活跃生成序列数。
- `max-num-seqs`：调度上限，不是实际 batch。
- `max-num-batched-tokens`：一次调度的 token 预算；长输入会被分 chunk，MTP 也会影响每序列的 token 预算。

固定客户端并发不保证 D 侧固定 batch：请求还可能处于 Prefill、传输或等待阶段。只能在 profiler 的 batch 轨迹或调度数据证实存在稳定的 `B_decode` 平台后，把它称作固定 Decode batch 基准。没有平台时，结果属于服务并发测试。

### 2.3 TTFT 比较的正确形式

将“GPU 计算时间”统一改称 **NPU 阶段执行时间**：算子执行本身包括 HBM 访问和必要的设备通信，不是纯算术 FLOPs 时间。

对本版本的非 layerwise 示例代理，源码会先给 P 发 `max_tokens=1` 的非流式请求，再请求 D 的流式结果。因此客户端看到的 TTFT 包含 P 阶段和 D 首次输出前的交接；不能只看到 P 已生成一个 token，就认定客户端已经收到首 token。[固定版本代理源码](https://github.com/vllm-project/vllm-ascend/blob/v0.23.0/examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py)

低排队时可以研究：

```text
TTFT_client ≈ P阶段关键路径 + P/D交接暴露时间 + D首次输出路径 + 代理/网络开销
```

这个式子是归因模型；存在重叠时按时间线算关键路径，不能把各阶段总耗时直接相加。

同时记录：

```text
T_P_span  = 一次请求 Prefill 首个相关设备事件到最后相关设备事件的跨度
T_P_union = 同一设备、该请求相关设备事件区间的并集长度
Δ_raw     = TTFT_client - T_P_span
r_raw     = Δ_raw / TTFT_client
```

`span` 包含阶段内部的设备空洞，`union` 才描述这段时间实际有相关设备事件的时长。两者都要留；只看 TTFT 接近 `span`，无法排除 CPU 供给不足。多卡 kernel 时长不可相加。对跨主机时间线，不能直接减未经对时验证的墙钟时间戳；优先比较各进程内部单调时钟测得的持续时间。

### 2.4 平均 TPOT 和流式事件间隔

主指标使用请求级平均：`TPOT=(请求结束时间－首输出时间)/(实际输出token数－1)`。本手册也额外记录最后输出事件到达时的 delivery TPOT。MTP 一次 step 可能接受多个 token，一个 SSE event 也可能携带多个 token，因此 SSE event gap 不能直接标成单 token ITL。

正式时延测试不返回 token IDs，避免把 128K 个输入 ID 在首响应中回传的额外流量计入常规 TTFT。只有长度校准使用 `--return-token-ids`。实际输入/输出长度每个请求都用最终 `usage` 校验；长度不符就判失败。该版本 completion API 支持 token-ID prompt、`add_special_tokens` 和 `return_token_ids`。[Completion 协议](https://github.com/vllm-project/vllm/blob/v0.23.0/vllm/entrypoints/openai/completion/protocol.py)

## 3. 硬件拓扑与可执行范围

| 现有资源 | 本文允许得出的结论 |
|---|---|
| 1 台 A2，8×64GB | 单机混部部署、容量校准、长度校验、时延和 profiler 基线 |
| 本文参考 PD 资源：4 台 P + 4 台 D A2 | 按固定版本官方 A2 拓扑执行完整 PD 路径 |
| 2 台或其他数量 A2 | 可以另设计布局，但需重新做模型/算子/通信适配验收；本文不把自拟两机 TP8×2 当官方已验证配置 |

参考 PD 布局来自固定版本 A2 段落：P0～P3 是四个**独立**的 DP8/TP1/EP 组；每组在一台 A2 的八张 NPU 上启动八个 API 实例。D0～D3 共同组成 DP32/TP1/EP 组，rank 起点为0、8、16、24。这里 DP 搭配 EP，不能解释成“一张64GB卡独立放下整个模型”。[A2 PD 源文档](https://github.com/vllm-project/vllm-ascend/blob/v0.23.0/docs/source/tutorials/models/DeepSeek-V4-Flash.md#522-a2-series-pd-separation-deployment)

| 角色 | 示例 IP | DP master | 本机 ranks | API ports | engine_id / kv_port |
|---|---|---|---|---|---|
| P0 | 10.10.0.11 | 本机 | 0..7 | 7100..7107 | 0 / 30000 |
| P1 | 10.10.0.12 | 本机 | 0..7 | 7100..7107 | 1 / 30100 |
| P2 | 10.10.0.13 | 本机 | 0..7 | 7100..7107 | 2 / 30200 |
| P3 | 10.10.0.14 | 本机 | 0..7 | 7100..7107 | 3 / 30300 |
| D0 | 10.10.0.21 | 10.10.0.21 | 0..7 | 7100..7107 | 4 / 30400 |
| D1 | 10.10.0.22 | 10.10.0.21 | 8..15 | 7100..7107 | 4 / 30400 |
| D2 | 10.10.0.23 | 10.10.0.21 | 16..23 | 7100..7107 | 4 / 30400 |
| D3 | 10.10.0.24 | 10.10.0.21 | 24..31 | 7100..7107 | 4 / 30400 |

D 的 engine_id/kv_port 按同一个分布式 D 组处理，保留官方设置，不为每台 D 随意编另一个组。P0 上代理监听1999。另备一台能运行客户端和访问 API 的负载机；小规模校准可暂用P0，但正式结果要检查客户端CPU和网卡未饱和。

本文脚本保留官方的 MTP=1，P/D 两侧一致；不在第一轮测试中擅自关闭 MTP 或改 DSpark。修改项明确为：关闭 P、D 自动 prefix cache，增加诊断和可配置 KV 预算，参数与输出路径可追踪。没有启用 AscendStore、LMCache、CPU/NVMe offload。P→D 的必要 KV 交接仍然存在。

## 4. 准备文件、镜像和模型

本 Markdown 附录含全部自编脚本；配套 ZIP 已将它们分成 `scripts/`。在每台 Linux 主机放成以下结构：

```text
/data/ascend-v4-bench/
  README.md
  env.sh
  scripts/...
  upstream/          # 现场下载的固定版本官方文件
  logs/
  profiles/
  results/
```

若只持有本 Markdown，可以把附录每段代码按标注文件名保存，或执行文末提供的附录提取命令。

每台主机执行：

```bash
mkdir -p /data/ascend-v4-bench
cd /data/ascend-v4-bench
# 将配套文件复制到本目录后执行：
cp scripts/env.example.sh env.sh
chmod +x scripts/*.sh
sed -i 's/\r$//' env.sh scripts/*.sh
ip -br address
npu-smi info
```

编辑 `env.sh`，填本机 `LOCAL_IP`、CPU/控制面网卡 `NIC`，所有节点一致的P/D列表，以及模型盘目录。多网卡机器不要用 `hostname -I` 的第一项猜地址。NPU 的 HCCN IP 与 `LOCAL_IP` 的控制面地址是不同配置。

先固定镜像 digest，后续所有节点使用同一 digest（同架构）：

```bash
source env.sh
docker pull "$IMAGE"
docker image inspect "$IMAGE" > image-inspect.json
IMAGE_DIGEST=$(docker image inspect --format '{{index .RepoDigests 0}}' "$IMAGE")
test -n "$IMAGE_DIGEST"
printf 'export IMAGE=%q\n' "$IMAGE_DIGEST" >> env.sh
```

固定版本安装资料列出 CANN/NNAL 9.1.0、torch 2.10.0、torch-npu 2.10.0.post4；以实际镜像内容及其匹配要求为准，主机驱动/固件需匹配 CANN。镜像自带用户态软件，不另在服务容器里单独升级 torch/vLLM。[固定版本安装要求](https://github.com/vllm-project/vllm-ascend/blob/v0.23.0/docs/source/installation.md)

在临时下载容器里获取模型，避免给正式服务镜像升级依赖。模型仓库可能需要先完成访问授权。下载失败时保留错误并解决，不替换成同名未知权重：

```bash
source env.sh
mkdir -p "$MODEL_DIR"
docker run --rm --entrypoint bash \
  -v "$MODEL_DIR:/download" "$IMAGE" -lc '
  python -m pip install modelscope
  modelscope download --model Eco-Tech/DeepSeek-V4-Flash-w8a8-mtp --local_dir /download
  '
test -f "$MODEL_DIR/config.json"
find "$MODEL_DIR" -maxdepth 1 -type f -name '*.json' -exec sha256sum {} + \
  > model-metadata-sha256.txt
# 建议首次下载后完整做一遍权重校验；这会读完整个模型目录：
find "$MODEL_DIR" -type f -name '*.safetensors' -exec sha256sum {} + \
  > model-weights-sha256.txt
```

将同一份模型和校验清单分发到其他节点；比较文件的相对路径和 hash。checkpoint 的 revision/下载元数据也归档。W8A8 必须在量化描述文件、加载日志中核对；文件名含 w8a8 本身不构成验证。

启动正式容器，模型目录以只读挂载：

```bash
bash scripts/start_container.sh
docker exec -it ascend-v4-bench bash -lc 'bash /bench/scripts/preflight.sh'
```

`preflight.sh` 将保存版本、参数帮助、设备信息和模型元数据。版本不符、缺参数、设备数不符时会停止。**停止是兼容性失败，不能通过删参数继续并沿用本基准名称。** `512g` 是 `/dev/shm` 上限，不是立即预占512GB；仍需确认机器的主存、磁盘和实际共享内存使用量足够。

取得与镜像版本对应的官方示例，归档源码 commit，**不将克隆源码 pip install 覆盖进镜像**：

```bash
docker exec -it ascend-v4-bench bash -lc '
  set -euo pipefail
  mkdir -p /bench/upstream
  git clone --branch v0.23.0 --depth 1 \
    https://github.com/vllm-project/vllm-ascend.git /bench/upstream/vllm-ascend
  git -C /bench/upstream/vllm-ascend rev-parse HEAD > /bench/upstream/source-commit.txt
  cp /bench/upstream/vllm-ascend/examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py \
    /bench/upstream/load_balance_proxy_server_example.py
  python -c "import aiohttp, httpx, fastapi, uvicorn"
  sha256sum /bench/upstream/load_balance_proxy_server_example.py > /bench/upstream/proxy-sha256.txt
  '
```

仓库路径已存在时不覆盖，检查归档 commit 后复用。8台机器上的软件及模型元数据必须一致。

## 5. 多机通信验收及 Mooncake

只做单机基线可跳过本节。PD 每台主机执行：

```bash
HCCN=/usr/local/Ascend/driver/tools/hccn_tool
test -f /etc/hccn.conf
for i in {0..7}; do
  "$HCCN" -i "$i" -link -g
  "$HCCN" -i "$i" -net_health -g
  "$HCCN" -i "$i" -ip -g
done
```

链路需健康；按实际 HCCN IP，对每个将参与通信的远端NPU做官方 `-ping` 检查。例如下面地址必须填远端 NPU 的 IP，而非服务器控制面 IP：

```bash
$HCCN -i 0 -ping -g address REPLACE_WITH_REMOTE_NPU_IP
```

API端口、HCCL控制通信、KV侧通道和NPU RDMA数据面均需连通。A2八NPU上，Mooncake AscendDirectTransport 的保留端口区间是20000～27999，故本文KV侧通道选30000以上；主机API能互相curl并不表示RDMA通过。[官方多机通信和端口说明](https://docs.vllm.ai/projects/ascend/en/latest/tutorials/features/pd_disaggregation_mooncake_multi_node.html)

容器内检查 Mooncake：

```bash
docker exec -it ascend-v4-bench bash -lc '
  python -c "from mooncake.engine import TransferEngine; import mooncake; print(mooncake.__file__)"
  '
```

这个导入检查只是必要条件。最终通过“实际传输调用成功＋D没有本地全量重算＋请求正确完成”验证数据面。如果镜像缺 Mooncake，按固定版本指南在每台容器中编译相同版本，并重新冻结依赖。以下命令只针对容器内、Ubuntu系工具链；openEuler或公司定制系统需换相应包管理器，不套用apt：

```bash
git clone --branch v0.3.9 --depth 1 https://github.com/kvcache-ai/Mooncake.git /bench/upstream/Mooncake
cd /bench/upstream/Mooncake
git rev-parse HEAD > /bench/upstream/mooncake-commit.txt
apt-get update
apt-get install -y mpich libmpich-dev
bash dependencies.sh -y
cmake -S . -B build -DUSE_ASCEND_DIRECT=ON
cmake --build build -j8
cmake --install build
python -c 'from mooncake.engine import TransferEngine; import mooncake; print(mooncake.__file__)'
python -m pip freeze > /bench/results/environment/pip-freeze-after-mooncake.txt
```

编译/安装失败就解决动态库路径或工具链问题，不在性能测试期间临时改变环境。安装路径查实际 `mooncake.__file__`，不硬写 Python3.12 的 site-packages 路径。[固定版本 Mooncake 安装步骤](https://github.com/vllm-project/vllm-ascend/blob/v0.23.0/docs/source/tutorials/features/pd_disaggregation_mooncake_multi_node.md#install-mooncake)

## 6. 可选但建议：启动容量探针

配套诊断钩子在 KV 分配完成、图捕获之前保存每个worker的 cache specs、计划字节数、底层去重storage字节数、torch allocated/reserved 和空闲容量。它不替代启动完成后和压测中的整卡监控。

这是**本文自编的诊断补丁**，不是上游功能。只应用到一次性服务容器，保存原文件；只接受已核对的函数锚点，匹配失败直接停止。容量快照只在初始化执行；传输计时默认关闭。先跑正确性smoke，再做带/不带探针的低并发A/B；若超出基线波动，正式时延数据使用无补丁容器。

```bash
docker exec -it ascend-v4-bench bash -lc '
  python /bench/scripts/audit_patch.py | tee /bench/results/audit-patch-manifest.txt
  '
```

启动后应看到 `/bench/results/capacity/*.jsonl`。`planned_cache_bytes` 是config声明的buffer大小；`actual_unique_cache_storage_bytes` 按底层storage去重，可能含传输对齐导致的额外分配。不要把同一storage被多个layer引用的次数重复乘进去。该源码中PD KV分配包含对齐处理，正是要同时留计划值与storage值的原因。

恢复原文件后必须重启所有相关进程：

```bash
docker exec -it ascend-v4-bench bash -lc 'python /bench/scripts/audit_patch.py --restore'
```

## 7. 跑通单台 A2 基线

新终端中前台启动；保留终端便于用Ctrl+C退出本服务，不使用全局 `pkill`：

```bash
docker exec -it ascend-v4-bench bash -lc \
  'set -o pipefail; bash /bench/scripts/serve.sh single 2>&1 | tee /bench/logs/single.log'
```

另一个终端做健康和语义smoke：

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/v1/models
curl -fsS http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"dsv4","messages":[{"role":"user","content":"请用一句话说明1+1等于多少。"}],"temperature":0,"max_tokens":128}'
```

HTTP200仅表示请求成功，不代表量化正确性。确认有连贯输出和正确的简单事实；正式量化质量需另用真实业务样本/基准题与可信参考比较。随机token性能集不用于证明准确率。

在负载机使用同一镜像工具环境生成定长token-ID数据；简单起步可以在当前容器中生成，再将 `data/` 和客户端脚本复制到独立负载机：

```bash
docker exec -it ascend-v4-bench bash -lc '
  source /bench/env.sh
  for L in 16384 65536 131072; do
    python /bench/scripts/generate_prompts.py --model "$MODEL" --length "$L" \
      --out "/bench/data/prompt_${L}.json"
  done
  '
```

每次请求修改前32个普通token，确保不同请求没有可复用的共同前缀。这个合成集控制token数量，但MoE路由和业务内容相关，所以最后还要用实际业务长文本复测。

先验长度：每个L生成1024输出token，开启token-ID回传仅用于校准：

```bash
docker exec -it ascend-v4-bench bash -lc '
  for L in 16384 65536 131072; do
    python /bench/scripts/bench_client.py --base-url http://127.0.0.1:8000 \
      --prompts "/bench/data/prompt_${L}.json" --mode wave --requests 1 --concurrency 1 \
      --output-length 1024 --return-token-ids --min-samples 1 \
      --out "/bench/results/single-length-${L}.json"
  done
  '
```

验收：每请求 `usage.prompt_tokens=L`、`usage.completion_tokens=1024`，调试模式输出ID数量也是1024，无截断、报错、重算。任意长度不符，本点不能纳入性能结果。正式测试去掉 `--return-token-ids`。

## 8. 按参考拓扑部署 PD

先Ctrl+C停掉单机服务，确认NPU没有该服务残留；同一主机不要同时跑单机TP8和PD rank。然后在每台主机的 `env.sh` 写入它自己的控制面IP及NIC。

按上面表格启动全部D节点，四个终端/主机分别运行以下命令。命令在对应宿主机执行：

```bash
# D0
docker exec -it ascend-v4-bench python /bench/scripts/launch_dp.py decode \
  --node-id 0 --rank-start 0 --master 10.10.0.21
# D1
docker exec -it ascend-v4-bench python /bench/scripts/launch_dp.py decode \
  --node-id 1 --rank-start 8 --master 10.10.0.21
# D2
docker exec -it ascend-v4-bench python /bench/scripts/launch_dp.py decode \
  --node-id 2 --rank-start 16 --master 10.10.0.21
# D3
docker exec -it ascend-v4-bench python /bench/scripts/launch_dp.py decode \
  --node-id 3 --rank-start 24 --master 10.10.0.21
```

P0～P3分别运行；各P是独立DP8组，master是各自本机：

```bash
# P0
docker exec -it ascend-v4-bench python /bench/scripts/launch_dp.py prefill \
  --node-id 0 --rank-start 0 --master 10.10.0.11
# P1
docker exec -it ascend-v4-bench python /bench/scripts/launch_dp.py prefill \
  --node-id 1 --rank-start 0 --master 10.10.0.12
# P2
docker exec -it ascend-v4-bench python /bench/scripts/launch_dp.py prefill \
  --node-id 2 --rank-start 0 --master 10.10.0.13
# P3
docker exec -it ascend-v4-bench python /bench/scripts/launch_dp.py prefill \
  --node-id 3 --rank-start 0 --master 10.10.0.14
```

启动器会打印每个rank的PID和日志路径；任一rank退出会结束**本启动器创建的**同机兄弟进程，避免半个组继续压测。其他主机的分布式组仍需正常停止并重启，不能只看剩余进程是否忙碌。

P0容器中生成完整端点清单并检查64个API：

```bash
source /bench/env.sh
python /bench/scripts/endpoints.py --p-hosts "${P_IPS[@]}" --d-hosts "${D_IPS[@]}" \
  --out /bench/results/endpoints.json
python /bench/scripts/control_endpoints.py --endpoints /bench/results/endpoints.json \
  --action health --out /bench/results/health-before-pd.json
```

全部健康后，在P0另一终端启动代理：

```bash
docker exec -it ascend-v4-bench bash -lc \
  'set -o pipefail; bash /bench/scripts/start_proxy.sh 2>&1 | tee /bench/logs/proxy.log'
curl -fsS http://10.10.0.11:1999/healthcheck
```

重复第7节的语义smoke和16K/64K/128K长度校准，地址改为 `http://10.10.0.11:1999`。如果关闭P prefix caching后某镜像路径失败，要记录不兼容并定位；不能打开prefix caching后仍把结果写作“无prefix cache基线”。

验收数据通路的一个诊断轮次：在所有节点 `env.sh` 末尾增加以下设置，然后停止并重启所有P/D及代理，再发一个16K/1K请求：

```bash
printf 'export AUDIT_TRANSFER_DIR=/bench/results/transfer\n' >> /bench/env.sh
```

在D节点 `results/transfer/*.jsonl` 中应看到 `mooncake_sync_read` 的字节数、成功返回、耗时与请求ID。检查D时间线没有对完整输入重新做本地Prefill；指标中的外部KV接收属于预期，自动prefix cache命中应为0。探针记录同步读取调用的**实际长度列表总字节数**，比用理论KV大小猜网络流量更可靠。调用耗时包含该读调用的等待，不等于TTFT中暴露出来的传输时延；分片重叠时不要简单求和。[MooncakeHybridConnector读取源码](https://github.com/vllm-project/vllm-ascend/blob/v0.23.0/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_hybrid_connector.py)

诊断通过后追加 `export AUDIT_TRANSFER_DIR=`，重启回正式基线。任何重试、重新Prefill、recomputed、缓存抢占都让“无排队低开销”样本失效，不能因请求最终成功而忽略。

## 9. 正式压测：先低并发归因，再扫稳态容量

### 9.1 监控与计数器快照

每台主机单独开一个监控终端，输出到该节点本地目录：

```bash
cd /data/ascend-v4-bench
bash scripts/monitor_host.sh results/npu-smi-baseline.log
```

负载机或P0容器另开终端，抓所有端点的原始Prometheus文本。按role/host/rank保留标签，**不要把多个API重复暴露的同一组指标盲目相加**：

```bash
python /bench/scripts/metrics_monitor.py --endpoints /bench/results/endpoints.json \
  --out /bench/results/metrics-baseline.jsonl --interval 2
```

每个测试点前后各取一次快照，计算counter增量而不是取累计数：

```bash
python /bench/scripts/control_endpoints.py --endpoints /bench/results/endpoints.json \
  --action metrics --out /bench/results/metrics-before.json
# 在此执行一个测试点
python /bench/scripts/control_endpoints.py --endpoints /bench/results/endpoints.json \
  --action metrics --out /bench/results/metrics-after.json
```

现场从 `/metrics` 中确认当前实际暴露的名称，检查 running/waiting、KV cache usage、prefix hits、preemptions、request queue time、Prefill/Decode耗时、MTP接受率及失败数据。未暴露某一项时写“未获取”，通过服务profiler补充；不能把缺失当成0。

### 9.2 低并发、低排队基线

先用 `C_client=1`，固定同一P/D布局，分别测16K、64K、128K。稳定窗口默认600秒，前120秒为burn-in；每个点至少30个完整请求，少于30则延长duration。代码只把起止均落在稳定窗口内的请求纳入主时延均值，启动和停止边缘请求被排除；token吞吐另按窗口内完成请求的实际usage计算，避免把窗口排除规则误用于吞吐。

```bash
python /bench/scripts/bench_client.py \
  --base-url http://10.10.0.11:1999 --model dsv4 \
  --prompts /bench/data/prompt_65536.json \
  --concurrency 1 --burnin 120 --duration 600 --output-length 1024 --min-samples 30 \
  --out /bench/results/pd-L65536-C1-r1.json
```

`sufficient_samples=false` 或脚本退出2表示结果尚不能验收，要看失败原因/样本数。固定并发由客户端C个worker各保持一条在途请求实现；JSON序列化在该请求计时之前完成，HTTP传输仍计入TTFT。原始JSONL包含请求ID、时刻和事件数，便于发现客户端发包过慢。

完整窗口过滤会排除跨越结束边界的慢请求，可能产生选择偏差。summary同时给出 `end_censored_fraction` 和按开始时刻纳入、等待排空后的 `start_cohort_tpot_ms`。如果跨界排除比例较高（例如超过10%），或两种均值明显不同，应延长稳定窗口并比较结果；排空时负载逐渐降低，start-cohort也不能无条件当作稳态值。不要只挑完整窗口中较快的请求下结论。

### 9.3 扫描负载与寻找30/50ms边界

粗扫建议：单机C=1/2/4/8/16/32；完整PD组C=1/8/32/128，之后在边界附近加密。以下是完整PD组的三次重复；**会持续较长时间，应先确认低并发与单个高并发点通过**：

```bash
for L in 16384 65536 131072; do
  for C in 1 8 32 128; do
    for R in 1 2 3; do
      python /bench/scripts/bench_client.py \
        --base-url http://10.10.0.11:1999 --model dsv4 \
        --prompts "/bench/data/prompt_${L}.json" --concurrency "$C" \
        --burnin 120 --duration 600 --output-length 1024 --min-samples 30 \
        --out "/bench/results/pd-L${L}-C${C}-r${R}.json" || { echo 'Stop and inspect failed point'; exit 1; }
    done
  done
done
```

每点前后加上唯一命名的指标快照。报错后应停查，不把后续结果当自动有效。平均TPOT接近目标时，在相邻C间二分/细扫；平均不必严格单调，边界点至少三次重复。原始均值用于本需求，另保留p95/p99与5分钟窗口变化，诊断失稳。

完成三次重复后，生成CSV和30/50ms的“时延候选点”。候选点还需要下一节的真实batch及第10节的HBM证据才能升级为最终容量结论；脚本将未测字段明确标为待补证：

```bash
python /bench/scripts/summarize_results.py --directory /bench/results \
  --pattern 'pd-L*-C*-r*.json' --out /bench/results/latency-candidates.csv
```

可用官方bench作为交叉校验，以下使用该版本支持的OpenAI backend；它的默认突发发包与本文明确切稳定窗口的闭环实验不同，不直接合并数据集：[固定版本bench实现](https://github.com/vllm-project/vllm/blob/v0.23.0/vllm/benchmarks/serve.py)

```bash
source /bench/env.sh
vllm bench serve --backend openai \
  --base-url http://10.10.0.11:1999 --endpoint /v1/completions \
  --model "$MODEL" --served-model-name dsv4 \
  --tokenizer "$MODEL" --tokenizer-mode deepseek_v4 --trust-remote-code \
  --dataset-name random --random-input-len 65536 --random-output-len 1024 \
  --random-range-ratio 0 --ignore-eos --num-prompts 64 \
  --request-rate inf --max-concurrency 8 \
  --save-result --save-detailed --result-dir /bench/results/vllm-bench
```

### 9.4 将客户端并发映射到实际Decode并发

正式汇总必须另外填入服务profiler或调度轨迹得到的每rank `B_decode_mean/p95/max`，以及稳定Decode平台持续时间。若P供给不足或长Prefill使D batch始终偏小，只能写“C个服务请求下的实测结果”；不能将C乘以单请求KV推成D的容量。

用Little定律做一致性检查：`平均活跃D序列数≈请求吞吐×平均D驻留时间`；D驻留包含KV接收/等待还是只含生成，要随测量口径注明。输出1K且TPOT30ms，单请求生成阶段约30.7秒；这只是时间量级，不能据此决定NPU可承载batch。

## 10. 固定HBM预算：测出可用KV容量下限

总容量按每个NPU分别列账：

```text
HBM_peak(rank) = 权重与常驻状态 + KV物理buffer + 激活/工作区 + 图/通信/运行时 + 分配器余量
```

这些项可能共享分配器或复用内存。实测总峰值和去重buffer是主依据，不能把多个来源已经包含的内存重复相加。各rank不平衡时，以最紧张rank约束部署；集群总和不能替代单卡上限。P侧在D读取完成前会保留部分源KV，因此P也要单独测容量和并发保留峰值。

容量探针中 `actual_unique_cache_storage_bytes` 给出当前预算的物理缓存；服务profiler给逻辑占用。若只有全局usage百分比，对单一均匀pool可用 `K_alloc×usage` 做近似；混合cache groups不能不加说明地当精确字节数。

**容量下限用预算扫描反证**：在已达30ms或50ms目标的工作点上，逐步缩小KV预算，直到无法同时维持原来的活跃Decode并发、吞吐与时延；再回退并细扫。仅“请求全做完”不算通过，因为引擎可能偷偷串行化、排队或重算。

`--kv-cache-memory-bytes` 指定每NPU KV预算，非整卡HBM；设置后会覆盖自动基于utilization推算的KV大小。该固定版本NPUWorker源码存在对应执行分支。[NPU预算处理](https://github.com/vllm-project/vllm-ascend/blob/v0.23.0/vllm_ascend/worker/worker.py#L527) [参数定义](https://docs.vllm.ai/en/v0.23.0/configuration/engine_args/#cacheconfig)

操作例：现场先从探针/日志得到最紧张D rank的自动预算 `K0`（字节），尝试它的85%，数值仅作扫描起点：

```bash
K0=REPLACE_WITH_MEASURED_D_KV_BUDGET_BYTES
K_TRY=$(python - "$K0" <<'PY'
import sys
print(int(sys.argv[1])*85//100)
PY
)
printf 'export D_KV_BYTES=%s\n' "$K_TRY" >> /bench/env.sh
```

在四台D节点设置相同候选预算；P保持原基线。**改变KV布局后应停止并重启整个PD组及代理**，避免旧buffer地址/元数据沿用；重启后重新通过健康、smoke和长度检查，再跑同一稳定负载。每次启动留新的日志/容量目录，勿把多个预算混在同一份结果里。

记录失败原因：

- 启动时单请求最大长度放不下：当前 `max-model-len=135000` 对该预算不可行，这不是性能优化失败。
- 权重加载、工作区或图捕获OOM：非KV预算本身的问题，应按阶段定位。
- KV紧张但无OOM：如果waiting、preempt/recompute增加、实际B下降，本点仍不通过。
- 最低通过预算：以三次重复都满足原工作点为准，再加按业务需求选定的余量，例如10%～20%；这是工程策略，不是硬件定律。

`max-model-len` 本手册始终135000，避免不同L时悄悄改变初始化容量约束。若还要评估按L专门裁剪max-model-len的收益，应另建一套结果，不混入本基线。

## 11. Profiling：对齐TTFT和NPU执行阶段

### 11.1 采集计划

先普通运行，warmup充分，得到低并发TTFT均值/标准波动；再采样profiling。优先16K/C1，确认归因方法后再做64K、128K。正式客户端仍输出1K。一次长请求的全部trace可能很大；磁盘不足时可另做短输出诊断，但必须先证明短输出与1K输出的TTFT基线一致，且不能拿短输出TPOT冒充1K结果。

在相关节点env中追加 `export PROFILE=1`，按第8节重启。各role/node/rank的profiler目录不同。端点只在启用profiler配置时才存在；普通代理不会替你广播这些控制请求。[Ascend服务Profiling指南](https://docs.vllm.ai/projects/ascend/en/main/developer_guide/performance_and_debug/service_profiling_guide.html)

```bash
printf 'export PROFILE=1\n' >> /bench/env.sh
# 停止并重启全部相关P/D与代理后，在P0执行：
python /bench/scripts/control_endpoints.py --endpoints /bench/results/endpoints.json \
  --action start_profile --out /bench/results/profile-start.json

python /bench/scripts/bench_client.py --base-url http://10.10.0.11:1999 \
  --prompts /bench/data/prompt_16384.json --mode wave --requests 1 \
  --concurrency 1 --output-length 1024 --min-samples 1 \
  --out /bench/results/profile-L16384-C1.json

python /bench/scripts/control_endpoints.py --endpoints /bench/results/endpoints.json \
  --action stop_profile --out /bench/results/profile-stop.json
```

所有相关EP参与rank都要保留trace。不要只采负责API的一个NPU，就声称获得整个EP组关键路径。先做单请求；并发trace里无法把某些kernel可靠归给某请求时，不能作该请求的TTFT分母。

每台容器分析输出：

```bash
python - <<'PY'
from pathlib import Path
from torch_npu.profiler.profiler import analyse
for folder in Path('/bench/profiles').rglob('*_ascend_pt'):
    print('Analysing', folder)
    analyse(str(folder))
PY
find /bench/profiles -name trace_view.json -o -name kernel_details.csv \
  -o -name step_trace_time.csv
```

用MindStudio Insight打开 `trace_view.json`：标出请求相关的所有Prefill chunks；检查是否有CPU调度空洞、HCCL等待和KV读取。Prefill chunk之间的时间不能在比较TTFT时消失。`max-num-batched-tokens=4096` 是参考P配置，所以128K通常会跨多个调度step；不能只拿最后一个step与TTFT比较。

需要算区间并集时，先在UI确认具体device的PID/TID和Prefill区间，再执行工具。**下列数字必须来自该份trace，不是固定可复制的设备编号**：

```bash
python /bench/scripts/trace_intervals.py \
  --trace /bench/profiles/REPLACE_WITH_TRACE/ASCEND_PROFILER_OUTPUT/trace_view.json \
  --pid REPLACE_WITH_DEVICE_PID --tids REPLACE_WITH_DEVICE_STREAM_TIDS \
  --start-us REPLACE_WITH_PREFILL_START_US --end-us REPLACE_WITH_PREFILL_END_US \
  --out /bench/results/prefill-device-intervals.json
```

该工具输出kernel_sum、active_union、span。只有PID/TID确实只对应目标设备执行流，数值才有意义；不要把host runtime API也圈进设备union。多卡按关键路径归因，不求kernel_sum跨卡总和。

### 11.2 测P/D交接时间

可选启动带时间戳的代理副本（先停旧代理）。本脚本保留上游路由语义，在单独文件中插入请求ID和两个HTTP阶段的计时：

```bash
docker exec -it -e PROXY_AUDIT=1 ascend-v4-bench bash /bench/scripts/start_proxy.sh
```

`results/proxy-audit/*.jsonl` 含同一请求的：

- `prefill_http_roundtrip`：代理向P发请求到完整P响应，包括P排队、执行、采样和来回HTTP。
- `decode_http_first_bytes`：代理向D发请求到首个非空HTTP数据块，包括KV接收、D调度等；**首字节不是首token**。

请求ID与客户端JSONL、传输探针关联。传输调用的 `duration_ms` 和 `bytes` 则来自第8节的同步读取计时。P、D各机器内部计时是有效持续时间；跨机器原始时间戳不能未经验证相减。

低并发时如果 `P_http` 已明显大于 `T_P_span`，继续查P框架/分chunk空洞；如果 `D_first_bytes` 随L急剧增加，查KV传输、布局变换和D等待。只有trace证明它们串行且不重叠时才做加法预算。

### 11.3 获得真实batch和排队证据

如果需要服务阶段/调度级trace，用MS Service Profiler做单独诊断轮次。先确认镜像是否带CLI及Python包；缺失时按官方指南安装，记录源码commit和安装清单，不与主性能轮次混用：

```bash
command -v msserviceprofiler
python -c 'import msserviceprofiler; print(msserviceprofiler.__file__)'
```

服务启动前设置配置。命令放到 `serve.sh` 的正式vLLM启动之前，或者在它的公共export段增加以下两项并重启；必须在实际服务进程中生效：

```bash
mkdir -p /bench/profiles/service
cat > /bench/profiles/service-config.json <<'JSON'
{"enable":1,"prof_dir":"/bench/profiles/service","acl_task_time":0}
JSON
export SERVICE_PROF_CONFIG_PATH=/bench/profiles/service-config.json
# 先用与本版本匹配的默认symbols；不复用其他版本私有函数路径。
```

仅导出到一个无关终端不会影响已启动服务。可将该export追加到各节点 `env.sh`，配置文件各节点都需存在。小负载跑完后，根据实际输出子目录解析：

```bash
msserviceprofiler parse --input-path /bench/profiles/service/REPLACE_WITH_RUN_DIRECTORY \
  --output-path /bench/profiles/service-parsed
find /bench/profiles/service-parsed -name request.csv -o -name batch.csv -o -name kvcache.csv
```

按现场CSV表头提取各rank真实batch、等待、cache分配。默认symbols缺少某阶段时再对照安装源码配置并验证，不能虚构字段。Profiler不支持/数据缺失时，本点只能声明缺证，不能宣称“batch固定、没有等待”。

正式TTFT/TPOT结论回到关闭profiler的版本重复测量；若profiling打开前后TTFT发生明显变化，不能把两次绝对时间直接相减来精确估算框架开销。优先用profile定位组成，在轻量代理计时/普通运行中确认改善。

## 12. 通过标准和优化顺序

### 12.1 三种结论分别验收

| 结论 | 最低证据 |
|---|---|
| 某C客户端并发下TPOT达标 | 稳定窗口平均TPOT≤目标、三次重复、每点有效样本≥30、输出1024、无失败 |
| 某实际Decode batch的HBM容量 | 每rank实际batch证据、KV物理pool与逻辑占用、同工作点预算扫描、无抢占/重算/串行化 |
| TTFT主要由NPU执行决定 | 低排队、全Prefill chunks覆盖、device span与union、交接/传输计时、profiling扰动评估 |

可以事先把 `r_raw≤10%` 当作“TTFT接近P执行时间”的团队目标；这个阈值由你定，不是上游性能保证。PD KV传输本身有不可忽略的成本时，`r_raw` 不达标不代表框架差。应再研究“去除关键路径上不可重叠交接成本后的额外框架开销”。如果TTFT意外小于测得阶段时间，优先判定测量口径、overlap、请求匹配或时钟有误，禁止直接截为0。

稳态还要检查：多个相邻窗口的吞吐、TPOT、waiting和HBM没有持续漂移；队列不长期增长。平均值达标但大量重试/长空档的结果应解释其成因，不作为“低开销”证据。

### 12.2 按瓶颈只改一个变量

1. **P的CPU/调度空洞大**：检查CPU绑定/NUMA、线程占用、客户端与代理CPU、chunk token budget。增加P预算可能提升Prefill效率，也可能增大激活峰值；重测HBM。
2. **P队列长而D不满**：先增加/调整P供给或降低到达压力；这时不能把D的低batch解释成HBM不足。
3. **D算子/HCCL关键路径长**：保持模型与KV精度，单独比较图模式、通信优化或并行布局。改布局必须重建HBM基线。
4. **KV交接成本高**：检查实际传输字节、RDMA健康、对齐/分组/重试。不得用理论HBM带宽替代网络传输测量。
5. **KV pool限制并发**：先完成预算扫描，再研究KV格式/布局；每次KV量化变化都需另做质量验证。
6. **MTP接受率或输出批量变化**：记录接受token数和每步产出，比较每接受token成本。不能用step耗时直接当TPOT。

不要一开始同时打开多项新特性。模型权重、KV dtype、block size、P/D布局、MTP、chunk预算任意一项变化，都要在结果ID中体现。下一软件版本用同一套工作负载重跑；不要把新版本的参数复制进旧镜像。

## 13. 最终结果表和交付清单

以下是模板，全部性能数值都待现场填入：

| Run ID | L/O | C_client | D实际B mean/max（逐rank） | P/D KV预算 bytes/NPU | 峰值HBM（最紧rank） | mean TTFT ms | mean TPOT ms | output tok/s | preempt/recompute/retry | 30ms/50ms达标 |
|---|---|---|---|---|---|---|---|---|---|---|
| 待测 | 16384/1024 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| 待测 | 65536/1024 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| 待测 | 131072/1024 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |

附另一张TTFT归因表：`T_P_span / T_P_union / P_HTTP / KV调用字节与耗时 / D_first_bytes / Δ_raw / profiling扰动 / 排队证据`。不要把表中的空白填0。

每次实验归档：

- 镜像digest、软件冻结清单、官方源码commit、模型revision及hash、每节点硬件/驱动/NIC/HCCN记录。
- 每个进程启动参数、每rank日志、所有节点容量JSON和HBM监控。
- 每请求客户端JSONL、稳定窗口summary、指标前后快照和连续监控。
- 最低通过KV预算与相邻失败点、失败原因、实际Decode batch证据。
- profiler原始文件、选取的设备和时间区间、时钟/重叠/采样扰动说明。
- 语义smoke及业务质量回归结果；全部性能结论标注适用的模型和配置。

## 14. 来源与验证边界

资料核对日期为2026-10-21，执行时仍要以固定digest里的实际参数和代码为准：

1. [v0.23.0 DeepSeek-V4-Flash 模型及A2拓扑](https://github.com/vllm-project/vllm-ascend/blob/v0.23.0/docs/source/tutorials/models/DeepSeek-V4-Flash.md)
2. [v0.23.0 安装/版本要求](https://github.com/vllm-project/vllm-ascend/blob/v0.23.0/docs/source/installation.md)
3. [v0.23.0 Mooncake多机部署](https://github.com/vllm-project/vllm-ascend/blob/v0.23.0/docs/source/tutorials/features/pd_disaggregation_mooncake_multi_node.md)
4. [v0.23.0 示例代理源码](https://github.com/vllm-project/vllm-ascend/blob/v0.23.0/examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py)
5. [v0.23.0 NPUWorker内存预算](https://github.com/vllm-project/vllm-ascend/blob/v0.23.0/vllm_ascend/worker/worker.py)
6. [v0.23.0 NPUModelRunner缓存分配](https://github.com/vllm-project/vllm-ascend/blob/v0.23.0/vllm_ascend/worker/model_runner_v1.py)
7. [v0.23.0 MooncakeHybridConnector传输](https://github.com/vllm-project/vllm-ascend/blob/v0.23.0/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_hybrid_connector.py)
8. [vLLM v0.23.0 KV cache接口](https://github.com/vllm-project/vllm/blob/v0.23.0/vllm/v1/kv_cache_interface.py)
9. [vLLM v0.23.0 Completion协议](https://github.com/vllm-project/vllm/blob/v0.23.0/vllm/entrypoints/openai/completion/protocol.py)
10. [Ascend 服务Profiling指南](https://docs.vllm.ai/projects/ascend/en/main/developer_guide/performance_and_debug/service_profiling_guide.html)
11. [vLLM v0.23.0 bench实现](https://github.com/vllm-project/vllm/blob/v0.23.0/vllm/benchmarks/serve.py)

配套脚本是本文的工程实现，不是官方承诺。所有NPU、RDMA、长上下文正确性和性能数字均需现场执行后才能标“通过”。本地验证结果另见 `VALIDATION.md`。

## 15. 单文件使用方法

如果只下载了本Markdown，保存为 `README.md` 后，在目标目录执行以下命令，提取附录中的全部脚本。只向当前目录下的 `scripts/` 写入；已有同名文件会覆盖，应在新实验目录执行：

```bash
python3 - <<'PY'
from pathlib import Path
import re
root = Path('.').resolve()
text = Path('README.md').read_text(encoding='utf-8')
pattern = r'^<!-- BEGIN_SCRIPT: (scripts/[A-Za-z0-9_.-]+) -->\n```(?:python|bash)\n(.*?)\n```\n<!-- END_SCRIPT -->'
items = re.findall(pattern, text, re.M | re.S)
assert items, 'No embedded scripts found'
for name, code in items:
    path = (root/name).resolve()
    assert path.parent == root/'scripts'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code+'\n', encoding='utf-8', newline='\n')
print('Extracted', len(items), 'scripts')
PY
cp scripts/env.example.sh env.sh
```

<!-- SCRIPT_APPENDIX -->

## 附录：完整脚本源码

以下代码与配套 `scripts/` 文件逐字一致。

<!-- BEGIN_SCRIPT: scripts/audit_patch.py -->
```python
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
```
<!-- END_SCRIPT -->

<!-- BEGIN_SCRIPT: scripts/bench_client.py -->
```python
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
```
<!-- END_SCRIPT -->

<!-- BEGIN_SCRIPT: scripts/capacity_probe.py -->
```python
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
```
<!-- END_SCRIPT -->

<!-- BEGIN_SCRIPT: scripts/control_endpoints.py -->
```python
"""Read health or toggle profiler on EVERY explicitly listed vLLM API instance."""
import argparse
import concurrent.futures
import json
from pathlib import Path
import urllib.request

p=argparse.ArgumentParser()
p.add_argument('--endpoints', type=Path, required=True)
p.add_argument('--action', choices=['health','start_profile','stop_profile','metrics'], required=True)
p.add_argument('--role', choices=['all','prefill','decode'], default='all')
p.add_argument('--out', type=Path)
a=p.parse_args()
rows=[r for r in json.loads(a.endpoints.read_text()) if a.role=='all' or r['role']==a.role]

def call(row):
    url=row['url']+'/'+a.action
    method='POST' if a.action in ('start_profile','stop_profile') else 'GET'
    try:
        with urllib.request.urlopen(urllib.request.Request(url,method=method), timeout=300) as response:
            return dict(**row, status=response.status, body=response.read().decode())
    except Exception as e: return dict(**row,status=None,error=repr(e))

with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool: result=list(pool.map(call,rows))
payload=json.dumps(result,indent=2)
if a.out:
    a.out.parent.mkdir(exist_ok=True,parents=True); a.out.write_text(payload,encoding='utf-8')
print('\n'.join(f"{r['url']}: {r['status']} {r.get('error','')}" for r in result))
raise SystemExit(0 if all(r['status']==200 for r in result) else 2)
```
<!-- END_SCRIPT -->

<!-- BEGIN_SCRIPT: scripts/endpoints.py -->
```python
"""Generate exact host:port lists for monitoring/profiler control. No discovery guesses."""
import argparse
import json
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('--p-hosts', nargs='+', required=True)
p.add_argument('--d-hosts', nargs='+', required=True)
p.add_argument('--out', type=Path, required=True)
a=p.parse_args()
rows=[dict(role=role, host=h, port=port, url=f'http://{h}:{port}')
      for role, hosts in [('prefill',a.p_hosts),('decode',a.d_hosts)]
      for h in hosts for port in range(7100,7108)]
a.out.parent.mkdir(exist_ok=True,parents=True)
a.out.write_text(json.dumps(rows,indent=2),encoding='utf-8')
print(len(rows), 'endpoints written')
```
<!-- END_SCRIPT -->

<!-- BEGIN_SCRIPT: scripts/env.example.sh -->
```bash
#!/usr/bin/env bash
# Copy to env.sh. Replace these example addresses before running PD.
export BENCH_ROOT=/data/ascend-v4-bench
export MODEL_DIR=/data/models/DeepSeek-V4-Flash-w8a8-mtp
export IMAGE=quay.io/ascend/vllm-ascend:v0.23.0
export CONTAINER=ascend-v4-bench
export MODEL=/models/DeepSeek-V4-Flash-w8a8-mtp
export SERVED_MODEL=dsv4
export NIC=REPLACE_WITH_CONTROL_NIC
export LOCAL_IP=REPLACE_WITH_THIS_HOST_CONTROL_IP
P_IPS=(10.10.0.11 10.10.0.12 10.10.0.13 10.10.0.14)
D_IPS=(10.10.0.21 10.10.0.22 10.10.0.23 10.10.0.24)
export D_MASTER=${D_IPS[0]}
export PROXY_HOST=${P_IPS[0]}
export PROXY_PORT=1999
export MAX_MODEL_LEN=135000
export SINGLE_MAX_SEQS=32 P_MAX_SEQS=16 D_MAX_SEQS=30
export P_TOKEN_BUDGET=4096 D_TOKEN_BUDGET=60
export MEM_UTIL=0.90 PROFILE=0
# Optional per-NPU KV budgets in BYTES, NOT whole-card HBM.
export SINGLE_KV_BYTES= P_KV_BYTES= D_KV_BYTES=
export AUDIT_CAPACITY_DIR=/bench/results/capacity
# Set only during the optional transfer diagnostic run.
export AUDIT_TRANSFER_DIR=
```
<!-- END_SCRIPT -->

<!-- BEGIN_SCRIPT: scripts/generate_prompts.py -->
```python
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
```
<!-- END_SCRIPT -->

<!-- BEGIN_SCRIPT: scripts/launch_dp.py -->
```python
"""Launch eight external DP ranks on one A2 host, using the documented DP8/DP32 layout."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument('role', choices=['prefill', 'decode'])
    p.add_argument('--master', required=True)
    p.add_argument('--node-id', type=int, required=True)
    p.add_argument('--rank-start', type=int, default=0)
    a = p.parse_args()
    assert 0 <= a.node_id < 4
    assert a.rank_start == (0 if a.role == 'prefill' else 8 * a.node_id)
    root = Path('/bench/logs'); root.mkdir(exist_ok=True, parents=True)
    processes, files = [], []
    try:
        for local in range(8):
            rank = a.rank_start + local
            env = dict(os.environ, NODE_ID=str(a.node_id), DP_RANK=str(rank),
                       DP_MASTER=a.master, API_PORT=str(7100 + local),
                       ASCEND_RT_VISIBLE_DEVICES=str(local))
            path = root / f'{a.role}-{a.node_id}-rank{rank}.log'
            f = path.open('a'); files.append(f)
            process = subprocess.Popen(['bash', '/bench/scripts/serve.sh', a.role],
                env=env, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
            processes.append(process)
            print(f'local={local} rank={rank} pid={process.pid} log={path}', flush=True)
        while True:
            for process in processes:
                if process.poll() is not None:
                    raise RuntimeError(f'Rank process {process.pid} exited with {process.returncode}')
            time.sleep(1)
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        for f in files:
            f.close()


if __name__ == '__main__':
    main()
```
<!-- END_SCRIPT -->

<!-- BEGIN_SCRIPT: scripts/metrics_monitor.py -->
```python
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
```
<!-- END_SCRIPT -->

<!-- BEGIN_SCRIPT: scripts/monitor_host.sh -->
```bash
#!/usr/bin/env bash
set -euo pipefail
out=${1:?Specify a NEW output file}
mkdir -p "$(dirname "$out")"
while true; do
  date -u +%Y-%m-%dT%H:%M:%S.%NZ
  npu-smi info
  sleep 2
done >> "$out"
```
<!-- END_SCRIPT -->

<!-- BEGIN_SCRIPT: scripts/patch_proxy.py -->
```python
"""Make a separate instrumented copy of the upstream example. Never overwrite upstream."""
import argparse
import ast
from pathlib import Path

INSTRUMENTATION = '''
# ASCEND_BENCH_PROXY_AUDIT_V1
import contextvars as _audit_contextvars
from capacity_probe import write_record as _audit_write
_audit_rid = _audit_contextvars.ContextVar('bench_request_id', default=None)
_audit_original_next = next_req_id

def next_req_id():
    return _audit_rid.get() or _audit_original_next()

@app.middleware("http")
async def _audit_request_id_middleware(request, call_next):
    rid = request.headers.get("x-request-id")
    token = _audit_rid.set(rid)
    try:
        return await call_next(request)
    finally:
        _audit_rid.reset(token)

_audit_original_send = send_request_to_service
async def send_request_to_service(client, endpoint, req_data, request_id, **kwargs):
    start = time.perf_counter_ns()
    try:
        return await _audit_original_send(client, endpoint, req_data, request_id, **kwargs)
    finally:
        _audit_write(os.getenv("PROXY_AUDIT_DIR"), "prefill_http_roundtrip", {
            "request_id":request_id, "duration_ms":(time.perf_counter_ns()-start)/1e6,
            "backend":str(client.base_url)})

_audit_original_stream = stream_service_response_with_retry
async def stream_service_response_with_retry(client, endpoint, req_data, request_id, **kwargs):
    start = time.perf_counter_ns()
    first = True
    async for chunk in _audit_original_stream(client, endpoint, req_data, request_id, **kwargs):
        if first and chunk:
            first = False
            _audit_write(os.getenv("PROXY_AUDIT_DIR"), "decode_http_first_bytes", {
                "request_id":request_id, "duration_ms":(time.perf_counter_ns()-start)/1e6,
                "backend":str(client.base_url)})
        yield chunk

'''


def main():
    p = argparse.ArgumentParser()
    p.add_argument('source', type=Path); p.add_argument('output', type=Path)
    a = p.parse_args()
    source = a.source.read_text(encoding='utf-8')
    for anchor in ('def next_req_id(', 'async def send_request_to_service(',
                   'async def stream_service_response_with_retry(', '@app.post("/v1/completions")'):
        if anchor not in source: raise RuntimeError(f'Upstream mismatch: {anchor}')
    anchor = 'if __name__ == "__main__":'
    if source.count(anchor) != 1: raise RuntimeError('Unexpected main entry point')
    modified = source.replace(anchor, INSTRUMENTATION+'\n'+anchor)
    ast.parse(modified)
    a.output.write_text(modified, encoding='utf-8')
    print(a.output)


if __name__ == '__main__': main()
```
<!-- END_SCRIPT -->

<!-- BEGIN_SCRIPT: scripts/preflight.sh -->
```bash
#!/usr/bin/env bash
set -euo pipefail
source /bench/env.sh
mkdir -p /bench/results/environment /bench/logs /bench/profiles /bench/upstream
stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="/bench/results/environment/$(hostname)-$stamp"
mkdir -p "$out"
npu-smi info | tee "$out/npu-smi.txt"
cat /usr/local/Ascend/driver/version.info > "$out/driver-version.txt"
python -m pip freeze > "$out/pip-freeze.txt"
vllm serve --help=all > "$out/serve-help.txt"
vllm bench serve --help > "$out/bench-help.txt"
python - <<'PY' | tee "$out/python-stack.txt"
import importlib.metadata as m
import json, os
import torch, torch_npu, vllm, vllm_ascend
from packaging.version import Version
for name in ('vllm', 'vllm-ascend', 'torch', 'torch-npu', 'transformers', 'triton-ascend'):
    print(name, m.version(name))
assert Version(m.version('vllm')).base_version == '0.23.0', 'Wrong vLLM version'
assert Version(m.version('vllm-ascend')).base_version == '0.23.0', 'Wrong Ascend version'
assert torch.npu.is_available()
assert torch.npu.device_count() == 8, 'This recipe assumes eight visible A2 devices'
config = json.load(open(os.path.join(os.environ['MODEL'], 'config.json')))
print('model_type:', config.get('model_type'))
assert config.get('model_type') == 'deepseek_v4'
print('quantization_config:', config.get('quantization_config'))
print('vllm_ascend:', vllm_ascend.__file__)
print('torch_npu:', torch_npu.__file__)
print('PREFLIGHT PYTHON CHECK PASSED')
PY
find "$MODEL" -maxdepth 1 -type f \
  \( -name '*.json' -o -name '*.py' -o -name '*.model' \) \
  -exec sha256sum {} + > "$out/model-metadata-sha256.txt"
for flag in --no-enable-prefix-caching --kv-cache-memory-bytes --profiler-config \
            --no-disable-hybrid-kv-cache-manager --speculative-config; do
  grep -q -- "$flag" "$out/serve-help.txt" || { echo "Missing required flag: $flag"; exit 1; }
done
printf 'Environment evidence: %s\n' "$out"
```
<!-- END_SCRIPT -->

<!-- BEGIN_SCRIPT: scripts/serve.sh -->
```bash
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
```
<!-- END_SCRIPT -->

<!-- BEGIN_SCRIPT: scripts/start_container.sh -->
```bash
#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/../env.sh"
mkdir -p "$BENCH_ROOT" "$MODEL_DIR"
docker pull "$IMAGE"
docker image inspect "$IMAGE" > "$BENCH_ROOT/image-inspect.json"
devices=()
for i in {0..7}; do
  test -c "/dev/davinci$i"
  devices+=(--device "/dev/davinci$i")
done
for name in davinci_manager devmm_svm hisi_hdc; do
  test -e "/dev/$name"
  devices+=(--device "/dev/$name")
done
mounts=()
for path in /usr/local/dcmi /usr/local/Ascend/driver /etc/ascend_install.info; do
  test -e "$path"
  mounts+=(-v "$path:$path:ro")
done
if test -f /etc/hccn.conf; then mounts+=(-v /etc/hccn.conf:/etc/hccn.conf:ro); fi
smi=$(command -v npu-smi)
mounts+=(-v "$smi:/usr/local/bin/npu-smi:ro")
docker run -dit --name "$CONTAINER" \
  --network host --ipc=private --shm-size=512g \
  "${devices[@]}" "${mounts[@]}" \
  -v "$BENCH_ROOT:/bench" -v "$MODEL_DIR:$MODEL:ro" \
  -w /bench -e PYTHONPATH=/bench/scripts "$IMAGE" bash
```
<!-- END_SCRIPT -->

<!-- BEGIN_SCRIPT: scripts/start_proxy.sh -->
```bash
#!/usr/bin/env bash
set -euo pipefail
source /bench/env.sh
p_hosts=(); p_ports=(); d_hosts=(); d_ports=()
for host in "${P_IPS[@]}"; do
  for port in {7100..7107}; do p_hosts+=("$host"); p_ports+=("$port"); done
done
for host in "${D_IPS[@]}"; do
  for port in {7100..7107}; do d_hosts+=("$host"); d_ports+=("$port"); done
done
proxy=/bench/upstream/load_balance_proxy_server_example.py
if [[ "${PROXY_AUDIT:-0}" == 1 ]]; then
  python /bench/scripts/patch_proxy.py "$proxy" /bench/upstream/proxy_instrumented.py
  proxy=/bench/upstream/proxy_instrumented.py
  export PROXY_AUDIT_DIR=/bench/results/proxy-audit
fi
exec python "$proxy" --host "$PROXY_HOST" --port "$PROXY_PORT" \
  --workers 1 --max-retries 1 \
  --prefiller-hosts "${p_hosts[@]}" --prefiller-ports "${p_ports[@]}" \
  --decoder-hosts "${d_hosts[@]}" --decoder-ports "${d_ports[@]}"
```
<!-- END_SCRIPT -->

<!-- BEGIN_SCRIPT: scripts/summarize_results.py -->
```python
"""CSV summary and latency-only candidates; hardware capacity still requires engine evidence."""
import argparse
import csv
import json
from pathlib import Path
import statistics

p=argparse.ArgumentParser()
p.add_argument('--directory',type=Path,required=True)
p.add_argument('--pattern',default='pd-L*-C*-r*.json')
p.add_argument('--out',type=Path,required=True)
a=p.parse_args()
groups={}
for path in sorted(a.directory.glob(a.pattern)):
    row=json.loads(path.read_text(encoding='utf-8'))
    key=(row['input_tokens'],row['output_tokens'],row['client_concurrency'])
    groups.setdefault(key,[]).append((path,row))
if not groups: raise SystemExit('No matching results')
rows=[]
for (length,output,concurrency),runs in sorted(groups.items()):
    valid=all(r['sufficient_samples'] and r['valid_requests']>=30 and
              not r['all_phase_failures'] and not r['measured_failures'] for _,r in runs)
    ttft=[r['ttft_text_ms']['mean'] for _,r in runs]
    tpot=[r['tpot_request_ms']['mean'] for _,r in runs]
    measured=[v for v in tpot if v is not None]
    qualified=valid and len(runs)>=3 and len(measured)==len(runs)
    rows.append(dict(input_tokens=length,output_tokens=output,client_concurrency=concurrency,
        repetitions=len(runs),all_lengths_and_samples_valid=valid,
        max_end_censored_fraction=max(r.get('end_censored_fraction',0) or 0 for _,r in runs),
        mean_of_run_ttft_ms=statistics.mean(ttft) if all(v is not None for v in ttft) else '',
        mean_of_run_tpot_ms=statistics.mean(measured) if measured else '',
        worst_run_tpot_ms=max(measured) if measured else '',
        candidate_30ms=qualified and max(measured)<=30,
        candidate_50ms=qualified and max(measured)<=50,
        actual_decode_batch='REQUIRES_ENGINE_TRACE',kv_budget_bytes='REQUIRES_CAPACITY_SNAPSHOT',
        queue_preempt_recompute_evidence='REQUIRES_METRICS_AND_TRACE',
        source_files=';'.join(str(path) for path,_ in runs)))
a.out.parent.mkdir(parents=True,exist_ok=True)
with a.out.open('w',newline='',encoding='utf-8-sig') as f:
    writer=csv.DictWriter(f,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
print(a.out)
for length in sorted({r['input_tokens'] for r in rows}):
    for target in (30,50):
        candidates=[r['client_concurrency'] for r in rows if r['input_tokens']==length and r[f'candidate_{target}ms']]
        print(f'L={length}, TPOT<={target}ms: max tested client concurrency candidate = {max(candidates) if candidates else None}; HBM/batch evidence still required')
```
<!-- END_SCRIPT -->

<!-- BEGIN_SCRIPT: scripts/trace_intervals.py -->
```python
"""Measure ONLY manually selected device streams in a selected stage/window.

PID/TID meaning must first be checked in MindStudio Insight. Do not select host
threads or a trace already aggregating multiple devices. Times are Chrome trace us.
"""
import argparse
import json
from pathlib import Path
import re


def union_length(intervals):
    total=0.0; end=None
    for start, stop in sorted(intervals):
        if end is None or start>end:
            total+=stop-start; end=stop
        elif stop>end:
            total+=stop-end; end=stop
    return total


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--trace',type=Path,required=True)
    p.add_argument('--pid',required=True)
    p.add_argument('--tids',nargs='*')
    p.add_argument('--start-us',type=float,required=True)
    p.add_argument('--end-us',type=float,required=True)
    p.add_argument('--name-regex',default='.*')
    p.add_argument('--out',type=Path,required=True)
    a=p.parse_args()
    if a.end_us<=a.start_us: p.error('end must be greater than start')
    data=json.loads(a.trace.read_text(encoding='utf-8'))
    events=data['traceEvents'] if isinstance(data,dict) else data
    spans=[]; names=set()
    for e in events:
        if e.get('ph')!='X' or str(e.get('pid'))!=a.pid: continue
        if a.tids and str(e.get('tid')) not in a.tids: continue
        if not re.search(a.name_regex,e.get('name','')): continue
        start=max(float(e['ts']),a.start_us)
        stop=min(float(e['ts'])+float(e.get('dur',0)),a.end_us)
        if stop>start: spans.append((start,stop)); names.add(e.get('name',''))
    if not spans: raise RuntimeError('No matching intervals; inspect trace device PID/TID selection')
    result=dict(events=len(spans),pid=a.pid,tids=a.tids,
        window_ms=(a.end_us-a.start_us)/1000,
        active_union_ms=union_length(spans)/1000,
        kernel_sum_ms=sum(t-s for s,t in spans)/1000,
        first_to_last_span_ms=(max(t for s,t in spans)-min(s for s,t in spans))/1000,
        distinct_names=sorted(names))
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__': main()
```
<!-- END_SCRIPT -->

