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
| P→D:20544 | curl 显示 `Connected to` 后等待；用户确认监听进程属于 vLLM |
| D→P 报错端口 | TCP 检查返回 `TCP_OK` |
| NPU0 双向跨机 ping | 尚未测试 |
| 尝试统一恢复 D 补丁 | 报错缺少 `.pd_audit_v030rc2.json`，详见第 13 节 |
| D 当前磁盘源码补丁检查 | STATE=False；RUNNER/CONNECTOR 新旧标记均为 0，probe_refs 和 BACKUPS 均为 `[]`，详见第 13.4 节 |

两个方向的目标端口在检查时已经验证 TCP 可达；这不能证明历史报错时刻也可达，或底层协议协商成功。D 当前检查的两个磁盘源码文件未发现本套采样注入痕迹。仍未确认：两侧容器的 HCCN 配置、跨机 NPU 网络、TLS 配置、实际加载的通信库是否一致，以及运行中 worker 是否与这次源码检查对应同一环境和代码状态。

`Connected to` 后等待不能记成 TCP 不通。通信端口不一定提供 HTTP 响应；curl 不能验证完整通信协议。此前 D→P 的报错路径与 P→D 的检查路径分别记录。

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

若执行第 12/13 节的无补丁对照，该轮只核对通信与请求是否成功，不运行依赖补丁的采样通过检查，也不将该轮作为已完成全部审计的性能成绩。

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

## 12. 判断是否与 D 端 Python 补丁有关

不能先排除补丁影响。对话中提供的 D 端采样代码有以下改动：

| 改动 | 行为和限制 |
|---|---|
| `capacity_probe.py` 的容量快照 | 初始化后调用一次 `torch.npu.synchronize()` 并写文件，改变初始化时序 |
| `step_probe.py` 的 batch 快照 | 增加逐步 CPU 处理和缓冲写盘，可能影响调度时序 |
| `measured_read()` 包装两处 KV read | 将原参数传给原始 `batch_transfer_sync_read()`，在调用结束后记录结果 |

这些补丁没有直接修改目标 IP、端口、端点匹配参数或底层 Connect 实现。但采样开销不能视为完全无影响；`measured_read()` 的日志写入也没有独立异常保护，写盘失败可能影响原异常的传播。

`transfer_executor_base.cpp` 本身属于 Mooncake 的 Ascend 传输实现。它的底层错误和后续 connector/Python 错误可能是同一次故障向上传播，需要按时间、PID 和目标地址关联。行号依赖实际编译版本，不能用最新版源码的同一行号直接对应用户机器。

当前有价值的对照方式：

以下是确有补丁时使用的通用分支。用户最新检查结果已全部为空，当前先按第 13.4 节判断，再执行第 14 节的 NPU 网络检查；不为完成恢复对照而重新安装补丁。

1. 保留失败日志和配置，记录当前带补丁的结果 A。
2. 正常停止 D，确认本轮所有 D worker 退出。
3. 先按第 13 节检查补丁状态；具备可信恢复条件后，才使用对应安装器恢复源码。
4. 保持相同权重、MRV1、TP/MTP、图模式、HBM、网络及 profiler 配置，重启 D，重发同一类短请求，记录结果 B。P 和代理保持相同配置。
5. 若 B 正常，需要再次安装同一补丁并重启 D 复测 A，才能增强补丁相关性的证据。

| 对照结果 | 判断 |
|---|---|
| 无补丁 B 仍报同样错误 | 故障在没有补丁时也能出现，继续检查原生通信链路 |
| 无补丁 B 正常 | 补丁或重启带来的状态变化成为重点嫌疑，单次结果不能区分两者 |
| A 重现、B 正常，再次 A 又重现 | 补丁相关性证据增强，再逐项隔离容量、step 和 read 采样 |

恢复源码后必须重启 worker；修改磁盘文件不会替换已经加载的 Python 对象。仅把采样环境变量设空也不等于框架源码已恢复，不应将两种对照混为一谈。

## 13. restore 报错：缺少安装状态文件

### 13.1 当前用户遇到的错误

```text
FileNotFoundError:
/vllm-workspace/vllm-ascend/vllm_ascend/.pd_audit_v030rc2.json
```

这说明当前恢复程序定位的包目录中没有新版安装状态文件。按本文新版 `audit_patch.py` 的实现，该文件在源码恢复操作之前读取，因此本次错误发生时尚未写回源码。

**它不能证明源码没有补丁，也不能证明源码已经恢复。**前述恢复步骤依赖“使用新版统一安装器成功安装，并保留其状态和备份”的前提，必须先核对。

可能的情况：

- 当时直接手工编辑了框架文件，没有运行新版统一 `--apply`。
- 使用旧安装器，仅产生 `.bench.bak` 或 `.steps.bak`。
- 容器、文件复制或清理过程中丢失了隐藏状态文件。
- 当前 Python 导入的是另一份安装目录。
- 曾经成功恢复；新版恢复成功后会删除状态文件和对应备份，再次恢复会找不到状态文件。

### 13.2 D 容器：只读检查当前源码与备份

使用启动 D 时的 Python 环境执行。下面不会修改框架源码或备份：

```bash
python - <<'PY'
import sys
from pathlib import Path
from importlib.util import find_spec

spec = find_spec("vllm_ascend")
if spec is None or spec.origin is None:
    raise SystemExit("vllm_ascend not found")

root = Path(spec.origin).parent
print("PYTHON:", sys.executable)
print("ROOT:", root)
print("STATE:", (root / ".pd_audit_v030rc2.json").exists())

markers = {
    "new": "ASCEND_BENCH_V030RC2",
    "old_capacity": "ASCEND_BENCH_AUDIT_V1",
    "old_step": "V4_2NODE_STEP_AUDIT",
}
files = {
    "RUNNER": "worker/model_runner_v1.py",
    "CONNECTOR": "distributed/kv_transfer/kv_p2p/mooncake_hybrid_connector.py",
}

for label, relative in files.items():
    path = root / relative
    if not path.is_file():
        print(label, "FILE_MISSING")
        continue
    text = path.read_text(encoding="utf-8")
    counts = {name: text.count(mark) for name, mark in markers.items()}
    refs = [name for name in ("capacity_probe", "step_probe")
            if name in text]
    print(label, counts, "probe_refs=", refs)
    backups = [suffix for suffix in
               (".pd030.bak", ".bench.bak", ".steps.bak")
               if Path(str(path) + suffix).is_file()]
    print(label, "BACKUPS:", backups)
PY
```

检查 `PYTHON`、`ROOT` 是否为实际启动 D 使用的环境。该脚本检查的是当前 Python 找到的磁盘文件；它不读取已经运行的 worker 内存，也不证明正在运行的进程加载了同一份源码。

### 13.3 按结果决定恢复路径

| 检查结果 | 下一步 |
|---|---|
| `new` 命中，存在 `.pd030.bak`，但 STATE=False | 新版补丁可能仍在，安装状态缺失；先核验备份来源、版本及差异，再确定恢复方式 |
| `old_capacity` 或 `old_step` 命中 | 属于旧补丁路径；核对对应备份和旧安装顺序后再恢复，不能直接套新版统一恢复 |
| 所有标记为 0，但 `probe_refs` 非空 | 可能手工改过，需进一步检查实际导入和调用位置 |
| 标记和 `probe_refs` 均为空 | 这两个文件未发现本套补丁痕迹；继续核对当前解释器与实际运行环境 |
| 源码文件缺失，或出现新旧混合标记 | 保留现状，先核对安装目录及源码来源 |

备份存在不等于备份可信；即使没有采样标记，也不能单凭这一点证明它是当前镜像的原始源码。需要核对当时版本、备份来源和实际差异。

此时先不要重新执行 `--apply`，不要新建空 JSON 来绕过错误，也不要删除或覆盖备份。安装器缺少的原始/补丁 SHA256 不能凭空补写。

无法复制长输出时，仅反馈：

```text
PYTHON / ROOT 是否为预期环境：
STATE：True / False
RUNNER：new / old_capacity / old_step 各命中次数；probe_refs：
RUNNER BACKUPS：
CONNECTOR：new / old_capacity / old_step 各命中次数；probe_refs：
CONNECTOR BACKUPS：
```

以上是通用状态识别路径，不提供未经核验的源码覆盖命令。用户本次已经补充完整结果，见下一节。

### 13.4 最新结果：标记、引用和备份全部为空

用户已确认本轮检查结果：

```text
STATE: False
RUNNER: new=0, old_capacity=0, old_step=0
RUNNER probe_refs: []
RUNNER BACKUPS: []
CONNECTOR: new=0, old_capacity=0, old_step=0
CONNECTOR probe_refs: []
CONNECTOR BACKUPS: []
```

当前判断：

- 这两个磁盘源码文件没有发现本套自动补丁的标记或辅助脚本引用，也没有相应备份和新版安装状态文件；当前没有可供这个安装器使用的恢复记录。
- 在运行中 D worker 确实由本次检查的环境启动、且启动后代码未变化的前提下，这套采样函数没有通过上述两个注入点参与推理。仅把辅助 Python 文件放在目录里，不会自动接入这两个调用位置。
- 因此，目前应把“上述采样函数正在执行并导致本次错误”从主要嫌疑中移开；这不是已证明所有框架源码与原镜像一致，也不能代替对进程实际加载环境的核对。
- `start_d.sh` 中的 MRV1、TP、MTP、图模式等启动设置仍需单独核对。源码采样注入与启动设置分别记录。

当前不再重复执行 `restore`，也不通过先 `apply` 再恢复来处理状态文件缺失。保留本次结果作为故障排查证据，下一步优先完成尚未执行的 NPU 网口跨机检查。

同时，这两个采样注入点当前没有接入，不能期待它们产出后续 BS 审计所需的记录。先完成通信故障排查，再安排采样安装、实际生效检查及性能测试；当前轮次不填写已通过采样审计的性能成绩。

## 14. 当前下一步：NPU0 跨机双向检查

目前主机目标端口 TCP 可达，NPU 跨机连通尚未测试。保持当前服务状态，在宿主机执行以下只读检查，不修改设备 IP、TLS 或其他通信配置。

### 14.1 P、D 宿主机分别查询 NPU0

```bash
/usr/local/Ascend/driver/tools/hccn_tool -i 0 -ip -g
/usr/local/Ascend/driver/tools/hccn_tool -i 0 -link -g
```

分别记录本机 NPU0 网口 IP 和链路状态。后续目标填写对端查询到的 NPU 网口 IP。

### 14.2 D 宿主机测试到 P 的 NPU0

```bash
/usr/local/Ascend/driver/tools/hccn_tool \
  -i 0 -ping -g address 填写P机器NPU0的IP
```

### 14.3 P 宿主机测试到 D 的 NPU0

```bash
/usr/local/Ascend/driver/tools/hccn_tool \
  -i 0 -ping -g address 填写D机器NPU0的IP
```

这里使用 NPU 网口 IP，不用主机通信 P_IP/D_IP 代替。记录结果：

```text
P NPU0 link：
D NPU0 link：
D NPU0 → P NPU0：成功 / 失败
P NPU0 → D NPU0：成功 / 失败
```

| 结果 | 后续处理 |
|---|---|
| 链路异常，或至少一个方向 ping 失败 | 记录机器、卡号和失败信息，让资源提供方核对两台 A2 的 NPU RoCE 网络、设备 IP、路由及交换机配置 |
| 两个方向均成功 | 继续覆盖实际传输涉及的其他卡对，并按第 4、7、9 节核对 HCCN 配置、TLS 和通信库版本及日志 |

一对 NPU ping 成功不能证明 TP8 的全部路径或完整 KV 传输正常。两台机器能通过 SSH/HTTP 互访，也不能代替 NPU 网络互通检查。故障仍未确认修复。

## 15. 核对依据

- [vLLM-Ascend v0.30.0rc2 官方 PD 部署检查](https://github.com/vllm-project/vllm-ascend/blob/v0.30.0rc2/docs/source/tutorials/features/pd_disaggregation_mooncake_multi_node.md)：A2 HCCN 配置、NPU 网口、跨机 ping 和 TLS 检查。
- [vLLM v0.30.0 IP 选择源码](https://github.com/vllm-project/vllm/blob/v0.30.0/vllm/utils/network_utils.py)：`get_ip()` 优先读取 VLLM_HOST_IP。
- [vLLM-Ascend v0.30.0rc2 Mooncake connector](https://github.com/vllm-project/vllm-ascend/blob/v0.30.0rc2/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_hybrid_connector.py)：connector 握手与传输引擎端口分别处理。
- [Mooncake 官方同错误码案例](https://github.com/kvcache-ai/Mooncake/issues/2300)：说明 103900/E19999 本身不足以唯一定位根因；该案例的同机同设备原因未经证实适用于本次双机场景。

这些链接用于追溯，目标测试机器无需访问外网。本文为当前故障的排查路径，不是已完成修复的记录。
