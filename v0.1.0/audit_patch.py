"""Apply guarded diagnostic hooks to the installed v0.23 runner/connector. Back up originals."""
import argparse
import ast
import hashlib
import importlib.util
import json
from pathlib import Path


def patch(path, anchor, replacement, expected):
    marker = '# ASCEND_BENCH_AUDIT_V1'
    source = path.read_text(encoding='utf-8')
    if marker in source:
        print('Already patched:', path)
        return
    if source.count(anchor) != expected:
        raise RuntimeError(f'{path}: expected {expected} anchors, got {source.count(anchor)}; stop, inspect installed source')
    modified = source.replace(anchor, replacement)
    ast.parse(modified)
    backup = Path(str(path)+'.bench.bak')
    if backup.exists():
        raise RuntimeError(f'Backup exists without marker: {backup}; inspect instead of overwriting')
    backup.write_text(source, encoding='utf-8')
    path.write_text(modified, encoding='utf-8')
    print(json.dumps(dict(file=str(path), original_sha256=hashlib.sha256(source.encode()).hexdigest(),
                         patched_sha256=hashlib.sha256(modified.encode()).hexdigest())))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, help='Optional source root, for offline validation')
    p.add_argument('--restore', action='store_true')
    a = p.parse_args()
    root = a.root
    if root is None:
        spec = importlib.util.find_spec('vllm_ascend')
        if spec is None or not spec.origin: raise RuntimeError('vllm_ascend not found')
        root = Path(spec.origin).parent
    paths = [root/'worker/model_runner_v1.py',
             root/'distributed/kv_transfer/kv_p2p/mooncake_hybrid_connector.py']
    if a.restore:
        for path in paths:
            backup = Path(str(path)+'.bench.bak')
            if backup.exists():
                path.write_text(backup.read_text(encoding='utf-8'), encoding='utf-8')
                backup.unlink()
                print('Restored:', path)
        return
    anchor = '        kv_caches = self.initialize_kv_cache_tensors(kv_cache_config)'
    patch(paths[0], anchor, anchor+'\n        # ASCEND_BENCH_AUDIT_V1\n        from capacity_probe import dump_capacity\n        dump_capacity(self, kv_caches)', 1)
    anchor = 'ret = self.engine.batch_transfer_sync_read(session_id, src_list, dst_list, length_list)'
    replacement = 'from capacity_probe import measured_read  # ASCEND_BENCH_AUDIT_V1\n        ret = measured_read(self.engine, session_id, src_list, dst_list, length_list, req_meta)'
    patch(paths[1], anchor, replacement, 2)


if __name__ == '__main__': main()
