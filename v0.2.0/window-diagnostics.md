# Window Diagnostics：C=1 正常，C=2 开始没有共同窗口

更新：2026-10-22。适用于 v0.2.0 README 的 `run_waves.sh` 扫描，输入 131072 token、输出 1024 token，后续分别按纯 D 实例平均 TPOT ≤30ms / ≤50ms 筛选 Batch Size。

本文包含完整的只读分析命令。目标机器不需要 Git，不重新发送请求、不重启模型、不修改源码或筛选阈值。

## 1. 当前现象和含义

用户反馈：C=1 没出现下面的提示，从 C=2 开始出现：

```text
No eligible full-overlap window: C=2 repeat=1
```

这意味着本轮窗口分析没有成功返回合格候选；**不表示 TPOT 已超过 30ms / 50ms，也不能认定 D 的最大 Batch Size=1。**

原窗口规则要求找到一段所有 C 个请求同时满足以下条件的区间：

- 每个请求的累计输出 token 数均在 32～896 之间，含边界。
- 共同窗口持续至少 5 秒，并且每个请求都有输出 token 进展。
- 请求、usage、输出 token ID 等数据通过分析脚本校验。

这个区间由客户端输出事件寻找，随后仍要结合 D worker、KV read 和 trace 审计，才可作为纯 D 性能证据。

`run_waves.sh` 只是并发提交请求，没有设置“所有请求的 KV 都就绪后同时进入 Decode”的屏障。P 预填充、KV 传输、D 调度以及客户端事件交付都可能影响观察到的重叠；仅凭这条提示不能确定根因。

另一个需要区分的情况：原 Shell 对 `pd_plateau.py` 的所有非零返回都打印同一句提示。分析脚本如果出现 Traceback / AssertionError，也会走到这里，不一定只是窗口不足。

## 2. P 容器：读取 C=2 的两个请求时间

在执行 `run_waves.sh` 的 P 容器运行。默认检查 `C2-r1`；如需查第二轮，改为 `export WAVE=C2-r2`。只检查已经结束的波次，避免读取仍在写入的结果。

```bash
source /tmp/pdtest030_slo/test.env
export WAVE=C2-r1

python - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ['RUN']) / 'waves'
prefix = os.environ.get('WAVE', 'C2-r1')
path = root / f'{prefix}.requests.jsonl'
print('WAVE =', prefix)
print('DIRECTORY =', root)
if not path.is_file():
    raise SystemExit('请求记录不存在：核对 RUN_ID、波次名称，并确认该波次已经完成')

rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
print('REQUEST_COUNT =', len(rows))
for i, row in enumerate(rows, 1):
    events = row.get('token_events', [])
    positive = [e for e in events if e.get('tokens', 0) > 0]
    count, reach32, exceed896 = 0, None, None
    for e in events:
        count += e.get('tokens', 0)
        if count >= 32 and reach32 is None:
            reach32 = e['t_s']
        if count > 896 and exceed896 is None:
            exceed896 = e['t_s']
    print(f'REQUEST_{i}:')
    print('  REQUEST_ID =', row.get('request_id'))
    print('  ERROR =', row.get('error'))
    print('  FIRST_OUTPUT_s =', positive[0]['t_s'] if positive else None)
    print('  LAST_OUTPUT_s  =', positive[-1]['t_s'] if positive else None)
    print('  REACH_32_s     =', reach32)
    print('  EXCEED_896_s   =', exceed896)
    print('  EVENT_TOKEN_TOTAL =', count)

window = root / f'{prefix}.window.json'
if window.is_file():
    w = json.loads(window.read_text())[0]
    print('PLATEAU_VALID =', w.get('plateau_valid'))
    print('PLATEAU_s =', w.get('plateau_s'))
else:
    print('WINDOW_FILE_MISSING：检查分析阶段是否有异常，或该波次尚未完成')
PY
```

输出中的 `REQUEST_1` / `REQUEST_2` 按文件记录顺序编号，不保证等于请求发起或完成顺序；用 `REQUEST_ID` 对照原始日志。

所有 `_s` 时间单位都是秒，均相对于同一波客户端测试的计时起点，可以相互比较，不是两台服务器的绝对时间。

## 3. 根据时间结果判断

| 结果 | 含义与下一步 |
|---|---|
| 一个请求的 FIRST_OUTPUT 晚于另一个的 LAST_OUTPUT | 客户端观察到的输出没有重叠，本波无法提供目标共同窗口 |
| 一个请求 EXCEED_896 早于另一个 REACH_32 | 两个请求没有同时处于规定 token 范围的候选区间 |
| `PLATEAU_s` 有值但小于 5 | 有可计算的共同窗口，但时长不足 |
| `PLATEAU_VALID=False` 且没有 `PLATEAU_s` | 没形成可计算的有效窗口；结合 token 事件检查，不能仅解释成服务器慢 |
| ERROR 非空、token 总量异常或窗口分析 Traceback | 优先处理请求/数据校验问题 |
| 两个输出区间接近，仍无法形成窗口 | 检查 token 事件是否集中成批交付，再对照 D worker 时间线 |

`REACH_32` 是观察到累计输出达到或超过 32 的事件时间；`EXCEED_896` 是首次超过 896 的事件时间。如果同一批流式事件跨过多个阈值，这些时间只反映客户端交付情况。

这两个阈值时间用于定位时间差；实际是否达到 5 秒，仍以 `pd_plateau.py` 按全部事件计算的结果为准。不能只用两个阈值相减就替代原脚本的窗口判定。

**客户端输出不重叠，不能单独证明 NPU 上完全没有 Decode 重叠。**缓冲或事件合并也可能影响观察，需要设备侧采样进一步定位。

## 4. 单独重跑窗口分析，区分无窗口和程序异常

如果第 2 步结果不明确，在同一 P 容器执行：

```bash
source /tmp/pdtest030_slo/test.env
export WAVE=C2-r1

python "$WORK/pd_plateau.py" \
  --summary "$RUN/waves/$WAVE.json" \
  --out "$RUN/window-debug-$WAVE.json"
```

这只重新分析已有请求数据，不发送新请求。输出到单独的 debug 文件，不覆盖原来的 `waves/*.window.json`；对同一 WAVE 重复执行会更新该 debug 文件。

- 正常打印 JSON，`plateau_valid=false`：按窗口时间和 token 事件排查；该脚本此时预期返回 2。
- Traceback / AssertionError：先保存完整异常和具体行号，确认请求长度、usage、token ID 和源文件是否匹配。
- `plateau_valid=true`：只说明候选窗口成立；还要运行 worker/传输审计，不直接宣称纯 D 已验证。

## 5. 定位后如何继续

如果时间显示两个请求进入输出阶段明显错开，优先对照同一波的请求 ID，检查 P 预填充完成、KV 传输完成、D 开始调度以及客户端交付时间。先确定时间差发生在哪一段，再决定调整 P 的供给能力、调度配置或测试组织方式。

在确认根因前：

- 不把 C=2 无窗口记成“TPOT 超标”或“HBM 容量不足”。
- 不直接把最大 BS 记录为 1；当前只能说 C=2 尚未获得合格窗口。
- 不通过降低 5 秒阈值、增加输出长度或去掉 worker 审计来让本轮通过；若改变测试口径，应另立轮次并明确记录。
- 不直接假定更大并发都失败或都成功；该现象不保证随并发单调变化。

如果当前波次组织方式持续无法让目标数量请求共同 Decode，应将结果记录为“本次负载未形成目标纯 D 窗口”，再设计能确保目标请求就绪和足够长共同解码区间的测试。原 `run_waves.sh` 不能保证一定扫出纯 D 的容量上限。

## 6. 不便拷贝长输出时，回报这些值

```text
WAVE = C2-r1（或实际名称）
REQUEST_COUNT =
请求1：FIRST_OUTPUT_s / LAST_OUTPUT_s / REACH_32_s / EXCEED_896_s =
请求2：FIRST_OUTPUT_s / LAST_OUTPUT_s / REACH_32_s / EXCEED_896_s =
两个请求 ERROR 是否均为 None =
两个请求 EVENT_TOKEN_TOTAL 是否均为 1024 =
PLATEAU_VALID =
PLATEAU_s =（没有该值就填“无”）
分析前是否有 Traceback / AssertionError =
```

本文按仓库中的 `run_waves.sh`、`bench_client.py` 和 `pd_plateau.py` 内容整理。尚未取得本次 C=2 的时间数据，未确认是 P 供给、KV 传输、D 调度还是客户端交付导致，未在用户 A2 上执行诊断或性能测试。
