# 昇腾 A2 · DeepSeek-V4-Flash W8A8 · 1P1D Profiling 入门包

适用环境：用户确认 **vLLM-Ascend v0.30.0-rc2、昇腾 A2 I800、DeepSeek-V4-Flash W8A8、已部署 1P1D**。方案整理日期：2026-10-26。

**你要完成的是一条证据链：先测系统快慢 → 再找时间花在哪里 → 提出一个改进 → 复测证明效果。** 单独发一张 NPU 利用率截图，或一份算子耗时排名，还不是完整的模型性能评测。

本包已查阅版本化官方文档/源码，提供方案、命令和辅助脚本；当前没有连接你的 NPU 服务器，因此没有实测性能结果。脚本在本地进行离线验证，目标机实际兼容性由环境检查和小规模试跑确认。

## 先打开哪份文件

| 文件 | 用途 |
|---|---|
| [01_完整解决思路.md](01_完整解决思路.md) | 从零理解指标、实验设计、并发和 PD 分析方法 |
| [02_上机操作手册.md](02_上机操作手册.md) | 按终端执行，从环境检查到采集和解析 |
| [03_时间线怎么看与问题定位.md](03_时间线怎么看与问题定位.md) | MindStudio Insight 分析顺序、计算方法、常见误判 |
| [04_汇报模板.md](04_汇报模板.md) | 给导师汇报时填写，性能数字全部留空 |
| [05_官方来源与版本核验.md](05_官方来源与版本核验.md) | 官方链接、已核实的版本细节和适用边界 |
| [configs/experiment_matrix.csv](configs/experiment_matrix.csv) | 分阶段测试矩阵；推荐从最小一行开始 |
| [configs/environment_template.json](configs/environment_template.json) | 补充物理机、卡分配、TP/EP、量化和 MTP 设置 |
| [scripts](scripts) | 6 个 Python 辅助脚本，业务压测调用官方 vLLM CLI |
| [VALIDATION.md](VALIDATION.md) | 本地验证范围和未完成的目标机检查 |

## 推荐的方法组合

1. **服务端到端压测：`vllm bench serve`。** 记录 TTFT、TPOT、P95/P99、输出吞吐、成功/失败数。
2. **NPU 时间线：vLLM-Ascend 内置 Ascend PyTorch Profiler。** 通过 P/D 各自的 `/start_profile`、`/stop_profile` 控制短窗口。
3. **可视化：MindStudio Insight。** 对照 CPU 调度、NPU 计算、通信和空闲区间。
4. **需要深入请求调度和 KV 传输时：MS Service Profiler。** 留到第二阶段，不把多种底层采集器同时叠加到一次实验里。

这是根据官方支持路径做出的入门选择，没有公开使用量统计可以证明某工具“使用人数最多”。[版本化官方采集指南](https://github.com/vllm-project/vllm-ascend/blob/v0.30.0rc2/docs/source/developer_guide/performance_and_debug/service_profiling_guide.md)

## 本次必须记住的 6 件事

- **1P1D = 一个 P 实例 + 一个 D 实例，不表示只有两张卡。** 物理机数量和每个实例用几张卡，仍需填写。
- 你说“一台机器”，本包优先兼容同机分卡；若实际两机，分别填写两侧 IP 并检查时钟同步。
- 请求并发不等于 D 的实际 batch size；TP 多卡也不等于多个独立服务实例。
- **正常压测时 profiler 关闭；性能诊断时短时开启。** 两组结果分别存档。
- 先并发 1，再逐档增加；不要直接开 32/64 并发做长时间采集。
- 所有 Linux 命令在 NPU 服务器/容器执行；本地 Windows 上只负责阅读、传文件和分析已取回的数据。

## 第一轮最小交付

先用 1024 输入 / 128 输出做连通性与基线，完成并发 1、2、4 的无 profiler 压测；选一个档位收集 P/D 各 3–5 秒的有效 trace。确认可用后再扩展输入长度和并发。

最后交付：环境表 + 延迟/吞吐表 + P/D 时间线截图 + 证据支持的瓶颈假设 + 一次对照复测。完整步骤见 02。
