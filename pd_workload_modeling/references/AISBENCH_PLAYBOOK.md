# aisbench 专用执行手册

来源与固定代码链接见 SOURCES.md 的S01-S06。本文件是执行指南，不认定用户当前版本等于检索版本。

## 1. 首先回答六个问题

```text
Q1：安装的是AISBench/benchmark的哪版/commit？
Q2：原始命令、最终配置、模型/数据集配置分别在哪里？
Q3：流式还是非流式，使用什么API适配器？
Q4：普通数据集、pressure、timestamp或其他模式？
Q5：total还是stable_stage统计，发出/结束窗口是什么？
Q6：结果实际是哪一代：汇总CSV/JSON、JSON/H5、JSONL/SQLite或其他？
```

先记录答案和证据，不根据扩展名直接选解析器。旧格式未验证schema时不要猜。

## 2. 推荐保全的文件

保留完整实验输出目录及配置，而不是只拿一张CSV：

- 原始启动命令和配置快照、版本/commit/容器摘要。
- 汇总结果与控制台错误。
- 逐请求detail；若有db_name引用，连同db_data对应数据库。
- 异常终止时的tmp缓存（保留原貌，不能当成全部成功）。
- 模型/tokenizer标识、实际usage计数和输入生成方式。

所查固定版本下，性能detail自动落盘，`--dump-eval-details`不是打开性能明细的通用必要开关。不要为了收集detail误用精度测评选项。

2026新增的批量并发CLI不能假定旧版可用；本任务80格清单由本包独立生成，不依赖该功能。

## 3. 若真实产物为JSONL + SQLite

只在字段和schema与已核查版本一致时走本分支。

### 安全且可审计的读取顺序

1. 流式读JSONL，保存文件名和行号，不一次载入全部文本。
2. 对每条记录保存data/id/uuid/turn等真实标识，具体字段按样例决定；不要任选一个id就假定全局唯一。
3. 读取db_name，解析后必须位于该run允许的db_data目录内；拒绝绝对路径和`..`越界。
4. SQLite用只读URI打开，先查询sqlite_master/表结构；不要调用会CREATE TABLE或创建空DB的初始化函数。
5. 核实numpy_store等真实表结构，再SELECT所需id；缺失引用记为缺失/未完成，不补0数组。
6. NumPy载入BLOB使用allow_pickle=False，限制数组大小并验证是一维有限数值时间数组；不要执行任何来自数据的代码。
7. 时间点数量、顺序和真实单位检查后，按本版公式重算指标。
8. 每条派生结果保留jsonl行号、db_name、引用id和转换版本。
9. 失败记录、数据库未提交、被杀进程缓存单列；不悄悄跳过。

解析器放原始目录之外，Python使用-I。需要numpy但环境没有时先报告，不自动联网安装。只处理数据，不执行原始目录内的setup.py或其他程序。

### 最小测试

- 内联数组与DB引用（仅真实版本存在的结构）。
- 合法引用、缺失DB、缺失id、越界db_name。
- 重复请求、失败请求、不完整时间数组。
- 秒→毫秒、O=1、时间倒置、NaN/Inf。
- 同时包含角色/usage事件的记录：必须保留“事件语义”，不能变成token语义。

## 4. 两个合法但不同的TPOT命名空间

### A. aisbench_response_tpot

本版公式经确认后：

```text
t0 = 请求打点
t1 = 首个记录的响应事件
tlast = 最后记录的响应事件
TPOT_response_ms = (tlast-t1) × 1000 / (实际输出tokens-1)
```

用途：客户端可见的平均输出响应性能、部署对比和经验模型。

限制：t1/tlast未必是首末内容token；可能含角色/usage/网络/交接开销。ITL数组通常是相邻响应事件间隔，不是真实逐token间隔。

### B. canonical_content_tpot

仅确有独立证据标识真实首末内容token时：

```text
TPOT_content_ms = (末内容token时刻-首内容token时刻) / (实际输出tokens-1)
```

即使是客户端内容token时间仍可能含传输/缓冲，不等于服务端纯step耗时。

### 操作要求

- 两类分别存文件/分组，不在同条曲线中混用。
- O<2时本地标记未定义；原报告若置0，保留原值并注明差别。
- 非流式数据通常不能分解TTFT/TPOT；保留E2E和吞吐分析，不能从总延迟补造首token时间。
- 只有汇总值无法恢复真实请求分布、事件数组和尾时延，不伪造请求记录。

## 5. 并发和RPS的三重检查

1. 配置最大并发：所查标准路径batch_size是客户端额度；按当前配置确认。
2. 客户端平均在途：有真实请求start/end时，可算占用面积/窗口；若与工具报告不符先查其窗口和过滤。
3. 服务端active decode batch：必须来自引擎观测，不能拿前两者代替。

有start/end时的完整群组自检：

```text
平均在途 ≈ sum(每请求start到end时长) / 群组窗口时长
请求完成率 × 平均完整请求时长 ≈ 同一群组的平均在途
```

这是一致性检查，不是用来反向构造缺失队列数据。失败/窗口裁剪需同口径；非稳态不能随便套稳态排队模型。

pressure模式检查：持续发压结束后可能仍等待长请求收尾；pressure_time不等于报告总Duration。长输出改变周转率与新prefill频度。

普通RPS模式检查：实际发送间隔、受并发额度阻塞的时间、请求总数是否足够；不要把“配置无限速”解释为同一时刻严格齐发。

stable_stage检查：读取该版本计算器的入选请求和窗口规则。不要用total的tokens分子除stable的duration；少数超长请求可能严重污染窗口边界估计。

## 6. 长度与可运行性检查

- 合成string参数512不保证512 tokens。
- tokenid经decode/re-tokenize和chat模板也可能改变实际输入。
- max_out_len/max_tokens是上限；数据集逐条参数可能覆盖全局参数。
- ignore_eos须服务端实际支持；EOS、stop、容量、超时均可能提前结束。
- input512/output65536至少需要66048实际token上下文；服务参数max-model-len=65536不够。目标输入若未包含模板，真实需求可能更大。
- 各档检查实际OutputTokens完整比例；不能把目标长度当实际长度。
- 检查finish_reason/错误/重试/截断；无对应字段则标记未知。

## 7. 给弱模型的完成条件

只有全部回答并保存证据，才能进入结构解释：

```text
[ ] 知道真实版本和API适配器。
[ ] 知道detail是否依赖外部DB，引用没有丢失。
[ ] 知道每个时间字段的边界和单位。
[ ] 明确使用response还是content TPOT。
[ ] 没有把响应事件ITL叫成真实token ITL。
[ ] 实际输入/输出与目标分别保存。
[ ] C/RPS/请求数/pressure/stable语义分别保存。
[ ] 失败/未完成/重试没有消失。
[ ] 缺少server queue/prefill/batch/KV-transfer时写unknown。
```

未满足的项限制对应结论，不要求所有额外profiler资料齐全才允许M0描述分析。
