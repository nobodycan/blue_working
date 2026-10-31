# 用 msModeling 分析 PD 混部与分离：从环境搭建到可复核的性能边界实验

> 整理日期：2026-10-31。本文记录一次基于 MindStudio Modeling（msModeling）的本地性能建模实践，包括实验演进、关键结果、遇到的问题，以及可复用的方法和资料链接。
>
> **范围声明：全文时延、吞吐与显存数字均为解析模型预测，不是昇腾设备实测。** 本次没有部署真实Ascend推理服务，也没有验证真实W8A8权重的精度。最后一轮A/C/D完整排空仍未全部结束：A已完成，C因预测显存超预算停止，D仍在运行。进行中数据按本文整理时的快照记录，实时状态以原始结果目录为准。

## 一、这次测评究竟想回答什么

最初的问题可以概括为：

> 输出长度越大，chunked prefill对decode的影响是否越容易被摊薄，从而让PD混部更容易满足时延要求？

随着实验推进，这个问题被拆成四层：

1. **机制层**：固定、有限的prefill干扰，是否会被更多输出token摊薄？
2. **系统层**：持续请求到达时，长输出是否改变实际decode batch、排队与KV占用？
3. **架构层**：相同总卡数下，PD分离改善输出连续性的同时，会不会恶化prefill队列和TTFT？
4. **容量层**：当真实上下文、实际batch和临时激活共同增长时，哪些形状会触发预测显存限制？

最后形成的认识不是“输出越长越好”，而是：

> **先验证调度和资源记账正确，再区分有限干扰、持续负载、聚批、上下文增长与容量限制，最后讨论架构边界。**

## 二、实验环境与测评口径

### 2.1 主机和软件

| 项目 | 本次配置 |
|---|---|
| 主机系统 | Windows 11 Pro，PowerShell 5.1 |
| CPU / 内存 | Intel Core i7-14700，32 GiB RAM |
| Python | 3.12.10，独立虚拟环境`.venv312` |
| msModeling | 26.2.0 |
| PyTorch | 2.10.0 |
| 后期主模型 | DeepSeek-V4-Flash，本地保存已审阅的原始`config.json` |
| 量化假设 | 专家与非专家Linear显式W8A8_DYNAMIC；LM Head默认不量化；attention/KV不量化 |
| 计算方式 | 解析成本模型；不下载模型权重或远程Python代码 |
| 主机并行 | 通常最多4个独立模型worker，底层CPU线程库限制为1线程；当前排空轮为3个worker |

这里必须区分三种“并行”：

- **主机worker并行**：这台Windows电脑同时计算几个独立仿真任务。
- **模拟TP并行**：目标推理实例在多少个画像设备上切分模型。
- **请求并发与实际batch**：服务中有多少未完成请求，以及某个step实际调度了多少decode请求。

主机有4个worker，不代表模拟4个推理实例；`cap=128`也不代表每步一定执行128个decode请求。

### 2.2 A3整卡不能直接等同于一个画像设备

本次采用的A3画像名称包含`128G_DIE`，但其单die建模容量为64 GiB。一张整卡包含两个die，因此：

```text
8张A3整卡 = 16个模拟die

混部：2个TP8实例，每实例4张整卡
分离：1个P/TP8实例 + 1个D/TP8实例，各4张整卡
```

两种布局的总卡数相同，但阶段资源分配不同。混部两个实例都能做P和D，分离只有一个P实例和一个D实例，因此系统差异不能全部归因于“隔离prefill”。

### 2.3 budget和cap的含义

- **budget4096**：每个引擎、每个调度step最多执行4096个query token，P与D共享。不是最大输入长度，也不是每个请求都独享4K chunk。
- **全局cap128**：最多准入128个尚未结束的请求生命周期，包含内部等待、prefill、交接、decode等状态。满额后新到达请求留在外部FIFO，不改变其原始到达时间。
- **实际D batch**：当前某个引擎的step里真正执行的decode请求数。必须从轨迹观测，不能用cap、running或D驻留数替代。

## 三、时间线：从跑通工具，到建立可信对照

日期按已有实验文档与运行记录整理；准备阶段及部分早期扫描没有独立核验到精确日历日期，以下按先后顺序记录，不用文件修改时间补造运行日期。

| 时间/阶段 | 做了什么 | 结果与认识 |
|---|---|---|
| 准备阶段，具体日期未单独核验 | 安装官方wheel，排查Python与Windows启动器问题 | 建立Python3.12环境；没有关闭安全策略 |
| 早期机制验证，具体日期未单独核验 | Qwen3-32B、A2画像，控制代表上下文与有限初始批次 | 平均TPOT随输出增长而下降，最大ITL不变 |
| 2026-10-29 | DeepSeek-V4-Flash W8A8 / A2机制验证 | 相同的有限扰动摊薄现象再次出现 |
| A2机制验证之后、转A3之前 | 32K/64K输入、不同输出/budget/初始并发的32点扫描 | 32点计算完成，8点有效，24点预测内存无效 |
| 2026-10-30 | A3按整卡计数，尝试单TP16，再验证双TP8 | TP16遇到reshape错误；双TP8成为后续明确标注的替代布局 |
| 2026-10-30 | 固定外部到达＋全局cap，验证主机并行一致性 | 建立不被准入背压推迟的开环到达与完整轨迹记录 |
| 2026-10-30 | 到达率探索与严格N4输出长度配对 | 区分“长输出增加重叠机会”和“已有有限干扰被摊薄” |
| 2026-10-30 | 独立复核、串行复跑、PD交接正确性审查 | 时序可复现，但显存估计存在未解释差异；发现原生交接记账问题 |
| 2026-10-30 | 修正交接后运行四组PD对照 | 分离改善后续ITL，但固定4P+4D布局的P队列、TTFT更差 |
| 2026-10-30启动的持续负载阶段 | 每秒1请求、600秒，六组混部/分离对照 | 六组完整窗口均可核验；4组PD排空，2组混部主机超时 |
| 2026-10-30启动、2026-10-31完成 | 单TP8、D batch1的有限P冲击与真实上下文增长配对 | O16至8192未见TPOT回升，固定干扰摊薄关系通过验证 |
| 2026-10-31 | 输入32K/4K × 输出128/2048四组混部对照 | 缩短输入显著提高D供给；短输入长输出建立D batch60工况 |
| 2026-10-31，本文整理时 | 补跑A/C/D至完整排空 | A得到完整TPOT；C预测显存超预算；D继续运行 |

下面按实验逻辑展开，而不是把所有数字混在一个总表里。

## 四、第一轮机制实验：平均变好，不等于停顿消失

### 4.1 先用Qwen，再换DeepSeek

最早用Qwen3-32B、A2画像做有限批次机制验证。在固定代表上下文4352的控制下：

- 输出32：平均TPOT约35.555 ms。
- 输出128：约27.317 ms。
- 输出512：约25.319 ms。
- 最大ITL始终约71.749 ms。

随后在DeepSeek-V4-Flash / A2 / TP8上，使用I4096、budget512、初始请求16、固定代表上下文4352，得到：

| 输出长度 | 平均TPOT ms | 最大ITL ms |
|---:|---:|---:|
|32|285.212|600.902|
|128|225.562|600.902|
|512|211.087|600.902|
|2048|207.495|600.902|

这支持的是**有限干扰被摊薄**，不是“长输出让prefill step本身更快”。

若匹配的纯D平均成本为`C(O)`，有限额外等待总量为`H`：

```text
TPOT(O) = C(O) + H / (O - 1)
```

分母增大可以降低平均值，但那几次长间隔仍然存在。

需要注意：早期优化器使用代表上下文、混合step近似；它不等于后续动态ServingCast逐token增长的真实调度轨迹，更不等于实机kernel共存测量。

记录：[初始指标与Qwen机制实验](README.md)、[DeepSeek A2机制结果](DEEPSEEK_V4_FLASH_A2.md)。

### 4.2 扩大32点扫描后，首先遇到的是容量问题

扫描范围：

```text
输入：32K、64K
输出：512、1024、2048、4096
budget：4096、8192
初始请求并发：64、128
合计：2 × 4 × 2 × 2 = 32点
```

32点均完成分类，其中8点有效、24点预测内存无效；有效点仅出现在32K输入、初始并发64的组合。

这轮的并发是**同步进入的有限初始批次**，不是后来的“固定到达率＋全局准入上限”。因此不能把它与动态cap64/cap128直接对比。

另一个教训是：W8A8 Linear不意味着KV也变成INT8。TP8下每die权重估算约35.614 GiB，64 GiB画像还要扣除10 GiB预留，再容纳KV与临时激活，大初始批次自然更容易触碰容量限制。

记录：[32点扫描口径](GRID_32K64K.md)、[扫描原始状态](ds4flash_a2_w8a8_grid_32k64k_v1/run_status.json)。

## 五、第二轮：转向A3、不可变到达与严格配对

### 5.1 TP16没有跑通，不能把双TP8冒充TP16

按8张双die A3整卡尝试单实例TP16时，V4建模前向在reshape处失败：

```text
shape '[1, 1024, 2048]' is invalid for input of size 4194304
```

这是模型形状/并行适配错误，不是显存OOM。没有修改安装包强行修补，而是保留失败记录，另行使用两个TP8实例；所有后续结果明确标注为双TP8。

### 5.2 原生发压存在背压，需要把到达与准入拆开

原生发压逻辑在并发满额时可能等待，进而推迟后续请求生成。如果直接把配置的request rate当作真实外部到达率，服务越忙，实际输入反而可能越少。

本地适配改成：

1. producer按绝对时刻`a_i = i / rate`产生请求。
2. 请求先进入外部FIFO。
3. dispatcher在全局cap有空位时准入。
4. TTFT从原始外部到达开始计时，而不是从准入时刻开始。

逐事件检查：

```text
已到达 = 已完成 + 已准入未完成 + 外部等待
```

另一处容易误读的语义是：本安装版原生`request_rate=0`表示零间隔突发，不是没有流量；正到达率使用确定性间隔，不是Poisson到达。

### 5.3 从低负载到真正出现P/D重叠

低负载0.01请求/秒的探索里，实际D batch峰值为1，混合step为0。这说明配置跑通了，却还不能说明混部干扰大小。

随后做0.1/0.2/0.4/0.8请求/秒探索，在已测0.4档出现明显P/D混合。再固定N4、到达时刻、输入、budget和总资源，只改变输出长度。

0.4请求/秒下的严格配对结果：

| 输出O | 平均TPOT ms | 最大ITL ms | >100ms事件数 | 平均E2E s |
|---:|---:|---:|---:|---:|
|16|343.966|1190.564|18|16.933|
|128|73.473|1190.564|18|21.104|
|512|46.249|1190.564|18|35.407|

平均TPOT下降，长间隔绝对次数和最大幅度却没有下降，请求整体结束还更晚。

0.2请求/秒下又出现另一种情况：O16没有混合，O128开始出现混合，平均TPOT先从25.166 ms升到71.380 ms，再在O512降至45.729 ms。这说明长输出既可能增加重叠机会，也可能摊薄已发生的有限干扰，不能预设单调性。

记录：[A3开环、到达率探索与配对结果](OPEN_LOOP_A3.md)。

## 六、第三轮：PD分离先修交接，再比较性能

### 6.1 发现D侧prompt KV预留被跳过

审查安装版26.2.0的原生调用链时发现：请求进入D实例后，交接状态过早改成DECODING，导致接收端完整prompt KV预留路径被跳过。

原生调度器和真实逻辑KV管理器的小型探针复现了：

```text
输入32768，交接后seq_len=32769
但D侧只分配1个slot/1个block
kv_transfer_done仍为False
```

如果不处理，这种记账会低估D侧驻留需求并改变后续等待行为。后续通过独立、作用域内适配器修正：

- 保留交接状态，直到D侧完整prompt预留成功。
- P侧源KV保留至D预留ACK后再释放。
- 传输中的请求仍计入全局cap。
- 角色检查保证P只做P、D只做D；本轮D侧重算不被静默接受。
- P侧因保留源KV无法继续调度时正确等待，由ACK唤醒。

这些是本安装版、该调用路径下的复现与修正，不能未经核验就断言其它版本相同。没有改写site-packages。

### 6.2 相同总资源不代表所有时延都同时改善

固定8张整卡、I32K、budget4K、N4、0.4请求/秒，比较混部与4P+4D分离布局。交接服务时间设置0/200 ms，200 ms是诊断假设，不是网络测量。

| 架构 | 输出 | 交接ms | 平均TTFT s | 平均TPOT ms | 后续ITL最大ms | 平均E2E s |
|---|---:|---:|---:|---:|---:|---:|
|混部|16|—|11.773|343.966|1190.451|16.933|
|分离|16|0|19.876|25.166|25.166|20.253|
|分离|16|200|19.876|38.499|25.166|20.453|
|混部|128|—|11.773|73.473|1190.451|21.104|
|分离|128|0|19.876|25.166|25.166|23.072|
|分离|128|200|19.876|26.741|25.166|23.272|

其中“后续ITL”排除了首个交接间隔`t2-t1`。200 ms传输时，这个首间隔约225 ms，不能把它隐藏在“后续全部约25 ms”的描述中。

当前服务契约由P发出首token，交接随后发生，所以200 ms：

- 不改变首token时刻。
- 让第二个及之后所有token整体后移200 ms。
- 使TPOT增加`200/(O-1)`ms。

本轮支持“分离改善出字连续性”，但固定卡比使P排队加重，TTFT与E2E并未更好。四组PD实际D batch峰值仍为1，说明尚未检验高D负载边界。

记录：[交接验证与限制](PD_DISAGG_VALIDATION.md)、[四组PD最终结果](PD_FOUR_CONTROLS_RESULTS.md)。

## 七、第四轮：每秒1请求，排队不能隐藏

将负载提高为每秒1请求，持续600秒，所有请求按0..599秒到达。比较混部与分离，O16/O128，PD另设0/200 ms交接，共六组。

| 架构 | 输出 | 600秒内完成 | 外部等待 | 已准入未完成 | 最终排空状态 |
|---|---:|---:|---:|---:|---|
|混部|16|121|351|128|主机6小时超时|
|混部|128|92|380|128|主机6小时超时|
|分离，0/200ms各一组|16|各63|各409|各128|两组均600/600完成|
|分离，0/200ms各一组|128|各63|各409|各128|两组均600/600完成|

关键区别：

- 两组混部的**600秒窗口记录完整**，但主机计算未完成整个排空，不能补造最终全队列时延。
- 四组PD全部排空，却在600秒停止新到达后，还需约5071～5074个模拟秒清空积压。最终完成不意味着能稳定承载1请求/秒。
- 在该固定PD布局中，32K prefill约9.45秒，O128单请求D阶段约3.20秒，D供给不足，实际D batch峰值仍为1。
- 混部主机计算超时不等于模拟部署更慢；同一600秒窗口中，混部完成数反而更多。

为防止旧结果被“修成成功”，保留原始失败状态；另用只读恢复脚本重建六组窗口的守恒和队列，不推断缺失token。

记录：[600秒持续负载最终报告](SUSTAINED_1RPS_RESULTS.md)。

## 八、第五轮：固定有限P冲击，验证真实上下文增长

为了检验“KV增长会不会抵消摊薄收益”，改用更干净的机制实验：

- 单TP8引擎、4张A3整卡，不与8卡系统直接比吞吐。
- 目标I32K，实际D batch固定1。
- A轨迹无外来prefill；B轨迹在目标输出第3个可见token时注入一次32K输入、O1的P-only请求。
- 目标上下文逐token真实增长。
- 输出扫描16、128、512、2048、8192。
- 无offload；A/B相同D进度的query_len/seq_len逐一匹配。

| 输出O | 纯D平均成本ms | 带P平均TPOT ms | 摊薄项H/(O−1) ms | 最大ITL ms |
|---:|---:|---:|---:|---:|
|16|25.165684|653.103136|627.937453|1190.564585|
|128|25.165724|99.331565|74.165841|1190.564585|
|512|25.165762|43.598369|18.432606|1190.564585|
|2048|25.165885|29.767283|4.601398|1190.564585|
|8192|25.166367|26.316296|1.149928|1190.564585|

五组额外等待H均约9419.062 ms，共同前16个token时序一致，P结束后的干净尾段恢复纯D基线。

**受测范围内没有看到TPOT从降转升。** 这不是“永远不会回升”，而是当前D batch1、V4解析模型、无offload条件下，纯D成本增长远小于摊薄项下降。

额外42个成本采样点中有1个当前batch预测内存无效。接近零的显存边界还受到跨进程内存估计差异影响，不能当作精确物理OOM阈值。

记录：[执行控制](CROSSOVER_EXECUTION.md)、[输出长度验证结果](OUTPUT_CROSSOVER_RESULTS.md)。

## 九、第六轮：先让D真正忙起来，再谈长输出效应

### 9.1 用2×2对照拆开输入和输出

提出两个候选方向：

1. 缩短输入并延长输出，减少P工作、增加D压力。
2. 输入不变，只延长输出，保持更清楚的输出长度对照。

最后不是只选其一，而是做四组，每组每秒1请求、120个请求、固定[0,120)窗口；资源、budget与cap不变。

| 组别 | 输入/输出 | 实际D峰值/引擎 | D-active平均batch | 窗口完成数 | 可见完整ITL均值ms | ITL p95 ms |
|---|---|---:|---:|---:|---:|---:|
|A|32K/128|12|6.169|0|1190.478|1190.574|
|B|4K/128|43|23.727|33|608.952|1172.291|
|C|32K/2048|12|6.169|0|1190.478|1190.574|
|D|4K/2048|60|29.781|0|658.919|1172.481|

这轮8个案例（4组16秒诊断＋4组120秒正式窗口）全部完成并通过审计。

得到三个重要观察：

- **缩短输入显著改善D供给**：截止前获得首token的请求由24/120增加到119/120。
- **P供给改善后，延长输出继续增加D batch**：B→D峰值43→60，但可见ITL均值增加约50 ms。
- **D压力与KV容量压力不是同一个旋钮**：输入变短也减少初始KV，不能把4K上下文的batch60外推到32K上下文。

A/C在窗口内没有请求完成128-token输出，因此还未充分走到由不同输出终止长度造成调度分叉的阶段；不能据此说延长输出无效。

### 9.2 为什么还要同时看长ITL的次数与占比

B组>100 ms事件8497次，D组9940次；分别约占可见完整间隔的92.7%和93.8%。它们的单位时间事件数比长输入组更多，但同时也产出了更多token。

因此必须区分：

- 绝对长间隔次数。
- 每请求长间隔次数。
- 每1000个可见间隔的长间隔数。
- 每秒长间隔事件数。
- 尚未闭合的ITL等待年龄下界。

输出变长、D工作变多后，仅比较事件次数或仅比较占比，都会失去一部分信息。

记录：[四组设计](INPUT_OUTPUT_PRESSURE_PLAN.md)、[最终解读](INPUT_OUTPUT_PRESSURE_RESULTS.md)。

### 9.3 为了补齐TPOT，继续完整排空；这次也暴露了新的容量限制

原窗口A/C/D没有完整请求TPOT，于是保持相同120请求及到达时刻，在新目录重跑并等待全量完成。没有可恢复调度器checkpoint，因此这是新运行，不是恢复旧进程。

本文整理时：

| 组别 | 完成情况 | 完整请求TPOT ms | 全体ITL均值ms | 说明 |
|---|---|---:|---:|---|
|A：32K/128|120/120|1042.942|1042.942|全量审计通过；前120秒token/调度/队列精确复现|
|B：4K/128|原窗口33/120|464.848，仅完成者|608.952，原窗口可见间隔|本轮未重跑，不是全队列同口径对照|
|C：32K/2048|0/120|不可得|不提供全队列值|模拟365.430秒时预测显存超预算，已停止|
|D：4K/2048|进行中|尚不可得|尚不可得|最近核验快照约508.0模拟秒、2050个step、0/120完整完成|

C失败时的形状包含38个D请求与1个P请求，后者query_len为4058：

```text
权重          35.614 GiB/die
当前batch KV   8.291 GiB/die
临时激活      11.360 GiB/die
预留          10.000 GiB/die
合计          65.265 GiB/die > 64 GiB/die
预测余量      -1.265 GiB/die
```

适配器依据负余量主动停止，未把该形状继续当作有效性能。**这是预测显存超预算，不是主机超时，也不是实机OOM实测。** 不能简单归因为KV占满，因为混合batch临时激活也很大；也不能推断所有其它调度策略在同资源下都不可行。

失败证据保留，没有擅自改参数让C“跑通”。D继续独立执行。

记录：[补跑方案](PRESSURE_DRAIN_PLAN.md)、[当前汇总表](ds4flash_a3_wholecard_input_output_pressure_drain_v1/table.txt)、[当前运行状态](ds4flash_a3_wholecard_input_output_pressure_drain_v1/run_status.json)、[C的显存失败形状](ds4flash_a3_wholecard_input_output_pressure_drain_v1/drain_C_I32768_O2048_N120_R1/memory_failure.json)。

## 十、踩过的坑：什么已解决，什么仍未解决

| 问题 | 表现与风险 | 本次处理 | 当前边界 |
|---|---|---|---|
| Python/torch版本不兼容 | Python3.15下依赖无法正确落地 | 新建Python3.12虚拟环境，固定已验证版本 | 不代表任意Python/torch组合兼容 |
| Windows应用控制拦截启动器 | `msmodeling.exe --help`被SAC/Application Control阻止 | 查Code Integrity事件及相关资料；没有关闭或更改安全策略 | 后续启动器可用，但恢复原因未观察到，不能声称找到通用修复 |
| 表字段/单位假设错误 | 初期读取内存DataFrame字段产生KeyError | 根据实际输出使用`avail_GB`等真实字段 | 升级版本需重核schema，不能硬套旧列名 |
| 静态并发被误当动态并发 | 初始满批64/128与cap64/128不是同一实验 | 改为固定外部到达＋全局生命周期cap | 实际D batch仍需逐step观测 |
| A3整卡/die口径混淆 | 卡数、TP和每die容量容易算错 | 明确8卡=16 die，验证实际前向 | 不把双TP8冒充单TP16 |
| V4单TP16 reshape失败 | 形状不匹配，不能进入目标布局 | 保留错误，改为明确标注的双TP8替代实验 | 原TP16适配未修复；不是OOM |
| 显存估计跨进程不完全重复 | 串行复跑最小余量差约0.243735 GiB | 时序、token/队列/调度哈希精确相同；内存一致性仍判失败 | 没有扩大1 MiB既定容差；原因仍未查明 |
| PD目的端prompt KV预留被跳过 | D仅记一个decode slot，低估占用 | 独立adapter保留交接状态、完整预留、ACK后释放源KV | 仅对本安装版/路径核验；非完整网络/物理缓存模型 |
| 重复native测试的回调污染 | 全局Blinker信号与重复ID造成旧回调干扰 | 每次测试使用独立Namespace，恢复Config和patch | 测试隔离通过不等于真实设备校准 |
| P源KV保留后的等待/唤醒 | 空闲P无可用KV时可能反复调度 | 正确yield，D侧ACK释放后唤醒 | 保留策略不是完整传输staging内存模型 |
| 异构CSV汇总失败 | 混部行与PD行字段不同，DictWriter报错 | 汇总使用字段并集，加回归测试，只读恢复已完成产物 | 原ERROR保留，不能因报告修复就改写原执行状态 |
| 主机6小时超时 | 动态逐token成本计算未排空 | 区分HOST_TIMEOUT；只恢复已完整记录的窗口 | 没有最终token就不编造全队列时延 |
| 近零/负内存与算子告警 | activation截零、算子回退、RoPE键告警 | 原始日志保留，负余量案例拒绝性能认证 | 单batch有效不等于完整驻留V4缓存安全 |
| 当前C混合batch负显存 | 权重、KV、激活、预留合计超过64 GiB/die | 保存具体形状并停止该案例，D独立继续 | 尚未通过新调度/新参数补救，不伪造完整TPOT |

还有两件“没有做过”的事必须明确：

1. **没有验证真实KV offload/H2D。** 该原生路径内存不足后的机制是等待/抢占重算，不是CPU KV驻留和预取；PD设备间交接也不是CPU→NPU的H2D。
2. **没有完成真实AISBench压测。** 只审阅了`aisbench_auto_tools_prefix`的编排思路，借鉴配置、日志和批量实验组织，没有把该工具运行结果混入本文。

## 十一、如何复用这次测评方法

### 11.1 先把指标写清楚

设外部到达为a，首个到最后一个可见token时刻为t1…tO：

```text
TTFT = t1 - a
ITL[k] = t[k] - t[k-1]
TPOT = (tO - t1) / (O - 1)，O >= 2
E2E = tO - a
```

TPOT已经包含首token后的实际间隔，不是“计算TPOT再加一次ITL”。纯D基线与额外等待的拆分，需要匹配实际batch和上下文的反事实实验，不能拿batch1的25 ms去减batch60的系统观测。

同组所有请求输出长度相同、全部完成时，请求平均TPOT等于全体ITL均值；如果只统计完成者TPOT，却用完成＋未完成请求的可见ITL，两者样本范围不同，自然可以不相等。

TTFT还要从原始外部到达算起，否则外部准入等待会被隐藏。已完成子集的时延必须同时附上未完成数量和等待年龄。

### 11.2 每次实验遵循同一个顺序

1. **锁定版本、设备单位、量化和总资源。** 保存模型JSON与源码哈希。
2. **做小型正确性检查。** 到达不漂移、cap不超限、token数正确、角色与交接记账正确。
3. **先确认实验确实触发目标机制。** 无P/D混合的低负载点不能用来研究混部干扰；cap128也不能替代实际D batch128。
4. **先小对照，再扩大矩阵。** 分别改变输入、输出、budget和到达率，必要时用2×2拆开交互。
5. **同时报告请求级、token级、队列和资源结果。** 不只看一列TPOT或token/s。
6. **固定观察窗和排空分开报告。** 冷启动窗口不是稳态，排空完成不是持续承载能力。
7. **独立重算与复现。** 每个可见token映射到真实step结束；逐事件验证队列守恒；检查时序与哈希。
8. **保留所有失败。** ERROR、MODEL_MEMORY_INVALID、TIME_LIMIT、HOST_TIMEOUT不能合并成一个OOM标签。

### 11.3 建议保存的最小证据集

```text
plan.json                    # 工作负载、资源、来源哈希、护栏
run_status.json              # 阶段和最终状态
metadata.json                # 实际解析的模型/量化/并行/内存配置
common.yaml / instances.yaml # 原生配置片段；部分适配还依赖本地worker
requests.csv                 # 包含未完成请求的完整请求清单
token_times_seconds.json     # 可见token时间
steps.jsonl                  # 真实执行step及预测成本
queue_events.jsonl           # 外部/内部队列与生命周期
progress.json                # 长任务进度
summary.json                 # 最终汇总
memory_failure.json          # 若失败，保留具体内存形状
*.audit.json / *.prefix.json # 独立核验与复现证据
```

这些YAML不能脱离本地adapter直接当成原生CLI的等价运行配置。尤其是不可变到达、O投影布局、reserve和PD交接处理，应通过对应worker执行。

### 11.4 本地入口示例

下面是复跑示例，不表示本文又启动了新实验。输出目录必须尚不存在；长输出解析建模可能耗时数小时。需要保留本目录脚本和已审阅模型JSON。

先查看已安装环境：

```powershell
.\.venv312\Scripts\python.exe --version
.\.venv312\Scripts\python.exe -m pip show msmodeling torch
.\.venv312\Scripts\python.exe -m pip check
```

运行四组固定窗口对照：

```powershell
.\.venv312\Scripts\python.exe -I .\experiments\pd_boundary\run_pressure_controls.py --output-dir .\experiments\pd_boundary\pressure_example_new --workers 4
```

基于原已完成窗口，补跑A/C/D完整排空：

```powershell
.\.venv312\Scripts\python.exe -I .\experiments\pd_boundary\run_pressure_drain.py --reference-root .\experiments\pd_boundary\ds4flash_a3_wholecard_input_output_pressure_v1 --output-dir .\experiments\pd_boundary\drain_example_new --workers 3 --case-timeout-seconds 86400
```

这是项目内的适配脚本，不是官方CLI的通用子命令。使用`-I`和显式虚拟环境解释器，是为了固定导入与运行环境，不是绕过Windows应用控制。这里只验证了本次Windows组合，不能据此说工具只有Windows版本。

## 十二、资料与实验记录链接

### 12.1 官方工具与文档入口

| 链接 | 用途 |
|---|---|
| [msModeling官方仓库](https://gitcode.com/Ascend/msmodeling) | 项目说明、源码、文档目录 |
| [官方版本发布](https://gitcode.com/Ascend/msmodeling/releases) | 核对版本与发行产物 |
| [本次26.2.0官方wheel](https://gitcode.com/Ascend/msmodeling/releases/download/tag_MindStudio_26.2.0.B110_001/msmodeling-26.2.0-py3-none-any.whl) | 本次使用的安装产物，不保证未来环境与依赖仍相同 |
| [官方Issues入口](https://gitcode.com/Ascend/msmodeling/issues) | 查询或提交版本相关问题；本文不声称所有本地问题均有对应上游issue |
| [官方README列出的文档检索站点](https://mindstudio-docs-master.readthedocs.io) | 检索安装、推理仿真、服务化仿真等文档 |
| [昇腾MindStudio产品入口](https://www.hiascend.com/cn/developer/software/mindstudio) | 产品与生态资料 |

安装包README中列出的重点文档相对路径如下。可从官方仓库进入，具体分支与内容以对应发行版为准，不把当前分支文档当成26.2.0的绝对行为保证：

- 安装：`docs/zh/install_guide/msmodeling_install_guide.md`
- 快速开始：`docs/zh/quick_start/tensorcast_throughput_optimizer_quick_start.md`
- 支持矩阵：`docs/zh/user_guide/support_matrix/support_matrix_user_guide.md`
- 模型推理仿真：`docs/zh/user_guide/msmodeling_tensor_cast_user_guide.md`
- 服务化吞吐仿真：`docs/zh/user_guide/msmodeling_throughput_optimizer_user_guide.md`
- 服务化实测寻优：`docs/zh/user_guide/msmodeling_optix_user_guide.md`

工具同时有仿真与实测寻优能力；**本文使用前者，不能因工具支持后者就把本文数字写成实测。**

### 12.2 模型与辅助资料

- [DeepSeek-V4-Flash官方模型配置页面](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash/blob/main/config.json)
- [原始config.json下载](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash/resolve/main/config.json)
- [AISBench auto tools prefix参考仓库](https://github.com/rayn-zzz/aisbench_auto_tools_prefix)
- [上述编排脚本原文](https://raw.githubusercontent.com/rayn-zzz/aisbench_auto_tools_prefix/main/aisbench_test.py)
- [GitCode对应参考仓库](https://gitcode.com/ztzx3156/aisbench_auto_tools_prefix)
- [Microsoft Smart App Control常见问题](https://support.microsoft.com/zh-cn/windows/security/threat-malware-protection/smart-app-control-frequently-asked-questions)

本地原始V4配置的SHA256为：

```text
B628E63398A645ABC711D92207F8737DD8140F7A4EF1E0A5B3616019E0DDD818
```

远端`main`内容可能变化；复现应核对本地已保存文件与哈希，不只依赖一个可变URL。

### 12.3 本地完整证据导航

下列链接是项目内相对链接，适合在本地Markdown查看器或保留目录结构的代码仓库中打开；它们不是已经发布的公网网址。

| 主题 | 文档/结果 |
|---|---|
| 初始指标与机制假设 | [README](README.md) |
| A2 / DeepSeek机制结果 | [DEEPSEEK_V4_FLASH_A2](DEEPSEEK_V4_FLASH_A2.md) |
| 32点静态扫描 | [GRID_32K64K](GRID_32K64K.md) |
| A3整卡、开环、配对实验 | [OPEN_LOOP_A3](OPEN_LOOP_A3.md) |
| PD交接问题与复核 | [PD_DISAGG_VALIDATION](PD_DISAGG_VALIDATION.md) |
| 四组PD及混部对照 | [PD_FOUR_CONTROLS_RESULTS](PD_FOUR_CONTROLS_RESULTS.md) |
| 每秒1请求、600秒窗口 | [SUSTAINED_1RPS_RESULTS](SUSTAINED_1RPS_RESULTS.md) |
| 有限P冲击及输出长度验证 | [OUTPUT_CROSSOVER_RESULTS](OUTPUT_CROSSOVER_RESULTS.md) |
| 输入/输出四组压力对照 | [INPUT_OUTPUT_PRESSURE_RESULTS](INPUT_OUTPUT_PRESSURE_RESULTS.md) |
| A/C/D完整排空补跑 | [PRESSURE_DRAIN_PLAN](PRESSURE_DRAIN_PLAN.md)、[当前报告](ds4flash_a3_wholecard_input_output_pressure_drain_v1/RESULTS.md) |
| 显存复现差异原始证据 | [serial_reproduction.json](ds4flash_a3_wholecard_openloop_verify_v1/serial_reproduction.json) |

## 结语：工具给出数字，实验设计决定数字能说明什么

这次工作最有价值的变化，是从“扫一张参数表”转向“构建可验证的测评链条”：

- 先统一卡、die、TP和量化口径。
- 再确认到达、准入、调度、交接、KV记账真的符合假设。
- 用逐token和逐step轨迹解释平均值，而不是只看汇总表。
- 将有限批次、持续到达、固定观察窗和完整排空分别报告。
- 把成功、预测内存失败、程序错误和主机超时都当作结果的一部分保留。

**当前证据支持：有限干扰可以被摊薄；持续负载下长输出会改变实际batch与排队；分离改善D连续性不保证整体时延更优；短输入有助于建立D压力，但并不保证低ITL。**

尚未完成的则包括：真实Ascend校准、完整驻留V4缓存账本、KV offload/H2D验证、双方同总资源下的最优配置搜索，以及有明确联合SLO的稳态边界。下一步应围绕这些缺口设计对照，而不是把已跑通的解析曲线直接写成生产结论。
