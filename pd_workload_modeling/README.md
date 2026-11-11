# A2 / aisbench AI负载建模与优化分析包

## 这是对前一份TPOT分析的扩展

本包针对同一实验：input512；output512/1024/2048/4096/8192/16384/32768/65536；C1/4/8/16/32；mixed一机、disagg两机。

前一包聚焦TPOT差距与摊薄；本包扩展为：

```text
负载语义与数据审计
→ 系统响应面
→ 竞争模型与留出预测
→ A2服务端瓶颈证据
→ 优化候选
→ 单变量A/B与回归验收
```

没有读取真实压测数据、连接A2服务器、执行profiler、修改推理服务或生成真实收益结论。模板是计划，不是观测。

## 先读这五条结论

1. **80个条件足以做当前部署范围的经验建模，不足以唯一识别硬件瓶颈。** 固定输入512不能识别prefill长度缩放，C不能代替真实batch，TPOT不能唯一拆HBM/计算/通信。
2. **aisbench标准流式打点是响应事件，不保证一个事件一个token。** 原TPOT可以合法比较客户端体验，但不能不加审计地称纯decode-step时间。
3. **输出变长有两个竞争趋势。** 有限开销按1/(L-1)降低；decode的context同时变长。应比较K0常数、K1摊薄、K2上下文、K3组合，而非只拟合下降曲线。
4. **PD初始KV交接通常由输入prompt决定。** 不应把65536输出当作一次65536-token的P→D传输。首token若由P返回，交接还可能主要影响首、次输出间隔。
5. **优化必须由证据驱动，并回到aisbench独立实测。** GPU模拟器参数不能直接迁移A2；匹配的msModeling可作A2结构模型候选，但仍需实际模型/后端/画像校准与留出验证。

## 文件导航

| 文件 | 用途 |
|---|---|
| [SKILL.md](SKILL.md) | 主Skill，12阶段的执行规则和门禁 |
| [METHODOLOGY.md](references/METHODOLOGY.md) | 完整方法论，每步操作、产物、条件与反例 |
| [AISBENCH_PLAYBOOK.md](references/AISBENCH_PLAYBOOK.md) | aisbench版本、detail/DB、指标与发压语义 |
| [DATA_INTERFACE.md](references/DATA_INTERFACE.md) | 标准化输入、模型脚本、准入和命令 |
| [STATISTICS_AND_ACCEPTANCE.md](references/STATISTICS_AND_ACCEPTANCE.md) | 独立重复、区间、接近性、SLO与优化验收 |
| [PROFILING_AND_OPTIMIZATION.md](references/PROFILING_AND_OPTIMIZATION.md) | vLLM-Ascend/MindIE/CANN分支、结构模型与优化卡 |
| [SOURCES.md](references/SOURCES.md) | 官方代码/论文/文档及可迁移边界 |
| [STAGE_PROMPTS.md](references/STAGE_PROMPTS.md) | 每轮可直接粘贴给内部模型的提示词 |
| [model_cases.py](scripts/model_cases.py) | 标准库竞争模型、长度留出与计划生成脚本 |
| templates/ | 80条件计划、空CSV表头与计划元数据，非测量数据 |

## 采用的业界实践

| 来源 | 借鉴什么 | 不照搬什么 |
|---|---|---|
| AISBench官方代码 | 原始打点、统计口径、真实负载模式 | 猜测schema、将chunk当token |
| Vidur | 微基准校准、shape性能表、事件模拟 | GPU参数与默认context覆盖 |
| AIConfigurator | 硬件/后端/版本性能数据库、PD候选搜索 | NVIDIA性能表与经验修正系数 |
| DistServe / Splitwise | P/D生命周期、网络约束、SLO goodput | 不同硬件上的容量与通信结论 |
| Sarathi-Serve | 实际调度、chunk预算与抢占 | 将长输出误当长prefill、万能chunk参数 |
| msModeling / 昇腾Profiler | A2结构模型、独立校准、服务/算子观测 | 未验证模型支持或未采计数器的结论 |
| GuideLLM | 窗口、派发延迟与goodput定义意识 | 强行替换aisbench、只按指标名对齐 |

出处及固定版本代码链接见SOURCES.md；本包自定义的竞争模型和验收流程不是业界已证明本实验结论的证据。

## 推荐执行方式

1. 将整个文件夹复制到内部分析环境，保持引用目录结构。
2. 原始数据放在文件夹之外，脚本与工作目录也不要放进原始数据目录。
3. 发下面启动提示词，只执行阶段0/1；随后按STAGE_PROMPTS逐阶段推进。
4. 每阶段保存STATE.md；换会话时只重读状态与本阶段附件。
5. 先审计真实aisbench结构，再编写转换器。附带脚本不猜原始JSON。
6. 有服务端权限和证据需求时才进入M2或优化执行；否则完整交付M0/M1与待补清单。

### 启动提示词

```text
严格按本分析包执行，不为迎合我的假设挑数据。

分析包：<绝对路径>
原始aisbench输出：<实际绝对路径，未提供则写unknown>
运行命令/配置：<实际路径或unknown>
新工作产物目录：<与原始数据分开的新目录>

计划条件：A2；输入512；输出512/1024/2048/4096/8192/16384/32768/65536；C1/4/8/16/32。
一机mixed与两机disagg。目标是负载建模、检验推论、定位瓶颈并制定优化验证，不预设推论成立。

先读README和SKILL.md，然后按当前阶段读取附件。
本轮只执行阶段0和阶段1。未知字段/单位/配置不能猜；不要自动搜盘。
保留aisbench响应事件与真实内容token两种语义；用于分析的模型不是被测模型。
内部数据不外传，不擅自安装，不采profile、不重启、不改服务、不跑压测。

每阶段写产物、门禁、反例/未知及下一步，更新STATE.md。
缺少某项只限制依赖它的分析，不要等所有资料齐全才做其他工作。
```

不需要内部平台原生支持Skill；也可以作为文件附件和阶段提示词使用。

## 本地脚本

Python3.9+，只依赖标准库。脚本不安装依赖、不访问网络、不调用模型API。

### 自测

```text
python -I "<包路径>/scripts/model_cases.py" self-test
```

### 生成计划

```text
python -I "<包路径>/scripts/model_cases.py" make-plan --out "<全新计划目录>"
```

### 对标准化响应TPOT建模

```text
python -I "<包路径>/scripts/model_cases.py" analyze --runs "<model_runs.csv>" --out "<全新模型结果目录>" --metric-semantics aisbench_response_tpot --timing-audited
```

timing-audited是事实标记，必须完成字段/单位/计时边界审计才能使用。

脚本会输出每条件均值、K0-K3、逐长度留出、最大两档留出、残差、端点敏感性和跨case耗时割线代理。**不自动生成CI，不证明因果，不预测未测C，不生成优化收益。** 统计推断按独立重复/时间块另行执行协议。

所有输出目录必须全新；空结果不填0；被模型排除的格点仍在描述/失败/覆盖报告中保留。

## 应交付的最终结果

- 当前80条件内的部署性能与负载状态地图。
- 每个并发下支持/反对/不足的推论判定。
- 经完整长度留出检验的模型，或模型失配的明确说明。
- 有证据等级和适用版本的瓶颈/优化卡。
- 已授权且实际执行才有的A/B、回归与收益结论。
- 数据、模型、资源、指标口径、适用域和不可辨识项。

原始数据尚未提供，因此以上是流程和工具，不是对A2实测结果的预判。
