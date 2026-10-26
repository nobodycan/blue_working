# 验证记录

本包提供方案和辅助脚本，不含真实 NPU 测试结果。

用户确认 HTTP 内置 profiler 后新增 `oneclick_aisbench_profile.py`：**12 项 HTTP 总控检查通过**，包括接口发现、准确 POST 路径、超时后的 stop、停止状态不明、无 PID/msprof 依赖，以及完整 case 的模拟 HTTP 流程。此为当前主脚本，旧 msprof 总控保留作其他场景参考。服务端输出文件不能由 HTTP 200 自动证明，脚本明确保留为待核验。

```bash
python3 validation/validate_http_oneclick.py
```

新增单文件总控后：**18 项总控离线检查 + 原有 11 项检查通过，共 29 项**。总控覆盖数据 N=4C、原命令参数与模板保留、原生请求数核对、指标触发、SSH/Docker 参数引用、采集失败清理、状态不明时中止后续测试，以及真实本地子进程 JSON 控制协议。PTY/msprof、SSH/Docker 连接与 CANN 实际采集仍未在目标机验证。

新增检查入口：

```bash
python3 validation/validate_oneclick.py
```

2026-10-26 按用户实际 AISBench 工况改写后本地执行：**11 项离线检查全部通过**。运行环境为 Windows / Python 3.12；HTTP 传输在进程内模拟，没有访问实际 P/D 服务。

离线检查入口：

```bash
python3 validation/validate_offline.py
```

检查范围：6 个保留脚本的语法与帮助、文档相对链接与配置格式、vLLM 旧辅助命令预览、路径输入校验、模拟 P/D HTTP 开启/停止和失败清理、ascend_pt 发现、旧 vLLM 汇总的正式/诊断分离、缺失指标留空、避免覆盖汇总；并检查新版矩阵与用户原始 128K/1K、C=1/2/4/8/16、N=4C、普通模式一致。

本轮主流程为 AISBench/wrapper 发压加服务端 msprof；保留脚本测试不表示已经验证了真实 AISBench 命令或 msprof 交互。msprof 的 attach/交互/导出未作目标机测试，文档中的命令以现场 help 与启动条件为前提。

模拟接口只返回测试响应，不执行模型推理。临时的合成 JSON 数字仅用于验证程序，运行后清理，不作为性能结果保留。

**现场仍需验证：** vLLM CLI 实际选项与 tokenizer；服务和模型兼容性；P/D profiler 接口；独占物理 device；每个 rank 的有效 NPU trace；原始数据解析；代理流式和 token 统计；正式吞吐/延迟；Insight 可视化与瓶颈结论。

本地 Windows 环境没有用户的 NPU/CANN 服务，不执行模型压测，不宣称通过 A2 真机验证。
