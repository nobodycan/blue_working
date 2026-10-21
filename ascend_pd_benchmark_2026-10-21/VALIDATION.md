# 本地验证记录

日期：2026-10-21。环境：Windows、本任务目录中的隔离Python依赖；没有连接昇腾机器。

已通过的检查：

1. 配套11个Python脚本的AST语法检查（最终数量见打包manifest）。
2. 所有Bash脚本经tree-sitter-bash语法树检查，无ERROR或missing节点。此项不等同于在Linux运行Bash或执行NPU服务。
3. 用下载的v0.23.0 NPUModelRunner/MooncakeHybridConnector源码验证诊断补丁：锚点数量正确、插入后Python语法有效、恢复后文本与原版一致。
4. 用下载的v0.23.0代理源码验证生成的instrumented副本语法有效。
5. 通过本机aiohttp模拟OpenAI SSE服务验证客户端：HTTP数据分片、一次事件含多个token、usage长度校验、不同请求唯一前缀、调试ID模式/普通模式、burn-in及结束窗口过滤。
6. 故意返回错误prompt_tokens，客户端拒绝该样本并返回失败状态，未将其计入有效请求。
7. 重叠设备区间的并集算法用已知输入校验，未把重叠时间重复相加。
8. 汇总脚本只输出时延候选点；三次重复中有一次TPOT超标时，不将该并发判作该目标达标。

尚未执行，必须在现场完成：

- 镜像pull、驱动/CANN兼容、权重下载/量化描述验证。
- A2模型加载、算子正确性、MTP正确性与长上下文质量验证。
- 多机HCCL/RDMA、Mooncake实际传输与完整参考拓扑启动。
- 性能、HBM、cache blocks、稳定Decode batch以及Profiler关键路径测量。

本手册没有把上述未执行项写作通过，也没有填写估计性能数值。
