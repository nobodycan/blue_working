# A2采集、结构模型与优化操作手册

本文件默认只用于制定计划。实际采集/重启/改服务/自动寻优必须另外获授权。内部trace留本地。

## 1. 分支选择：先看真实stack

| 真实stack/状态 | 路径 | 禁止做法 |
|---|---|---|
| 已匹配版本的vLLM-Ascend，profiler已配置 | worker侧Ascend profiler，服务HTTP启停 | 包裹客户端冒充服务端采集 |
| vLLM-Ascend框架排队/batch/KV问题 | 匹配版本MS Service Profiler或真实引擎指标 | 算子trace缺queue却编造排队时长 |
| MindIE/ATB或其他CANN服务，已预置动态采集 | 匹配CANN版本的worker attach | 默认MindIE有/start_profile |
| 未预置且不允许重启 | 已有低开销指标，或该版支持的独立系统采样 | 假装可任意attach并得到完整算子数据 |
| 仅历史aisbench文件 | M0/M1，提出最小追加证据 | 从TTFT拆出纯prefill/通信 |

来源S13-S18，版本号是例证，不是用户环境认定。

## 2. 先计划有限哨兵

基础候选每部署：C1/L512、C8/L8192、C32/L65536；另加最明显异常格。按预算裁剪，最多8条件作为初轮，不重跑全部80。

若极端格OOM/超时，不静默缩短输出。记录不可行原因，再选择最大可行点，并保留原格失败。

每个候选：

1. 无profiler基线，确认真实输入/输出、成功率和负载方式。
2. 低开销服务指标：running/waiting、KV、抢占/重算、batch、网络/CPU。
3. 只对最能区分竞争解释者采详细trace。
4. 长请求选实际context的早/中/后窗口；不要凭同样墙钟秒数假设处于同token位置。
5. 算子短窗口和框架长期采集是不同方案，不强行统一采集时长。某版MS Service Profiler建议更长窗口时按其要求执行。
6. 记录profile开关对延迟/吞吐的扰动，不混作正式性能数值。

## 3. vLLM-Ascend采集分支

### 已核实的版本样板

S14的v0.22.1rc文档支持启动配置片段：

```text
--profiler-config '{"profiler":"torch","torch_profiler_dir":"<服务端本地目录>","torch_profiler_with_stack":false}'
```

这里只是该版本的配置片段，不是可直接启动用户模型的完整命令。真实dtype、max-model-len、TP等保持baseline，不能抄文档的小模型示例。

### 执行顺序（获授权后）

1. 对照实际安装版--help/源码确认该选项存在；旧版查旧配置，不同时设置冲突的新旧方式。
2. 如需重启先保存原命令、健康检查和回滚命令，经授权再加配置。
3. 确定真实服务节点端口；PD分别配置P/D输出目录和rank标识。已知某些proxy不转发profiling端点，不能仅打网关。
4. 开始真实负载，等待计划context/阶段；以请求关联和服务端观测判断。
5. 对实际受控服务发 `POST /start_profile`，只在已验证该版接口及授权范围内。
6. 到达计划窗口末尾发 `POST /stop_profile`；即使采样出错也应执行停止/清理，避免无限采集。
7. 404时先查是否配置/该版支持，不反复撞接口；不能临时声称已打开。
8. 核对每个实际rank的ascend_pt、时间范围、文件非空；P/D目录不可混同。
9. 按同版torch_npu离线analyse处理复制件，原始trace保留。
10. 记录失败与缺rank，缺少哪部分就限制哪部分解释。

### 框架级观测

MS Service Profiler按版本配置符号与SERVICE_PROF_CONFIG_PATH，解析request.csv/kvcache.csv/batch.csv。API/符号随版本变化，配置时必须有同版来源，不把任意安装版当最新。

算子级profile不会自动给出完整请求队列/服务调度；框架级profile也不会自动拥有全部PMU/HBM事件。

## 4. CANN msprof分支

S15明确的CANN商用8.2.RC1示例：应用启动前设置

```text
PROFILING_MODE=dynamic
```

之后可使用该版支持的attach样板：

```text
msprof --dynamic=on --pid=<真实worker_pid> --output=<服务端本地新目录> --runtime-api=on --aicpu=on
```

进入交互后start/stop/quit。任务级算子/AI Core指标选项另按该版支持表与--help核实后添加；不能认为上面最小命令已采齐所有计数器。

限制：
- 该版dynamic与delay/duration不能同时用。
- 多卡/多PID支持有限，不能承诺一次pid覆盖全部rank。
- 容器任务在相应容器采集。
- 应用启动时未准备动态采集，不在运行进程外设置变量后假称其已生效。
- 其他CANN版本存在文档差异，先检查自身帮助和实际支持，不机械复制8.2命令。
- 系统独立采样不需要用户程序的某些模式，不等于取得框架栈或请求关联。

## 5. trace分析的固定顺序

1. 时间域/单位和P/D/rank对应。
2. 请求生命周期、排队、handoff关键路径，先不看Top算子百分比。
3. 真实每步batch、prefill tokens、context分布、KV状态、抢占/重计算。
4. 计算/通信/CPU空洞的实际重叠与依赖，不把跨stream/rank耗时简单求和。
5. 按shape比较attention/MatMul/MoE/HCCL等事件，区分P/D KV connector和collective。
6. 对相同context/batch状态做比较，再解释短/长输出差异。
7. 分开“内存容量高”“访存量大”“有效带宽低/高”“算力利用率”，不互相替代。
8. 必要时分次采指标组，避免多个采集器争用。
9. 工具输入格式适配：msprof-analyze/Insight不会补采缺失信息；msProbe用于精度，不当作常规性能采集器。

产物：trace_manifest.csv、critical_path.md、operator_shapes.csv、bottleneck_evidence.md。

## 6. M2优先路径：审计msModeling，不先移植GPU模拟器

### 具体步骤

1. 记录msModeling release/commit、依赖、实际支持矩阵；不自动安装或升级。
2. 确认被测模型结构、精度、attention实现、MoE/MLA/投机等特性是否匹配。不能把分析模型名称当被测模型。
3. 选真实A2 SKU/卡数/HBM/拓扑对应画像；缺profile时写缺失，不套A100/H100参数。
4. 从该版本官方示例复制一份本地配置；逐项通过schema和--help核实字段，不凭本文猜CLI。
5. 最小跑一个已测且可行的case；区分simulator batch与客户端C，明确请求到达、负载窗口和输出平均方法。
6. 导出结构账本：各阶段算子/shape/逻辑KV/通信路径，和真实trace对账，先查少层/多层/重复计数。
7. 用独立A2微基准校准代表shape：低/中batch，context至少覆盖512、2k、8k、32k、约66k的实际可行点；prefill另做输入扫描才能建长度模型。
8. 通信校准区分节点内TP/EP与跨节点KV链路；容量、启动延迟、带宽、重叠分别验证。
9. 校准集与验证集预先分开；完整输出档/并发档留出，并在长context/高C边界验证。
10. 分别比较TTFT、TPOT、E2E、吞吐和尾部误差，排队区单列；不只校准一个总TPOT后声称全模型准确。
11. 达到预定误差预算后才用仿真筛选参数候选，再由aisbench实机复测。
12. 若支持矩阵不匹配，保持M0/M1，或做明示的模型扩展；不擅自将工具输出当真实支持。

msModeling的服务化自动寻优会运行真实服务、调整配置，不属于只读建模。必须另获权限、预算和回滚方案。

## 7. 每个优化候选的标准卡

```text
ID / 目标case / baseline版本
症状及证据文件 / 未排除解释
假设机制 / 可观测的预期变化
适用stack与参数来源（版本、schema/源码路径）
唯一改动 / 保持不变的配置 / 回滚命令
主指标与最小有意义收益 / SLO和正确性护栏
重复/配对/运行顺序 / 目标与回归格
是否授权 / 是否执行 / 结果与状态
```

无证据的参数保持“待证实候选”，不填预计提升20%之类虚构数值。涉及输出截断、滑窗、量化或路由语义变化时必须单独说明正确性影响。

## 8. 最小决策树

```text
若发生器受限 → 先修测量能力，重测baseline
否则若容量/抢占事件明确 → 先处理可行性与KV状态
否则若排队/阶段冲突明确 → 测调度/预算候选
否则若kernel/访存/通信关键路径明确 → 测对应后端支持优化
否则 → 补最能区分解释的一项观测，不批量调参
```

任何分支的优化都回到aisbench无profiler的单变量A/B和回归验收。
