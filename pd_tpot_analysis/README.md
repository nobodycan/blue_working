# PD TPOT Analysis

用于分析昇腾 A2 上一机 PD 混部与两机 PD 分离的 aisbench 实验结果。

本仓库提供分阶段 Skill、逐步提示词、数据契约、分析协议和标准化数值分析器；**不包含实际压测数据，也不包含已验证的性能结论**。

## 从这里开始

1. 阅读 [使用说明和首次启动提示词](START_HERE.md)。
2. 将 [SKILL.md](SKILL.md) 和附件提供给内部模型。
3. 按 [分阶段提示词](references/STAGE_PROMPTS.md) 逐步执行，不跳过字段审计与质量门禁。
4. 根据真实 aisbench 文件编写转换器，再将标准化 CSV 交给脚本。

## 文件

| 文件 | 用途 |
|---|---|
| [START_HERE.md](START_HERE.md) | 使用说明、启动提示词、命令模板 |
| [SKILL.md](SKILL.md) | 分阶段执行规则与验收门禁 |
| [DATA_CONTRACT.md](references/DATA_CONTRACT.md) | 标准化数据接口与指标口径 |
| [PROTOCOL.md](references/PROTOCOL.md) | 分析、统计、图表与报告规范 |
| [STAGE_PROMPTS.md](references/STAGE_PROMPTS.md) | 可逐条复制给模型的提示词 |
| [analyze_normalized.py](scripts/analyze_normalized.py) | Python 标准库数值分析器 |

## 自测

需要 Python 3.9 或更新版本，不需要安装第三方依赖：

```sh
python -I scripts/analyze_normalized.py self-test
```

脚本不直接解析未知格式的 aisbench 文件。标准化输入及完整运行命令见 [START_HERE.md](START_HERE.md)。

## 分别回答四个问题

1. 混部与分离的 TPOT 差距是否随输出变长缩小？
2. 是否符合 `ΔTPOT = A / (实际输出 token 数 - 1) + δ` 的有限开销摊薄形态？
3. 在已测长输出区间内，差异是否进入明确的业务容差？
4. 是否有 token 时间线、prefill 事件或控制实验支持具体干扰机制？

差距缩小不等于因果已证实；差异不显著不等于等效；模型截距接近零不等于无限长度下严格相等。

## 数据安全

原始数据、标准化数据和分析结果请存放在仓库外。本仓库只用于方法、提示词和代码；不要提交内部压测记录、日志、凭据或模型文件。
