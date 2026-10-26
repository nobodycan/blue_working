# msprof 动态采集：AISBench 继续发压，采集实际 P/D worker

适用目标：已运行的 vLLM-Ascend PD 服务。**以下是按官方动态 attach 路径编写的版本条件化操作，不是宣称任意 msprof 安装都可直接 attach。** 先核实本机 help 和对应 CANN 文档。

## 1. 没有 msprof --version，怎样确认能力

在 **服务实际运行的同一个容器、相同用户** 中执行：

```bash
command -v msprof
readlink -f "$(command -v msprof)"
msprof --help > msprof_help.txt 2>&1
grep -E -- '--dynamic|--pid|--output|--task-time|--runtime-api|--export|--duration|--delay' msprof_help.txt
```

路径用于判断是不是与当前 CANN 配套的工具，不要只看宿主机上另一个 msprof。不要尝试另一个不确定的 `-v` 参数来猜版本。

查安装目录的版本文件；具体位置随镜像不同，存在才读取：

```bash
for f in \
  /usr/local/Ascend/ascend-toolkit/latest/version.cfg \
  /usr/local/Ascend/ascend-toolkit/latest/version.info \
  /usr/local/Ascend/cann/latest/version.cfg \
  /usr/local/Ascend/driver/version.info; do
  if [ -f "$f" ]; then printf '\n%s\n' "$f"; cat "$f"; fi
done
```

如果这些位置没有文件，按 `readlink -f` 指向的实际安装根目录、镜像版本说明和包清单查找。驱动版本不是 CANN 版本，不能互相代替。保存 help 和路径，即使暂时没有版本号也可判断下一步。

| 检查结果 | 决策 |
|---|---|
| 支持 dynamic/pid，且启动条件成立 | 继续 attach 路线 |
| 只有工具文件，但 help/依赖不正常 | 先修正本容器的 CANN 工具环境；不升级整套栈碰运气 |
| 缺少 dynamic/pid，或现场文档不支持该模式 | 使用 02 的 vLLM 内置 profiler 路线 |
| 服务没做动态准备，目前不能重启 | 保存现状；不能通过当前终端 export 让旧 worker 获得启动环境。可使用已经配置好的框架采集接口，否则安排准备窗口 |

## 2. Attach 的启动前提：这是最容易漏的一步

官方 attach 路径要求 **启动 AI 任务前** 设置：

```bash
export PROFILING_MODE=dynamic
# 随后用你原来已经验证可用的 P 或 D 启动脚本启动该服务。
```

在 P 和 D 各自的实际启动环境中配置，保留其他模型参数。若旧服务没有这个环境，需要计划内重新启动相应模型服务；**不必因此重建容器，也不能靠重开终端伪装已经生效**。服务重启后按原 cache 方案重新准备，并做新的 OFF 对照。

如果通过容器编排/服务管理器启动，应将变量传入真正启动 worker 的环境，而不只是 exec 进容器后的 shell。不要在本轮同时启用 torch_npu profiler、msServiceProfiler 的设备采集或其他同底层采集。

[官方动态采集条件](https://www.hiascend.com/doc_center/source/en/CANNCommunityEdition/900/devaids/Profiling/atlasprofiling_16_0016.html)

## 3. 选择 PID：不能只找 HTTP API 进程

查看 P 或 D 容器的进程和设备占用：

```bash
ps -eo pid,ppid,args
npu-smi info
```

结合实际 worker 日志、设备占用信息和进程父子关系，建立映射：

| 角色 | worker PID（本容器） | global/local rank | 实际物理 NPU | 动态 socket |
|---|---|---|---|---|
| P | 待填写 | 待填写 | 待填写 | 待验证 |
| D | 待填写 | 待填写 | 待填写 | 待验证 |

vLLM 的 API/front-end 或 EngineCore 可能不直接执行 NPU kernel。采了它得到空数据，不能推出设备空闲。不要仅凭进程名含 python/vllm 就把所有 PID 都附加上。

可只列出当前用户的动态采集 socket，**不删除**：

```bash
find "$HOME" -maxdepth 1 -name 'dynamic_profiling_socket_*' -print
```

socket 位置/命名以当前版本和日志为准；不存在就查 worker 的启动环境、CANN 初始化和权限。陈旧 socket 不能证明旧 PID 仍活着。不要把文档里的清理通配符直接用于当前用户目录，先确认是否对应仍运行的任务。

确认某个 worker 的动态变量是否传入，可以只读取这一项（如果当前用户有权限）：

```bash
export TARGET_PID=实际workerPID
python3 - "$TARGET_PID" <<'PY'
import sys
from pathlib import Path
items = (Path('/proc') / sys.argv[1] / 'environ').read_bytes().split(b'\0')
print(next((x.decode(errors='replace') for x in items
            if x.startswith(b'PROFILING_MODE=')), 'PROFILING_MODE not visible'))
PY
```

此检查不打印完整环境或 key；它只能辅助验证启动环境，不代替实际 attach 和有业务的采集验收。无读取权限时用服务启动记录核验，不随意提升权限。

## 4. 第一轮只采一个确定的 worker

先从 P 一个有效 worker 试采，确认链路，再同工况采 D。**单 rank 试采只是通路检查，不能代表整个 TP/EP 实例。**

在 P 服务所在容器的一个交互终端中：

```bash
export P_WORKER_PID=实际P侧用卡workerPID
export P_PROF_OUT=/data/pd_profile_aisbench/session01/C8_ON_P_rank0
mkdir -p "$P_PROF_OUT"
df -h "$P_PROF_OUT"
msprof --dynamic=on --pid="$P_WORKER_PID" \
  --output="$P_PROF_OUT" --task-time=on
```

以上以现场 help 确认支持 `--task-time=on` 为前提，on/l0/l1 的能力和默认项按匹配版本判断；不把 torch profiler 的 Level1 参数传给 msprof。必要时在**另外一轮**加入已核实支持的 `--runtime-api=on`，进一步看 runtime 下发/同步；别一开始打开所有高级指标。

进入 msprof 交互提示后先等待，不立即采启动/预热。另一个客户端终端用 02 中原 AISBench 命令发压。确认目标阶段实际有工作，在 **msprof 提示符中**输入：

```text
(msprof) start
```

观察约 3–5 秒有效 NPU 活动后输入：

```text
(msprof) stop
(msprof) quit
```

`start/stop/quit` 是该交互界面的指令，不是在另一个 Bash 窗口执行的系统命令。只有返回明确成功并写出数据，才认为窗口采集完成。stop/导出耗时不等于模型执行耗时。

**动态 attach 不叠加 `--delay` 或 `--duration`。** 官方将它们定义为另一种延迟/定时采集模式，与 dynamic 不兼容；这里按交互 start/stop 控制。128K prefill 可能远长于窗口，先确认有效事件，再针对目标阶段适当延长，记录真实覆盖范围。

## 5. D 侧怎么采，如何扩到多卡

恢复同工况的 cache 状态，创建新的 AISBench case；在 D 容器用真实 D worker 重复第 4 步，输出目录改成 D 和实际 rank。仍然通过 proxy 发业务请求，不绕过 PD 改向 D 直接发送 prompt。

多卡采集方式随 CANN 版本不同：部分旧文档要求逐卡/逐 rank 处理，新文档允许一个控制命令指定多个 PID。**未核实前统一从单个明确的 PID 开始，不宣称 attach 父进程自动覆盖全部子 worker。**

现场 help/匹配文档明确支持 PID 列表时，才可在同一容器里使用：

```bash
# 仅在现场版本明确支持多个 PID 后使用；填入已核验的用卡 worker。
export ROLE_PIDS=12345,12346
msprof --dynamic=on --pid="$ROLE_PIDS" \
  --output=/data/pd_profile_aisbench/session01/role_all_ranks --task-time=on
```

12345/12346 是占位示例，不能原样运行。收集结果中每个 PID/device/rank 是否都产生有效数据，要逐项验收。若版本仅支持逐卡，以其约束执行，或改用 vLLM 内置 API 采集实例内 worker。

P/D 跨容器或跨机器，各自在本地容器运行 msprof。P/D 同物理 device 或存在其他采集器时，不得重叠采集；不同 device 也需满足版本限制。确实需要全链路时再安排同步控制，记录各自实际开关时间与时基误差，不用手工点 start 的时刻假定精准同步。

## 6. 导出和查看

停止完成后保留原始 `PROF_XXX` 目录，先检查是否已经自动生成解析结果。若没有，且 help 支持 export，在同一 CANN 环境中执行：

```bash
msprof --export=on --output="$P_PROF_OUT"
```

也可以指定具体的 `PROF_XXX`；父目录包含多个 PROF 时，按版本支持批量导出。不要把 output 换成一个没有原始数据的全新空目录。新旧版本可能导出 `mindstudio_profiler_output`、`timeline/summary` 或 DB，具体以日志和版本文档为准。

[官方导出 FAQ](https://mindstudio-docs-master.readthedocs.io/zh-cn/latest/msprof/docs/zh/support/faq/)

主要看：`msprof_*.json` 时间线、`op_summary*.csv`、`op_statistic*.csv` 和版本对应通信/DB 数据。导入 MindStudio Insight 时保留完整目录，确认支持当前格式。**纯 msprof 不是 ascend_pt 解析路径**，不要强行用本包 `analyze_profiles.py` 寻找它。

## 7. 必须通过的采集验收

- attach 目标是正确 worker，本次进程确实在预期 NPU 上执行。
- start/stop 成功，目录属于本次 run，没有混用上次数据。
- 导出的目标窗口内有真实设备 kernel，不只是 runtime/API 或空白。
- 目标角色、PID、rank、device 与实验映射一致；全卡结论不能只依据单 rank。
- 记录覆盖了 P、KV/入场或 D 的哪个阶段；未知阶段写未知，不猜测。
- AISBench 的普通整轮结果和 NPU 短窗口分别留存。

msprof 默认缺少完整 Python/PyTorch 框架归因。看见设备空白仍需请求/调度/connector 事件才能解释；不能把全部空白叫 KV 传输。

## 8. 失败时怎么判断

| 现象 | 优先检查 |
|---|---|
| version 选项不识别 | 跳过该选项，用 help、工具真实路径和 CANN 版本文件 |
| dynamic/pid 不识别 | 版本或错误工具路径；改用适配的框架路线，不编造参数 |
| 找不到对应 socket/无法 attach | 启动前环境、目标 PID、同容器同用户、实际 CANN 初始化 |
| 采集目录有数据但没 kernel | 采到 front-end、窗口无业务、选错 device 或某些采集项未启用 |
| 只有部分卡有数据 | 未覆盖全部 worker，或多 PID/多 device 行为与预期不符 |
| 数据特别大或性能大幅下降 | 采集范围/时长、磁盘、叠加采集；缩短诊断窗口做 OFF/ON 对照 |
| stop 失败/状态不明 | 先在该交互会话停止并查服务日志，确认停止后再跑正式轮；不删除活跃 socket 或强杀模型凑结果 |

本包没有在你的 NPU 上执行上述命令。现场 CANN 版本未确定，因此多 PID、可用指标、输出文件和进程覆盖均以实际验证为准。
