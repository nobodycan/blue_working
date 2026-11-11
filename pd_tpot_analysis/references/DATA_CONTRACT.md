# 数据契约：先确认含义，再映射字段

## 1. 总体约束

- 所有 CSV 使用 UTF-8；允许 BOM。第一行是英文列名。空值用空字符串，不使用 `NaN`、`Inf`、`null`、`-1` 代替缺失值。
- 所有时间统一为毫秒。秒乘 1000；微秒除以 1000；纳秒除以 1000000。单位必须有证据，不能按数值大小猜。
- 所有 token 数为非负整数，必须来自真实 tokenizer/计数记录，不使用字符数或字节数。
- 目标长度与实际长度分列保存。不能用 max_new_tokens 代替实际 output_tokens。
- 原始文件不能修改。保留绝对来源路径和可定位记录的 source_locator。
- 这里的列名是我们定义的标准化接口，不表示 aisbench 原始文件一定包含同名字段。
- 示例表头不是实际实验数据。不要把示例补成虚构观测。

## 2. runs.csv：一行代表一次真实运行

必需列：

| 列名 | 含义 / 允许值 |
|---|---|
| run_id | 全局唯一运行 ID；不能把同一次运行拆成多个独立重复 |
| analysis_group | 已完成审计的对照组；同组允许比较，未审计配置不应放在一起 |
| mode | `mixed` 为混部；`disagg` 为分离 |
| concurrency | 本次设定并发，正整数；不代表全程实际 decode batch |
| target_input_tokens | 目标输入长度，正整数 |
| target_output_tokens | 目标输出长度，正整数 |
| repeat_id | 该条件下独立运行的标识；不能来自请求序号 |
| pair_id | 仅真实成对设计填写；无配对设计为空。唯一性按同组/并发/长度/模式检查 |
| workload | `burst` / `closed_loop` / `open_loop` / `unknown` |
| scope | 本脚本只接受 `full_requests`：选中完整请求的全 decode 区间，而非截断稳态窗口 |
| run_status | `completed` / `failed` / `interrupted`；completed 仍可以包含失败请求 |
| expected_requests | 预期测量请求数，不含预热；未知留空，不得用并发填入 |
| source_file | 原始文件路径或来源清单 |

CSV 表头：

```csv
run_id,analysis_group,mode,concurrency,target_input_tokens,target_output_tokens,repeat_id,pair_id,workload,scope,run_status,expected_requests,source_file
```

建议另建 `deployment_audit.md` 或 `run_metadata.json` 保存：
- 模型完整版本、量化/精度、上下文上限、框架和 aisbench 版本。
- 每个阶段机器数、NPU 数、NPU 型号、TP/PP/DP、副本数。
- 调度策略、chunked prefill、max batch、KV cache 预算、抢占/重计算/换出。
- prompt 数据集和随机种子、输入长度分布、prefix cache 命中情况。
- EOS、ignore_eos、max_new_tokens、超时、预热和采样参数。
- 请求到达策略、请求总数、测试时长、最大在途请求数。
- 服务端和客户端计时边界、stream/chunk 输出语义、时间单位的证据。
- 机器背景负载和时间段；是否同一 decode 机器做混部/分离切换。
- 多个长度/并发是否在同一个 sweep 内重复：这影响跨条件不确定性。

analysis_group 分组规则：
1. 输入目标、模型、精度、计时口径、负载类型和测量范围不能混组。
2. 与 mode 必然绑定的差异，如一机混部/两机分离，必须明确记录；此组只支持部署方案比较。
3. 不与 mode 绑定、又影响性能的配置变化，原则上拆组。
4. 若某次运行可能存在配置错误，先标记并单独检查，不以其结果是否有利为排除理由。
5. 不同组不能在最终图里无说明拼成一条线。

## 3. requests.csv：一行代表一次请求尝试

必需列：

| 列名 | 含义 / 允许值 |
|---|---|
| run_id | 对应 runs.csv |
| request_id | 本 run 内唯一请求尝试 ID；重试需新 ID，可另存逻辑请求 ID |
| status | `success` / `failed` / `timeout` / `cancelled` / `incomplete` |
| is_warmup | `0` / `1`；不能自行把首批慢请求当预热 |
| input_tokens | 实际输入 token 数；可未知，需在报告披露 |
| output_tokens | 实际输出 token 数；success 必须填写 |
| decode_ms | 成功请求的完整首 token 到末 token 耗时；L >= 2 时必须填写 |
| decode_time_source | `last_minus_first_token` 或 `e2e_minus_ttft`；非成功记录可空 |
| ttft_ms | 可选值，列必须存在；没有可靠值时为空 |
| e2e_ms | 可选值，列必须存在；没有可靠值时为空 |
| source_file | 原始文件 |
| source_locator | 例如 JSON 路径/数组索引/JSONL 行号 |

CSV 表头：

```csv
run_id,request_id,status,is_warmup,input_tokens,output_tokens,decode_ms,decode_time_source,ttft_ms,e2e_ms,source_file,source_locator
```

时间规则：

```text
N_i = output_tokens_i - 1
TPOT_i = decode_ms_i / N_i
```

- L < 2 的请求 TPOT 未定义；保留请求，计入成功数，但不进入 TPOT 均值。
- decode_ms 首选可靠的首/末 token 时间差。
- 只有确认 e2e 从发出请求计至末 token，且 TTFT 从同一起点计至首 token 时，才能使用 `e2e_ms - ttft_ms`。
- 如果 e2e 结束于 HTTP 关闭/结果后处理结束，不可直接相减后冒充 decode_ms；先说明偏差或使用正确时间戳。
- 有 decode_ms、ttft_ms、e2e_ms 三者时应交叉核对。附带脚本允许 1ms 或 decode_ms 的 0.1% 的舍入误差，取较大值；超限报错，应回查口径，不自动放宽容差。
- 若只有工具报告的 TPOT，先确认分母、首 token 处理和聚合定义。不能仅为满足接口反推 `decode_ms = 工具 TPOT × (目标输出 - 1)`。
- ITL/chunk 间隔不能默认当逐 token 间隔。除非已确认流式事件与真实 token 一一对应。
- 所有成功请求的计时来源应在同一 analysis_group 内一致；切换来源应拆组或明确校准，附带脚本会阻止混用。

过滤规则：
- 预热记录保留，由 is_warmup 排除；报告预热数量和识别依据。
- 失败、超时、取消和未完成记录保留；计入失败率，不参与成功请求 TPOT 的主估计。
- 必须报告“成功请求条件下的 TPOT”，并警告失败引发的幸存者偏差。
- 未完成运行保留在原表，主分析排除并报告数量；不可只留下成功运行而隐去失败。
- 不自动按慢/快删除异常值，不自动删 EOS 提前结束的请求。

## 4. 运行级和条件级估计量

运行内两种指标：

```text
request_mean_tpot_ms = mean(TPOT_i)
token_weighted_tpot_ms = sum(decode_ms_i) / sum(output_tokens_i - 1)
```

这两者通常不相等。必须保留两列，并在计划中确定主指标。

条件级先对每个独立 run 计算指标，再对 run 等权平均。主分析不按 run 请求数加权，以免一轮较多请求支配运行间比较。若另报请求池化估计量，另起名称，不能覆盖主指标。

分位数约定：
- 附带脚本输出的是“每运行的请求 TPOT 分位数”；条件级主结果不是把这些分位数平均后称为全局 P95。
- 请求 TPOT 的 P95 不等于 ITL 的 P95；两者衡量不同问题。

## 5. token_intervals.csv：可选机制分析

建议字段：

```csv
run_id,request_id,token_index,token_timestamp_ms,itl_ms,active_decode_batch,prefill_overlap,prefill_event_id,clock_domain,source_file,source_locator
```

规则：
- token_index 从 1 开始，首 token 无前一个 token 的 ITL，留空。
- 必须验证真实 token 边界、token 个数与 output_tokens 一致，以及时间戳非递减。
- 客户端和服务端不同 clock_domain 不可直接相减；先建立偏移/漂移校准。
- prefill_overlap 没有证据时为空，不能用 ITL 尖峰反推标签再宣称尖峰与 prefill 相关。
- active_decode_batch 来自观测，不能直接填设定 concurrency。
- 各 token 之间相关，不能把百万 token 当百万独立实验重复。

## 6. 只有汇总数据时的受限分支

保存真实汇总表，不创建虚假的 requests.csv。

至少保留：run_id、组、mode、并发、目标长度、实际输出统计、工具 TPOT 定义/单位/聚合、成功失败数、重复标识、来源。

允许：同口径的运行级 TPOT 趋势、差值和相对差、重复运行的区间估计（真实重复充分时）。

受限：
- 不能重新计算请求 TPOT 分布、P95、实际输出完整分布。
- 实际长度不明时，不计算可解释为额外 decode 耗时的 E。
- 无法核对的工具口径必须在结论标明。
- 无 token 证据时，不验证干扰发生的时间位置。

附带 analyze_normalized.py 只接受逐请求标准接口；汇总分支需要另写更小的运行级适配器，不能伪造逐请求数据绕过验证。
