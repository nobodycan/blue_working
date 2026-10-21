"""Diagnostic-only helpers. Startup snapshots count underlying NPU storages once."""
import dataclasses
import json
import os
from pathlib import Path
import socket
import threading
import time

_lock = threading.Lock()


def write_record(directory, kind, record):
    if not directory:
        return
    root = Path(directory); root.mkdir(parents=True, exist_ok=True)
    row = dict(kind=kind, host=socket.gethostname(), pid=os.getpid(),
               time_ns=time.time_ns(), monotonic_ns=time.perf_counter_ns(), **record)
    path = root / f'{socket.gethostname()}-{os.getpid()}.jsonl'
    with _lock, path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(row, default=str) + '\n')


def _storages(value):
    import torch
    found = {}
    def visit(obj):
        if isinstance(obj, torch.Tensor):
            if obj.device.type != 'npu':
                return
            storage = obj.untyped_storage()
            key = (str(obj.device), storage.data_ptr(), storage.nbytes())
            found[key] = storage.nbytes()
        elif isinstance(obj, dict):
            for item in obj.values(): visit(item)
        elif isinstance(obj, (tuple, list)):
            for item in obj: visit(item)
    visit(value)
    return sum(found.values()), len(found)


def dump_capacity(runner, kv_caches):
    directory = os.getenv('AUDIT_CAPACITY_DIR')
    if not directory:
        return
    import torch
    torch.npu.synchronize()
    cfg = runner.kv_cache_config
    groups = []
    for i, group in enumerate(cfg.kv_cache_groups):
        spec = group.kv_cache_spec
        row = dict(index=i, spec_type=type(spec).__name__, layers=list(group.layer_names),
                   spec_repr=repr(spec))
        for name in ('block_size', 'page_size_bytes', 'real_page_size_bytes'):
            try: row[name] = getattr(spec, name)
            except (AttributeError, NotImplementedError): pass
        if dataclasses.is_dataclass(spec): row['fields'] = dataclasses.asdict(spec)
        # An individual spec bound; do not multiply/group-sum it blindly.
        try:
            row['spec_max_memory_usage_bytes'] = spec.max_memory_usage_bytes(runner.vllm_config)
        except (AttributeError, NotImplementedError, TypeError) as e:
            row['bound_unavailable'] = str(e)
        groups.append(row)
    kv_bytes, count = _storages(kv_caches)
    model_items = list(runner.model.parameters()) + list(runner.model.buffers())
    model_bytes, _ = _storages(model_items)
    parallel = runner.vllm_config.parallel_config
    free, total = torch.npu.mem_get_info()
    write_record(directory, 'capacity_post_kv_before_graph', dict(
        dp_rank=parallel.data_parallel_rank, tp_size=parallel.tensor_parallel_size,
        device=str(runner.device), num_blocks=cfg.num_blocks,
        planned_cache_bytes=sum(t.size for t in cfg.kv_cache_tensors),
        actual_unique_cache_storage_bytes=kv_bytes, unique_cache_storages=count,
        model_parameter_buffer_storage_bytes=model_bytes,
        model_load_memory_usage_bytes=getattr(runner, 'model_memory_usage', None),
        allocated_bytes=torch.npu.memory_allocated(), reserved_bytes=torch.npu.memory_reserved(),
        free_bytes=free, total_bytes=total,
        tensors=[dict(size=t.size, shared_by=t.shared_by) for t in cfg.kv_cache_tensors],
        groups=groups))


def measured_read(engine, session_id, src_list, dst_list, length_list, req_meta):
    directory = os.getenv('AUDIT_TRANSFER_DIR')
    if not directory:
        return engine.batch_transfer_sync_read(session_id, src_list, dst_list, length_list)
    start = time.perf_counter_ns()
    result, error = None, None
    try:
        result = engine.batch_transfer_sync_read(session_id, src_list, dst_list, length_list)
        return result
    except BaseException as exc:
        error = repr(exc)
        raise
    finally:
        stop = time.perf_counter_ns()
        write_record(directory, 'mooncake_sync_read', dict(
            request_id=req_meta.get('request_id'),
            remote_request_id=req_meta.get('remote_request_id'),
            duration_ms=(stop-start)/1e6, bytes=sum(int(n) for n in length_list),
            segments=len(length_list), result=result, error=error))
