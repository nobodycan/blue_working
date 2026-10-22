# A2 HCCN Setup：一台已有 Device IP，另一台未配置

更新：2026-10-22。适用于两台各 8 张 NPU 的昇腾 A2，在**宿主机 root 的 Bash 终端**配置 IPv4，再验收跨机 RoCE。目标机器不需要 Git、pip 或下载脚本；本文使用驱动自带 `hccn_tool`、Bash 和 Python 3 标准库。

本文统一称呼：

| 名称 | 含义 |
|---|---|
| REF | 已经有 NPU Device IP 的机器，作为查询和测试的对端 |
| NEW | NPU IPv4 查询提示 `no ip was preset there`、需要配置的机器 |
| Device IP | `hccn_tool -i 卡号 -ip -g` 查到的 NPU 网口地址 |
| Host IP | SSH、HTTP、vLLM API 使用的宿主机地址，如原来的 P_IP、D_IP |

若你的 P 已配置、D 未配置，则 REF=P、NEW=D；若相反，按实际角色执行。**Device IP 与原来启动 vLLM 的 P_IP / D_IP 分别记录，不把后者替换成 Device IP。**

执行顺序：采集两台信息 → 确认 NEW 的地址分配及接线 → 备份 → 只配 NEW 的 NPU0 → 双向 ping → 配 NEW 其余卡 → 覆盖所需卡对 → 原厂 RoCE READ/SEND 打流 → 回到 PD 短请求。

## 1. 先明确哪些信息可以参考、哪些必须重新分配

**REF 有 IP，只能帮助核对网段和接线；不能据此判断哪些地址空闲。**

| 项目 | 如何确定 |
|---|---|
| REF 每卡 IP、掩码 | 在 REF 用下面第 2 步读取 |
| NEW 每卡 IP | 从资源方/网络管理员取得明确分配；16 张卡的地址均须唯一 |
| NEW 每卡掩码 | 使用该端口实际所属网段的规划；同一二层网段可参考 REF 对应卡 |
| NEW 网关 | 按 NEW 的 NPU 网络规划填写；同网段直接互通通常无需新增默认网关 |
| 交换机端口、VLAN、网络平面 | 核对两台实际接线及交换机配置，不能只看地址前缀 |
| MTU、PFC、DSCP/TC、TLS | 核对两端及交换机的现行部署要求，首次配 IP 时不随意修改 |

不要复制 REF 的 IP，也不要按“加 8、加 100、换最后一位”直接设置；ping 不回包也不能证明地址无人使用。不要整份复制 REF 的 `/etc/hccn.conf` 到 NEW。

如果资源方尚未分配 NEW 地址，先完成第 2～3 步，将下面内容发给资源方，收到分配后再执行第 4 步：

```text
现有两台昇腾 A2，拟运行双机 PD，8 NPU/台。
REF 已有 NPU IP；NEW 各卡查询提示 no ip was preset there。
请确认/提供：
1. NEW 的 8 个 NPU 端口是否已接入可跨机通信的 RoCE 网络。
2. NEW 每卡 Device IPv4、掩码、必要的网关，以及对应交换机端口/VLAN/网络平面。
3. 这些 IP 已为本次资源分配保留，且与 REF 和其他机器无冲突。
4. REF/NEW 哪些卡对允许互通；本次 Mooncake TP8 的实际路径是否全部可达。
5. NPU 与交换机 MTU、PFC、DSCP/TC、TLS 的部署要求。
附：两台第 2 步输出，尤其每卡 IP、link、LLDP、MAC。
```

## 2. 两台宿主机：各采集一次原始信息

分别 SSH 到两台**宿主机**，进入 root Bash；不要在模型容器里完成首次配置。

```bash
sudo -i
bash
id -u
```

`id -u` 必须输出 `0`。下面 REF 填 `ROLE=REF`，NEW 填 `ROLE=NEW`；其余命令相同：

```bash
export ROLE=REF
export HCCN=/usr/local/Ascend/driver/tools/hccn_tool
export OUT="/root/hccn-${ROLE}-$(date +%Y%m%d-%H%M%S)-$$"
test -x "$HCCN" || { echo "找不到驱动自带 hccn_tool，停止"; exit 1; }
mkdir -m 700 "$OUT"
printf '%s\n' "$OUT"

hostname > "$OUT/hostname.txt"
date -Is > "$OUT/time.txt"
npu-smi info > "$OUT/npu-smi.txt" 2>&1
cat "$OUT/npu-smi.txt"
ip -br addr > "$OUT/host-ip.txt"
ip route > "$OUT/host-route.txt"
if test -r /usr/local/Ascend/driver/version.info; then
  cp -a /usr/local/Ascend/driver/version.info "$OUT/driver-version.info"
fi
if test -r /etc/hccn.conf; then
  cp -a /etc/hccn.conf "$OUT/hccn.conf.before"
else
  printf '%s\n' '配置前 /etc/hccn.conf 不存在或不可读' > "$OUT/hccn.conf.before.absent"
fi

for i in 0 1 2 3 4 5 6 7; do
  for item in ip gateway route link optical lldp mac mtu speed netdetect net_health tls; do
    "$HCCN" -i "$i" "-$item" -g > "$OUT/npu${i}-${item}.txt" 2>&1
    rc=$?
    printf 'NPU=%s QUERY=%s RC=%s\n' "$i" "$item" "$rc" >> "$OUT/query-status.txt"
  done
done

for i in 0 1 2 3 4 5 6 7; do
  printf '\n===== NPU %s =====\n' "$i"
  cat "$OUT/npu${i}-ip.txt" "$OUT/npu${i}-gateway.txt" "$OUT/npu${i}-link.txt"
done
```

保留终端打印的 `$OUT` 路径。上述循环故意保留失败信息，不因某一查询不受支持而丢掉后续证据。先看 `npu-smi info` 的实际设备数量和编号；如果不是 0～7，后续列表必须按本机实际设备调整，本指南的八卡批量块不可直接套用。

### 2.1 在 REF 重点抄下这些值

```bash
cat "$OUT/npu0-ip.txt"
cat "$OUT/npu0-gateway.txt"
cat "$OUT/npu0-lldp.txt"
cat "$OUT/npu0-mtu.txt"
cat "$OUT/npu0-tls.txt"
```

然后对卡 1～7 查看相应文件，不要假定八张卡都在同一子网。IP 查询中的 `ipaddr` / `netmask` 才是需要记录的 Device IP 和掩码。网关以 `-gateway -g` 的结果及网络规划为准。[官方 IP/掩码查询](https://www.hiascend.com/doc_center/source/zh/HDK/2610/A2/A2HCCN/hccntoola2_008.html)、[网关查询](https://www.hiascend.com/doc_center/source/zh/HDK/2610/A2/A2HCCN/hccntoola2_010.html)。

### 2.2 在 NEW 重点检查链路和交换机邻居

```bash
cat "$OUT/npu0-link.txt"
cat "$OUT/npu0-optical.txt"
cat "$OUT/npu0-lldp.txt"
cat "$OUT/npu0-mac.txt"
cat "$OUT/npu0-route.txt"
```

逐卡核对：

| 结果 | 处理 |
|---|---|
| NEW 无 IP，但 link UP | 可以继续准备地址配置，仍需确认 VLAN/地址分配 |
| link DOWN | 先核对端口、光模块/线缆、交换机端口和驱动状态；填 IP 不能代替接线 |
| LLDP 有 System Name / Port ID | 记录交换机名称和接口，交给网络管理员核对 VLAN/平面 |
| LLDP 无结果 | 可能未启用或查询受限；向管理员核对接线，不能据此直接断言没接线 |
| optical 没有信息 | 结合 DAC/光模块类型和实际型号判断，不能单凭这一项判定不支持 RoCE |

**LLDP 的 `Management Address` 是邻居的管理地址，不能直接当作 NPU 网关。**LLDP 展示的 PVID/VLAN 信息是排查线索，仍需核对交换机端口实际配置。官方还说明 link DOWN 时无法查询对端 LLDP。[官方 LLDP 说明](https://www.hiascend.com/doc_center/source/zh/HDK/2610/A2/A2HCCN/hccntoola2_014.html)。

## 3. 选择适用的网络路径，填写地址表

### 3.1 两端对应卡在同一个二层网络 / VLAN

使用同一实际子网中的不同地址和正确掩码；通常可先验证直接互通，不需要为了测试人为添加默认网关。**同一 IP 前缀本身不能证明两端属于同一 VLAN。**

如果八张卡属于多个独立网络平面，每卡按自己平面的规划填写；不要把所有卡强行放进同一个网段。

### 3.2 两端通过三层路由跨子网互通

需要 NEW 和 REF 各自到对端的有效路由，以及网络侧对 RoCE 的支持。NEW 使用本端网关，REF 必须已有返回路径。第 5 步提供 NEW 的默认网关设置命令；若平台要求策略路由、专用静态路由或交换机修改，先取得管理员提供的具体配置，再继续，不通过猜网关或扩大掩码规避路由。

### 3.3 填写这张表

| NEW 卡号 | NEW 分配的 IP | NEW 掩码 | 对应 REF 卡号 | REF 现有 IP | REF 掩码 | NEW 网关，无则 `-` | NEW 检测目标，无则 `-` |
|---|---|---|---|---|---|---|---|
| 0 | 待分配 | 待确认 | 0，按接线改 | 实际值 | 实际值 | 待确认 | 待确认 |
| 1 | 待分配 | 待确认 | 1，按接线改 | 实际值 | 实际值 | 待确认 | 待确认 |
| 2 | 待分配 | 待确认 | 2，按接线改 | 实际值 | 实际值 | 待确认 | 待确认 |
| 3 | 待分配 | 待确认 | 3，按接线改 | 实际值 | 实际值 | 待确认 | 待确认 |
| 4 | 待分配 | 待确认 | 4，按接线改 | 实际值 | 实际值 | 待确认 | 待确认 |
| 5 | 待分配 | 待确认 | 5，按接线改 | 实际值 | 实际值 | 待确认 | 待确认 |
| 6 | 待分配 | 待确认 | 6，按接线改 | 实际值 | 实际值 | 待确认 | 待确认 |
| 7 | 待分配 | 待确认 | 7，按接线改 | 实际值 | 实际值 | 待确认 | 待确认 |

同号只是填表起点，对端卡号必须按实际连通关系确定。检测目标应是从 NEW 该 NPU 可达、允许探测的地址，如规划的网关；它不是必填的“第二个网关”。

## 4. NEW 宿主机：停止本机 NPU 任务并保存地址计划

正常停止 NEW 上使用这些 NPU 的模型/压测任务。REF 后续做原厂打流前，也要停止使用测试卡的任务。使用原启动终端 Ctrl+C 或已有服务管理方式；不要执行 `pkill python` / `killall`。

```bash
npu-smi info
ps -eo user,pid,ppid,args | grep -E '[v]llm|[r]oce_test|[b]ench_client'
```

确认没有需要保留的任务正在使用待配置/测试卡。继续使用 NEW 第 2 步的 root Bash；若换了终端，先恢复变量，`OUT` 填 **NEW 实际的采集目录**：

```bash
export HCCN=/usr/local/Ascend/driver/tools/hccn_tool
export OUT=''  # 填 NEW 在第 2 步打印的实际目录
: "${OUT:?请填写 NEW 的采集目录}"
test -d "$OUT" || { echo "目录不存在，停止"; exit 1; }
```

下面命令创建地址计划。**先编辑代码块中各个空字符串，填好再运行**；每个数组都要有 8 项，按 NEW 卡 0～7 排列。`PEER_IDS` 表示 NEW 卡 i 对应的 REF 卡号。地址为空时后续检查会拒绝配置。

```bash
test ! -e "$OUT/new-plan.env" || { echo "计划文件已存在，先查看，不覆盖"; exit 1; }
cat > "$OUT/new-plan.env" <<'PLAN_EOF'
HCCN=/usr/local/Ascend/driver/tools/hccn_tool

# L2：每行对应两端同一二层子网；ROUTED：管理员已确认跨子网路由。
TOPOLOGY=''

NEW_IPS=('' '' '' '' '' '' '' '')
NEW_MASKS=('' '' '' '' '' '' '' '')
PEER_IDS=(0 1 2 3 4 5 6 7)
PEER_IPS=('' '' '' '' '' '' '' '')
PEER_MASKS=('' '' '' '' '' '' '' '')

# 没有新增默认网关/检测对象的卡填 -；有则填规划中的实际地址。
GATEWAYS=('-' '-' '-' '-' '-' '-' '-' '-')
DETECT_IPS=('-' '-' '-' '-' '-' '-' '-' '-')
PLAN_EOF
chmod 600 "$OUT/new-plan.env"
cat "$OUT/new-plan.env"
```

如果已创建但需要改值，可直接编辑：

```bash
vi "$OUT/new-plan.env"
```

### 4.1 仅检查计划格式，不写入 NPU

```bash
source "$OUT/new-plan.env"
for arr in NEW_IPS NEW_MASKS PEER_IDS PEER_IPS PEER_MASKS GATEWAYS DETECT_IPS; do
  declare -n vals="$arr"
  test "${#vals[@]}" -eq 8 || { echo "$arr 必须有 8 项"; exit 1; }
done
unset -n vals

python3 - "$TOPOLOGY" "${NEW_IPS[@]}" "${NEW_MASKS[@]}" \
  "${PEER_IDS[@]}" "${PEER_IPS[@]}" "${PEER_MASKS[@]}" \
  "${GATEWAYS[@]}" "${DETECT_IPS[@]}" <<'PY'
import ipaddress as ip
import sys

a = sys.argv[1:]
if len(a) != 57 or a[0] not in ('L2', 'ROUTED'):
    raise SystemExit('停止：拓扑须填 L2 或 ROUTED，且每组为 8 项')
mode = a[0]
groups = [a[1 + 8*k:1 + 8*(k+1)] for k in range(7)]
new, masks, ids, peer, pmasks, gateways, detects = groups
if sorted(ids) != [str(i) for i in range(8)]:
    raise SystemExit('停止：PEER_IDS 必须为 REF 实际卡号 0～7 的排列')
reserved = [ip.IPv4Network('192.168.1.0/24'), ip.IPv4Network('192.168.2.0/24')]
seen = set()
def interface(addr, mask):
    # 要求显式点分十进制掩码，避免空值被当成其他形式解析。
    if len(mask.split('.')) != 4:
        raise ValueError('掩码必须为点分十进制，如实际分配的 255.x.x.x')
    x = ip.IPv4Interface(addr + '/' + mask)
    if x.network.prefixlen > 30:
        raise ValueError('本流程不自动配置 /31、/32，交由网络管理员处理')
    if (x.ip.is_multicast or x.ip.is_unspecified or x.ip.is_loopback
            or x.ip == x.network.network_address or x.ip == x.network.broadcast_address
            or any(x.ip in r for r in reserved)):
        raise ValueError('存在不适用的设备地址：' + str(x.ip))
    if x.ip in seen:
        raise ValueError('16 个设备地址之间存在重复：' + str(x.ip))
    seen.add(x.ip)
    return x

try:
    local = [interface(new[i], masks[i]) for i in range(8)]
    remote = [interface(peer[i], pmasks[i]) for i in range(8)]
    for i, (n, p) in enumerate(zip(local, remote)):
        if mode == 'L2' and n.network != p.network:
            raise ValueError('L2 对应卡必须属于相同子网/掩码，NEW 卡 ' + str(i))
        if gateways[i] != '-':
            g = ip.IPv4Address(gateways[i])
            if (g not in n.network or g in seen
                    or g in (n.network.network_address, n.network.broadcast_address)):
                raise ValueError('网关须为本卡子网有效地址且不得等于设备地址，卡 ' + str(i))
        if detects[i] != '-':
            d = ip.IPv4Address(detects[i])
            if d.is_multicast or d.is_unspecified or d.is_loopback or d in {x.ip for x in local}:
                raise ValueError('检测目标无效或误填本机设备地址，卡 ' + str(i))
        print('NEW NPU%d %s <-> REF NPU%s %s gateway=%s detect=%s'
              % (i, n, ids[i], p, gateways[i], detects[i]))
except ValueError as e:
    raise SystemExit('停止：' + str(e))
print('FORMAT_OK：只证明填写格式和表内一致性；不证明地址空闲、VLAN、路由或 RoCE 已通。')
PY
```

必须看到最后一行 `FORMAT_OK`，并核对打印的每一行与资源方分配一致。此检查不探测全网地址冲突，也不验证 REF 当前配置；REF 地址需对照第 2 步原始输出。

## 5. NEW 宿主机：先只配置一张卡

### 5.1 确认 NPU0 确实未配置

```bash
"$HCCN" -i 0 -ip -g
"$HCCN" -i 0 -link -g
```

确认 IP 查询仍为 `no ip was preset there`，且卡号属于本次 NEW。若已经有有效地址，先读回核对，不继续覆盖。

### 5.2 写入 IP，按计划设置可选项

下面定义一个仅用于**首次配置无 IPv4 的卡**的函数，直接复制完整代码块。命令返回失败时立即停止该卡，保留日志；不会自动修改 REF。

```bash
source "$OUT/new-plan.env"
configure_new_card() (
  set -euo pipefail
  i="$1"
  : "${NEW_IPS[$i]:?缺少 NEW IP}"
  : "${NEW_MASKS[$i]:?缺少 NEW 掩码}"
  before="$("$HCCN" -i "$i" -ip -g 2>&1 || true)"
  printf '%s\n' "$before"
  if ! printf '%s\n' "$before" | grep -qi 'no ip was preset there'; then
    echo "停止：NEW 卡 $i 不是已确认的无 IPv4 状态，请人工核对"
    exit 1
  fi

  "$HCCN" -i "$i" -ip -s address "${NEW_IPS[$i]}" netmask "${NEW_MASKS[$i]}"
  if test "${GATEWAYS[$i]}" != '-'; then
    "$HCCN" -i "$i" -gateway -s gateway "${GATEWAYS[$i]}"
  fi
  if test "${DETECT_IPS[$i]}" != '-'; then
    "$HCCN" -i "$i" -netdetect -s address "${DETECT_IPS[$i]}"
  fi

  # 首次设置可能在约 15 秒后发生短暂 link 抖动，先等待再检查。
  sleep 30
  for attempt in 1 2 3 4 5 6; do
    date -Is
    "$HCCN" -i "$i" -link -g
    sleep 5
  done
  "$HCCN" -i "$i" -ip -g
  "$HCCN" -i "$i" -route -g
  if test "${GATEWAYS[$i]}" != '-'; then "$HCCN" -i "$i" -gateway -g; fi
  if test "${DETECT_IPS[$i]}" != '-'; then
    "$HCCN" -i "$i" -netdetect -g
    "$HCCN" -i "$i" -net_health -g
  fi
)

set -o pipefail
configure_new_card 0 2>&1 | tee "$OUT/configure-npu0-$(date +%H%M%S).log"
rc=${PIPESTATUS[0]}
printf '配置流程返回码=%s\n' "$rc"
```

**返回码为 0 只表示本块命令执行完毕。**检查最后 IP/掩码与计划一致、六次 link 检查均为 UP，再进入第 6 步。若 link 抖动未结束或状态未知，先停止后续配置并检查原因，不自动执行 link down/up。

这里实际调用的原厂设置命令是：

```bash
# 以下是语法提示，不要原样执行占位符：
# hccn_tool -i 卡号 -ip -s address 本卡DeviceIP netmask 本卡掩码
# hccn_tool -i 卡号 -gateway -s gateway 本卡实际网关
# hccn_tool -i 卡号 -netdetect -s address 实际检测目标
```

注意：A2 官方文档将 `192.168.1.x`、`192.168.2.x` 标为板内 vNIC 使用的网段，不用于本流程配置；还说明设置新 IP 可能删除该卡原有的异网段网关/路由。首次配置可能短暂发生 link UP→DOWN→UP。[官方 IP 设置说明](https://www.hiascend.com/doc_center/source/zh/HDK/2610/A2/A2HCCN/hccntoola2_097.html)。网关和检测对象分别使用独立命令，检测对象配合网络健康检查使用。[网关设置](https://www.hiascend.com/doc_center/source/zh/HDK/2610/A2/A2HCCN/hccntoola2_099.html)、[检测对象设置](https://www.hiascend.com/doc_center/source/zh/HDK/2610/A2/A2HCCN/hccntoola2_101.html)。

若 IP 设置成功、后续网关或检测对象设置失败，该卡已经部分配置，**不要重新运行整个函数**；先读回三个属性，仅修复失败项。例如只修复卡 0 的已确认网关：

```bash
source "$OUT/new-plan.env"
"$HCCN" -i 0 -ip -g
"$HCCN" -i 0 -gateway -g
test "${GATEWAYS[0]}" != '-' || { echo "本卡没有规划网关，停止"; exit 1; }
"$HCCN" -i 0 -gateway -s gateway "${GATEWAYS[0]}"
"$HCCN" -i 0 -gateway -g
```

## 6. 两台宿主机：NPU0 对应卡双向 ping

### 6.1 NEW → REF

在 NEW，沿用本终端的地址数组：

```bash
"$HCCN" -i 0 -ping -g address "${PEER_IPS[0]}" pkt 128
```

### 6.2 REF → NEW

在 REF 填写 NEW 卡 0 对应的 REF 卡号，以及刚设置的 NEW 卡 0 地址：

```bash
HCCN=/usr/local/Ascend/driver/tools/hccn_tool
REF_CARD=''     # 填 NEW 卡 0 对应的 REF 卡号，例如实际是 0 才填 0
NEW_NPU0_IP=''  # 填刚设置并读回的 NEW 卡 0 Device IP
: "${REF_CARD:?请填写 REF 对应卡号}"
: "${NEW_NPU0_IP:?请填写 NEW 卡 0 Device IP}"
"$HCCN" -i "$REF_CARD" -ping -g address "$NEW_NPU0_IP" pkt 128
```

两端均须实际收到回包、丢包率为 0。不要以命令返回码、Host 普通 `ping` 或 HTTP `/metrics` 成功替代 Device ping。使用 `pkt 128` 避免默认大报文干扰初步连通检查。[官方 Device ping](https://www.hiascend.com/doc_center/source/zh/HDK/2610/A2/A2HCCN/hccntoola2_024.html)。

若失败：

```bash
# 在失败源端执行；CARD 填实际源卡号。
CARD=0
"$HCCN" -i "$CARD" -ip -g
"$HCCN" -i "$CARD" -link -g
"$HCCN" -i "$CARD" -route -g
"$HCCN" -i "$CARD" -gateway -g
"$HCCN" -i "$CARD" -lldp -g
tail -n 100 /var/log/hccn_tool/hccn.log
tail -n 100 /var/log/hccn_tool/hccn_config.log
```

核对源/目标 Device IP、掩码、交换机 VLAN/路由和 ICMP 策略。此时先不要配置其余七张卡，不自动关闭 TLS、防火墙或重置所有 NPU。

## 7. NEW 宿主机：逐张配置其余卡并验收

**仅在卡 0 双向 ping 已通过后执行。**仍在定义过 `configure_new_card` 的 NEW 终端中，每次执行一张卡，例如卡 1：

```bash
configure_new_card 1 2>&1 | tee "$OUT/configure-npu1-$(date +%H%M%S).log"
rc=${PIPESTATUS[0]}
printf '配置流程返回码=%s\n' "$rc"
"$HCCN" -i 1 -ping -g address "${PEER_IPS[1]}" pkt 128
```

读回 IP/掩码、确认 link 稳定 UP、NEW→REF 收到回包；随后在 REF 用对应 REF 卡号 ping NEW 卡 1。按第 6 步同样方法完成反向检查。

通过后，将上面块中的卡号 `1` 和日志文件名逐次改成 `2`、`3`、`4`、`5`、`6`、`7`，逐卡执行和双向验收。不要改数组顺序，也不要将 REF 同号卡当作默认对端。

### 7.1 两台读取最终状态

两端各执行：

```bash
HCCN=/usr/local/Ascend/driver/tools/hccn_tool
for i in 0 1 2 3 4 5 6 7; do
  printf '\n===== NPU %s =====\n' "$i"
  "$HCCN" -i "$i" -ip -g
  "$HCCN" -i "$i" -link -g
  "$HCCN" -i "$i" -mtu -g
  "$HCCN" -i "$i" -tls -g
done
```

核对 16 个 Device IP 唯一，NEW 的地址全部符合分配；两端 TLS 满足当前传输库要求，MTU 与网络规划匹配。读取异常时记录原始结果，不把空输出解释为 TLS 已关闭。

### 7.2 覆盖实际需要的全部卡对

八组对应卡 ping 通过，不代表所有跨卡传输路径都通过。若部署要求两台 8×8 任意 NPU 互通，两端都应逐源卡测试全部八个目标地址；若平台采用隔离网络平面，按实际支持的路径验收，并确认 Mooncake 的设备选择符合该拓扑。

下列块在每台宿主机各运行一次，`TARGET_IPS` 填**对端真实 NPU0～7 地址**。保留原始回包输出并逐项核对；此块不自动打印“全部通过”。

```bash
HCCN=/usr/local/Ascend/driver/tools/hccn_tool
TARGET_IPS=('' '' '' '' '' '' '' '')
for value in "${TARGET_IPS[@]}"; do
  test -n "$value" || { echo "请填全对端 8 个 Device IP"; exit 1; }
done
PING_OUT="/root/hccn-ping-$(date +%Y%m%d-%H%M%S)-$$"
mkdir -m 700 "$PING_OUT"
for src in 0 1 2 3 4 5 6 7; do
  for dst in 0 1 2 3 4 5 6 7; do
    printf 'NPU%s -> 对端NPU%s (%s)\n' "$src" "$dst" "${TARGET_IPS[$dst]}"
    "$HCCN" -i "$src" -ping -g address "${TARGET_IPS[$dst]}" pkt 128 \
      > "$PING_OUT/npu${src}-to-peer${dst}.txt" 2>&1
    rc=$?
    printf 'src=%s dst=%s rc=%s\n' "$src" "$dst" "$rc" >> "$PING_OUT/command-status.txt"
    cat "$PING_OUT/npu${src}-to-peer${dst}.txt"
  done
done
printf '原始记录目录：%s\n' "$PING_OUT"
```

如果只要求分平面路径，先按规划缩小 `src`、`dst` 列表，分平面分别执行；未经要求的跨平面失败不直接判硬件故障。已有独立 `hccn_check.py` 也可采集和验收，本文所有必要原厂命令已给全，不依赖该文件。

## 8. 两台宿主机：原厂 RoCE 实际打流

先做一对已通过双向 ping 的卡，随后覆盖 PD 实际需要的卡对。打流前确认两端测试卡无业务占用，且机型要求的 PowerMode 为默认非节能配置。两端 operation、消息长度、次数、端口和网络优先级参数必须相同。

下面用 NEW 卡 0 与它对应的 REF 卡测试；打开两个宿主机终端，**server 先执行并保持等待，再运行 client**。

### 8.1 READ：NEW 发起，读取 REF

如果 NEW=D、REF=P，此方向对应 D 从 P 读取数据的基础链路测试。

**REF 先执行：**

```bash
set -o pipefail
HCCN=/usr/local/Ascend/driver/tools/hccn_tool
REF_CARD=''  # 填与 NEW 卡 0 对应的实际 REF 卡号
: "${REF_CARD:?请填写 REF 卡号}"
BW_OUT="/root/hccn-read-server-$(date +%Y%m%d-%H%M%S)-$$.log"
"$HCCN" -i "$REF_CARD" -roce_test ib_read_bw \
  -s 1048576 -n 1000 -p 18515 -tcp 2>&1 | tee "$BW_OUT"
rc=${PIPESTATUS[0]}
printf '原厂返回码=%s 日志=%s\n' "$rc" "$BW_OUT"
```

**REF 正常进入等待后，NEW 执行：**

```bash
set -o pipefail
source "$OUT/new-plan.env"
BW_OUT="$OUT/read-client-$(date +%H%M%S).log"
"$HCCN" -i 0 -roce_test ib_read_bw \
  -s 1048576 -n 1000 -p 18515 address "${PEER_IPS[0]}" -tcp \
  2>&1 | tee "$BW_OUT"
rc=${PIPESTATUS[0]}
printf '原厂返回码=%s 日志=%s\n' "$rc" "$BW_OUT"
```

两边结束并保存结果后再进行下一轮。交换两台 server/client 角色可测反向 READ；卡号和 `address` 始终分别使用本地源卡号与对端 server 的 Device IP。

### 8.2 SEND：REF 发，NEW 收

**NEW 先执行：**

```bash
set -o pipefail
HCCN=/usr/local/Ascend/driver/tools/hccn_tool
"$HCCN" -i 0 -roce_test ib_send_bw \
  -s 1048576 -n 1000 -p 18515 -tcp \
  2>&1 | tee "/root/hccn-send-server-$(date +%Y%m%d-%H%M%S)-$$.log"
rc=${PIPESTATUS[0]}
printf '原厂返回码=%s\n' "$rc"
```

**NEW 正常进入等待后，REF 执行：**

```bash
set -o pipefail
HCCN=/usr/local/Ascend/driver/tools/hccn_tool
REF_CARD=''
NEW_NPU0_IP=''
: "${REF_CARD:?请填写 REF 对应卡号}"
: "${NEW_NPU0_IP:?请填写 NEW 卡 0 Device IP}"
"$HCCN" -i "$REF_CARD" -roce_test ib_send_bw \
  -s 1048576 -n 1000 -p 18515 address "$NEW_NPU0_IP" -tcp \
  2>&1 | tee "/root/hccn-send-client-$(date +%Y%m%d-%H%M%S)-$$.log"
rc=${PIPESTATUS[0]}
printf '原厂返回码=%s\n' "$rc"
```

本节省略 `-tclass`，使用原厂测试默认策略；**若网络要求指定 traffic class，所有两端命令均按规划增加相同的 `-tclass 数值`，不能盲目套用示例中的 132。**`-tcp` 指建链方式，实际数据测试由 `ib_read_bw` / `ib_send_bw` 指定，不是 Host TCP 吞吐测试。

合格证据：两端测试匹配、命令正常结束，原厂输出含本次消息大小/迭代次数对应的带宽表，`BW average` 为正并保留单位。等待连接、打印 `roce_report` 或返回码 0 本身不够。初步正带宽仅证明所选卡对能传输；达到额定性能、全部卡对可用和 Mooncake 可用分别验收。[官方 RoCE 测试命令及使用要求](https://www.hiascend.com/doc_center/source/zh/HDK/2610/A2/A2HCCN/hccntoola2_054.html)。

若强制中断或异常超时，device 侧测试可能仍在。确认选中卡上只有自己本轮的 `roce_test` 后，才在对应宿主机清理，例如卡 0：

```bash
# 会终止该卡上的全部 roce_test；卡号和测试归属确认后才能执行。
/usr/local/Ascend/driver/tools/hccn_tool -i 0 -roce_test reset
```

该命令不用于修复 IP/VLAN，也不要在正常轮次之间自动批量执行。

## 9. NEW 宿主机：核对配置文件和容器可见性

### 9.1 核对持久配置记录

```bash
test -r /etc/hccn.conf || { echo "配置文件不可读，停止验收"; exit 1; }
cat /etc/hccn.conf
cp -a /etc/hccn.conf "$OUT/hccn.conf.after"
if test -f "$OUT/hccn.conf.before"; then
  diff -u "$OUT/hccn.conf.before" "$OUT/hccn.conf.after" || true
fi
```

检查 NEW 的每卡 IP/掩码等记录符合实际设置；文件缺记录时查看 `/var/log/hccn_tool/hccn_config.log`，不要只手工补文件就宣称生效。当前设备状态始终用 `hccn_tool` 读回验证。

官方另有 `hccn_tool -i 卡号 -cfg recovery`，用途是按保存的配置恢复设备设置；它不是“清空本次修改”命令，不在本流程自动执行。原先文件为空或缺失时，仅拷回旧文件不等于已移除 device 侧新 IP。[官方配置恢复说明](https://www.hiascend.com/doc_center/source/zh/HDK/2610/A2/A2HCCN/hccntoola2_102.html)。

### 9.2 两台宿主机分别检查本机模型容器

填各自容器名：

```bash
C=''
: "${C:?请填写本机模型容器名}"
docker inspect -f '{{.HostConfig.NetworkMode}}' "$C"
docker inspect -f '{{range .Mounts}}{{println .Source "->" .Destination "RW=" .RW}}{{end}}' "$C"
sha256sum /etc/hccn.conf
docker exec "$C" sh -c 'test -r /etc/hccn.conf && sha256sum /etc/hccn.conf'
```

仍应是本次部署使用的 `host` 网络；容器中的 `/etc/hccn.conf` 应可读且与**本宿主机**一致。文件 hash 对不上或不可读，检查容器挂载，不复制另一台的配置。正确挂载项通常是：

```bash
# 加在完整的 docker run 创建命令中；这一行不是独立执行命令。
# -v /etc/hccn.conf:/etc/hccn.conf:ro
```

已有容器无法通过 `docker restart` 增加新 bind mount。若确实缺挂载，需要在保留原模型目录、设备、镜像、网络和其他参数的完整创建配置中补上，再重建该模型容器。不要仅凭这一行另建一个缺设备的容器。

配置更新后，正常重新启动 P/D 模型进程及代理，使其重新读取本机设备信息；沿用已经核对过的模型启动参数。不要将原 `P_IP` / `D_IP`、`HCCL_IF_IP` 等 Host 地址统一改成 NPU Device IP。

## 10. 回到 PD：先做短请求，再恢复性能测试

模型及 P 端代理重新就绪后，在 **P 容器**执行。以下沿用原部署的代理端口 1999、服务名 `dsv4`；若你改过则填实际值：

```bash
curl --noproxy '*' -fsS http://127.0.0.1:1999/healthcheck
curl --noproxy '*' -N --fail-with-body --max-time 300 \
  http://127.0.0.1:1999/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"dsv4","prompt":"Explain CPU caches.","max_tokens":64,"min_tokens":64,"ignore_eos":true,"temperature":0,"stream":true,"stream_options":{"include_usage":true}}'
```

同时检查这次请求对应时间段的 P、D 和代理日志：

- 输出正常，最终 usage 的 `completion_tokens=64`。
- D 没有新增 `MatchEndpoints failed`、`103900`、`E19999` 或 KV 传输失败。
- 正常结束前没有错误事件；不能只看 `[DONE]`。

网络配置和本次短 PD 请求通过后，再恢复 128K/1K、纯 D 平均 TPOT ≤30 ms / ≤50 ms 的正式流程。短请求成功不代表 128K 容量、BS、稳态或性能目标已通过。

## 11. 执行后留下这张验收表

| 检查项 | 记录内容 |
|---|---|
| 实际机型/驱动 | 两台 `npu-smi info`、驱动版本 |
| 地址分配来源 | NEW 八卡地址的分配记录、掩码、必要网关 |
| 接线 | 每卡交换机端口/VLAN/网络平面 |
| 配置前/后 | 两台原始采集目录，NEW 的 before/after 和设置日志 |
| IP/link | 16 个设备地址唯一，读回正确，链路稳定 UP |
| ping | 对应卡双向，以及实际业务要求的完整路径 |
| READ/SEND | 具体卡对、方向、参数、两端完整数值结果 |
| 容器配置 | 每个容器读取的是自己宿主机的 hccn.conf |
| PD 短请求 | usage、同一时间段三方日志，无新增传输错误 |

### 若仍然失败，优先回报这六项

```text
1. NEW 卡0：配置后的 ipaddr / netmask。
2. REF 对应卡：卡号、ipaddr / netmask。
3. 两端 link 状态，以及 NEW→REF / REF→NEW Device ping 的回包/丢包率。
4. 两端 LLDP 的 System Name / Port ID；管理员确认的 VLAN/平面。
5. 原厂 READ 测试第一条错误，或两端 BW average 数值和单位。
6. 若 READ 正常而 PD 失败：最新第一条传输错误和目标 IP:端口。
```

如果原厂跨机 READ 已正常、PD 仍报原错误，继续查 Mooncake/HIXL 的设备绑定、元信息、协议和版本；不再仅凭 HTTP 端口通断解释该错误。

---

本文命令按昇腾 A2 HDK 26.1.0 官方文档核对；实际以机器安装驱动的命令支持情况为准。编写环境没有你的 A2，未执行 IP 修改、RoCE 打流或 PD 验收，不预填任何实测结果。
