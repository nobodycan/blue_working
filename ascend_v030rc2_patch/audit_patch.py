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
