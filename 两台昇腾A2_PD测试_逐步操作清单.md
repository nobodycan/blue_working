# 两台昇腾 A2：128K 输入 / 1K 输出 PD 测试逐步操作清单

日期：2026-10-21。以下命令在两台 A2 的 **Linux Bash** 中执行。

## 0. 填写本次使用的参数

| 项目 | 本清单使用的值 | 执行前填写 |
|---|---|---|
| P 机器业务 IP | `10.10.0.11` | 替换成实际 IP |
| D 机器业务 IP | `10.10.0.12` | 替换成实际 IP |
| P / D 网卡名 | `eth0` / `eth0` | 分别用实际网卡名替换 |
| P 访问 D 的 SSH 用户 | `REPLACE_SSH_USER` | 替换成实际用户 |
| 每台 NPU | 8 张、每张 64GB | 用 npu-smi 确认 |
| 权重 | `Eco-Tech/DeepSeek-V4-Flash-w8a8-mtp` | 固定使用此权重 |
| 镜像初始 tag | `quay.io/ascend/vllm-ascend:v0.23.0` | 后续改为实际 digest |
| 输入 / 输出 | 131072 / 1024 token | 保持不变 |
| 测试目录 | `/data/v4flash-ops` | 保持不变或统一修改 |
| 模型目录 | `/data/models/DeepSeek-V4-Flash-w8a8-mtp` | 保持不变或统一修改 |
| 容器名 | `v4flash-ops` | 保持不变 |
| 本轮编号 | `pd128k_001` | 新一轮改成未使用的编号 |

执行要求：

- 具备 Docker 使用权限，能写 `/data`，P 能通过 SSH/SCP/rsync 访问 D。
- 宿主机驱动/固件已安装，并与镜像的 CANN 9.1.0 配套。未确认时先让管理员处理。
- 两台专网/RoCE 已由管理员配置并验收。不要自行批量修改 NPU IP。
- 如果权重是 `0731-w8a8`，停止使用本清单，先准备匹配配置。
- 本清单没有 A2 实测结果，检查通过后再填写性能数值。
- 所有检查失败时，先停止当前阶段并保存日志；修复后使用新文件名重跑。

## 1. 两台机器：上传并解压操作包

将 `a2_pd_operations_2026-10-21.zip` 上传到两台机器的 `/data/v4flash-ops/`。两台分别执行：

```bash
mkdir -p /data/v4flash-ops /data/models
cd /data/v4flash-ops
unzip -n a2_pd_operations_2026-10-21.zip
ls env.example.sh scripts/launch_prefill.sh scripts/launch_decode.sh
ls upstream/load_balance_proxy_server_example.py
```

检查四个文件均存在。本清单使用新的 /data/v4flash-ops 目录和 v4flash-ops 容器名。已有同名目录/配置时先归档，或统一换成未使用的目录；不要将新脚本包与旧版文件混装。

## 2. 两台机器：检查系统和设备

```bash
cd /data/v4flash-ops
mkdir -p bootstrap
hostname | tee bootstrap/hostname.txt
uname -a | tee bootstrap/uname.txt
npu-smi info | tee bootstrap/npu-smi.txt
cat /usr/local/Ascend/driver/version.info | tee bootstrap/driver-version.txt
free -h | tee bootstrap/memory.txt
df -h /data /dev/shm | tee bootstrap/disk.txt
ip -br addr | tee bootstrap/ip-addr.txt
ip route | tee bootstrap/ip-route.txt
timedatectl status | tee bootstrap/time-status.txt
command -v docker python3 curl rsync unzip
```

有 chrony 的机器再执行：

```bash
chronyc tracking | tee bootstrap/chrony.txt
```

确认每台 8 张健康的 64GB NPU、没有其他任务占用；上述命令均可用；磁盘足够；两台时钟已同步。不满足时先修复环境。

## 3. P 机器：创建公共配置

```bash
cd /data/v4flash-ops
cp env.example.sh env.sh
cat >> env.sh <<'EOF'
export RUN_ID=pd128k_001
export P_IP=10.10.0.11
export D_IP=10.10.0.12
export SSH_USER=REPLACE_SSH_USER
EOF
vi /data/v4flash-ops/env.sh
```

修改 P_IP、D_IP、SSH_USER，然后确认：

```bash
source /data/v4flash-ops/env.sh
printf 'P=%s D=%s SSH_USER=%s RUN_ID=%s\n' "$P_IP" "$D_IP" "$SSH_USER" "$RUN_ID"
```

输出还含 REPLACE 时先修改。

## 4. P 机器：创建本机网卡配置

替换 P 的 IP 和网卡名后执行：

```bash
cat > /data/v4flash-ops/env.local.sh <<'EOF'
export LOCAL_IP=10.10.0.11
export NIC=eth0
EOF
source /data/v4flash-ops/env.local.sh
ip addr show dev "$NIC"
printf 'LOCAL_IP=%s NIC=%s\n' "$LOCAL_IP" "$NIC"
```

## 5. D 机器：创建本机网卡配置

替换 D 的 IP 和网卡名后执行：

```bash
cat > /data/v4flash-ops/env.local.sh <<'EOF'
export LOCAL_IP=10.10.0.12
export NIC=eth0
EOF
source /data/v4flash-ops/env.local.sh
ip addr show dev "$NIC"
printf 'LOCAL_IP=%s NIC=%s\n' "$LOCAL_IP" "$NIC"
```

## 6. P 机器：检查到 D 的访问

```bash
source /data/v4flash-ops/env.sh
ping -c 3 "$D_IP"
ssh "$SSH_USER@$D_IP" 'hostname; npu-smi info'
```

首次 SSH 按集群要求核实主机指纹。保存管理员的专网/RoCE 验收记录到 bootstrap 目录。

## 7. P 机器：拉取并冻结镜像

```bash
source /data/v4flash-ops/env.sh
docker pull "$IMAGE"
docker image inspect "$IMAGE" > "$BENCH_ROOT/bootstrap/image-inspect.json"
IMAGE_DIGEST=$(docker image inspect "$IMAGE" --format '{{index .RepoDigests 0}}')
printf '%s\n' "$IMAGE_DIGEST" | tee "$BENCH_ROOT/bootstrap/image-digest.txt"
```

检查 IMAGE_DIGEST 含 `@sha256:`，写入配置：

```bash
python3 - "$BENCH_ROOT/env.sh" "$IMAGE_DIGEST" <<'PY'
from pathlib import Path
import sys
path=Path(sys.argv[1]);digest=sys.argv[2]
assert '@sha256:' in digest
with path.open('a') as f:f.write('\nexport IMAGE='+digest+'\n')
PY
source "$BENCH_ROOT/env.sh"
printf '%s\n' "$IMAGE"
```

## 8. P 机器：下载模型

```bash
source /data/v4flash-ops/env.sh
mkdir -p "$MODEL_DIR"
docker run --rm --network host \
  -v "$MODEL_DIR:/download" --entrypoint bash "$IMAGE" -lc '
    python -m venv /tmp/model-download-env
    /tmp/model-download-env/bin/pip install modelscope
    /tmp/model-download-env/bin/modelscope download \
      --model Eco-Tech/DeepSeek-V4-Flash-w8a8-mtp --local_dir /download
  '
ls "$MODEL_DIR/config.json"
find "$MODEL_DIR" -maxdepth 1 -name '*.safetensors' | head
ls -la "$MODEL_DIR"
du -sh "$MODEL_DIR"
```

确认下载结束、权重分片完整、MTP 权重存在；保存下载日志和仓库 revision。

## 9. P 机器：生成模型校验表

```bash
source /data/v4flash-ops/env.sh
cd "$MODEL_DIR"
find . -type f -name '*.safetensors' -print0 | sort -z | xargs -0 sha256sum \
  > "$BENCH_ROOT/model-weights.sha256"
find . -maxdepth 1 -type f \
  \( -name '*.json' -o -name '*.model' -o -name '*.tiktoken' -o -name '*.py' \) \
  -exec sha256sum {} + > "$BENCH_ROOT/model-metadata.sha256"
```

## 10. P 机器：复制公共配置和模型到 D

```bash
source /data/v4flash-ops/env.sh
scp "$BENCH_ROOT/env.sh" "$SSH_USER@$D_IP:$BENCH_ROOT/env.sh"
rsync -a --info=progress2 "$MODEL_DIR/" "$SSH_USER@$D_IP:$MODEL_DIR/"
scp "$BENCH_ROOT/model-weights.sha256" "$BENCH_ROOT/model-metadata.sha256" \
  "$SSH_USER@$D_IP:$BENCH_ROOT/"
```

不要用 P 的 env.local.sh 覆盖 D 的文件。

## 11. D 机器：校验模型

```bash
source /data/v4flash-ops/env.sh
cd "$MODEL_DIR"
sha256sum -c "$BENCH_ROOT/model-weights.sha256"
sha256sum -c "$BENCH_ROOT/model-metadata.sha256"
docker pull "$IMAGE"
docker image inspect "$IMAGE" --format '{{json .RepoDigests}}'
```

全部文件显示 OK 后继续。

## 12. 两台机器：分别启动容器

```bash
cd /data/v4flash-ops
source env.sh
bash scripts/start_container.sh
docker ps --filter "name=$CONTAINER"
docker exec "$CONTAINER" npu-smi info
```

同名容器已存在时先核对所属任务；不要删除其他任务的容器。

## 13. 两台机器：运行环境预检查

```bash
source /data/v4flash-ops/env.sh
docker exec "$CONTAINER" bash /bench/scripts/preflight.sh
docker exec "$CONTAINER" python -c \
  'import torch,torch_npu,mooncake,aiohttp,fastapi,httpx,uvicorn; print("IMPORTS_OK")'
```

应显示 PREFLIGHT PYTHON CHECK PASSED 和 IMPORTS_OK。核对 vLLM/Ascend 基版本 0.23.0、torch 2.10.0、torch-npu 2.10.0.post4。缺包、缺算子或版本不符时先修复整套环境。

## 14. 两台机器：安装诊断探针

```bash
source /data/v4flash-ops/env.sh
docker exec "$CONTAINER" python /bench/scripts/audit_patch.py
docker exec "$CONTAINER" python /bench/scripts/patch_steps.py
```

出现 source mismatch 或 anchor 不匹配时停止；不要继续启动模型。

## 15. 两台机器：创建本轮目录和输入文件

```bash
source /data/v4flash-ops/env.sh
mkdir -p "$BENCH_ROOT/results/$RUN_ID" "$BENCH_ROOT/logs" "$BENCH_ROOT/pids"
docker exec "$CONTAINER" python /bench/scripts/generate_prompts.py \
  --model /models/DeepSeek-V4-Flash-w8a8-mtp \
  --length 131072 --out /bench/data/prompt-131072.json
sha256sum "$BENCH_ROOT/data/prompt-131072.json"
```

两台输入文件哈希一致后继续。

## 16. 两台机器：启动 HBM 监控

P 机器执行：

```bash
source /data/v4flash-ops/env.sh
nohup bash "$BENCH_ROOT/scripts/host_monitor.sh" \
  "$BENCH_ROOT/results/$RUN_ID/P-hbm.log" \
  > "$BENCH_ROOT/logs/$RUN_ID-P-hbm-monitor.log" 2>&1 &
echo $! > "$BENCH_ROOT/pids/$RUN_ID-P-hbm.pid"
```

D 机器执行：

```bash
source /data/v4flash-ops/env.sh
nohup bash "$BENCH_ROOT/scripts/host_monitor.sh" \
  "$BENCH_ROOT/results/$RUN_ID/D-hbm.log" \
  > "$BENCH_ROOT/logs/$RUN_ID-D-hbm-monitor.log" 2>&1 &
echo $! > "$BENCH_ROOT/pids/$RUN_ID-D-hbm.pid"
```

## 17. P 机器：单独启动 Prefill 服务

```bash
source /data/v4flash-ops/env.sh
docker exec -d \
  -e PROFILE=1 \
  -e AUDIT_STEP_DIR="/bench/results/$RUN_ID/steps/pd-P" \
  -e AUDIT_CAPACITY_DIR="/bench/results/$RUN_ID/capacity/pd-P" \
  "$CONTAINER" bash -lc '
    source /bench/env.sh
    exec bash /bench/scripts/launch_prefill.sh \
      > "/bench/logs/$RUN_ID-P.log" 2>&1
  '
tail -f "$BENCH_ROOT/logs/$RUN_ID-P.log"
```

API ready 后按 Ctrl+C 退出 tail。P 服务仍在运行。完整启动脚本附在文末。

## 18. D 机器：单独启动 Decode 服务

```bash
source /data/v4flash-ops/env.sh
docker exec -d \
  -e PROFILE=1 \
  -e AUDIT_STEP_DIR="/bench/results/$RUN_ID/steps/pd-D" \
  -e AUDIT_TRANSFER_DIR="/bench/results/$RUN_ID/transfer/pd-D" \
  -e AUDIT_CAPACITY_DIR="/bench/results/$RUN_ID/capacity/pd-D" \
  "$CONTAINER" bash -lc '
    source /bench/env.sh
    exec bash /bench/scripts/launch_decode.sh \
      > "/bench/logs/$RUN_ID-D.log" 2>&1
  '
tail -f "$BENCH_ROOT/logs/$RUN_ID-D.log"
```

API ready 后按 Ctrl+C 退出 tail。D 服务仍在运行。完整启动脚本附在文末。

## 19. P 机器：等待两端健康

```bash
source /data/v4flash-ops/env.sh
python3 "$BENCH_ROOT/scripts/wait_ready.py" \
  --url "http://$P_IP:8100/health" \
  --url "http://$D_IP:8200/health" --timeout 1800
```

应看到两个 READY。超时后先查看对应模型日志。

## 20. P 机器：单独启动代理

```bash
source /data/v4flash-ops/env.sh
docker exec -d -e PROXY_AUDIT=1 "$CONTAINER" bash -lc '
  source /bench/env.sh
  exec bash /bench/scripts/start_proxy.sh \
    > "/bench/logs/$RUN_ID-proxy.log" 2>&1
'
python3 "$BENCH_ROOT/scripts/wait_ready.py" \
  --url "http://$P_IP:$PROXY_PORT/healthcheck" --timeout 120
```

代理 READY 后继续。

## 21. P 机器：启动 P/D 指标采集

```bash
source /data/v4flash-ops/env.sh
cat > "$BENCH_ROOT/endpoints.json" <<EOF
[
  {"role":"P","url":"http://$P_IP:8100"},
  {"role":"D","url":"http://$D_IP:8200"}
]
EOF
docker exec -d "$CONTAINER" bash -lc '
  source /bench/env.sh
  exec python /bench/scripts/metrics_monitor.py \
    --endpoints /bench/endpoints.json --interval 1 \
    --out "/bench/results/$RUN_ID/pd-metrics.jsonl" \
    > "/bench/logs/$RUN_ID-metrics.log" 2>&1
'
curl --noproxy '*' -fsS "http://$P_IP:8100/metrics" \
  > "$BENCH_ROOT/results/$RUN_ID/P-metrics-initial.txt"
curl --noproxy '*' -fsS "http://$D_IP:8200/metrics" \
  > "$BENCH_ROOT/results/$RUN_ID/D-metrics-initial.txt"
ls -lh "$BENCH_ROOT/results/$RUN_ID/pd-metrics.jsonl"
```

确认指标文件持续增长。

## 22. P 机器：发送短请求

```bash
source /data/v4flash-ops/env.sh
curl --noproxy '*' -fsS "http://$P_IP:$PROXY_PORT/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"dsv4","messages":[{"role":"user","content":"请用中文计算 2 加 3。"}],"temperature":0,"max_tokens":256,"stream":false}' \
  | tee "$BENCH_ROOT/results/$RUN_ID/short-check.json"
```

人工检查输出。乱码、异常重复或 HTTP 错误时停止并保存两端日志。

## 23. P 机器：校验一次完整 128K/1K 请求

```bash
source /data/v4flash-ops/env.sh
docker exec "$CONTAINER" python /bench/scripts/bench_client.py \
  --base-url "http://$P_IP:$PROXY_PORT" \
  --prompts /bench/data/prompt-131072.json \
  --mode wave --concurrency 1 --requests 1 --min-samples 1 \
  --output-length 1024 --return-token-ids --timeout 7200 \
  --out "/bench/results/$RUN_ID/length-check.json"
cat "$BENCH_ROOT/results/$RUN_ID/length-check.json"
cat "$BENCH_ROOT/results/$RUN_ID/length-check.requests.jsonl"
```

必须满足：valid_requests=1、all_phase_failures=0、输入 usage=131072、输出 usage=1024、output_ids=1024。

D 机器检查传输记录：

```bash
source /data/v4flash-ops/env.sh
find "$BENCH_ROOT/results/$RUN_ID/transfer/pd-D" -name '*.jsonl' -type f -size +0c
```

无记录时先检查连接器和日志，完成验收再继续。

## 24. P 机器：热身三次

```bash
source /data/v4flash-ops/env.sh
for R in 1 2 3; do
  docker exec "$CONTAINER" python /bench/scripts/bench_client.py \
    --base-url "http://$P_IP:$PROXY_PORT" \
    --prompts /bench/data/prompt-131072.json \
    --mode wave --concurrency 1 --requests 1 --min-samples 1 \
    --output-length 1024 --timeout 7200 \
    --out "/bench/results/$RUN_ID/warmup-C1-r$R.json" || break
done
```

三次均无错误后继续。

## 25. P 机器：对候选值发送三轮有限波次

```bash
source /data/v4flash-ops/env.sh
mkdir -p "$BENCH_ROOT/results/$RUN_ID/waves"
for C in 1 2 4 8 16 24 32; do
  for R in 1 2 3; do
    if ! docker exec "$CONTAINER" python /bench/scripts/bench_client.py \
      --base-url "http://$P_IP:$PROXY_PORT" \
      --prompts /bench/data/prompt-131072.json \
      --mode wave --concurrency "$C" --requests "$C" --min-samples "$C" \
      --output-length 1024 --return-token-ids --timeout 7200 \
      --out "/bench/results/$RUN_ID/waves/C${C}-r${R}.json" \
      > "$BENCH_ROOT/logs/$RUN_ID-C${C}-r${R}.log" 2>&1; then
      echo "请求失败：C=$C R=$R，停止更大候选值。"
      break 2
    fi
    docker exec "$CONTAINER" python /bench/scripts/pd_plateau.py \
      --summary "/bench/results/$RUN_ID/waves/C${C}-r${R}.json" \
      --out "/bench/results/$RUN_ID/waves/C${C}-r${R}.window.json" \
      > "$BENCH_ROOT/logs/$RUN_ID-C${C}-r${R}-window.log" 2>&1
    WINDOW_STATUS=$?
    printf 'C=%s R=%s window_exit=%s\n' "$C" "$R" "$WINDOW_STATUS"
  done
done
```

保存每轮请求文件、summary 和 window JSON。window_exit=2 的轮次标记为未通过，不作为有效纯 D 结果。

## 26. P 机器：复制候选窗口到 D

```bash
source /data/v4flash-ops/env.sh
ssh "$SSH_USER@$D_IP" "mkdir -p '$BENCH_ROOT/results/$RUN_ID/waves'"
scp "$BENCH_ROOT/results/$RUN_ID/waves/"*.window.json \
  "$SSH_USER@$D_IP:$BENCH_ROOT/results/$RUN_ID/waves/"
```

## 27. D 机器：核对每轮实际 Batch Size

```bash
source /data/v4flash-ops/env.sh
for C in 1 2 4 8 16 24 32; do
  for R in 1 2 3; do
    WINDOW="$BENCH_ROOT/results/$RUN_ID/waves/C${C}-r${R}.window.json"
    if [[ ! -f "$WINDOW" ]]; then continue; fi
    docker exec "$CONTAINER" python /bench/scripts/audit_window.py \
      --steps "/bench/results/$RUN_ID/steps/pd-D" \
      --transfer "/bench/results/$RUN_ID/transfer/pd-D" \
      --summary "/bench/results/$RUN_ID/waves/C${C}-r${R}.window.json" --run 0 \
      > "$BENCH_ROOT/results/$RUN_ID/waves/C${C}-r${R}.audit.json" \
      2> "$BENCH_ROOT/logs/$RUN_ID-C${C}-r${R}-audit.err"
    AUDIT_STATUS=$?
    printf 'C=%s R=%s audit_exit=%s\n' "$C" "$R" "$AUDIT_STATUS"
  done
done
```

查看通过情况：

```bash
python3 - "$BENCH_ROOT/results/$RUN_ID/waves" <<'PY'
from pathlib import Path
import json,sys
root=Path(sys.argv[1])
for c in (1,2,4,8,16,24,32):
    flags=[]
    for r in (1,2,3):
        p=root/f'C{c}-r{r}.audit.json'
        try:flags.append(json.loads(p.read_text()).get('valid') is True)
        except Exception:flags.append(False)
    print('candidate=',c,'valid_repeats=',sum(flags),'flags=',flags)
PY
```

只有三轮全部 valid=true 的候选值，才能选作本轮固定 BS。

## 28. D 机器：选定实测 BS，写入配置

从三轮全部通过的值中选择。下面 8 必须替换成实际通过值：

```bash
source /data/v4flash-ops/env.sh
CANDIDATE=8
docker exec "$CONTAINER" python /bench/scripts/write_bs.py \
  --directory "/bench/results/$RUN_ID/waves" \
  --candidate "$CANDIDATE" \
  --out "/bench/results/$RUN_ID/selection.env"
cat "$BENCH_ROOT/results/$RUN_ID/selection.env"
```

应输出 CONFIRMED BS=...。

- 有 TPOT 上限时，先核对三轮 window JSON 的 tpot_equivalent_ms，再选满足上限的值。
- 没有通过值时，标记“未获得合格固定 BS”；保存日志，执行第 44 节离线检查，暂不执行依赖实测 BS 的命令。
- 补测 6、12 等值时，修改第 25～27 步列表；重复候选值必须用新运行编号，不能覆盖记录。

## 29. P 机器：取回实测 BS

```bash
source /data/v4flash-ops/env.sh
scp "$SSH_USER@$D_IP:$BENCH_ROOT/results/$RUN_ID/selection.env" \
  "$BENCH_ROOT/results/$RUN_ID/selection.env"
source "$BENCH_ROOT/results/$RUN_ID/selection.env"
printf '实测 BS=%s；后续并发设置为 %s\n' "$BS" "$BS"
```

P、D 打开新终端后均重新加载：

```bash
source /data/v4flash-ops/env.sh
source "$BENCH_ROOT/results/$RUN_ID/selection.env"
```

## 30. D 机器：导出容量快照

```bash
source /data/v4flash-ops/env.sh
docker exec "$CONTAINER" python /bench/scripts/capacity_table.py \
  --directory "/bench/results/$RUN_ID/capacity/pd-D" \
  --out "/bench/results/$RUN_ID/D-capacity-startup.csv"
cat "$BENCH_ROOT/results/$RUN_ID/D-capacity-startup.csv"
npu-smi info | tee "$BENCH_ROOT/results/$RUN_ID/D-hbm-after-wave.txt"
```

## 31. 两台机器：填写本轮 HBM 表

D 机器执行：

```bash
source /data/v4flash-ops/env.sh
less "$BENCH_ROOT/results/$RUN_ID/D-hbm.log"
less "$BENCH_ROOT/results/$RUN_ID/D-capacity-startup.csv"
```

P 机器执行：

```bash
source /data/v4flash-ops/env.sh
source "$BENCH_ROOT/results/$RUN_ID/selection.env"
cat "$BENCH_ROOT/results/$RUN_ID/waves/C${BS}-r1.window.json"
cat "$BENCH_ROOT/results/$RUN_ID/waves/C${BS}-r2.window.json"
cat "$BENCH_ROOT/results/$RUN_ID/waves/C${BS}-r3.window.json"
```

按三轮时间戳找到 HBM 日志中的对应区间，逐卡填写：

| NPU | HBM 总量 | 该 BS 下 2 秒采样最高占用 | 采样最小余量 | KV 池预留 GiB | KV 使用率峰值 | 抢占/重算 |
|---|---:|---:|---:|---:|---:|---:|
| 0～7 各一行 | 实测 | 实测 | 实测 | CSV | 指标日志 | 必须为 0 |

- HBM 数值注明“2 秒采样最高占用”，不用 startup CSV 代填；接近边界的点补充 profiler 内存记录。
- KV 使用率从 D metrics 读取；先核对实际指标名称、HELP 和标签，不把重复暴露的值求和。
- 任一卡 KV 使用率≥95% 或 HBM 余量≤5%，标“接近容量边界”。
- 出现 OOM、preemption、recomputed 或分配失败，标“未通过”，调整后新建结果目录重测。

## 32. P 机器：测 30 次单并发 TTFT

```bash
source /data/v4flash-ops/env.sh
mkdir -p "$BENCH_ROOT/results/$RUN_ID/single"
for R in $(seq 1 30); do
  if ! docker exec "$CONTAINER" python /bench/scripts/bench_client.py \
    --base-url "http://$P_IP:$PROXY_PORT" \
    --prompts /bench/data/prompt-131072.json \
    --mode wave --concurrency 1 --requests 1 --min-samples 1 \
    --output-length 1024 --timeout 7200 \
    --out "/bench/results/$RUN_ID/single/r$R.json" \
    > "$BENCH_ROOT/logs/$RUN_ID-single-r$R.log" 2>&1; then
    echo "单并发第 $R 次失败，停止并检查日志。"
    break
  fi
done
```

执行检查，必须显示 PASS：

```bash
docker exec "$CONTAINER" python /bench/scripts/check_runs.py \
  --directory "/bench/results/$RUN_ID/single" --runs 30 --concurrency 1
```


## 33. P 机器：将并发设为实测 BS，测 10 轮

```bash
source /data/v4flash-ops/env.sh
source "$BENCH_ROOT/results/$RUN_ID/selection.env"
mkdir -p "$BENCH_ROOT/results/$RUN_ID/batch"
for R in $(seq 1 10); do
  if ! docker exec "$CONTAINER" python /bench/scripts/bench_client.py \
    --base-url "http://$P_IP:$PROXY_PORT" \
    --prompts /bench/data/prompt-131072.json \
    --mode wave --concurrency "$BS" --requests "$BS" --min-samples "$BS" \
    --output-length 1024 --timeout 7200 \
    --out "/bench/results/$RUN_ID/batch/r$R.json" \
    > "$BENCH_ROOT/logs/$RUN_ID-batch-r$R.log" 2>&1; then
    echo "BS 并发第 $R 轮失败，停止并检查日志。"
    break
  fi
done
```

执行检查，必须显示 PASS：

```bash
docker exec "$CONTAINER" python /bench/scripts/check_runs.py \
  --directory "/bench/results/$RUN_ID/batch" --runs 10 --concurrency "$BS"
```


## 34. P 机器：生成 TTFT 对比结果

```bash
source /data/v4flash-ops/env.sh
docker exec "$CONTAINER" bash -lc '
  source /bench/env.sh
  source "/bench/results/$RUN_ID/selection.env"
  python /bench/scripts/compare_ttft.py \
    --bs "$BS" --tolerance 0.10 \
    --single /bench/results/"$RUN_ID"/single/r*.json \
    --batch /bench/results/"$RUN_ID"/batch/r*.json
' | tee "$BENCH_ROOT/results/$RUN_ID/ttft-comparison.json"
```

抄录 single_mean_ttft_ms、batch_mean_ttft_ms、batch_mean_last_ttft_ms、batch_to_single、last_to_single、relative_error_to_mentor_prediction、request_start_spread_ms。

## 35. P 机器：持续负载测三轮

```bash
source /data/v4flash-ops/env.sh
source "$BENCH_ROOT/results/$RUN_ID/selection.env"
mkdir -p "$BENCH_ROOT/results/$RUN_ID/steady"
for R in 1 2 3; do
  if ! docker exec "$CONTAINER" python /bench/scripts/bench_client.py \
    --base-url "http://$P_IP:$PROXY_PORT" \
    --prompts /bench/data/prompt-131072.json \
    --mode steady --concurrency "$BS" --burnin 300 --duration 1800 \
    --output-length 1024 --timeout 7200 --min-samples 30 \
    --out "/bench/results/$RUN_ID/steady/r$R.json" \
    > "$BENCH_ROOT/logs/$RUN_ID-steady-r$R.log" 2>&1; then
    echo "持续负载第 $R 轮未通过，检查日志和样本数。"
    break
  fi
done
```

执行检查，必须显示 PASS：

```bash
docker exec "$CONTAINER" python /bench/scripts/check_runs.py \
  --directory "/bench/results/$RUN_ID/steady" --runs 3 --concurrency "$BS" --mode steady
```

样本不足或 end_censored_fraction>0.10 时，使用新输出目录，将 duration 增至 3600 或更长后重测。

## 36. P 机器：复制持续负载 summary 到 D

```bash
source /data/v4flash-ops/env.sh
ssh "$SSH_USER@$D_IP" "mkdir -p '$BENCH_ROOT/results/$RUN_ID/steady'"
scp "$BENCH_ROOT/results/$RUN_ID/steady/"r*.json \
  "$SSH_USER@$D_IP:$BENCH_ROOT/results/$RUN_ID/steady/"
```

## 37. D 机器：导出实际 BS 分布

```bash
source /data/v4flash-ops/env.sh
for R in 1 2 3; do
  docker exec "$CONTAINER" python /bench/scripts/report_batches.py \
    --steps "/bench/results/$RUN_ID/steps/pd-D" \
    --summary "/bench/results/$RUN_ID/steady/r$R.json" \
    --out "/bench/results/$RUN_ID/steady/r$R-actual-bs.json"
done
```

逐轮记录 8 个 worker 的 min/p50/p95/max/iteration_weighted_mean，不把此表直接填成客户端并发值。

## 38. P 机器：采集单并发 Profiling

确认没有其他压测请求后执行：

```bash
source /data/v4flash-ops/env.sh
curl --noproxy '*' -fsS -X POST "http://$P_IP:8100/start_profile"
curl --noproxy '*' -fsS -X POST "http://$D_IP:8200/start_profile"
docker exec "$CONTAINER" python /bench/scripts/bench_client.py \
  --base-url "http://$P_IP:$PROXY_PORT" \
  --prompts /bench/data/prompt-131072.json \
  --mode wave --concurrency 1 --requests 1 --min-samples 1 \
  --output-length 1024 --timeout 7200 \
  --out "/bench/results/$RUN_ID/profile-single.json"
curl --noproxy '*' -fsS -X POST "http://$P_IP:8100/stop_profile"
curl --noproxy '*' -fsS -X POST "http://$D_IP:8200/stop_profile"
```

请求失败也执行两条 stop_profile；不把本次结果加入正式 TTFT 平均值。

## 39. P 机器：采集 BS 并发 Profiling

```bash
source /data/v4flash-ops/env.sh
source "$BENCH_ROOT/results/$RUN_ID/selection.env"
curl --noproxy '*' -fsS -X POST "http://$P_IP:8100/start_profile"
curl --noproxy '*' -fsS -X POST "http://$D_IP:8200/start_profile"
docker exec "$CONTAINER" python /bench/scripts/bench_client.py \
  --base-url "http://$P_IP:$PROXY_PORT" \
  --prompts /bench/data/prompt-131072.json \
  --mode wave --concurrency "$BS" --requests "$BS" --min-samples "$BS" \
  --output-length 1024 --return-token-ids --timeout 7200 \
  --out "/bench/results/$RUN_ID/profile-batch.json"
curl --noproxy '*' -fsS -X POST "http://$P_IP:8100/stop_profile"
curl --noproxy '*' -fsS -X POST "http://$D_IP:8200/stop_profile"
```

采集前检查磁盘。两端保留完整 trace。

## 40. 两台机器：分析 Profiling 文件

```bash
source /data/v4flash-ops/env.sh
docker exec -i "$CONTAINER" python - <<'PY'
from pathlib import Path
from torch_npu.profiler.profiler import analyse
for p in Path('/bench/profiles').rglob('*ascend_pt'):
    analyse(str(p))
PY
find "$BENCH_ROOT/profiles" -name trace_view.json
find "$BENCH_ROOT/profiles" -name kernel_details.csv
```

将 trace_view.json 复制到装有 MindStudio Insight 的电脑，依次操作：

1. 单请求：标出 P Prefill 起止、KV 传输、D 首次输出。
2. BS 并发：定位已通过审核的 Decode 公共窗口。
3. 检查该窗口没有 Prefill、KV 传输、重计算或抢占。
4. 保存截图和时间坐标。
5. 逐卡检查，不将 8 卡 kernel duration 相加后填入 TTFT。

## 41. 两台机器：保存诊断阶段检查结果

```bash
source /data/v4flash-ops/env.sh
npu-smi info > "$BENCH_ROOT/results/$RUN_ID/npu-after-diagnostic.txt"
docker logs "$CONTAINER" > "$BENCH_ROOT/logs/$RUN_ID-container.log" 2>&1
```

P 机器额外执行：

```bash
curl --noproxy '*' -fsS "http://$P_IP:8100/metrics" \
  > "$BENCH_ROOT/results/$RUN_ID/P-metrics-final.txt"
curl --noproxy '*' -fsS "http://$D_IP:8200/metrics" \
  > "$BENCH_ROOT/results/$RUN_ID/D-metrics-final.txt"
```

检查 P/D 日志中的 OOM、preemption、recomputed、transfer failed、traceback。异常时间段对应的样本标无效。

## 42. 可选：实测更小 KV 池预算

第 28 步已有实测 BS、且没有在途请求时再执行。先从 D-capacity-startup.csv 读取当前 KV 池大小，选择相近的更小值。

### 42.1 D 机器：设置一个候选预算并重启 D

下面 8GiB 只是命令示例，必须换成你选定的预算。

```bash
source /data/v4flash-ops/env.sh
source "$BENCH_ROOT/results/$RUN_ID/selection.env"
K=$((8*1024*1024*1024))
printf '%s\n' "$K" > "$BENCH_ROOT/results/$RUN_ID/current-kv-budget.txt"
docker restart "$CONTAINER"
docker exec -d \
  -e PROFILE=0 -e KV_BYTES="$K" \
  -e AUDIT_STEP_DIR="/bench/results/$RUN_ID/steps/K$K" \
  -e AUDIT_TRANSFER_DIR="/bench/results/$RUN_ID/transfer/K$K" \
  -e AUDIT_CAPACITY_DIR="/bench/results/$RUN_ID/capacity/K$K" \
  "$CONTAINER" bash -lc '
    source /bench/env.sh
    exec bash /bench/scripts/launch_decode.sh \
      > "/bench/logs/$RUN_ID-D-K$KV_BYTES.log" 2>&1
  '
python3 "$BENCH_ROOT/scripts/wait_ready.py" \
  --url "http://$D_IP:8200/health" --timeout 1800
```

启动失败时保存日志，将该预算记为失败，不继续发送正式请求。

### 42.2 P 机器：取回预算值并测三轮

```bash
source /data/v4flash-ops/env.sh
source "$BENCH_ROOT/results/$RUN_ID/selection.env"
scp "$SSH_USER@$D_IP:$BENCH_ROOT/results/$RUN_ID/current-kv-budget.txt" \
  "$BENCH_ROOT/results/$RUN_ID/current-kv-budget.txt"
K=$(cat "$BENCH_ROOT/results/$RUN_ID/current-kv-budget.txt")
mkdir -p "$BENCH_ROOT/results/$RUN_ID/kv-sweep/K$K"
for R in 1 2 3; do
  if ! docker exec "$CONTAINER" python /bench/scripts/bench_client.py \
    --base-url "http://$P_IP:$PROXY_PORT" \
    --prompts /bench/data/prompt-131072.json \
    --mode wave --concurrency "$BS" --requests "$BS" --min-samples "$BS" \
    --output-length 1024 --return-token-ids --timeout 7200 \
    --out "/bench/results/$RUN_ID/kv-sweep/K$K/r$R.json"; then
    echo "预算 $K 的请求未通过，保存日志。"
    break
  fi
  docker exec "$CONTAINER" python /bench/scripts/pd_plateau.py \
    --summary "/bench/results/$RUN_ID/kv-sweep/K$K/r$R.json" \
    --out "/bench/results/$RUN_ID/kv-sweep/K$K/r$R.window.json"
done
ssh "$SSH_USER@$D_IP" "mkdir -p '$BENCH_ROOT/results/$RUN_ID/kv-sweep/K$K'"
scp "$BENCH_ROOT/results/$RUN_ID/kv-sweep/K$K/"*.window.json \
  "$SSH_USER@$D_IP:$BENCH_ROOT/results/$RUN_ID/kv-sweep/K$K/"
```

### 42.3 D 机器：审核三轮

```bash
source /data/v4flash-ops/env.sh
K=$(cat "$BENCH_ROOT/results/$RUN_ID/current-kv-budget.txt")
for R in 1 2 3; do
  docker exec "$CONTAINER" python /bench/scripts/audit_window.py \
    --steps "/bench/results/$RUN_ID/steps/K$K" \
    --transfer "/bench/results/$RUN_ID/transfer/K$K" \
    --summary "/bench/results/$RUN_ID/kv-sweep/K$K/r$R.window.json" --run 0 \
    > "$BENCH_ROOT/results/$RUN_ID/kv-sweep/K$K/r$R.audit.json"
done
```

三轮成功、无抢占、固定 BS 审核通过，才把预算记为成功。

1. 按 1～2GiB 降低，重复 42.1～42.3。
2. 找到相邻成功/失败预算后，按 256MiB 细化。
3. 记录最大失败预算和最小成功预算。
4. 重复预算值使用新 RUN_ID，不覆盖旧记录。
5. 最后执行第 43 节恢复自动预算，不把缩池结果混入原始预算 TTFT。

## 43. 两台机器：关闭逐步诊断，重启正式测量服务

确认没有在途请求，再重启本测试容器。

### 43.1 P 机器

```bash
source /data/v4flash-ops/env.sh
docker restart "$CONTAINER"
docker exec -d -e PROFILE=0 -e KV_BYTES= \
  -e AUDIT_STEP_DIR= -e AUDIT_TRANSFER_DIR= \
  -e AUDIT_CAPACITY_DIR="/bench/results/$RUN_ID/capacity/formal-P" \
  "$CONTAINER" bash -lc '
    source /bench/env.sh
    exec bash /bench/scripts/launch_prefill.sh \
      > "/bench/logs/$RUN_ID-formal-P.log" 2>&1
  '
```

### 43.2 D 机器

```bash
source /data/v4flash-ops/env.sh
docker restart "$CONTAINER"
docker exec -d -e PROFILE=0 -e KV_BYTES= \
  -e AUDIT_STEP_DIR= -e AUDIT_TRANSFER_DIR= \
  -e AUDIT_CAPACITY_DIR="/bench/results/$RUN_ID/capacity/formal-D" \
  "$CONTAINER" bash -lc '
    source /bench/env.sh
    exec bash /bench/scripts/launch_decode.sh \
      > "/bench/logs/$RUN_ID-formal-D.log" 2>&1
  '
```

### 43.3 P 机器：重启代理和指标采集

```bash
source /data/v4flash-ops/env.sh
python3 "$BENCH_ROOT/scripts/wait_ready.py" \
  --url "http://$P_IP:8100/health" --url "http://$D_IP:8200/health" --timeout 1800
docker exec -d -e PROXY_AUDIT=0 "$CONTAINER" bash -lc '
  source /bench/env.sh
  exec bash /bench/scripts/start_proxy.sh \
    > "/bench/logs/$RUN_ID-formal-proxy.log" 2>&1
'
python3 "$BENCH_ROOT/scripts/wait_ready.py" \
  --url "http://$P_IP:$PROXY_PORT/healthcheck" --timeout 120
docker exec -d "$CONTAINER" bash -lc '
  source /bench/env.sh
  exec python /bench/scripts/metrics_monitor.py \
    --endpoints /bench/endpoints.json --interval 1 \
    --out "/bench/results/$RUN_ID/formal-metrics.jsonl" \
    > "/bench/logs/$RUN_ID-formal-metrics.log" 2>&1
'
```

### 43.4 P 机器：热身并重复正式 TTFT

```bash
source /data/v4flash-ops/env.sh
source "$BENCH_ROOT/results/$RUN_ID/selection.env"
for R in 1 2 3; do
  docker exec "$CONTAINER" python /bench/scripts/bench_client.py \
    --base-url "http://$P_IP:$PROXY_PORT" \
    --prompts /bench/data/prompt-131072.json \
    --mode wave --concurrency "$BS" --requests "$BS" --min-samples "$BS" \
    --output-length 1024 --timeout 7200 \
    --out "/bench/results/$RUN_ID/formal-warmup-r$R.json" || break
done
mkdir -p "$BENCH_ROOT/results/$RUN_ID/formal-single" "$BENCH_ROOT/results/$RUN_ID/formal-batch"
for R in $(seq 1 30); do
  docker exec "$CONTAINER" python /bench/scripts/bench_client.py \
    --base-url "http://$P_IP:$PROXY_PORT" \
    --prompts /bench/data/prompt-131072.json \
    --mode wave --concurrency 1 --requests 1 --min-samples 1 \
    --output-length 1024 --timeout 7200 \
    --out "/bench/results/$RUN_ID/formal-single/r$R.json" || break
done
for R in $(seq 1 10); do
  docker exec "$CONTAINER" python /bench/scripts/bench_client.py \
    --base-url "http://$P_IP:$PROXY_PORT" \
    --prompts /bench/data/prompt-131072.json \
    --mode wave --concurrency "$BS" --requests "$BS" --min-samples "$BS" \
    --output-length 1024 --timeout 7200 \
    --out "/bench/results/$RUN_ID/formal-batch/r$R.json" || break
done
docker exec "$CONTAINER" bash -lc '
  source /bench/env.sh
  source "/bench/results/$RUN_ID/selection.env"
  python /bench/scripts/compare_ttft.py --bs "$BS" --tolerance 0.10 \
    --single /bench/results/"$RUN_ID"/formal-single/r*.json \
    --batch /bench/results/"$RUN_ID"/formal-batch/r*.json
' | tee "$BENCH_ROOT/results/$RUN_ID/formal-ttft-comparison.json"
```

再执行两项检查，均须显示 PASS 后才使用正式对比结果：

```bash
docker exec "$CONTAINER" python /bench/scripts/check_runs.py \
  --directory "/bench/results/$RUN_ID/formal-single" --runs 30 --concurrency 1
docker exec "$CONTAINER" python /bench/scripts/check_runs.py \
  --directory "/bench/results/$RUN_ID/formal-batch" --runs 10 --concurrency "$BS"
```

将正式与诊断结果分开保存，记录两组差异。

## 44. 可选：D 机器离线固定 Batch Size 检查

没有在途请求时执行。结果保存到 offline 目录，不能填入在线 PD 结果栏。

### 44.1 D 机器：重启测试容器，确认 NPU 释放

```bash
source /data/v4flash-ops/env.sh
docker restart "$CONTAINER"
npu-smi info
```

确认没有残留模型进程。此时不要启动在线 D 服务。

### 44.2 D 机器：扫描固定 B

```bash
source /data/v4flash-ops/env.sh
mkdir -p "$BENCH_ROOT/results/$RUN_ID/offline"
for B in 1 2 4 8 16 24 32; do
  if ! docker exec \
    -e AUDIT_STEP_DIR="/bench/results/$RUN_ID/steps/offline-B$B" \
    -e AUDIT_CAPACITY_DIR="/bench/results/$RUN_ID/capacity/offline-B$B" \
    "$CONTAINER" bash -lc "source /bench/scripts/common.sh; \
      python /bench/scripts/offline_decode.py --batch $B --warmup 1 --repeats 3 \
      --out /bench/results/$RUN_ID/offline/B$B.json" \
    > "$BENCH_ROOT/logs/$RUN_ID-offline-B$B.log" 2>&1; then
    echo "离线 B=$B 未通过，先检查日志。"
    break
  fi
  for R in 1 2 3; do
    if ! docker exec "$CONTAINER" python /bench/scripts/audit_window.py \
      --steps "/bench/results/$RUN_ID/steps/offline-B$B" \
      --summary "/bench/results/$RUN_ID/offline/B$B.json" --run "$R" \
      > "$BENCH_ROOT/results/$RUN_ID/offline/B${B}-r${R}.audit.json"; then
      echo "离线 worker 审核未通过，停止更大 B。"
      break 2
    fi
  done
done
```

### 44.3 D 机器：导出离线结果表

```bash
docker exec "$CONTAINER" python /bench/scripts/offline_table.py \
  --directory "/bench/results/$RUN_ID/offline" \
  --out "/bench/results/$RUN_ID/offline-summary.csv"
```

使用某个点之前，检查它的三份 worker 审核。继续在线测试时恢复第 18～20 步；已运行的 P/代理不要重复启动。

## 45. P 机器：填写提交给导师的结果文件

```bash
source /data/v4flash-ops/env.sh
cat > "$BENCH_ROOT/results/$RUN_ID/RESULT.md" <<'EOF'
# 本次测试结果

## 配置

- P 机器型号/NPU/单卡 HBM：
- D 机器型号/NPU/单卡 HBM：
- 镜像 digest：
- 权重仓库/revision/文件哈希：
- 驱动、CANN、torch、torch_npu、vLLM、Ascend 版本：
- 输入 token：131072
- 输出 token：1024
- P/D TP：8/8
- P/D DP：1/1
- MTP：1
- max_num_seqs：
- P/D token budget：
- KV 池预算：

## 纯 D 测量

- 在线 PD 是否获得三轮有效纯 D 窗口：
- 实测 Decode Batch Size：
- 三轮窗口时长/worker 调度步数：
- 三轮纯 D tokens/s：
- 三轮 TPOT_equivalent_ms：
- D 实际 BS 分布：
- P 实际 BS/每步 Prefill token 数：

## HBM

- 最紧张 NPU：
- HBM 总量：
- 该 BS 下运行 HBM 峰值/最小余量：
- KV 池预留量/使用率峰值：
- 抢占/重算/分配失败次数：
- 是否接近容量边界：
- 如做缩池：最大失败预算/最小成功预算：

## TTFT

- 单并发样本数/平均/P50/P95 TTFT：
- C=实测 BS 的波次数/请求数：
- C=实测 BS 的平均/P50/P95 TTFT：
- 每波最慢 TTFT 平均：
- 平均 TTFT 比值/最慢 TTFT 比值：
- 与 BS 倍预测的偏差：
- 每波提交时间差：
- 正式测量与诊断测量差异：

## 附件

- 请求 JSONL、summary、window、8 worker 审核：
- 8 卡 HBM、P/D metrics、日志：
- Profiling trace、截图和时间坐标：
- 如只完成离线 D 检查，另列离线结果并标在线项未完成：

## 本轮未通过项

- 步骤、时间、错误、对应日志、处理结果：
EOF
```

逐项填写。未通过项写“未完成/未通过”，不要填 0 或用离线结果代填在线结果。

## 46. 两台机器：停止本次 HBM 监控

P 机器执行：

```bash
source /data/v4flash-ops/env.sh
kill -TERM "$(cat "$BENCH_ROOT/pids/$RUN_ID-P-hbm.pid")"
```

D 机器执行：

```bash
source /data/v4flash-ops/env.sh
kill -TERM "$(cat "$BENCH_ROOT/pids/$RUN_ID-D-hbm.pid")"
```

## 47. 两台机器：停止测试容器并归档

确认本测试容器未被其他任务共用，再分别执行：

```bash
source /data/v4flash-ops/env.sh
docker stop "$CONTAINER"
NODE_TAG=$(hostname)
cd "$BENCH_ROOT"
tar -czf "/data/${RUN_ID}-${NODE_TAG}.tar.gz" \
  env.sh env.local.sh image-inspect.json bootstrap \
  model-weights.sha256 model-metadata.sha256 \
  scripts upstream results/"$RUN_ID" logs profiles
ls -lh "/data/${RUN_ID}-${NODE_TAG}.tar.gz"
```

将两台归档文件、RESULT.md 和 Profiling 截图交给导师，按实验室要求存放。

## 48. 完成前逐项勾选

- [ ] 两台设备、驱动、镜像 digest、模型哈希已保存。
- [ ] P、D、代理分别启动并健康。
- [ ] 短请求正常，128K/1K token 数通过。
- [ ] BS 来自实际 D 调度，8 worker 审核已保存。
- [ ] 三轮纯 D 窗口有效，没有传输、Prefill、重算或抢占。
- [ ] HBM 表记录了 8 张卡及最紧张卡。
- [ ] 单并发 TTFT 已完整测 30 次。
- [ ] C=实测 BS 已完整测 10 波。
- [ ] 持续负载三轮完成，样本数和跨窗比例已检查。
- [ ] Profiling 已分析，截图已保存。
- [ ] 逐步诊断已关闭，正式 TTFT 已配对复测。
- [ ] 在线、离线、缩池结果分开保存。
- [ ] 未通过项已记录，没有覆盖原始文件。
- [ ] 两台结果包和 RESULT.md 已归档。

## 附录 A. P 机器完整启动脚本

以下文件已包含在操作包中。按第 17 步调用，不需要再次手工创建。

```bash
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
```

## 附录 B. D 机器完整启动脚本

以下文件已包含在操作包中。按第 18 步调用。

```bash
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
```

## 附录 C. 代理完整启动命令

在 P 容器中单独前台排查时使用。执行前先停止已运行的同端口代理；不要同时执行两份。

```bash
source /bench/scripts/common.sh
python /bench/upstream/load_balance_proxy_server_example.py \
  --host 0.0.0.0 --port "$PROXY_PORT" --workers 1 --max-retries 1 \
  --prefiller-hosts "$P_IP" --prefiller-ports 8100 \
  --decoder-hosts "$D_IP" --decoder-ports 8200
```

