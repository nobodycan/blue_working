# 标准化输入、模型脚本与复现命令

## 1. 两层输入，不跳过原始审计

层A：原始aisbench → 逐请求/运行观测表，字段按真实版本映射，详见AISBENCH_PLAYBOOK.md。

层B：审计过的运行观测 → model_runs.csv → model_cases.py。

附带脚本只负责层B，不直接解析任意aisbench文件，不读取SQLite或H5，不自动画图，不生成置信区间，不采profile。

## 2. model_runs.csv 必需表头

```csv
run_id,analysis_group,mode,concurrency,target_input_tokens,target_output_tokens,repeat_id,run_status,scope,workload,request_mean_tpot_ms,valid_tpot_requests,success_requests,failed_requests,actual_output_min,actual_output_max,actual_input_mismatches,actual_input_unknown,source_file
```

| 字段 | 规则 |
|---|---|
| run_id | 全局唯一独立运行ID，不是请求ID |
| analysis_group | 已核查的可比配置组；不可把模型/精度/时间语义/负载方式混合 |
| mode | mixed或disagg |
| concurrency | 设定C，正整数；不是实际batch |
| target_input_tokens | 本组目标输入，通常512 |
| target_output_tokens | 目标输出，>=2 |
| repeat_id | 条件内真实独立运行标识；一轮多个导出不可重复填写 |
| run_status | completed/failed/interrupted |
| scope | 仅full_requests：完整请求的平均量，不是截断窗口 |
| workload | burst/closed_loop/open_loop/unknown |
| request_mean_tpot_ms | 先对有效成功请求计算TPOT_i，再在run内求均值；单位ms/token |
| valid_tpot_requests | 参与该均值的成功请求数 |
| success_requests | 所有成功请求数，含TPOT未定义者 |
| failed_requests | 失败/超时/取消/未完成尝试的合计，细分状态在层A保留 |
| actual_output_min/max | 纳入均值的请求的实际长度min/max，不是目标值复制 |
| actual_input_mismatches | 该run测量请求中实际输入不等于目标值的个数 |
| actual_input_unknown | 无法确认实际输入token数的请求数 |
| source_file | 可追溯来源或来源清单 |

CSV使用UTF-8，允许BOM，标准缺失值为空，不用NaN/Inf/-1伪装缺失。

不明的计数不能填0。如果无法建立本接口，保留M0受限汇总分析，不伪造数据让脚本通过。

### 可选的实际输入范围

可增加 `actual_input_min,actual_input_max` 两列。例如目标512但chat模板后所有请求实际均为528：保持target_input_tokens=512，mismatches照实计数，实际min=max=528。脚本使用528作为模型S，并在case_means保留actual_model_input_tokens，不篡改原计划。

同一analysis_group内模型实际S必须一致；不同固定S需要拆组。实际输入仍有分布/未知且不能确认固定时，当前简化脚本不适用，但M0描述结果必须保留。没有这两列时，仅mismatches=0且unknown=0才可用目标值作S。

### 指标语义由命令显式指定

- `aisbench_response_tpot`：确认过的该版aisbench响应事件TPOT。
- `canonical_content_tpot`：真实内容token首末边界已确认的TPOT。

一个文件/一次命令只使用一种定义，analysis_group也不能混时间语义。原始TPOT聚合若不是每请求均值，先重新计算或另建受限接口，不偷换名称。

### 与旧pd_tpot_analysis包的关系

旧包的run_metrics.csv包含本脚本需要的列，可以作为接口来源，但必须确认其生成时遵守原数据契约，并明确真实计时定义。

不能为了复用旧脚本而把响应事件时长假称内容token时长。若只有aisbench事件数据，直接通过本包适配器构造response命名空间的model_runs.csv。

## 3. 脚本准入与排除

结构错误：重复run、重复repeat、未知mode、混组、非法数值、TPOT非正等，脚本失败并输出错误。

模型排除但保留原因：

- run未完整结束；
- 无有效TPOT或有成功请求TPOT未定义；
- 实际输入未知/非固定，或与目标不同且没有实际固定范围证据；
- 实际输出不是所有有效请求都等于目标；
- TPOT缺失。

这些排除是半解析长度模型的适用域，不是删除原始描述结果。被排除格仍必须在coverage、失败率和M0报告中出现。若大量实际输入偏离512，可按真实输入重新审计分组或采用更一般的实际长度模型，不能把观测改成512。

包含失败但有完整有效成功请求的run可以参与条件成功模型；case_means/manifest保留失败计数。必须说明幸存者偏差，不能把模型结果当整体服务可靠性。

## 4. 命令

脚本Python3.9+，只依赖标准库。所有输出目录必须全新，不覆盖旧结果。

### 自测

```text
python -I "<包路径>/scripts/model_cases.py" self-test
```

自测是明确标记的数学夹具，不是真实压测记录。

### 生成80条件计划及空表头

```text
python -I "<包路径>/scripts/model_cases.py" make-plan --out "<全新计划目录>"
```

输出planned_cases.csv、model_runs_header.csv、plan_manifest.json。标记为计划，不能拿来当观测。

### 对已审计aisbench响应TPOT建模

```text
python -I "<包路径>/scripts/model_cases.py" analyze --runs "<model_runs.csv>" --out "<全新模型结果目录>" --metric-semantics aisbench_response_tpot --timing-audited
```

`--timing-audited`仅在字段、单位、公式和起终点确已核实后填写；不是绕过门禁的开关。

有独立内容边界证据时，使用另一份输入文件和新目录，把metric-semantics改为canonical_content_tpot。不要用同一组指标只换标签。

## 5. 输出

| 文件 | 阅读目的 |
|---|---|
| model_manifest.json | 参数、hash、准入/排除、错误、失败、语义与限制 |
| excluded_runs.csv | 哪些run未用于模型及具体原因 |
| case_means.csv | 每条件run等权均值、运行间SD、成功失败计数、来源 |
| model_fits.csv | K0-K3、参数、训练误差、长度留出误差、端点敏感性 |
| length_holdout_predictions.csv | 每次完整长度留出的观测/预测/误差 |
| model_residuals.csv | 按长度检查系统弯曲/拐点 |
| tail_proxy.csv | 不同case累积耗时的割线代理，不是真实后段ITL |
| model_notes.csv | 点数不足或秩不足的跳过原因 |

空表只有no_rows表头，不是0。比例转百分比乘100。预测为负不能截断后伪称模型更好，应报告失配。

## 6. 参数和验证的精确定义

脚本内部：

```text
x_inverse = 512 / (L-1)
x_context = (S + L/2) / 1000
```

输出finite_overhead_ms = 逆长度系数 × 512；context_slope_ms_per_1k是每1000平均上下文token的TPOT相关系数。

这只是数值缩放；a是在context=0和逆长度=0坐标下的回归截距，超出实测条件，不能直接称真实纯decode底噪。

每个C、每个mode及两者gap分别拟合。条件均值等权，非请求池化；不拟合C的连续函数。

- LOO：同一长度所有重复汇成条件点后，整档留出。
- 长输出压力：最大两档长度留出，其他档训练；对完整计划是32768/65536。缺格时是实际可用的最大两档，必须查看预测表，不谎称总是同两档。
- K3至少需5个长度做LOO；留出最大两档后还需足够自由度，否则该压力结果为空。
- 不提供参数CI、接近性检验、真实roofline或服务端调度模拟。
- 不预测未测并发、未测输入、不同模型/硬件；这些需要额外数据和M2校准。

看model_fits时优先LOO和长输出压力误差，再看残差和简单性。不能只挑训练R²最大的模型。
