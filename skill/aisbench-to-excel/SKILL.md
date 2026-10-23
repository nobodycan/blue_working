---
name: aisbench-to-excel
description: 将 AISBench 大模型服务性能评测的终端方框表格或日志批量汇总为 Excel，按并发、输入输出长度和 total/stable 阶段比较 TTFT、TPOT、ITL、E2EL、吞吐量及分位数。适用于 Performance Parameters 和 Common Metric 输出，不处理单模型离线 NPU 推理的 ais_bench --model 性能摘要。
---

使用 [scripts/aisbench_to_excel.py](scripts/aisbench_to_excel.py) 解析用户提供的终端文本或 .log/.out/.txt 文件。脚本依赖 Python 3.9+ 与 openpyxl 3.1+；若缺少依赖，在任务环境安装 `python -m pip install "openpyxl>=3.1,<4"`。无需安装 AISBench，无需连接推理服务。

将以下示例中的脚本路径替换为当前 skill 目录下的绝对路径：

```bash
python scripts/aisbench_to_excel.py ./logs -r -o ./results.xlsx
python scripts/aisbench_to_excel.py report.log -o results.xlsx --input-len 128k --output-len 1k
cat report.log | python scripts/aisbench_to_excel.py - -o results.xlsx
```

若输入为聊天中的文本，先将原文保存到任务目录，再运行脚本。保留结果任务标题和完整表头。一个文件可以包含多个结果块。重复实验保留独立行，每个结果块的每个 Stage 单独成行；不自行跨实验求平均。

参数来源优先级为：Common Metric 的 Max Concurrency（配置并发），然后每块 `AISBENCH_META`，再命令行默认值，最后文件名。模型和数据集在无显式元数据时取自 `Performance Results of task: 模型/数据集:`。文件名约定如 `c32_in128k_out1k.log`，k=1024。配置输入/输出长度与实测 InputTokens/OutputTokens Average 分列。`Concurrency` 是实际平均并发，不能代替 `Max Concurrency`。只有实测平均长度时，配置长度应留空。

同一文件中不同长度的实验若缺少配置参数，可在每块结果前加一行：

```text
# AISBENCH_META {"concurrency":32,"input_len":131072,"output_len":1024,"tag":"PD实验A"}
```

标记仅作用于紧随其后的一个结果块。并行进程将表格行交错写入同一文件时无法可靠区分来源，应使用各任务单独日志。

脚本自动处理 Unicode/ASCII 边框、ANSI 颜色、变化的统计列、缺少 Stage 的旧表格以及不同时间单位；时间统一为 ms。缺失值留空，原始文本、单位和行号保留在“指标明细”。未知单位不猜测，损坏行不补造。存在问题时检查“解析提示”；要求完整数据时使用 `--strict`。未知参数不能填成零，也不能从 token 总量反推固定输入长度。

先核对导出的结果块数量、关键指标和参数来源，再向用户提供 Excel 链接。脚本输出数量为块数与块/阶段行数。读取输入不会运行压测。需要完整的命令、格式边界或来源时，阅读 [README.md](README.md)。
