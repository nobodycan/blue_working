# AISBench 终端日志汇总到 Excel

针对 AISBench/benchmark 大模型服务性能测评的方框表格，直接读取终端复制文本或日志。无需安装 AISBench，不发起推理请求。支持 Linux、Windows、macOS，Python 3.9+。

```text
│ Performance Parameters │ Stage │ Average   │ Min │ Max │ Median │ P95 │ P99 │ N │
│ TTFT                   │ total │ 2006.0 ms │ ... │ ... │ ...    │ ... │ ... │ 8 │
│ Common Metric          │ Stage │ Value     │
│ Max Concurrency        │ total │ 32        │
```

依据官方文档核对格式（2026-10-23）：

- [官方服务性能评测示例](https://ais-bench-benchmark.readthedocs.io/en/latest/base_tutorials/scenes_intro/performance_benchmark.html)：包含完整终端表格和结果块标题。
- [官方性能指标解释](https://ais-bench-benchmark.readthedocs.io/en/latest/base_tutorials/results_intro/performance_metric.html)：区分 Concurrency、Max Concurrency、请求级与整体吞吐。

`tests/official_excerpt.log` 从上述官方示例选取部分行和统计列，保留原数值用于回归；`examples/demo.log` 与 `demo.xlsx` 均为构造的演示数据，不是真实测量。

## 1. 安装和运行

在解压后的目录运行：

```bash
python -m pip install -r requirements.txt

# 汇总目录中的 .log/.out/.txt，递归包含子目录
python scripts/aisbench_to_excel.py ./logs -r -o results.xlsx

# 指定多个文件或通配符（引号内通配符由脚本展开，Windows 也支持）
python scripts/aisbench_to_excel.py "logs/*.log" another.out -o results.xlsx

# 一个文件内有多轮完整结果
python scripts/aisbench_to_excel.py all_runs.log -o results.xlsx

# 运行附带演示
python scripts/aisbench_to_excel.py examples/demo.log -o my_demo.xlsx
```

脚本拒绝覆盖已有输出；确认重新导出时加 `--force`。每次从原始日志重建工作簿，不向旧 Excel 追加，以免重复计算同一批日志。

## 2. 如何保存终端输出

Linux 下将你原有的 AISBench 命令输出保存下来，`2>&1` 同时捕获 stderr，`tee` 保留终端显示：

```bash
set -o pipefail
# 将 YOUR_CONFIG.py 替换为你现有的配置文件
ais_bench YOUR_CONFIG.py --mode perf --debug 2>&1 | tee c32_in128k_out1k.log
python scripts/aisbench_to_excel.py c32_in128k_out1k.log -o results.xlsx
```

后续不同并发、输入长度的实验分别保存为类似名称，再一次汇总整个 logs 目录。脚本只解析已有输出，文件名不改变压测参数。真正的输入输出长度、并发仍在 AISBench 配置中设置。

直接管道输入也支持，读到 EOF 后生成 Excel：

```bash
cat c32_in128k_out1k.log | python scripts/aisbench_to_excel.py - -o results.xlsx --input-len 128k --output-len 1k
```

PowerShell 使用：

```powershell
Get-Content -Raw -Encoding utf8 .\c32_in128k_out1k.log | python .\scripts\aisbench_to_excel.py - -o .\results.xlsx --input-len 128k
```

Windows 推荐直接传日志文件路径，避免旧版终端的管道字符编码转换。自动识别 UTF-8、带 BOM 的 UTF-16 和 GB18030；可用 `--encoding gb18030` 显式指定文件编码。

## 3. 不同并发、长度如何识别

**配置并发**优先使用结果表里的 `Max Concurrency`。`Concurrency` 单独放在“实际平均并发”列，不拿它当配置值。

配置输入/输出长度不一定显示在结果表中，可用以下方式补充：

1. **文件名约定**：`c32_in128k_out1k.log`、`concurrency32_input_len131072_output_len1024.log` 或 `c32_i131072_o1024.log`。不根据纯数字位置猜测含义。k=1024、m=1048576，`128k` 为 131072。
2. **单次默认参数**：`--concurrency 32 --input-len 128k --output-len 1k`。这些参数对本次全部输入生效，适合相同配置的多次重复实验。
3. **单文件多轮标记**：在每轮完整结果之前加 JSON 元数据：

```text
# AISBENCH_META {"concurrency":32,"input_len":131072,"output_len":1024,"tag":"PD方案A"}
[时间戳] [ais_bench] [INFO] Performance Results of task: my-model/dataset:
...这一轮的完整表格...

# AISBENCH_META {"concurrency":64,"input_len":65536,"output_len":1024,"tag":"PD方案A"}
[时间戳] [ais_bench] [INFO] Performance Results of task: my-model/dataset:
...下一轮的完整表格...
```

也可在运行命令前用 `echo` 将标记写入日志。标记只作用于紧随其后的一个结果块，不向后续实验传递。

来源优先级：结果表 `Max Concurrency` > 每块 `AISBENCH_META` > 命令行默认值 > 文件名。模型/数据集可用 `--model` / `--dataset` 或元数据指定，否则从官方结果任务标题提取。`--tag` 用来记录机器、版本、PD方案等实验标签。汇总末尾“参数来源”可回溯识别方式。并发冲突会出现在“解析提示”中。

`InputTokens Average`、`OutputTokens Average` 是实测平均长度，分别保存在独立列。不能将 131070 之类的实测值默认为配置 128k；缺少配置长度时留空。

## 4. Excel 内容

- **汇总**：每个结果块的每个 stage 一行。前面是模型、数据集、配置并发、实际并发、配置/实测长度、请求数、整体吞吐；随后优先显示 TTFT/TPOT/ITL/E2EL 的 Average 和 P99，再保留其余全部指标、统计列和来源行号。自定义 P95 等列自动保留。
- **指标明细**：每个统计值一行，包含结果ID、stage、指标、统计项、换算后数值、单位、原始单元格文本、源文件和行号。
- **解析提示**：仅在存在提示时出现，记录配置参数缺失、坏行、重复指标、缺失值、未知单位或不完整表格。

Excel 数值保存为真正的数字，可筛选、排序、透视。时间自动统一为 ms；支持 s/ms/us/µs/μs/ns，百分数保存为 Excel 比例值。请求级 `OutputTokenThroughput` 与整体 `Output Token Throughput` 使用不同列名，避免混用。`total` 与 `stable` 不合并统计，也不会将某阶段的吞吐或实测并发套用到另一阶段。

重复实验不去重、不平均，使用不同结果ID。重复文件路径仅读取一次。同一结果/阶段的重复指标保留全部明细，汇总采用首个值并提示。

## 5. 完整性和格式边界

```bash
# 任何解析提示均视为错误，不生成 Excel，退出码为 2
python scripts/aisbench_to_excel.py logs -r -o results.xlsx --strict
```

默认尽量保存可解析数据并将问题写入工作簿，成功导出退出码为 0。完全没有结果表、输入文件无法读取或输出被占用时退出码为 2。全部文件先解析完成再写输出。

支持官方 `Performance Parameters` / `Common Metric` 表头、Unicode `│` 和 ASCII `|` 边框、ANSI 颜色、变化的统计列，以及缺少 Stage 的旧格式（记为 total）。旧版汇总字段 `Total Generated Tokens` 统一为 `Total Output Tokens`。未知指标按原名保留。

不解析单模型离线 NPU 工具 `ais_bench --model model.om` 的性能摘要，也不解析 vLLM 自带 bench 的其他格式。终端软换行若被复制成真实换行，脚本会提示并跳过损坏行；建议保存原始日志。多个进程交错写入同一表格时无法可靠恢复，应分别保存各任务日志。输入文件整体读入内存，超大日志建议按实验分文件。

## 6. 作为 skill 使用

`skill/aisbench-to-excel/` 是自包含 skill，可复制到目标工具的 skills 目录，例如 `~/.codex/skills/aisbench-to-excel/`。本包提供可复制的 skill，未自动更改用户级配置。

调用示例：

```text
使用 $aisbench-to-excel，把 /data/logs 里不同并发和输入长度的 AISBench 终端日志汇总为 Excel。
```

当前目录的 `scripts/aisbench_to_excel.py` 可单独复制运行。

## 7. 验证

```bash
python -m unittest discover -s tests -v
```

回归测试覆盖官方示例选段、多轮结果、两种边框、total/stable 分离、单位换算、动态分位数、缺失值、截断行、元数据优先级、文件/目录/通配符/管道输入、编码识别和导出后重新读取的数值类型。
