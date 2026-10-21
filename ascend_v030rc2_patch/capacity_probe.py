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
