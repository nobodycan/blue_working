# Worker Diagnostics：第十步出现 16 个 worker / AssertionError

更新：2026-10-22。适用于本目录 README 第十步的 D 容器采样检查，目标配置为每实例 TP=8、DP=1、MRV1。

本文给出完整的只读检查命令；目标机器不需要 Git，不修改源码、不停止服务、不删除或移动采样记录。

## 1. 先确认失败的是哪条断言

README 第十步中，`python - <<'PY'` 之后按 Python 代码行计数，第八行是：

```python
assert len(workers)==8
```

如果终端先显示：

```text
capacity workers: 16
...
AssertionError
```

说明上述“记录中的 worker 数必须为 8”的检查失败。它不是模型推理异常的充分证据，也不表示 Batch Size=16。

原检查的统计方式是：

```python
workers={(r['host'],r['pid'],r['device']) for r in rows}
```

其中 `rows` 来自 `$RUN/d-capacity` 中全部 `*.jsonl` 的 `capacity_post_kv_before_graph` 记录，没有按当前启动轮次过滤。不同 PID 会算作不同 worker；相同 `(host,pid,device)` 的重复行不会单独增加数量。

如果解决网络问题期间曾在同一采样目录下重启 D，可能同时留下上次 8 个 PID 和本次 8 个 PID，得到 16。也可能存在重复启动的服务、其他实例/容器的记录或实际启动参数不符；当前尚未取得进程证据，不能确定是哪一种。

**不要把断言改成 16、删除断言，或直接按 device 去重后继续性能测试。**

## 2. 在 D 容器执行只读检查

进入执行 README 第十步检查的同一个 D 容器，复制以下整段。先加载原来的环境文件，不改 `RUN_ID`：

```bash
source /tmp/pdtest030_slo/test.env

python - <<'PY'
import json
import os
from pathlib import Path
from datetime import datetime

root = Path(os.environ['RUN']) / 'd-capacity'
print('DIRECTORY:', root)
print('CURRENT_CONTAINER_HOST:', os.uname().nodename)
if not root.is_dir():
    raise SystemExit('目录不存在：先核对是否进入正确 D 容器、RUN_ID 和采样路径')

groups = {}
errors = []
files = sorted(root.glob('*.jsonl'))
for path in files:
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            if r.get('kind') != 'capacity_post_kv_before_graph':
                continue
            key = (r['host'], int(r['pid']), str(r['device']))
            groups.setdefault(key, []).append(r)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            errors.append(f'{path.name}:{lineno}: {exc}')

print('FILES:', len(files))
print('RECORDED_WORKERS:', len(groups))
exists_count = 0
for (host, pid, device), rows in sorted(groups.items()):
    proc = Path('/proc') / str(pid)
    exists = proc.exists()
    exists_count += int(exists)
    try:
        cmd = (proc / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
    except OSError:
        cmd = '<unavailable>'
    r = min(rows, key=lambda x: x.get('time_ns', 0))
    ts = r.get('time_ns', 0)
    stamp = datetime.fromtimestamp(ts / 1e9).isoformat(timespec='seconds') if ts else 'UNKNOWN'
    print(f'HOST={host} PID={pid} DEVICE={device} PROC_EXISTS={exists}')
    print(f'  FIRST_RECORD_TIME={stamp} ROWS={len(rows)}')
    print(f"  TP={r.get('tp_size')} DP_RANK={r.get('dp_rank')}")
    print(f"  RUNNER={r.get('runner_module')}")
    print(f'  CMD={cmd[:160]}')

print('PID_EXISTS_COUNT:', exists_count)
print('PID_ABSENT_COUNT:', len(groups) - exists_count)
print('PARSE_ERRORS:', len(errors))
for error in errors[:10]:
    print('PARSE_ERROR:', error)
print('仅输出诊断信息；以上数量不代表本轮审计已经通过。')
PY

npu-smi info
ps -eo pid,ppid,lstart,args | grep -Ei '[v]llm|[m]ultiproc|[w]orker'
```

脚本会输出每组记录的 HOST、PID、device、首次记录时间（容器本地时区）、TP、DP rank、runner 和当前 PID 的命令行。

`PID_EXISTS_COUNT` / `PID_ABSENT_COUNT` 按记录中的 `(host,pid,device)` 分组计数，不等于唯一操作系统进程数量。`PROC_EXISTS` 只检查这个数字 PID 在当前容器的 `/proc` 是否存在；它不是 worker 身份校验。

注意以下限制：

- PID 可能被其他进程复用；`PROC_EXISTS=True` 必须结合 CMD、启动时间及 NPU 进程信息解释。
- 不同主机/容器可能有相同数字 PID；其他 HOST 的记录不能直接与本容器 `/proc` 对应。
- `npu-smi` 与容器中的 `ps` 可能展示不同 PID 命名空间，需要结合实际宿主机进程映射判断。
- `PARSE_ERRORS>0` 时先保留并检查具体文件/行，不能把未解析的记录静默忽略后判定通过。

## 3. 按结果决定下一步

| 观察结果 | 判断和后续动作 |
|---|---|
| 记录 16 组，8 组 PID 存在、8 组不存在，并且有两组启动时间 | 支持“历史采样与本次采样混入”的判断；还需核对当前进程身份与配置 |
| 16 组 PID 都存在 | 看 CMD、启动时间、父子关系及 NPU 占用，检查是否同时运行多个 D 实例；不能仅凭数量认定全是 worker |
| 出现多个 HOST | 检查目录是否被共享、导入或沿用其他容器记录，也可能是容器重建后沿用旧目录 |
| 本次记录的 TP 不为 8，或出现不符合预期的 DP rank | 核对实际启动参数，不能将结果套用到本轮 TP8/DP1 口径 |
| runner 不是 `vllm_ascend.worker.model_runner_v1` | 与本版采样适配不符，先处理 runner/启动配置 |
| 没记录、目录不存在或有解析错误 | 先核对容器、RUN_ID、日志路径、补丁与采样状态，暂不进行长请求 |

同一 HOST、两组不同时间的 8 个 PID，是重要线索；它仍需与实际重启历史、当前服务进程对应。不要只保留数值最大的 8 个 PID，也不要只取每卡时间最新的一条记录：这些做法可能拼出不属于同一次引擎启动的集合。

## 4. 如果确认为历史采样混入

处理顺序：

1. 保留当前目录和模型日志，记录旧 RUN_ID。
2. 正常停止本轮旧 D 模型及相关测试进程，确认旧 worker 已退出；不要批量终止所有 Python。
3. 按 README 为下一轮使用新的 RUN_ID，并让 P、D、代理/监控和测试结果保持同一轮次配置。
4. 重新启动模型，使进程从启动时就把 capacity、transfer、step 记录写入新目录。
5. 重新执行短 PD 请求，再执行 README 第十步检查。

**仅在新终端修改 RUN_ID 或新建一个空目录，不会改变已经运行的 D 进程的环境变量和采样输出位置。**capacity 记录在模型启动阶段产生，清掉旧文件后仅发送短请求也不会重新生成启动快照。

如果确认实际同时存在多个 D 服务，先识别各自实例及启动来源，再正常停止本次不需要的实例；不要直接按照采样文件中的历史 PID 批量执行 kill。

本文件暂不提供自动清理或自动挑选“当前 8 个 worker”的命令，避免在身份未确认时丢失证据。没有取得本机诊断输出前，不预判应停止哪个进程或移动哪个目录。

## 5. 第十步最终仍需通过哪些检查

| 项目 | 合格要求 |
|---|---|
| 采样来源 | 来自本次 D 引擎启动，没有其他轮次混入 |
| worker | 8 个预期 TP worker，身份与实际运行实例对应 |
| 并行配置 | 本轮 TP=8、DP=1 |
| runner | `vllm_ascend.worker.model_runner_v1` |
| KV 读取 | 本轮短请求实际产生成功、bytes>0 的 KV read；不能借用旧成功记录 |
| 请求与日志 | 正常完成，核对 usage 与同一时间段 P/D/代理日志，无新增传输错误 |

README 原代码的 `any(successful_read)` 同样扫描整个 transfer 目录。如果沿用旧目录，即使它通过，也可能命中旧轮次的成功记录；因此应先保证本次采样目录来源清楚。

以上通过后才进入 128K/1K、Batch Size 和纯 D 平均 TPOT ≤30 ms / ≤50 ms 的测试。

## 6. 不方便拷贝长输出时，只回报这些值

```text
RECORDED_WORKERS =
PID_EXISTS_COUNT =
PID_ABSENT_COUNT =
PARSE_ERRORS =
HOST 是一个还是多个 =
两组记录的时间是否明显分开 =
D 是否在相同 RUN_ID 下重启过 =
当前记录 TP / DP_RANK / RUNNER 是否符合预期 =
```

本文根据仓库中第十步检查代码和 capacity 记录写入方式整理。尚未取得这次 D 容器的只读检查输出，未确认根因，未在你的 A2 上执行诊断、重启或性能测试。
