# AISBench 既有负载上的昇腾 PD Profiling 方案

更新：2026-10-26。适用：**vLLM-Ascend 0.30.0rc2 / A2 I800 / DeepSeek-V4-Flash W8A8 / 1P1D**。

你已经用 AISBench 和社区工具 `aisbench_auto_tools_prefix` 做过压测。本版从已有结果继续：**客户端仍用这两个工具发相同请求，服务端增加 NPU profiling，用 MindStudio Insight 解释原有延迟和吞吐。** 无需为了 profiling 换压测客户端。

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
| 服务端采集主路线 | 已安装的 `msprof` 动态 attach，先核实能力和 worker 启动条件 |
| 服务端采集备选 | vLLM-Ascend 内置 Ascend PyTorch Profiler，直连 P/D HTTP 开关 |
| 查看 timeline / 算子 / 通信 | MindStudio Insight |
| 解释框架调度 / KV 请求关联 | 有需要再补 MS Service Profiler；不和其他底层采集叠开 |

**msprof 在机器上 ≠ 当前运行的 worker 已满足动态 attach 条件。** 需要核对 `--help`、服务启动前的 `PROFILING_MODE=dynamic` 和实际用卡 PID。详见 06。msprof 参数和 PyTorch profiler 的 Level 不是一套配置。

## 阅读顺序

1. [01_完整解决思路.md](01_完整解决思路.md)：按你的 128K/1K、五档并发设计 OFF/ON 对照和采集窗口。
2. [02_上机操作手册.md](02_上机操作手册.md)：保留现有 AISBench 工具，准备 case、prefix 状态和服务端采集。
3. [06_msprof动态采集操作.md](06_msprof动态采集操作.md)：工具能力、worker PID、attach、start/stop、解析和失败排查。
4. [03_时间线怎么看与问题定位.md](03_时间线怎么看与问题定位.md)：P/D 证据怎么读，哪些时间不能相加。
5. [04_汇报模板.md](04_汇报模板.md)、[05_官方来源与版本核验.md](05_官方来源与版本核验.md)。

配置：[实验矩阵](configs/experiment_matrix.csv)、[环境记录模板](configs/environment_template.json)。

## 本包脚本怎么用

| 脚本 | 本次用途 |
|---|---|
| `collect_env.py` | P/D 环境、包版本和 msprof 帮助留档，不依赖 msprof --version 成功 |
| `observe.py` | P/D metrics 与 npu-smi 的辅助趋势记录 |
| `profile_window.py` | **仅备选 PyTorch 路线**的 P/D HTTP 控制；不能控制 msprof attach |
| `analyze_profiles.py` | **仅 PyTorch 路线**的 ascend_pt 解析；msprof 的 PROF 目录见 06 |
| `run_bench.py`、`summarize_results.py` | 上一版保留的 vLLM CLI 辅助脚本；**本次 AISBench 主流程不使用，也不能解析 AISBench 报告** |

本版没有给 AISBench 或 wrapper 虚构 `--profile` 参数。两个工具继续负责发压，采集窗口在服务端控制。

## 最小完成目标

从已测的 C=1 和 C=8（或原报告中更有代表性的档位）各复现一轮普通模式 OFF，再做同负载诊断 ON。先抓 P 一个有效 worker、再抓 D 一个有效 worker，确认有真实 kernel 后扩展 rank；正式瓶颈结论需要足够多 rank 的证据。P、D 不同物理 device 且版本支持时可以同轮采集，但不要求第一轮就同时采全卡。

输出应为：**原生 AISBench 报告 + 数据/config/cache 状态 + msprof 原始数据与 timeline + 采集窗口说明 + P/KV/D 瓶颈证据**。没有连接目标服务器，因此本包不含真机跑分。
