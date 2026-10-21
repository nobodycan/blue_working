# vLLM 0.30.0 + vLLM-Ascend 0.30.0rc2：采样模块完整修改稿

本稿只针对前文手册中的源码采样方案。P 端继续部署代理和压测客户端，D 端运行模型与采样。下文列出全部替换文件、实际框架改动、安装与恢复命令。每个文件都提供完整内容，目标机器不需要 Git。

适用边界：这是一套明确选择 Model Runner V1（MRV1）的实验配置。不要把 MRV1 测试成绩当作默认 runner 或 MRV2 的成绩。若目标是保留当前 MRV2 配置，则不要应用本稿的启动修改；需要针对 MRV2 的实际路径另做采样适配。两台同时使用 vLLM 0.30.0 与 vLLM-Ascend 0.30.0rc2，并核对使用同一镜像构建、权重与 KV 配置。

已完成官方源码上的离线安装/恢复检查和 CPU 模拟数据检查；没有在你的 A2 上执行模型推理。`--check` 验证版本和源码位置，不表示已验证全部运行时接口、正确性或性能。

## 1. 全部文件与改动范围

| 文件 | 执行/放置位置 | 修改内容 |
|---|---|---|
| `audit_patch.py` | D 容器 | 合并容量、worker step、两处 KV read 的安装；用 AST 定位多行语句，完整预检后才写入，提供备份、校验与统一恢复 |
| `capacity_probe.py` | D 容器 | 使用新 KV 描述字段；按 NPU 底层 storage 去重；保留旧 CSV 所需字段；记录 KV read 结束时间、耗时、字节数和失败状态 |
| `step_probe.py` | D 容器 | 记录实际请求 batch 与 scheduled token 数；用保守 CPU 计数分类 decode；不把 draft token 当成独立请求，不逐步同步 NPU |
| `patch_steps.py` | D 容器 | 禁用旧的第二次安装操作，避免同一位置插入两次采样 |
| `start_p.sh` | P 容器 | 明确 `VLLM_USE_V2_MODEL_RUNNER=0`；其余模型参数保持上一版配置 |
| `start_d.sh` | D 容器 | 同样明确 MRV1；其余模型参数保持上一版配置 |

真正修改已安装框架源码的只有两个文件：

1. `vllm_ascend/worker/model_runner_v1.py`：在 KV cache 张量初始化返回后插入容量采样，在 `execute_model` 的 `_update_states` 返回后插入 CPU 请求状态采样。
2. `vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_hybrid_connector.py`：将两处同步读取调用替换为记录返回值与耗时的包装调用，保留原返回值和异常传播。

不修改模型权重。容量快照属于 graph capture 前的启动时刻，不代表运行峰值；参数/buffer storage 与 KV storage 分别去重，不能默认把两类数值直接相加。仅记录同步 KV read 的区间，完整纯 D 验证仍须结合 trace 核对其他传输、变换算子及重算。

## 2. 框架源码中实际插入/替换的全部语句

KV 多行初始化调用本身保持原样，其后增加：

```python
# ASCEND_BENCH_V030RC2:capacity
from capacity_probe import dump_capacity
dump_capacity(self, kv_caches)
```

`execute_model` 中 `deferred_state_corrections_fn = self._update_states(...)` 整条语句之后增加：

```python
# ASCEND_BENCH_V030RC2:step
from step_probe import snapshot
snapshot(self, scheduler_output)
```

connector 中两处下列语句：

```python
ret = self.engine.batch_transfer_sync_read(session_id, src_list, dst_list, length_list)
```

均替换为：

```python
# ASCEND_BENCH_V030RC2:read
from capacity_probe import measured_read
ret = measured_read(self.engine, session_id, src_list, dst_list, length_list, req_meta)
```

以上由下面的安装脚本自动执行，缩进与插入位置由 AST 获取，不要求你手改框架行号。若厂商改动导致语句或函数不符合检查，会停止而不会绕过检查。

## 3. 先停止服务，再创建完整替换文件

D 打补丁前先正常停止当前 D 服务。启动 Shell 增加 MRV1 设置后，P/D 都需要重新启动才能生效。停止方式使用原启动终端 Ctrl+C 或现有服务管理系统；不要批量 kill Python 进程。先保留当前启动参数与日志。

P、D 容器各执行以下初始化；每次新终端也需要设置 WORK：

```bash
export WORK=/tmp/pdtest
mkdir -p "$WORK"
```

以下四个 Python 文件在 D 容器完整创建。两个 Shell 则分别放在对应 P/D 容器。每个代码块已包含创建命令，不需要下载其他源码或引用未给出的公共 Shell。


### 文件 1：audit_patch.py（完整内容）

```bash
cat > "$WORK/audit_patch.py" <<'PD030_FILE_EOF'
#!/usr/bin/env python3
"""Diagnostic hooks for vLLM 0.30.0 + vLLM-Ascend 0.30.0rc2, MRV1 only."""
import argparse
import ast
import hashlib
from importlib.metadata import version
from importlib.util import find_spec
import json
import os
from pathlib import Path
import stat
import tempfile

MARK = "ASCEND_BENCH_V030RC2"
FILES = ("worker/model_runner_v1.py",
         "distributed/kv_transfer/kv_p2p/mooncake_hybrid_connector.py")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def method(tree, name):
    cls = [n for n in tree.body if isinstance(n, ast.ClassDef)
           and n.name == "NPUModelRunner"]
    matches = [n for c in cls for n in c.body
               if isinstance(n, ast.FunctionDef) and n.name == name]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one NPUModelRunner.{name}")
    return matches[0]


def assignment(scope, target, function):
    return [n for n in ast.walk(scope) if isinstance(n, ast.Assign)
            and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id == target and isinstance(n.value, ast.Call)
            and ast.unparse(n.value.func) == function]


def edit(source, changes):
    lines = source.splitlines(keepends=True)
    for node, code, insert in sorted(changes, key=lambda x: x[0].lineno, reverse=True):
        indent = " " * node.col_offset
        replacement = [indent + line + "\n" for line in code.splitlines()]
        if insert:
            lines[node.end_lineno:node.end_lineno] = replacement
        else:
            lines[node.lineno - 1:node.end_lineno] = replacement
    result = "".join(lines)
    ast.parse(result)
    return result.encode("utf-8")


def prepare(original):
    runner, connector = [data.decode("utf-8") for data in original]
    for source in (runner, connector):
        if any(m in source for m in (MARK, "ASCEND_BENCH_AUDIT_V1", "V4_2NODE_STEP_AUDIT")):
            raise RuntimeError("Existing diagnostic markers: restore previous patches first")
    tree = ast.parse(runner)
    caps = assignment(method(tree, "initialize_kv_cache"), "kv_caches",
                      "self.initialize_kv_cache_tensors")
    steps = assignment(method(tree, "execute_model"), "deferred_state_corrections_fn",
                       "self._update_states")
    if len(caps) != 1 or len(steps) != 1:
        raise RuntimeError("Runner AST mismatch; no files changed")
    keywords = {k.arg for k in caps[0].value.keywords}
    if not {"kernel_block_sizes", "kv_cache_allocation_context"} <= keywords:
        raise RuntimeError("Unexpected KV initialization signature; no files changed")
    runner_new = edit(runner, [
        (caps[0], f"# {MARK}:capacity\nfrom capacity_probe import dump_capacity\ndump_capacity(self, kv_caches)", True),
        (steps[0], f"# {MARK}:step\nfrom step_probe import snapshot\nsnapshot(self, scheduler_output)", True),
    ])
    reads = assignment(ast.parse(connector), "ret", "self.engine.batch_transfer_sync_read")
    expected = ["session_id", "src_list", "dst_list", "length_list"]
    if len(reads) != 2 or any([ast.unparse(x) for x in n.value.args] != expected
                              or n.value.keywords for n in reads):
        raise RuntimeError("Connector AST mismatch; expected two KV reads; no files changed")
    code = (f"# {MARK}:read\nfrom capacity_probe import measured_read\n"
            "ret = measured_read(self.engine, session_id, src_list, dst_list, length_list, req_meta)")
    connector_new = edit(connector, [(n, code, False) for n in reads])
    return (runner_new, connector_new)


def atomic_write(path, data):
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, name = tempfile.mkstemp(prefix=path.name + ".pd030.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--restore", action="store_true")
    p.add_argument("--root", type=Path, help="Offline source tree for validation only")
    a = p.parse_args()
    if a.root is None:
        spec = find_spec("vllm_ascend")
        if spec is None or spec.origin is None:
            raise SystemExit("vllm_ascend not found")
        root = Path(spec.origin).parent
    else:
        root = a.root
    paths = [root / name for name in FILES]
    backups = [Path(str(path) + ".pd030.bak") for path in paths]
    state_path = root / ".pd_audit_v030rc2.json"

    if a.restore:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        originals = [b.read_bytes() for b in backups]
        for path, data, row in zip(paths, originals, state):
            if digest(data) != row["original_sha256"]:
                raise RuntimeError(f"Backup checksum mismatch: {path}")
            if digest(path.read_bytes()) not in (row["original_sha256"], row["patched_sha256"]):
                raise RuntimeError(f"Source changed after patch; refusing overwrite: {path}")
        for path, data in zip(paths, originals):
            atomic_write(path, data)
        state_path.unlink()
        for backup in backups:
            backup.unlink()
        print("RESTORED: both source files match their original checksums")
        return

    if a.root is None:
        core, ascend = version("vllm"), version("vllm-ascend")
        if core.split("+")[0] != "0.30.0" or ascend.split("+")[0] != "0.30.0rc2":
            raise RuntimeError(f"Unsupported versions: vllm={core}, ascend={ascend}")
    if state_path.exists() or any(b.exists() for b in backups):
        raise RuntimeError("Patch state/backups already exist; inspect or --restore first")
    for path in paths:
        for suffix in (".bench.bak", ".steps.bak"):
            if Path(str(path) + suffix).exists():
                raise RuntimeError("Old backup exists; restore old patch first")
    for name in ("capacity_probe.py", "step_probe.py"):
        ast.parse(Path(__file__).with_name(name).read_text(encoding="utf-8"))
    original = [path.read_bytes() for path in paths]
    patched = prepare(original)
    state = [dict(file=name, original_sha256=digest(old), patched_sha256=digest(new))
             for name, old, new in zip(FILES, original, patched)]
    print(json.dumps(state, indent=2))
    if a.check:
        print("CHECK PASSED; no files changed; runtime MRV1/field checks still required")
        return
    for backup, data in zip(backups, original):
        with backup.open("xb") as f:
            f.write(data)
    with state_path.open("x", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    try:
        for path, data in zip(paths, patched):
            atomic_write(path, data)
    except Exception:
        for path, data in zip(paths, original):
            atomic_write(path, data)
        raise
    print("APPLIED: capacity + worker step + two KV read hooks. Restart D with MRV1.")


if __name__ == "__main__":
    main()
PD030_FILE_EOF
```


### 文件 2：capacity_probe.py（完整内容）

```bash
cat > "$WORK/capacity_probe.py" <<'PD030_FILE_EOF'
"""Diagnostic startup storage snapshot and synchronous Mooncake read events."""
import json
import operator
import os
from pathlib import Path
import socket
import threading
import time

_lock = threading.Lock()


def write_record(directory, kind, record):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname()
    row = dict(kind=kind, host=host, pid=os.getpid(), time_ns=time.time_ns())
    row.update(record)
    with _lock, (root / f"{host}-{os.getpid()}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def storages(value):
    import torch
    found = {}
    def visit(obj):
        if isinstance(obj, torch.Tensor) and obj.device.type == "npu":
            storage = obj.untyped_storage()
            key = (str(obj.device), storage.data_ptr(), storage.nbytes())
            found[key] = storage.nbytes()
        elif isinstance(obj, dict):
            for child in obj.values():
                visit(child)
        elif isinstance(obj, (tuple, list)):
            for child in obj:
                visit(child)
    visit(value)
    return sum(found.values()), len(found)


def dump_capacity(runner, kv_caches):
    directory = os.getenv("AUDIT_CAPACITY_DIR")
    if not directory:
        return
    if runner.vllm_config.use_v2_model_runner:
        raise RuntimeError("This diagnostic supports Model Runner V1 only")
    import torch
    torch.npu.synchronize()
    cfg = runner.kv_cache_config
    tensors = []
    for item in cfg.kv_cache_tensors:
        tensors.append({name: getattr(item, name) for name in
                        ("size", "layers", "layer_stride", "block_stride", "offset", "host_resident")
                        if hasattr(item, name)})
    groups = []
    for index, group in enumerate(cfg.kv_cache_groups):
        spec = group.kv_cache_spec
        entry = dict(index=index, spec_type=type(spec).__name__,
                     layers=list(group.layer_names), spec_repr=repr(spec))
        for name in ("block_size", "page_size_bytes", "real_page_size_bytes"):
            try:
                entry[name] = getattr(spec, name)
            except (AttributeError, NotImplementedError, ValueError, AssertionError, TypeError) as exc:
                entry[name + "_unavailable"] = repr(exc)
        groups.append(entry)
    kv_bytes, count = storages(kv_caches)
    if kv_bytes <= 0:
        raise RuntimeError("No NPU KV backing storage found; diagnostic layout mismatch")
    model_bytes, _ = storages(list(runner.model.parameters()) + list(runner.model.buffers()))
    free, total = torch.npu.mem_get_info()
    write_record(directory, "capacity_post_kv_before_graph", dict(
        runner_module=type(runner).__module__, runner_class=type(runner).__name__,
        dp_rank=runner.vllm_config.parallel_config.data_parallel_rank,
        tp_size=runner.vllm_config.parallel_config.tensor_parallel_size,
        device=str(runner.device), num_blocks=cfg.num_blocks,
        actual_unique_cache_storage_bytes=kv_bytes, unique_cache_storages=count,
        model_parameter_buffer_storage_bytes=model_bytes,
        allocated_bytes=torch.npu.memory_allocated(), reserved_bytes=torch.npu.memory_reserved(),
        free_bytes=free, total_bytes=total, tensors=tensors, groups=groups,
        note="Before graph capture; descriptor sizes are metadata, not additive physical allocations"))


def measured_read(engine, session_id, src_list, dst_list, length_list, req_meta):
    directory = os.getenv("AUDIT_TRANSFER_DIR")
    if not directory:
        return engine.batch_transfer_sync_read(session_id, src_list, dst_list, length_list)
    start = time.perf_counter_ns()
    result, error = None, None
    try:
        result = engine.batch_transfer_sync_read(session_id, src_list, dst_list, length_list)
        try:
            code = operator.index(result)
            if code < 0:
                error = f"return_code={code}"
        except TypeError:
            error = f"unexpected_return_type={type(result).__name__}"
        return result
    except BaseException as exc:
        error = repr(exc)
        raise
    finally:
        stop = time.perf_counter_ns()
        end_wall = time.time_ns()
        write_record(directory, "mooncake_sync_read", dict(
            time_ns=end_wall, request_id=req_meta.get("request_id"),
            remote_request_id=req_meta.get("remote_request_id"),
            duration_ms=(stop-start)/1e6, bytes=sum(int(n) for n in length_list),
            segments=len(length_list), result=result, error=error,
            success=error is None))
PD030_FILE_EOF
```


### 文件 3：step_probe.py（完整内容）

```bash
cat > "$WORK/step_probe.py" <<'PD030_FILE_EOF'
"""CPU request-state snapshots for MRV1; never synchronize NPU per step."""
import atexit
import json
import os
from pathlib import Path
import socket
import time

_buffers = {}
_last_flush = 0.0


def flush():
    for path, rows in _buffers.items():
        if rows:
            with path.open("a", encoding="utf-8") as f:
                f.write("".join(rows))
            rows.clear()


atexit.register(flush)


def snapshot(runner, out):
    directory = os.getenv("AUDIT_STEP_DIR")
    if not directory:
        return
    rows = []
    for rid, scheduled in out.num_scheduled_tokens.items():
        scheduled = int(scheduled)
        if scheduled <= 0:
            continue
        state = runner.requests.get(rid)
        plen = getattr(state, "num_prompt_tokens", None)
        computed = getattr(state, "num_computed_tokens", None)
        previous_drafts = getattr(state, "prev_num_draft_len", None)
        known = plen is not None and computed is not None and previous_drafts is not None
        lower_bound = int(computed) - max(0, int(previous_drafts)) if known else None
        rows.append(dict(request_id=rid, scheduled_tokens=scheduled,
                         prompt_tokens=plen, computed_tokens=computed,
                         previous_draft_tokens=previous_drafts,
                         conservative_computed_tokens=lower_bound,
                         decode=bool(lower_bound >= plen) if known else None))
    if sum(r["scheduled_tokens"] for r in rows) != int(out.total_num_scheduled_tokens):
        raise RuntimeError("Scheduler token counts disagree; diagnostic schema mismatch")
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname()
    row = dict(kind="worker_step", time_ns=time.time_ns(), host=host,
               pid=os.getpid(), device=str(runner.device),
               runner_module=type(runner).__module__, batch_size=len(rows),
               scheduled_tokens=int(out.total_num_scheduled_tokens),
               all_decode=bool(rows) and all(r["decode"] is True for r in rows),
               classification="conservative CPU state after _update_states", requests=rows)
    path = root / f"{host}-{os.getpid()}.jsonl"
    buffer = _buffers.setdefault(path, [])
    buffer.append(json.dumps(row, default=str) + "\n")
    global _last_flush
    now = time.monotonic()
    if len(buffer) >= 32 or now - _last_flush >= 0.25:
        flush()
        _last_flush = now
PD030_FILE_EOF
```


### 文件 4：patch_steps.py（完整内容）

```bash
cat > "$WORK/patch_steps.py" <<'PD030_FILE_EOF'
#!/usr/bin/env python3
"""Old separate step installer is deliberately disabled for this diagnostic."""
raise SystemExit(
    "Do not run the old separate step patch. "
    "audit_patch.py now handles capacity, steps and KV reads together. "
    "Use: python audit_patch.py --check / --apply / --restore"
)
PD030_FILE_EOF
```


### 文件 5：start_p.sh（完整内容）

```bash
cat > "$WORK/start_p.sh" <<'PD030_FILE_EOF'
#!/usr/bin/env bash
# Run inside the Prefill container: vLLM 0.30.0 + vLLM-Ascend 0.30.0rc2, MRV1, 8 NPUs.
# Example: LOCAL_IP=10.10.0.11 NIC=eth0 MODEL=/models/DeepSeek-V4-Flash-w8a8-mtp bash start_p.sh
set -euo pipefail

# Fixed MRV1 diagnostic baseline; also retain for formal comparisons.
export VLLM_USE_V2_MODEL_RUNNER=0

MODEL="${MODEL:-/models/DeepSeek-V4-Flash-w8a8-mtp}"
NIC="${NIC:-eth0}"
: "${LOCAL_IP:?Set LOCAL_IP to the communication IP of this Prefill machine}"

command -v vllm >/dev/null 2>&1 || { echo "ERROR: vllm is not on PATH" >&2; exit 1; }
[[ -f "$MODEL/config.json" ]] || { echo "ERROR: model config not found: $MODEL/config.json" >&2; exit 1; }
[[ -d "/sys/class/net/$NIC" ]] || { echo "ERROR: network interface not found: $NIC" >&2; exit 1; }

export ASCEND_RT_VISIBLE_DEVICES="${ASCEND_RT_VISIBLE_DEVICES:-0,1,2,3,4,5,6,7}"
export OMP_PROC_BIND=false
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-10}"
export PYTORCH_NPU_ALLOC_CONF="${PYTORCH_NPU_ALLOC_CONF:-expandable_segments:True}"
export HCCL_BUFFSIZE="${HCCL_BUFFSIZE:-1024}"
export TASK_QUEUE_ENABLE=1
export HCCL_OP_EXPANSION_MODE=AIV
export VLLM_RPC_TIMEOUT=3600000
export VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=30000
export HCCL_EXEC_TIMEOUT=204
export HCCL_CONNECT_TIMEOUT=1200
export HCCL_IF_IP="$LOCAL_IP"
export GLOO_SOCKET_IFNAME="$NIC"
export HCCL_SOCKET_IFNAME="$NIC"
export TP_SOCKET_IFNAME="$NIC"
export NO_PROXY="${NO_PROXY:-},127.0.0.1,localhost,$LOCAL_IP${P_IP:+,$P_IP}${D_IP:+,$D_IP}"
export no_proxy="$NO_PROXY"

if [[ -f /usr/lib/aarch64-linux-gnu/libjemalloc.so.2 ]]; then
  export LD_PRELOAD="/usr/lib/aarch64-linux-gnu/libjemalloc.so.2${LD_PRELOAD:+:$LD_PRELOAD}"
fi

extra=()
if [[ -n "${KV_BYTES:-}" ]]; then
  extra+=(--kv-cache-memory-bytes "$KV_BYTES")
fi

if [[ "${PROFILE:-0}" == 1 ]]; then
  profile_dir="${PROFILE_DIR:-/tmp/pdtest/profiles/prefill}"
  mkdir -p "$profile_dir"
  profile_json=$(python -c 'import json,sys; print(json.dumps(dict(profiler="torch", torch_profiler_dir=sys.argv[1], torch_profiler_with_stack=False)))' "$profile_dir")
  extra+=(--profiler-config "$profile_json")
fi

exec vllm serve "$MODEL" \
  --host 0.0.0.0 \
  --port "${P_PORT:-8100}" \
  --served-model-name "${SERVED_MODEL:-dsv4}" \
  --tensor-parallel-size 8 \
  --data-parallel-size 1 \
  --enable-expert-parallel \
  --max-model-len "${MAX_MODEL_LEN:-133120}" \
  --max-num-seqs "${MAX_SEQS:-32}" \
  --max-num-batched-tokens "${P_TOKEN_BUDGET:-8192}" \
  --gpu-memory-utilization "${MEM_UTIL:-0.90}" \
  --seed 1024 \
  --quantization ascend \
  --tokenizer-mode deepseek_v4 \
  --trust-remote-code \
  --block-size 128 \
  --no-enable-prefix-caching \
  --enable-chunked-prefill \
  --no-disable-hybrid-kv-cache-manager \
  --async-scheduling \
  --safetensors-load-strategy prefetch \
  --enforce-eager \
  --speculative-config '{"method":"mtp","num_speculative_tokens":1,"enforce_eager":true}' \
  --additional-config '{"enable_cpu_binding":true,"enable_dsa_cp":false}' \
  --kv-transfer-config '{"kv_connector":"MooncakeHybridConnector","kv_role":"kv_producer","kv_port":"30000","engine_id":"0","kv_connector_extra_config":{"prefill":{"dp_size":1,"tp_size":8},"decode":{"dp_size":1,"tp_size":8}}}' \
  "${extra[@]}"
PD030_FILE_EOF
```


### 文件 6：start_d.sh（完整内容）

```bash
cat > "$WORK/start_d.sh" <<'PD030_FILE_EOF'
#!/usr/bin/env bash
# Run inside the Decode container: vLLM 0.30.0 + vLLM-Ascend 0.30.0rc2, MRV1, 8 NPUs.
# Example: LOCAL_IP=10.10.0.12 NIC=eth0 MODEL=/models/DeepSeek-V4-Flash-w8a8-mtp bash start_d.sh
set -euo pipefail

# Fixed MRV1 diagnostic baseline; also retain for formal comparisons.
export VLLM_USE_V2_MODEL_RUNNER=0

MODEL="${MODEL:-/models/DeepSeek-V4-Flash-w8a8-mtp}"
NIC="${NIC:-eth0}"
: "${LOCAL_IP:?Set LOCAL_IP to the communication IP of this Decode machine}"

command -v vllm >/dev/null 2>&1 || { echo "ERROR: vllm is not on PATH" >&2; exit 1; }
[[ -f "$MODEL/config.json" ]] || { echo "ERROR: model config not found: $MODEL/config.json" >&2; exit 1; }
[[ -d "/sys/class/net/$NIC" ]] || { echo "ERROR: network interface not found: $NIC" >&2; exit 1; }

export ASCEND_RT_VISIBLE_DEVICES="${ASCEND_RT_VISIBLE_DEVICES:-0,1,2,3,4,5,6,7}"
export OMP_PROC_BIND=false
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-10}"
export PYTORCH_NPU_ALLOC_CONF="${PYTORCH_NPU_ALLOC_CONF:-expandable_segments:True}"
export HCCL_BUFFSIZE="${HCCL_BUFFSIZE:-1024}"
export TASK_QUEUE_ENABLE=1
export HCCL_OP_EXPANSION_MODE=AIV
export VLLM_RPC_TIMEOUT=3600000
export VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=30000
export HCCL_EXEC_TIMEOUT=204
export HCCL_CONNECT_TIMEOUT=1200
export HCCL_IF_IP="$LOCAL_IP"
export GLOO_SOCKET_IFNAME="$NIC"
export HCCL_SOCKET_IFNAME="$NIC"
export TP_SOCKET_IFNAME="$NIC"
export NO_PROXY="${NO_PROXY:-},127.0.0.1,localhost,$LOCAL_IP${P_IP:+,$P_IP}${D_IP:+,$D_IP}"
export no_proxy="$NO_PROXY"

if [[ -f /usr/lib/aarch64-linux-gnu/libjemalloc.so.2 ]]; then
  export LD_PRELOAD="/usr/lib/aarch64-linux-gnu/libjemalloc.so.2${LD_PRELOAD:+:$LD_PRELOAD}"
fi

extra=()
if [[ -n "${KV_BYTES:-}" ]]; then
  extra+=(--kv-cache-memory-bytes "$KV_BYTES")
fi

if [[ "${PROFILE:-0}" == 1 ]]; then
  profile_dir="${PROFILE_DIR:-/tmp/pdtest/profiles/decode}"
  mkdir -p "$profile_dir"
  profile_json=$(python -c 'import json,sys; print(json.dumps(dict(profiler="torch", torch_profiler_dir=sys.argv[1], torch_profiler_with_stack=False)))' "$profile_dir")
  extra+=(--profiler-config "$profile_json")
fi

exec vllm serve "$MODEL" \
  --host 0.0.0.0 \
  --port "${D_PORT:-8200}" \
  --served-model-name "${SERVED_MODEL:-dsv4}" \
  --tensor-parallel-size 8 \
  --data-parallel-size 1 \
  --enable-expert-parallel \
  --max-model-len "${MAX_MODEL_LEN:-133120}" \
  --max-num-seqs "${MAX_SEQS:-32}" \
  --max-num-batched-tokens "${D_TOKEN_BUDGET:-512}" \
  --gpu-memory-utilization "${MEM_UTIL:-0.90}" \
  --seed 1024 \
  --quantization ascend \
  --tokenizer-mode deepseek_v4 \
  --trust-remote-code \
  --block-size 128 \
  --no-enable-prefix-caching \
  --enable-chunked-prefill \
  --no-disable-hybrid-kv-cache-manager \
  --async-scheduling \
  --safetensors-load-strategy prefetch \
  --speculative-config '{"method":"mtp","num_speculative_tokens":1,"enforce_eager":true}' \
  --compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY"}' \
  --additional-config '{"ascend_compilation_config":{"enable_npugraph_ex":true,"enable_static_kernel":false},"enable_cpu_binding":true,"enable_dsa_cp":false,"multistream_overlap_shared_expert":true,"recompute_scheduler_enable":true}' \
  --kv-transfer-config '{"kv_connector":"MooncakeHybridConnector","kv_role":"kv_consumer","kv_port":"30100","engine_id":"1","kv_connector_extra_config":{"prefill":{"dp_size":1,"tp_size":8},"decode":{"dp_size":1,"tp_size":8}}}' \
  "${extra[@]}"
PD030_FILE_EOF
```


## 4. D 容器：先只检查，再统一安装

确保四个 Python 文件都在同一个 WORK 目录，D 服务已停止：

```bash
export WORK=/tmp/pdtest
python "$WORK/audit_patch.py" --check
```

必须看到 `CHECK PASSED; no files changed`。此命令不会写入框架源码。遇到版本不符、旧补丁标记、旧备份或 AST 不匹配时，停止并保留输出。

检查通过后执行：

```bash
python "$WORK/audit_patch.py" --apply
```

必须看到 APPLIED。新脚本会同时处理三类采样，不再执行 `python patch_steps.py`。重复执行安装会拒绝，不会重复打补丁。备份文件后缀为 `.pd030.bak`，状态文件为安装目录内的 `.pd_audit_v030rc2.json`；保留它们用于恢复。

`--root` 只供离线源码验证使用，会跳过安装包版本检查。你的机器按上面命令执行，不要传 `--root`。

## 5. P/D 按 MRV1 基线重新启动

P 容器执行，改成真实 P IP、网卡、容器内权重路径：

```bash
export WORK=/tmp/pdtest
LOCAL_IP=10.10.0.11 NIC=eth0 MODEL=/models/DeepSeek-V4-Flash-w8a8-mtp \
  bash "$WORK/start_p.sh"
```

D 容器执行，改成真实 D IP、网卡和模型路径；RUN_ID 使用新值，不覆盖上一轮：

```bash
export WORK=/tmp/pdtest
export RUN_ID=pd030_mrv1_001
export RUN="$WORK/runs/$RUN_ID"
test ! -e "$RUN" || { echo 'RUN_ID 已存在，请更换'; exit 1; }
mkdir -p "$RUN/d-capacity" "$RUN/d-transfer" "$RUN/d-steps"

LOCAL_IP=10.10.0.12 NIC=eth0 MODEL=/models/DeepSeek-V4-Flash-w8a8-mtp \
PYTHONPATH="$WORK${PYTHONPATH:+:$PYTHONPATH}" \
AUDIT_CAPACITY_DIR="$RUN/d-capacity" \
AUDIT_TRANSFER_DIR="$RUN/d-transfer" \
AUDIT_STEP_DIR="$RUN/d-steps" \
  bash "$WORK/start_d.sh"
```

两端 Shell 各自设置 MRV1。正式对比也保留相同设置，避免诊断 MRV1 与正式 MRV2 混用。代理、客户端、测试结果仍放 P，发往 P 的 1999 端口。

注意：这里仅改变采样和 runner 选择，Shell 其余参数沿用前一版；仍需以本机启动日志、模型正确输出和精确长度校验确认当前镜像可运行，不能把离线补丁检查当作完整部署验证。

## 6. 新终端检查采样是否真正生效

D 启动后，在 D 容器新终端执行：

```bash
export WORK=/tmp/pdtest
export RUN_ID=pd030_mrv1_001
export RUN="$WORK/runs/$RUN_ID"
python - <<'PY'
import json
import os
from pathlib import Path
root = Path(os.environ['RUN']) / 'd-capacity'
rows = [json.loads(line) for p in root.glob('*.jsonl')
        for line in p.read_text(encoding='utf-8').splitlines()]
rows = [r for r in rows if r.get('kind') == 'capacity_post_kv_before_graph']
workers = {(r['host'], r['pid'], r['device']) for r in rows}
print('capacity worker count:', len(workers))
for r in rows:
    print(r['device'], r['runner_module'], r['runner_class'],
          'KV GiB=', round(r['actual_unique_cache_storage_bytes']/1024**3, 3))
assert len(workers) == 8, 'Expected 8 D worker capacity snapshots'
assert all(r['runner_module'] == 'vllm_ascend.worker.model_runner_v1' for r in rows)
PY
```

先跑一条短 PD 请求，再检查 d-transfer 与 d-steps 目录产生非空 JSONL。没有容量快照、模块不是预期 MRV1、只有部分 worker、日志中 decode 状态持续未知时，停止正式实验并检查运行配置。存在文件本身不算验证通过。

后续使用原来的窗口审计和 Batch Size 汇总工具前，核对新一轮 RUN_ID 和跨机器导出的路径。输出仍保留原工具使用的字段：`time_ns`、`batch_size`、`all_decode`、`duration_ms`、`bytes`、`error`、容量 CSV 所需字段。失败返回码现在会写入 error，不会被当成成功的 KV read。

## 7. 正式测试前恢复框架源码

先停止诊断 D 并导出完整采样结果，然后只执行这一条统一恢复命令：

```bash
export WORK=/tmp/pdtest
python "$WORK/audit_patch.py" --restore
```

不再执行旧的 `patch_steps.py --restore`。新恢复会检查两个源码文件和备份的校验值，检测到补丁以外的编辑会拒绝覆盖。恢复完成应显示 `RESTORED`。

重新启动 D 时清除采样变量，仍使用同一 MRV1 基线：

```bash
export WORK=/tmp/pdtest
unset AUDIT_CAPACITY_DIR AUDIT_TRANSFER_DIR AUDIT_STEP_DIR
LOCAL_IP=10.10.0.12 NIC=eth0 MODEL=/models/DeepSeek-V4-Flash-w8a8-mtp \
AUDIT_CAPACITY_DIR= AUDIT_TRANSFER_DIR= AUDIT_STEP_DIR= \
  bash "$WORK/start_d.sh"
```

重新预热，再测同阶段的单并发与 C=已确认BS 的波次和持续负载。不要将 patch 后的诊断成绩直接替代恢复后的正式成绩。

## 8. 已做与未做的验证

- 已完成：下载的官方标签源码上，AST 定位、四处安装点、重复安装拒绝、错误源码预检、恢复时的外部编辑保护、原始字节恢复。
- 已完成：CPU 模拟数据中按请求数计 batch；草稿乐观计数和缺失字段不误判纯 D；负返回码与异常不被误记成功；底层共享 storage 不重复统计。
- 未完成：你的镜像源码差异、A2 模型启动、运行正确性、128K/1K 长输入、实际 NPU 时间线、纯 D 稳态与性能验证。

## 官方核对依据

- [Ascend v0.30.0rc2 runner 源码](https://github.com/vllm-project/vllm-ascend/blob/v0.30.0rc2/vllm_ascend/worker/model_runner_v1.py)
- [Ascend v0.30.0rc2 Mooncake connector](https://github.com/vllm-project/vllm-ascend/blob/v0.30.0rc2/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_hybrid_connector.py)
- [vLLM v0.30.0 KV 描述结构](https://github.com/vllm-project/vllm/blob/v0.30.0/vllm/v1/kv_cache_interface.py)
- [vLLM v0.30.0 runner 选择](https://github.com/vllm-project/vllm/blob/v0.30.0/vllm/config/vllm.py)

目标机器不需要访问这些链接，也不需要 Git。
