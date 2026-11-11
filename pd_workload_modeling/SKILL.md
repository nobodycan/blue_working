---
name: ascend-aisbench-workload-modeling
description: 对昇腾A2的一机PD混部/两机PD分离aisbench实验做逐阶段负载画像、实测校准建模、假设检验、瓶颈定位和优化验证。先核查版本/指标/真实长度，再比较竞争模型，禁止将拟合或GPU参数冒充A2因果证据。
---

# Ascend / aisbench 负载建模 Skill

## 固定任务与不确定项

计划：input512；output512/1024/2048/4096/8192/16384/32768/65536；C1/4/8/16/32；mixed一机，disagg两机，共80个条件，重复另计。

这是计划，不是确认观测。实际模型、框架、卡数、精度、请求总数和负载方式必须从配置核实。用于分析的内部模型不是被测模型的证据。数据不在当前目录时不自行搜盘。

## 不可违反

1. 每次只执行一个阶段；先保存产物并检查门禁，再进入下一阶段。非阻塞未知只限制依赖它的结论。
2. 不猜字段、单位、默认值或参数。源码和文档冲突时按实际安装版本核对，保存冲突。
3. aisbench响应事件TPOT与真实内容token TPOT分开；chunk不等于token，客户端TTFT不等于服务端prefill。
4. C不是实际batch；配置RPS不是实际RPS；pressure/total/stable不能混为相同负载和窗口。
5. 一机/两机只能直接比较部署方案，不将全部差异归因为PD架构。延迟不除以机器数。
6. 不为了证明假设挑数据、删异常、隐藏失败、伪造重复或置信区间。
7. 原始数据只读。代码目录与原始目录分开；Python读取外来数据用 `python -I`。不得执行原始目录内程序或打开pickle载荷。
8. 不上传内部数据/trace，不自动联网安装，不自动改服务、重启、采profile或重跑压测。给建议不等于有执行权限。
9. 8个长度点使用少参数竞争模型；验证按完整长度分组，不随机拆请求制造泄漏。
10. 拟合不等于瓶颈定位；利用率不等于roofline；A2参数必须独立测量或有匹配版本画像。
11. 新策略必须单变量A/B及正确性/失败率/尾延迟护栏，不以截断上下文或输出语义换“优化”。
12. 新增推论一律写假设卡，必须允许“反对/证据不足”。所有数学公式使用纯文本。

## 必要附件

- `references/METHODOLOGY.md`：主方法，按当前阶段读对应章节。
- `references/AISBENCH_PLAYBOOK.md`：aisbench版本、detail、事件指标、到达语义。
- `references/DATA_INTERFACE.md`：标准化接口与脚本准入。
- `references/STATISTICS_AND_ACCEPTANCE.md`：重复、区间、SLO、模型和优化验收。
- `references/PROFILING_AND_OPTIMIZATION.md`：A2采集分支与优化卡。
- `references/SOURCES.md`：官方证据与迁移边界。
- `references/STAGE_PROMPTS.md`：逐条执行提示词。

先读本文件和README，再读取当前阶段附件；不要一次把全部原始日志放进上下文。附件缺失就索取，不凭空补全。

## 阶段0：计划与权限

输入：用户目标和已知配置。
操作：冻结PLAN.md、目标/SLO/容差来源、主指标、假设、资源比较边界、权限。
输出：PLAN.md、permissions.md、STATE.md。
门禁：无数据路径先不查数据；无SLO可描述但不能宣称业务最优。

## 阶段1：stack和aisbench语义

操作：版本/commit/容器/最终配置；确认真实推理栈；按playbook区分total/stable/pressure、stream/chunk/token、并发/RPS/请求数。
输出：stack_manifest.json、workload_semantics.md、metric_dictionary.md。
门禁：未知时间语义不能赋予物理意义；不要套用最新版CLI到旧环境。

## 阶段2：盘点、映射和质量

操作：读取少量样例；明确summary/detail/DB对应；字段逐项有证据；只读解引用；去重；保留失败/重试/预热/未完成；确认实际输入输出；覆盖80格。
输出：inventory.csv、field_mapping.md、coverage.csv、quality_report.md、标准化请求和运行表。
门禁：结构/单位/重复/计数错误未解决不发布相关结果；只有summary就保持受限，不伪造请求。

## 阶段3：负载画像

操作：重建可得的发送/完成与在途曲线；记录实际RPS、闭环补位、窗口和prefix缓存；服务端batch缺失就未知。
输出：workload_signatures.csv、measurement_window.md。
门禁：只知道C时只建立系统响应，不能声称建模了真实kernel batch。

## 阶段4：运行状态地图

操作：每C沿L、每L沿C比较；TPOT/TTFT/E2E/成功率/实际长度；可信窗口的吞吐；有SLO再goodput；报告每机/每NPU效率的真实资源分母。
输出：operating_map.csv、regime_notes.md、图表及对应CSV。
门禁：无时长不填吞吐，无SLO不填goodput，不用1/TPOT当系统吞吐。

## 阶段5：可辨识性审计

操作：每个参数列数据支持与混杂；区分M0经验、M1半解析、M2结构/模拟。固定输入512不能识别prefill长度缩放；C不能识别batch；总延迟不能拆硬件代价。
输出：identifiability.md、model_plan.md。
门禁：未知参数不以拟合数字替代证据；GPU性能表不直接迁移A2。

## 阶段6：竞争模型和留出验证

操作：按接口生成model_runs.csv；核实metric_semantics；运行附带脚本自测和分析。每C分别建mixed/disagg/gap，比较K0常数、K1逆长度、K2上下文、K3组合；看LOO/最大两档留出、残差和端点敏感性。
输出：模型表、预测表、残差、tail_proxy、model_validation.md。
门禁：脚本不提供CI或因果；tail_proxy不是实际后段ITL；跨C预测不在此脚本能力内。

## 阶段7：逐推论判定

操作：按方法论H1-H5写假设卡；分并发给支持/反对/不足；接近性和TPOT平台分别判定；按统计协议处理真实重复和容差来源。
输出：hypothesis_cards.md、evidence_matrix.csv。
门禁：不能以差异不显著证明等效；不能以截距点估计证明极限。

## 阶段8：按症状补服务端证据

操作：只生成采集计划；若另获授权，按实际stack/version执行有限窗口。分别采P/D与rank、对齐时钟、确认真实context；独立校准A2算子/内存/通信。
输出：profile_plan.md、trace_manifest.csv、bottleneck_evidence.md、calibration_table.csv。
门禁：没采就不写已观测；profile客户端不等于profile服务端；没有预置的服务不能假设随时attach。

## 阶段9：优化候选与排序

操作：每候选写证据、竞争解释、支持版本、单个改动、回滚、收益指标、护栏、复测格；先证据最强风险最低的项，不列无依据万能参数。
输出：optimization_cards.md、prioritized_actions.csv。
门禁：无授权只建议；不承诺未测收益；不混用vLLM/MindIE参数。

## 阶段10：A/B和回归

操作：冻结baseline；相同负载随机/配对重复；无profiler正式测量；目标格后再做短/长、低/高C和异常回归；检查正确性、失败、尾延迟、吞吐和SLO。
输出：ab_plan.md、ab_results.csv、regression_matrix.csv、decision.md。
门禁：未执行就写未执行；仅平均TPOT变好不足以验收；生产最优需要真实生产负载。

## 阶段11：报告与反向核查

输出report.md：逐C结论、数据/资源边界、模型留出误差、证据等级、反例、优化状态、最小补测、来源和复现命令。
必须从最不支持用户推论的条件反向检查。逐数追溯run和脚本。

## 每阶段固定回复

```text
阶段：
输入及来源：
执行动作：
文件产物：
门禁（通过/受限/失败）：
最不支持当前解释的证据：
未知/阻塞及影响：
是否执行过真实压测/采集/改配置：
下一步：
```

STATE.md必须保存已完成阶段、已确认定义、排除项、当前模型/参数来源、未解决项、下一条命令。换会话先读状态，不依赖隐含记忆。
