# PD Network Diagnostics

昇腾 A2 双机 PD 网络排查，更新于 2026-10-22。适用于本目录 README 中的 vLLM 0.30.0、vLLM-Ascend 0.30.0rc2、MooncakeHybridConnector、每实例 TP=8 场景。

本文包含所需命令和内嵌 Python，无需 Git、下载脚本或压缩包。执行位置标在各步骤标题中。排查时保留原始日志，先完成只读检查，再根据结果修改配置。

## 0. 当前已知事实

用户已确认：

| 项目 | 当前结果 |
|---|---|
| 第 10 步短 PD 请求 | 客户端看到 `[DONE]` |
| D 端报错 | `Failed to connect to target ...:20026` |
| 错误码 | `status 103900`、`E19999 Internal error` |
| 后续具体错误 | `MatchEndpoints failed timeout: 10000ms` |
| target IP | 等于配置的 P_IP |
| P、D 容器网络 | 均为 `host` |

仍未确认：报错端口的 TCP 可达性、两侧容器的 HCCN 配置、跨机 NPU 网络、TLS 配置、实际加载的通信库是否一致。

**当前结论：KV 传输建链检查未通过，暂停性能测试。**目标 IP 填错和 Docker bridge 漏映射端口的方向已缩小，但不能据此认定 TCP、RDMA 或底层端点协商正常。

`MatchEndpoints` 这段日志提示优先检查底层端点匹配及相关网络配置；仅凭这一行还不能确定唯一根因，也不能确定是哪一层等待超时。本轮没有拿到完整 P/D 底层日志或在 A2 上复现。

`[DONE]` 是 SSE 流结束标记。它不能单独作为 P→D KV 传输成功、完整输出成功或纯 D 性能有效的依据。

## 1. 固定本次报错时间、目标和原始日志

记录以下内容，后续 P、D 日志按同一时间段对齐：

```text
请求时间：
请求 ID（日志有则填写）：
P_IP：
D_IP：
D 报错中的完整 target IP:port：
P 侧同一时刻错误：
D 侧 E19999 前后具体错误：
```

本例端口是 `20026`。模型重启后端口可能变化，后续命令使用**当前运行日志里的最新端口**。

本手册的 API 端口是 P=8100、D=8200；connector 的 `kv_port` 是 P=30000、D=30100。传输引擎/底层建链还涉及其他端口，不能只验证 API 端口。不要仅根据本次 `20026` 报错，将 `kv_port` 改成 `20026`。

## 2. P 容器：检查当前报错端口是否监听

保持当前模型服务运行，在 P 容器执行：

```bash
ss -lntp 'sport = :20026'
```

如果容器缺少 `ss`，可在 P 宿主机执行同一命令。当前使用 host 网络，宿主机检查也可用于核对主机监听。

| 输出 | 处理 |
|---|---|
| 监听在 P_IP 或覆盖该 IP 的通配地址 | 继续第 3 步 |
| 只监听 127.0.0.1 | 核对服务绑定地址和实际进程环境 |
| 没有监听 | 核对目标是否为当前端口、P worker 是否仍存活、P 的传输引擎初始化日志 |

不要额外启动一个临时服务占住此端口来让检查通过。

## 3. D 容器：测试到 P 报错端口的 TCP 可达性

替换 IP 和当前端口，在 D 容器执行：

```bash
python - <<'PY'
import socket

target = ("填写报错中的P_IP", 20026)
try:
    with socket.create_connection(target, timeout=3):
        print("TCP_OK", target)
except OSError as exc:
    print("TCP_FAIL", target, repr(exc))
PY
```

该命令只建立并关闭一次 TCP 连接，不发送协议数据，不能验证 Mooncake/HIXL 协议握手或 NPU 数据传输。

| 结果 | 下一步 |
|---|---|
| `Connection refused` | 核对目标进程、监听地址、端口是否变化，以及主动拒绝连接的访问策略 |
| `timed out` | 核对路由、目标监听及主机/网络访问策略 |
| `No route to host` | 核对路由和相关网络策略 |
| `TCP_OK` | 继续 HCCN、NPU 网口和通信配置检查 |

此步骤仍失败时，不进入性能测试。`TCP_OK` 也不能作为 RDMA 成功的结论。

## 4. 两台宿主机：核对各自容器的 HCCN 配置

P 宿主机执行，替换 P 容器名：

```bash
test -s /etc/hccn.conf && echo HOST_HCCN_OK || echo HOST_HCCN_MISSING_OR_EMPTY
sha256sum /etc/hccn.conf
docker exec P容器名 test -s /etc/hccn.conf
docker exec P容器名 sha256sum /etc/hccn.conf
```

D 宿主机执行，替换 D 容器名：

```bash
test -s /etc/hccn.conf && echo HOST_HCCN_OK || echo HOST_HCCN_MISSING_OR_EMPTY
sha256sum /etc/hccn.conf
docker exec D容器名 test -s /etc/hccn.conf
docker exec D容器名 sha256sum /etc/hccn.conf
```

检查：

- 两个容器中的文件都存在且非空。`docker exec ... test -s` 成功时没有输出，退出码为 0。
- P 容器与 **P 宿主机**的 SHA256 一致。
- D 容器与 **D 宿主机**的 SHA256 一致。
- P、D 两台机器的配置不要求相同，不互相复制。
- 一致性只说明容器看到的是本机配置，配置本身是否正确还需结合实际 NPU IP 和网络测试判断。

若缺少挂载，在安排停机并保留原容器参数、模型挂载和结果后，修正容器创建配置，增加本机文件挂载：

```text
-v /etc/hccn.conf:/etc/hccn.conf:ro
```

这是容器创建参数，不能通过在现有容器终端执行这行文字来补挂载。不要为了修复它直接删除现有容器或测试数据。

## 5. 两台宿主机：检查 8 个 NPU 网口

P、D 宿主机分别执行：

```bash
HCCN=/usr/local/Ascend/driver/tools/hccn_tool
test -x "$HCCN" || { echo '请确认本机 hccn_tool 路径'; exit 1; }

for i in {0..7}; do
  echo "NPU $i"
  "$HCCN" -i "$i" -ip -g
  "$HCCN" -i "$i" -link -g
  "$HCCN" -i "$i" -net_health -g
done
```

记录各卡 IP，检查链路是否 UP、健康状态是否正常。这里的 NPU IP 与主机通信 P_IP/D_IP 是不同配置项，按实际输出区分，不凭地址外观猜测。

若有 DOWN 或健康异常，记录机器和卡号，先处理物理网络/设备网络，再重试 PD。

## 6. 两台宿主机：做 NPU 网口跨机测试

先测一对，目标使用第 5 步查到的**对端 NPU 网口 IP**。

D 宿主机执行：

```bash
/usr/local/Ascend/driver/tools/hccn_tool \
  -i 0 -ping -g address 填写P机器NPU0的IP
```

P 宿主机执行：

```bash
/usr/local/Ascend/driver/tools/hccn_tool \
  -i 0 -ping -g address 填写D机器NPU0的IP
```

两向都成功后，根据 P/D 日志中实际参与传输的源卡、目标卡组合，替换 `-i` 和目标地址继续覆盖其他路径。只验证 NPU0→NPU0，不能证明 TP8 的实际传输路径全部正常；只做主机 `ping P_IP` 也不能代替此检查。

NPU ping 成功仍不能替代完整的数据传输测试。它用于先定位网络连通问题。

## 7. 两台宿主机：检查 NPU TLS 设置

在两台宿主机分别执行，比较参与通信卡的开关配置：

```bash
HCCN=/usr/local/Ascend/driver/tools/hccn_tool
for i in {0..7}; do
  echo "NPU $i"
  "$HCCN" -i "$i" -tls -g
done
```

记录两侧配置是否一致及读取是否报错。如果配置不一致，交由环境管理员结合证书、设备和现有集群要求核对。本文不给出关闭 TLS 的修复命令。

## 8. 两个容器：核对 IP 选择和通信环境

在 P、D 容器分别执行：

```bash
python - <<'PY'
import os
from vllm.utils.network_utils import get_ip

for name in (
    "VLLM_HOST_IP", "HCCL_IF_IP", "HCCL_SOCKET_IFNAME",
    "ASCEND_BASE_PORT", "ASCEND_CONNECT_TIMEOUT", "ASCEND_TRANSFER_TIMEOUT",
    "HCCL_INTRA_ROCE_ENABLE", "ASCEND_GLOBAL_RESOURCE_CONFIG",
):
    print(name, "=", os.getenv(name))
print("get_ip() =", get_ip())
PY
```

新终端的环境不一定等于运行中 worker 的环境。若要核对实际进程，从当前错误日志取得本机 worker PID，然后执行以下命令，只打印相关配置：

```bash
export WORKER_PID=填写当前报错worker的PID
python - <<'PY'
import os
from pathlib import Path

pid = int(os.environ["WORKER_PID"])
names = {
    "VLLM_HOST_IP", "HCCL_IF_IP", "HCCL_SOCKET_IFNAME",
    "ASCEND_BASE_PORT", "ASCEND_CONNECT_TIMEOUT", "ASCEND_TRANSFER_TIMEOUT",
    "HCCL_INTRA_ROCE_ENABLE", "ASCEND_GLOBAL_RESOURCE_CONFIG",
}
items = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
values = {}
for item in items:
    key, sep, value = item.partition(b"=")
    name = key.decode(errors="replace")
    if sep and name in names:
        values[name] = value.decode(errors="replace")
for name in sorted(names):
    print(name, "=", values.get(name, "<unset>"))
PY
```

此前启动脚本显式设置了 `HCCL_IF_IP`，没有显式设置 `VLLM_HOST_IP`。若自动 IP 选择错误，在两个启动 Shell 中、`vllm serve` 之前补充：

```bash
export VLLM_HOST_IP="$LOCAL_IP"
```

P 的 LOCAL_IP 使用 P 通信 IP，D 使用 D 通信 IP。修改后需要正常停止并重启相应模型服务，使 worker 重新初始化；在新终端 export 不会修改已有进程。若原本选择正确，此项不能解释为已经找到本次根因。

当前已确认错误目标 IP 等于 P_IP，因此应优先完成前面的端口、HCCN 和 NPU 网络检查。

## 9. 网络检查通过后：核对软件构建和两侧底层日志

两个容器分别执行，输出包版本：

```bash
python - <<'PY'
from importlib.metadata import distributions, version

for name in ("vllm", "vllm-ascend", "torch", "torch-npu"):
    try:
        print(name, version(name))
    except Exception as exc:
        print(name, type(exc).__name__)
for dist in distributions():
    name = dist.metadata.get("Name", "")
    if "mooncake" in name.lower() or "hixl" in name.lower():
        print(name, dist.version)
PY
```

没有打印 HIXL 包不代表系统没有 HIXL：它也可能以 CANN 动态库形式提供。

两台宿主机分别核对镜像构建：

```bash
docker inspect -f '{{.Config.Image}} {{.Image}}' 容器名
```

若镜像 ID 不同，继续核对实际包和库；相同镜像 ID 也不能代替宿主机驱动/固件及挂载库一致性检查。

保留以下信息：

- 两侧 CANN/HIXL、Mooncake 版本和驱动/固件版本。
- 两侧实际加载的通信库位置；仅 pip 版本相同不能证明动态库相同。
- 当前一次请求的 P/D 应用日志和 CANN 底层日志。
- `MatchEndpoints` 前后的具体原因、时间、PID，以及 `FUNC/FILE/LINE`。

底层日志通常在运行用户的 `$HOME/ascend/log`，实际位置以环境配置为准。在两个容器各执行以下只读查找，显示最近的相关匹配：

```bash
if command -v rg >/dev/null 2>&1; then
  rg -n -i -C 3 'MatchEndpoints|103900|E19999|Failed to connect|endpoint|rank|handshake' \
    "$HOME/ascend/log" 2>/dev/null | tail -n 120
else
  grep -Rni -E 'MatchEndpoints|103900|E19999|Failed to connect|endpoint|rank|handshake' \
    "$HOME/ascend/log" 2>/dev/null | tail -n 120
fi
```

输出为空仅表示该目录未找到匹配，不能判定底层没有错误；检查实际日志路径。保留完整原始文件，终端只需抄报错时刻附近的关键行。

## 10. 修复后复测的通过条件

仅在完成对应修复、服务重新就绪后，按主 README 第 10 步重新发送短 PD 请求，并执行 D 端采样检查。

进入后续 128K/1K 测试前，至少满足：

- 短请求返回有效输出和正常结束信息；不只看 `[DONE]`。
- 本次请求对应的 P/D 日志没有新的建链、KV 传输失败或重算错误。
- D 采样有本次请求对应的非零字节成功 KV read，而非仅存在历史成功记录。
- 8 个 D worker 的 MRV1/容量采样检查通过。
- 后续主 README 的精确输入/输出长度校验通过，才继续 TPOT/BS 筛选。

本轮保留 `10000ms` 超时值作为故障证据。需要调整超时时，应先确定具体超时层、参数及调整依据，不能把延长等待视为网络已修复。

## 11. 最小反馈表

无法复制长日志时，填写这些值即可：

```text
目标IP=P_IP：是（已确认）
P/D网络=host：是（已确认）
当前目标端口：
P端口监听：有 / 无；监听IP：
D到该端口TCP：OK / refused / timed out / 其他
P容器hccn.conf与本机宿主机：一致 / 缺失 / 不一致
D容器hccn.conf与本机宿主机：一致 / 缺失 / 不一致
NPU链路异常卡号：无 / P卡号 / D卡号
NPU0双向跨机ping：成功 / 失败
其他实际传输路径：已测通过 / 有失败 / 尚未测
TLS配置：一致 / 不一致 / 尚未核对
P侧同期关键错误：
D侧具体错误：MatchEndpoints failed timeout:10000ms
```

## 12. 核对依据

- [vLLM-Ascend v0.30.0rc2 官方 PD 部署检查](https://github.com/vllm-project/vllm-ascend/blob/v0.30.0rc2/docs/source/tutorials/features/pd_disaggregation_mooncake_multi_node.md)：A2 HCCN 配置、NPU 网口、跨机 ping 和 TLS 检查。
- [vLLM v0.30.0 IP 选择源码](https://github.com/vllm-project/vllm/blob/v0.30.0/vllm/utils/network_utils.py)：`get_ip()` 优先读取 VLLM_HOST_IP。
- [vLLM-Ascend v0.30.0rc2 Mooncake connector](https://github.com/vllm-project/vllm-ascend/blob/v0.30.0rc2/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_hybrid_connector.py)：connector 握手与传输引擎端口分别处理。
- [Mooncake 官方同错误码案例](https://github.com/kvcache-ai/Mooncake/issues/2300)：说明 103900/E19999 本身不足以唯一定位根因；该案例的同机同设备原因未经证实适用于本次双机场景。

这些链接用于追溯，目标测试机器无需访问外网。本文为当前故障的排查路径，不是已完成修复的记录。
