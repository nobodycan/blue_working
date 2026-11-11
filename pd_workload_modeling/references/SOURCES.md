# 业界来源、可迁移实践与版本边界

检索/核验日期：2026-11-11。研究通过 Exa 的三路 Opus 资料核查及主线程补充检索完成；搜索共返回80条候选结果（含重复/不合格结果），这不是80个已逐页核验的独立来源数量。

优先官方代码、作者论文和产品版本化文档。第三方概述不作为字段、接口或收益数字的最终依据。本文只列实际用于方法设计的来源；不把这些项目的GPU实验收益移植为A2预期收益。

## A. aisbench：以实际安装版本和源码为准

### S01 官方项目与安装身份

- https://github.com/AISBench/benchmark
- https://ais-bench-benchmark.readthedocs.io/zh-cn/latest/
- https://raw.githubusercontent.com/AISBench/benchmark/d99493ac1914ff41415196eece58f8f296f9e2d6/setup.py

核验：本次查看的固定源码版本为 `d99493ac1914ff41415196eece58f8f296f9e2d6`；setup.py 中distribution名称为 `ais_bench_benchmark`，CLI为 `ais_bench`。这不是用户实际安装版本的认定，也不能与其他同名离线工具混同。

采用：先锁版本、配置与真实产物，再选择解析分支。

### S02 指标推导：不是凭名称解释

- https://raw.githubusercontent.com/AISBench/benchmark/d99493ac1914ff41415196eece58f8f296f9e2d6/ais_bench/benchmark/utils/perf_details.py
- https://raw.githubusercontent.com/AISBench/benchmark/d99493ac1914ff41415196eece58f8f296f9e2d6/ais_bench/benchmark/summarizers/default_perf.py
- https://ais-bench-benchmark.readthedocs.io/zh-cn/latest/base_tutorials/results_intro/performance_metric.html

固定版本源码：

```text
latency = time_points[-1] - time_points[0]
ttft = time_points[1] - time_points[0]
tpot = (latency - ttft) / (output_tokens - 1)，仅output_tokens>1
itl = diff(time_points[1:])
```

推导函数返回秒；报告层做毫秒转换。共享函数对不可用TPOT返回None，而default summarizer可转换为0；本地分析对L<2应标记未定义，不把0当真实快速生成。

采用：保留aisbench响应事件指标及版本，不冒充真实token-step或服务端纯计算。

### S03 响应事件打点与内容边界

- https://raw.githubusercontent.com/AISBench/benchmark/d99493ac1914ff41415196eece58f8f296f9e2d6/ais_bench/benchmark/models/api_models/base_api.py
- https://raw.githubusercontent.com/AISBench/benchmark/d99493ac1914ff41415196eece58f8f296f9e2d6/ais_bench/benchmark/models/api_models/vllm_custom_api_chat.py

所查链路按重组后的SSE响应事件记录时间，排除空事件和[DONE]，不是每个TCP包或每个真实token。角色事件、usage事件、一事件多个token会影响边界。具体后端必须核验；不能声称所有版本全部相同。

采用：将客户端event ITL和真实token ITL分开；不按chunk内token数均分时间来伪造精细trace。

### S04 detail与数据库的成套保存

- https://raw.githubusercontent.com/AISBench/benchmark/d99493ac1914ff41415196eece58f8f296f9e2d6/ais_bench/benchmark/openicl/icl_inferencer/output_handler/base_handler.py
- https://raw.githubusercontent.com/AISBench/benchmark/d99493ac1914ff41415196eece58f8f296f9e2d6/ais_bench/benchmark/openicl/icl_inferencer/output_handler/db_utils.py
- https://raw.githubusercontent.com/AISBench/benchmark/master/docs/source_zh_cn/advanced_tutorials/stable_stage.md

固定版本实现使用 `performances/.../*_details.jsonl`，并以 `db_name`、`__db_ref__` 引用 `db_data/*.db` 中的NumPy数组。SQLite表为numpy_store，数组BLOB以np.save保存、np.load(..., allow_pickle=False)读取。这里不是独立.npy文件。

官方部分文档仍有旧JSON/H5示例；本次未确定所有旧release的schema与迁移点，因此旧数据按实际文件核对。只保存CSV或JSONL可能丢失请求时序。读取用户DB应使用只读SQLite，而不是原样调用会建表的官方init_db。

### S05 并发、RPS、pressure与稳态

- https://raw.githubusercontent.com/AISBench/benchmark/d99493ac1914ff41415196eece58f8f296f9e2d6/ais_bench/benchmark/openicl/icl_inferencer/icl_base_api_inferencer.py
- https://raw.githubusercontent.com/AISBench/benchmark/d99493ac1914ff41415196eece58f8f296f9e2d6/ais_bench/benchmark/tasks/utils.py
- https://raw.githubusercontent.com/AISBench/benchmark/d99493ac1914ff41415196eece58f8f296f9e2d6/ais_bench/benchmark/calculators/base_perf_metric_calculator.py
- https://raw.githubusercontent.com/AISBench/benchmark/d99493ac1914ff41415196eece58f8f296f9e2d6/ais_bench/benchmark/calculators/stable_perf_metric_calculator.py

所查普通链路batch_size是客户端最大在途请求额度；默认报告Concurrency可由请求生命周期占用面积/测量时长计算，Max Concurrency是配置上限。request_rate还受并发闸门和发生器影响。

`--pressure`为固定槽完成后继续补请求的语义；其初始增槽节奏不能直接当后续恒定到达率。stable_stage改变统计选段，不提供服务端阶段观测。

采用：配置C、平均在途C、active decode batch、请求率分列；total/stable窗口不混算。源码与文档存在RPS阈值和分布表述差异，不将某个默认值写成跨版本公理。

### S06 真实输入与输出

- https://ais-bench-benchmark.readthedocs.io/zh-cn/latest/advanced_tutorials/synthetic_dataset.html
- https://ais-bench-benchmark.readthedocs.io/en/latest/base_tutorials/scenes_intro/performance_benchmark.html
- https://ais-bench-benchmark.readthedocs.io/en/latest/base_tutorials/all_params/models.html

合成string长度不等于token数；tokenid转字符串再tokenize也可能变化；chat模板可增加输入。max_out_len是生成上限，ignore_eos透传且依赖服务支持，不能保证超容量请求完整生成。

采用：以实际token数核对512和各输出档，最大上下文至少容纳66048实际token；模板/特殊token还要计入。

## B. 负载建模与PD：学流程，不抄硬件数值

### S07 Vidur

- https://github.com/microsoft/vidur
- https://arxiv.org/abs/2405.05465

可迁移：目标算子微基准、shape特征、通信校准、调度事件模拟、与真实端到端结果验证。

不能直接迁移：GPU性能表、默认context覆盖、GPU调度残差。已有profile不证明A2或约66k context精度。接近容量时，服务时间的小误差可被排队放大。

### S08 AIConfigurator

- https://github.com/ai-dynamo/aiconfigurator
- https://github.com/ai-dynamo/aiconfigurator/blob/main/collector/README.md
- https://arxiv.org/html/2601.06288v1

可迁移：按hardware/backend/version管理执行性能数据；长context分段计算；PD资源速率匹配；候选搜索后真实部署验证。

不能直接迁移：NVIDIA性能库、经验修正因子、链路参数。所查README已提示向AISimulate维护迁移，应固定release/commit和数据库，不把不同年代混用。它不构成本实验已获A2支持的证明。

### S09 DistServe

- https://www.usenix.org/system/files/osdi24-zhong-yinmin.pdf
- https://github.com/LLMServe/DistServe

可迁移：TTFT/TPOT SLO约束下goodput；P/D阶段隔离；资源、并行与网络拓扑联合考虑。

边界：论文设备上的prefill饱和长度、通信可忽略条件不能照搬A2。文中的简化排队模型有到达/服务假设，不适用于任意闭环并发测试。

### S10 Splitwise

- https://www.microsoft.com/en-us/research/wp-content/uploads/2023/12/Splitwise_ISCA24.pdf
- https://github.com/mutinifni/splitwise-sim
- https://github.com/mutinifni/splitwise-sim/blob/main/performance_model.py

可迁移：P→KV交接→D的生命周期，性能表驱动、到达trace与内存/网络约束。

边界：原性能预测器含简化mixed修正，不应当A2常数；未显式覆盖当前历史context分布的实现不能原样外推长decode。

### S11 Sarathi-Serve

- https://www.usenix.org/system/files/osdi24-agrawal.pdf
- https://github.com/microsoft/sarathi-serve
- https://raw.githubusercontent.com/microsoft/sarathi-serve/main/sarathi/core/scheduler/sarathi_scheduler.py

可迁移：decode/prefill真实调度、token budget、chunk与抢占需要入模型；混合迭代不是独立阶段简单相加。

边界：本实验输入512是短prefill；输出65536不会将其变成长prefill。chunk预算不是默认第一优化项，需先看到实际插入干扰证据。

### S12 GuideLLM（补充测量方法，不替换aisbench）

- https://vllm-project.github.io/guidellm/0.8.0/guides/metrics/
- https://vllm-project.github.io/guidellm/stable/reference/guidellm/schemas/benchmark/goodput/

可迁移：明确measurement window、派发延迟、scheduled latency、SLO attainment与goodput。官方明确自身“Time Per Output Token”可能含首token，而SLO的tpot使用另一口径，再次说明跨工具不能只按名称对齐。

采用：aisbench仍是测评入口；没有SLO就不编造goodput，没有计划到达记录就不能补出隐含等待。

## C. A2：首选匹配的昇腾工具链

### S13 msModeling

- https://github.com/Ascend/msmodeling
- https://github.com/Ascend/msmodeling/blob/master/README.md

官方README列出A2设备画像、分阶段推理/服务化PD仿真、Roofline/校准解析/Profiling模式及独立微基准相关能力。

采用：M2优先评估其实际模型/特性/后端/设备画像是否匹配，作为A2本地结构模型候选；固定版本并独立校准、留出验证。仿真输出不是本次实测结论。其真实服务自动寻优会启动或修改服务，默认不在分析权限内。

尤其注意：用于分析的内部DeepSeek模型，不等于被测服务一定是DeepSeek；目标模型必须从实验配置确认。

### S14 vLLM-Ascend服务采集

- https://docs.vllm.ai/projects/ascend/en/v0.22.1rc/developer_guide/performance_and_debug/service_profiling_guide.html
- https://docs.vllm.ai/projects/ascend/en/latest/developer_guide/performance_and_debug/service_profiling_guide.html

v0.22.1rc官方指南确认：`--profiler-config`配置后，以`POST /start_profile`、`POST /stop_profile`控制算子采集，输出ascend_pt，再离线分析。旧环境变量和新版配置不可不查版本就混用。

同一指南区分Ascend PyTorch Profiler算子级采集与MS Service Profiler框架级采集，后者可输出request/kvcache/batch CSV。latest补充P/D需各自配置，特定代理不转发profiling接口，应直接连接真实节点。

采用：采真实worker，不包裹aisbench客户端冒充服务端profile；MindIE不能照搬这些HTTP端点。

### S15 CANN动态采集：不是任意进程随时attach

- https://www.hiascend.com/doc_center/source/zh/canncommercial/82RC1/devaids/Profiling/atlasprofiling_16_0016.html
- https://www.hiascend.com/doc_center/source/zh/canncommercial/82RC1/devaids/Profiling/atlasprofiling_16_0011.html
- https://www.hiascend.com/doc_center/source/zh/canncommercial/82RC1/devaids/Profiling/atlasprofiling_16_0012.html

CANN商用8.2.RC1支持launch与attach；attach要求应用启动前设置PROFILING_MODE=dynamic。该版动态采集与delay/duration互斥，且多卡/PID能力有限；其他版本核查自己的--help。

任务级、系统级采集提供不同信息，msprof命令行不等于PyTorch调用栈采集。系统内存占用、带宽、算子访存和KV逻辑容量不是同一指标。

### S16 msprof-analyze / Insight / msProbe的分工

- https://github.com/Ascend/msprof-analyze
- https://msinsight.readthedocs.io/zh-cn/latest/
- https://github.com/Ascend/msprobe

msprof-analyze分析已采profiling；Insight查看时间线/算子/通信；msProbe主要用于精度采集比对，不能替代延迟/吞吐profile。分析器不能补出根本没采的事件或硬件计数器；内部trace/张量不外传。

### S17 MindIE监控与PD版本分支

- https://www.hiascend.com/developer/techArticles/20250327-1
- https://www.hiascend.com/doc_center/source/zh/mindie/21RC2/mindiellm/llmdev/mindie_llm0299.html
- https://www.hiascend.com/document/detail/zh/mindie/10RC3/mindieservice/servicedev/mindie_service0144.html

官方资料涉及服务监控、running/waiting/KV/抢占指标和P/D调配，但跨1.0.RC3、2.1.RC2与其他版本有显著特性限制。仅以用户安装版schema与支持矩阵为准，不把旧版限制说成全版本限制，也不把MindIE配置项翻译成vLLM万能参数。

### S18 不应默认应用的“优化特性”

- https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/dynamic_batch.html
- https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/dynamic_chunk_pipeline_parallel.html
- https://www.hiascend.com/doc_center/source/zh/mindie/230/mindiellm/llmdev/mindie_llm0536.html

各特性有设备、模型、长度、PD模式或其他特性互斥限制。长输入P节点的优化，不等于本实验短输入长输出的优化；有限长度验证不等于65k输出可用。先审计，再单变量测试。

## D. 本包自己的方法，不冒充业界现成结论

以下是本任务的分析设计：K0/K1/K2/K3竞争模型、逐长度留出、最大两档压力留出、跨case tail_proxy、假设卡与优化验收门禁。

这些是可复现的候选分析方法，不是“业界已证明本实验TPOT会下降”的引用证据。它们必须由用户真实数据、真实重复及追加采集验证。
