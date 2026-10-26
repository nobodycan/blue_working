# AISBench 既有负载上的昇腾 PD Profiling 方案

更新：2026-10-26。适用：**vLLM-Ascend 0.30.0rc2 / A2 I800 / DeepSeek-V4-Flash W8A8 / 1P1D**。

你已经用 AISBench 和社区工具 `aisbench_auto_tools_prefix` 做过压测。本版从已有结果继续：**客户端仍用这两个工具发相同请求，服务端增加 NPU profiling，用 MindStudio Insight 解释原有延迟和吞吐。** 无需为了 profiling 换压测客户端。

**直接运行：**[一键运行.md](一键运行.md)；当前单文件总控为 [oneclick_aisbench_profile.py](oneclick_aisbench_profile.py)，使用你们现有的 `--profiler-config` + HTTP start/stop 路线。先前的 msprof attach 脚本保留作其他环境参考，本次不用。

## 你的原始实验，以这组为基准

| 项目 | 已确认设置 |
|---|---|
| AISBench 统计 | 普通模式，非 stable_stage |
| 输入 / 输出目标 | 128K / 1K，即 131072 / 1024 token；实际长度保留原生报告核验 |
| 并发 C | 1、2、4、8、16 |
| 每档数据条数 N | 4×C，即 4、8、16、32、64 |
| 工具 | AISBench + `aisbench_auto_tools_prefix` |
| 已有采集工具 | 机器能找到 `msprof`；`msprof --version` 不支持 |
| 仍需记录 | CANN/msprof 实际版本、prefix/cache 状态、TP/EP/MTP、卡分配 |

**第一轮不切稳态、不改 prefix 比例、不换模板、不改 MTP/图模式、不改请求数。** 先拿原工况解释清楚；延长请求数、改变 prefix 或单独研究 D 都另建实验组。

## 用什么工具

| 工作 | 工具 |
|---|---|
| 复现负载和普通模式报表 | 原 AISBench 与 `aisbench_test.py` 命令 |
| 服务端采集主路线 | vLLM-Ascend 内置 Ascend PyTorch Profiler；启动时 `--profiler-config`，采集时直连 P/D 的 `/start_profile`、`/stop_profile` |
| 其他环境参考 | msprof 动态 attach；本次不使用，无需为当前方案填写 PID 或设置 PROFILING_MODE |
| 查看 timeline / 算子 / 通信 | MindStudio Insight |
| 解释框架调度 / KV 请求关联 | 有需要再补 MS Service Profiler；不和其他底层采集叠开 |

**已按你们的实际流程修正主路线。** 正确参数拼写是 `--profiler-config`；P/D 各配置自己的输出目录，业务请求仍发 proxy，采集开关直连实例。安装了 msprof 不意味着需要用它 attach。内置 wrapper 在服务进程中使用 torch_npu.profiler。[0.30.0rc2 官方指南](https://github.com/vllm-project/vllm-ascend/blob/v0.30.0rc2/docs/source/developer_guide/performance_and_debug/service_profiling_guide.md)

## 阅读顺序

1. [01_完整解决思路.md](01_完整解决思路.md)：按你的 128K/1K、五档并发设计 OFF/ON 对照和采集窗口。
2. [02_上机操作手册.md](02_上机操作手册.md)：保留现有 AISBench 工具，准备 case、prefix 状态和服务端采集。
3. [一键运行.md](一键运行.md)：当前推荐的 HTTP 总控配置与运行；06 的 msprof attach 说明仅供其他采集方式参考。
4. [03_时间线怎么看与问题定位.md](03_时间线怎么看与问题定位.md)：P/D 证据怎么读，哪些时间不能相加。
5. [04_汇报模板.md](04_汇报模板.md)、[05_官方来源与版本核验.md](05_官方来源与版本核验.md)。

配置：[实验矩阵](configs/experiment_matrix.csv)、[环境记录模板](configs/environment_template.json)。

## 本包脚本怎么用

| 脚本 | 本次用途 |
|---|---|
| `collect_env.py` | P/D 环境、包版本和 msprof 帮助留档，不依赖 msprof --version 成功 |
| `observe.py` | P/D metrics 与 npu-smi 的辅助趋势记录 |
| `oneclick_aisbench_profile.py` | 当前主流程：原 AISBench 发压 + P/D HTTP profiler 控制与留档 |
| `profile_window.py` | 单独控制 P/D HTTP 采集短窗口，不负责 AISBench 发压 |
| `analyze_profiles.py` | 当前 PyTorch 路线的 ascend_pt 解析，需在匹配的 torch_npu/CANN 环境执行 |
| `run_bench.py`、`summarize_results.py` | 上一版保留的 vLLM CLI 辅助脚本；**本次 AISBench 主流程不使用，也不能解析 AISBench 报告** |

本版没有给 AISBench 或 wrapper 虚构 `--profile` 参数。两个工具继续负责发压，采集窗口在服务端控制。

## 最小完成目标

从已测的 C=1 和 C=8（或原报告中更有代表性的档位）复现普通模式 OFF，再做同负载 ON。HTTP 开关由实例分发到其 worker，不需要手动选 PID；最终仍须检查各 rank 的有效数据。总控默认 P、D 分轮，避免把同工况不同轮误当成同一请求的精确跨侧拆解。

输出应为：**原生 AISBench 报告 + 数据/config/cache 状态 + ascend_pt/timeline + 采集窗口说明 + P/KV/D 瓶颈证据**。没有连接目标服务器，因此本包不含真机跑分。
