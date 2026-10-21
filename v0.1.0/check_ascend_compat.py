#!/usr/bin/env python3
"""Read installed versions and source anchors without changing package files.

Run in each P/D container with: python check_ascend_compat.py
Matching anchors alone do not establish patch compatibility.
"""

from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
from pathlib import Path
import hashlib


def main():
    for name in ("vllm", "vllm-ascend", "torch", "torch-npu"):
        try:
            print(f"{name}: {version(name)}")
        except PackageNotFoundError:
            print(f"{name}: 未找到包元数据")

    spec = find_spec("vllm_ascend")
    if spec is None or spec.origin is None:
        raise SystemExit("未找到 vllm_ascend 源码目录")

    root = Path(spec.origin).parent
    print("源码目录:", root)

    checks = {
        "worker/model_runner_v1.py": [
            "kv_caches = self.initialize_kv_cache_tensors(kv_cache_config)",
            "deferred_state_corrections_fn = self._update_states(",
            "# ASCEND_BENCH_AUDIT_V1",
            "# V4_2NODE_STEP_AUDIT",
        ],
        "distributed/kv_transfer/kv_p2p/mooncake_hybrid_connector.py": [
            "ret = self.engine.batch_transfer_sync_read(session_id, src_list, dst_list, length_list)",
        ],
    }

    for relative, anchors in checks.items():
        path = root / relative
        print("\n文件:", path)
        if not path.is_file():
            print("文件不存在")
            continue
        data = path.read_bytes()
        text = data.decode("utf-8")
        print("SHA256:", hashlib.sha256(data).hexdigest())
        for anchor in anchors:
            print(f"命中次数={text.count(anchor)} | {anchor}")


if __name__ == "__main__":
    main()
