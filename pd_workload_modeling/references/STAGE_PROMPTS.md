# 分阶段提示词：一次只发送一段

先用README的启动提示词。下面每轮均要求读取SKILL.md、STATE.md和当前阶段附件；保留来源，不依赖对话记忆。

通用约束：仅访问已提供路径；原始只读、内部不外传；未知不猜；没有单独授权不采profile、不重启、不改服务、不跑压测。每阶段写产物、检查门禁、更新STATE.md。

## 0：计划与权限

```text
本轮只执行阶段0。读取METHODOLOGY步骤0及STATISTICS_AND_ACCEPTANCE。
创建PLAN.md，明确80条件是计划值，真实模型/stack尚待审计。
列M0/M1/M2目标、主指标语义、SLO/容差/平台与预测误差标准、重复单位、补测预算、权限。
阈值若未提供写unknown，可继续非依赖工作；不能替我选一个有利阈值当事前约定。
说明一机mixed/两机disagg的资源混杂；分析用的内部模型不代表被测模型。
输出permissions.md和STATE.md；没给数据路径不自行搜盘。
```

## 1：aisbench与stack审计

```text
本轮只执行阶段1。读AISBENCH_PLAYBOOK和SOURCES的A/C部分。
在代码目录做只读版本/配置查询，保存真实失败信息。
确认AISBench版本/commit、推理backend、API适配器、stream、pressure/request_rate/stable_stage组合。
建立时间语义表：请求发送、首/末响应事件、首/末内容token、HTTP结束，逐字段给证据和单位。
明确当前可用的是aisbench_response_tpot还是canonical_content_tpot，不能互换。
输出stack_manifest.json、metric_dictionary.md、workload_semantics.md。
客户端C、平均在途C、真实decode batch分列；缺少server字段写unknown，不编造。
```

## 2：读取真实文件并标准化

```text
本轮只执行阶段2。读AISBENCH_PLAYBOOK第2/3/4/6节和DATA_INTERFACE。
只在授权目录取混部/分离、短/长、高并发少量样例，写inventory和field_mapping。
不要凭记忆套JSON结构。若为JSONL+DB，验证引用、schema和路径，SQLite只读，NumPy allow_pickle=False；不运行原目录脚本。
统计80格覆盖、独立重复、失败/超时/取消/未完成/重试/预热。
核对实际InputTokens/OutputTokens，目标不等于实际；65536输出要校验总context容量。
为真实格式编写转换器及测试，逐条抽查秒到毫秒和TPOT重算，对比summary并解释差异。
生成标准化request/run表、quality_report.md；model_runs.csv按接口构造，不能填假0过门禁。
只有汇总数据时保留受限M0，不伪造逐请求记录。
```

## 3：重建负载画像

```text
本轮只执行阶段3。读METHODOLOGY步骤3和AISBENCH_PLAYBOOK第5节。
按真实start/end重建客户端在途和实际派发间隔，没时间戳不补造。
识别burst/closed_loop/open_loop/unknown及请求总数，不以C代替请求数。
特别检查长输出是否降低补请求频率、是否改变prefill/decode比例。
说明full-request群组和stable事件窗口的分子分母，不把跨窗口半段当完整请求。
对有数据者做生命周期面积/窗口一致性检查；不能反算出缺失queue/batch。
输出workload_signatures.csv、measurement_window.md，写尚不能辨识的负载参数。
```

## 4：运行状态地图

```text
本轮只执行阶段4。读METHODOLOGY步骤4和STATISTICS第1/7节。
每个C沿L、每个L沿C列两部署的TTFT/TPOT/E2E、真实重复、实际长度和失败。
可信Duration才算系统吞吐；不要用1/TPOT。正确区分单请求吞吐和系统总吞吐。
有明确SLO才算goodput，分别报告不可判断和失败，不通过删样本提高达标率。
资源归一化使用实际机器/NPU数量，TPOT不除2。
输出operating_map.csv、regime_notes.md；图旁保留质量和失败信息。
只标已测候选最佳点，不宣称全局最优并发。
```

## 5：可辨识性与模型计划

```text
本轮只执行阶段5。读METHODOLOGY步骤5/6和SOURCES的B/C部分。
对每个待估参数写：已有证据、混杂、可否估计、最小补充、允许措辞。
固定输入512不能拟合prefill长度缩放；C不能替代实际batch；TPOT不能唯一拆HBM/计算/通信。
确定先M0再M1，M2只有真实校准才进入。
冻结K0/K1/K2/K3竞争、逐长度LOO、最大两档压力留出及误差预算。
不训练80格黑盒模型冒充结构识别，不抄GPU带宽、经验常数。
输出identifiability.md、model_plan.md。缺少预测预算只做探索性误差报告。
```

## 6：执行候选拟合与验证

```text
本轮只执行阶段6。读DATA_INTERFACE和STATISTICS第6节。
先运行model_cases.py self-test；再审计model_runs.csv是否满足固定真实长度、输入、完整run和均值口径。
核实后以正确--metric-semantics和--timing-audited运行analyze，输出新目录。
按C分别读mixed/disagg/gap的K0-K3：训练误差、LOO、长输出留出、残差、端点敏感性。
不能只看R²；不能因负预测而截断改善评分；模型失配就承认。
解释tail_proxy仅是不同case割线，不是真实后段ITL。
所有排除run保留在原始M0和覆盖报告；失败/输入偏差不能消失。
输出model_validation.md，说明哪些解释竞争、哪些模型不适用。此脚本无CI、无跨C预测、无因果结论。
```

## 7：推论与接近性

```text
本轮只执行阶段7。读METHODOLOGY步骤7和STATISTICS全篇。
写H1-H5假设卡，分别列前提、对照、反例、判定、当前证据、最小补测。
按真实独立run/块处理不确定性；重复不足只描述。若实现bootstrap按协议固定seed/B并编写测试。
不要将请求或token当独立重复；配对必须有设计证据，不能按排序凑。
长输出接近性用明确容差和CI；事后阈值标探索性；P不显著不是等效。
TPOT下降、差距缩小、进入容差、TPOT平台是四个不同答案，逐C回答。
无prefill事件不能写机制证实；输出hypothesis_cards.md和evidence_matrix.csv。
```

## 8：生成采集与校准计划

```text
本轮只执行阶段8的计划部分，不执行真实采集。
读PROFILING_AND_OPTIMIZATION和METHODOLOGY步骤8。
根据最能区分竞争解释的症状选少量哨兵，优先D长decode的context/KV/抢占及handoff。
确认实际stack/version后选择vLLM服务端点、框架profiler、CANN attach或仅系统采样；禁止万能命令。
明确服务启动前准备、P/D实际端口/rank、时间域、早中后context窗口、停止和回滚、profile扰动对照。
常规初始P→D KV主要由输入prompt决定，不把65536输出当初始传输量；首token由P返回时检查handoff是否落入TPOT。
若做M2，优先审计匹配的msModeling支持矩阵与A2画像；列独立microbench和留出验证，不自动寻优。
输出profile_plan.md、calibration_plan.md及权限请求。没有授权就明确未执行。
```

## 9：优化卡与优先级

```text
本轮只执行阶段9。依据已有真实证据写optimization_cards.md。
每项列具体症状/文件、竞争解释、参数同版来源、唯一改动、回滚、主收益、护栏和回归格。
顺序优先发生器限制、容量/抢占、队列/调度，再实际kernel/访存/通信关键路径。
input512/output64k不是长prefill；没干扰证据不先盲调chunk，没真实MoE/MLA证据不套相关开关。
没有测得关键路径分量占比不编收益百分比；没有SLO只做Pareto备选。
输出prioritized_actions.csv，标注待证实/待授权/已执行，默认不执行。
```

## 10：优化验证计划与结果

```text
本轮执行阶段10，先检查真实压测/改配置权限。未授权只交付ab_plan.md，不运行。
若获授权：单变量A/B，冻结baseline、相同输入/输出/发压/时间语义，随机/真实配对重复。
正式性能无profiler，先目标格再短长/低高C/异常回归，预算允许再全部80格。
比较正确性、失败率、TTFT/TPOT/E2E尾部、吞吐和SLO，不只看平均TPOT。
改变并发额度要区分运行点重选与同负载加速。严禁截断语义算无损优化。
按统计协议给收益和护栏，不足则证据不足，失败则回滚。
输出ab_results.csv、regression_matrix.csv、decision.md；没有执行不填虚构结果。
```

## 11：最终报告

```text
本轮只汇总已产生的产物，不新增未经验证的性能结论。
report.md包括：逐C结论、真实覆盖/失败/长度、指标和资源边界、M0/M1/M2状态、留出误差、反例、优化接受/拒绝/不足/未执行。
从最不支持用户推论的格点反向检查；无法支持就明确说无法支持。
每个数字追溯run/source/脚本，每个参数建议追溯同版官方来源。
列当前80条件不能回答的问题、最小补测、全部复现命令和权限边界。
不要把已生成脚本的自测当真实压测通过，不把建议写成已实现收益。
更新STATE.md，列已完成与仍阻塞的范围。
```
