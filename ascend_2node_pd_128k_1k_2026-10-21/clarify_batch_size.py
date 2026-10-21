from pathlib import Path
root=Path(r'D:\work\ascend_2node_pd_128k_1k_2026-10-21')
p=root/'README-body.md'
s=p.read_text(encoding='utf-8')
s=s.replace('版本：2026-10-21。','版本：2026-10-21，修订 1.1（明确 BS=Batch Size）。',1)
anchor='固定口径：'
notation='''### 符号约定：BS 始终指 Batch Size

本文中的 **BS（Batch Size）是一次模型执行实际处理的序列数**。本任务首先关注 Decode Batch Size。客户端并发使用独立符号 C。

| 符号/参数 | 含义 | 本任务怎样使用 |
|---|---|---|
| `BS_D(t)`，原文简写 `B_D(t)` | D 某次调度实际处理的序列数 | 从 D worker 逐步记录得到，作为实测 Batch Size |
| `BS_D*`，原文简写 `B*` | 选定的、已验证有效的 Decode Batch Size | 用于评估对应 HBM 和 Decode 性能 |
| `BS_P(t)` | P 某次调度实际处理的序列数 | 解释 TTFT 的批处理行为，另记本步各请求的 Prefill token 数 |
| `C` / `--concurrency` | 客户端同时在途的请求数 | 后续按导师要求，**将其数值设置为实测 `BS_D*`** |
| `--batch` | 离线实验提交的目标批次序列数 `BS_req` | 必须核对实际 `BS_D(t)` 是否等于该值 |
| `--max-num-seqs` | 引擎配置的序列数上限 | 是配置上限，不能作为实测 BS |

正文及脚本文件名保留 B 作为 Batch Size 的简写。`C=BS_D*` 是后续压测的**数值赋值**：不表示 Batch Size 与并发是同一个概念，也不保证后续运行的实际 D batch 仍等于该数值。P 和 D 的 Batch Size 也须分别测量。

你的实验链条应读作：**实测纯 D 的 Batch Size → 评估该 BS 对应的 HBM → 测 C=1 的 TTFT → 设置 C=实测 BS，再测 TTFT，同时复核新的 P/D 实际 BS。** 如果只得到 BS 分布、没有稳定的固定批次窗口，应先交付分布和有效窗口信息，不能直接把分布最大值当作稳定 BS。

'''
assert s.count(anchor)==1
s=s.replace(anchor,notation+anchor,1)
s=s.replace('| D：真实 PD、并发 B | 验证导师提出的缩放关系 |','| D：真实 PD、C=实测 BS_D* | 将实测 Batch Size 的数值用作客户端并发，验证缩放关系 |')
s=s.replace('- 把 `C` 记作客户端并发，把 `B_D(t)` 记作某次 D 调度实际参与计算的序列数。TP8 的一个批次 B 是 B 条序列，**不是 8B 条**。','- 以 `BS_D(t)` 记录一次 D 执行的实际 Batch Size，简写 B。TP8 的一个批次 BS=B 是 B 条序列，8 张 NPU 共同执行这些序列，**不是 8B 条独立请求**。')
start=s.index('### 1.1 关于“显存没满，TTFT 就是单并发的 B 倍”')
end=s.index('定义并同时报告：',start)
s=s[:start]+'''### 1.1 明确 Batch Size 后，怎样验证 TTFT 缩放关系

你的原始要求是：先测得纯 D 的 `BS_D*=B`，再将客户端并发设置为 `C=B`，比较这次 TTFT 与 `C=1` 的 TTFT。这个实验可以成立。**但“D 的 BS=B 且 HBM 没满”仍不足以推出 `TTFT(C=B)=B×TTFT(C=1)`。**

原因是，D 的 Batch Size 主要描述本次 Decode 并行处理多少条序列；端到端 TTFT 还由 P 的实际 Batch Size、Prefill token 调度、排队、KV 传输和 D 首输出决定。`C=B` 时，`BS_P(t)` 和 `BS_D(t)` 都要重新观察。

如果 P 确实将 B 条请求作为同一个完整 Prefill 批次处理，定义：

```text
T_P(B) = P 完成这个 B 序列 Prefill 批次所需的墙钟时间
S_P(B) = B × T_P(1) / T_P(B)
```

`S_P(B)=1` 才表示该批次相对逐条处理没有获得吞吐加速；`S_P(B)>1` 时，`T_P(B)<B×T_P(1)`。HBM 能容纳该批次只提供容量条件，不能决定 S_P(B)。即使 `T_P(B)` 接近 B 倍，端到端 TTFT 还要检查其他阶段，不能直接替换成 Prefill 时间。

另一个边界例子是：每请求 P 服务时间恰好为 T、B 个请求几乎同时到达、P 逐条串行处理且其他开销可忽略。这时各请求 TTFT 约为 `T,2T,…,BT`，平均值约为 `(B+1)/2×T`，最后一条约为 `B×T`。**这个例子描述逐条串行调度，不是 BS=B 的一次 Prefill 批处理预测，也不用于推定导师具体指哪一种情况。**

因此，保留你提出的 B 倍关系作为待验证假设；测量中同时记录平均 TTFT、最慢请求 TTFT，以及实际 P/D Batch Size。

'''+s[end:]
s=s.replace('### 6.4 计算口径和如何选 B','### 6.4 计算口径、记录实测 BS，并选定本次被测 BS')
s=s.replace('| B 请求值 | 8 worker 实际 B |','| 设定 Batch Size（BS_req） | 8 worker 实测 Decode Batch Size（BS_D） |')
s=s.replace('B 的选取要有规则：','''先填写每个有效点的**实测 Batch Size**。如果导师只是要求记录当前 BS，不需要先找最大 BS 或吞吐最优 BS。`BS_D*` 表示本次选定的被测 Batch Size，不自动代表最优点。

如需从多个有效 BS 选取一个点，才应用下面的规则：''')
s=s.replace('## 10. 实验 D：以 B* 为并发数，验证真实 PD','## 10. 实验 D：将实测 Batch Size 的数值设为并发数（C=BS_D*）')
s=s.replace('假设实验 A 按既定规则选出了 B*=8。下面 `8` 只是示例，替换为**你实际测得并选定的值**。','''假设经过逐步记录核验，选定的实测 Decode Batch Size 为 `BS_D*=8`，则本轮设置 `C=8`。下面 `B=8` 保存这个 Batch Size 数值，`--concurrency "$B"` 将该数值赋给客户端并发；两者语义不同。

如果 8 只在离线隔离实验中通过，它仍是 D 能力参考值。下面真实 PD 运行必须重新验证实际 `BS_D(t)`，才能报告“PD 下达到 BS=8”。若导师要求 BS 必须首先取自在线 PD，应先用第 10.3 节持续负载与 `report_batches.py` 记录在线 BS，再回到本节设置 `C=该实测 BS`。输入/输出、版本和内存预算均保持一致。''')
s=s.replace('| 指标 | A：D 隔离固定 B* | C：PD 单请求 | D：PD 并发 B* |','| 指标 | A：D 隔离固定 BS_D* | C：PD 单请求 | D：PD，C=BS_D* |')
s=s.replace('| 实际 Decode B min/P50/P95/max | 实测 | 实测 | 实测 |','| 实际 Decode Batch Size min/P50/P95/max | 实测 | 实测 | 重新实测，不能从 C 直接填写 |\n| 实际 Prefill Batch Size / 每步 Prefill token 数 | 设置阶段单列 | 实测 | 实测 |')
s=s.replace('D 隔离实验中，按 ___ 规则选定 B*=___；8 个 worker 实际 B 均为 ___，','D 隔离实验中，记录实测 Batch Size，并选定 BS_D*=___；8 个 worker 实际 BS 均为 ___，')
s=s.replace('真实 PD 下，C=1 的平均 TTFT=___；C=B* 的平均 TTFT=___，','将选定实测 Batch Size 的数值赋给客户端并发 C=BS_D*。\n真实 PD 下，C=1 的平均 TTFT=___；C=BS_D* 的平均 TTFT=___，')
s=s.replace('P 实际批处理/队列为 ___，D 实际 batch 分布为 ___，','P 实际 Batch Size/Prefill token 调度/队列为 ___，D 实际 Batch Size 分布为 ___，')
p.write_text(s,encoding='utf-8',newline='\n')
print('Updated terminology, batch interpretation, TTFT hypothesis, and result tables.')
