#!/usr/bin/env python3
"""Discover concrete ascend_pt directories, then optionally run official analysis."""
import argparse
import json
from pathlib import Path


def has_output(path):
    out = path / "ASCEND_PROFILER_OUTPUT"
    return out.is_dir() and any((out / name).is_file() for name in [
        "trace_view.json", "kernel_details.csv", "op_statistic.csv"])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root", type=Path)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--force", action="store_true", help="reanalyze existing output; preserve raw copy first")
    a = ap.parse_args()
    root = a.root.resolve()
    if not root.is_dir():
        ap.error("root directory does not exist")
    profiles = ([root] if root.name.endswith("ascend_pt") else
                sorted(p for p in root.rglob("*ascend_pt") if p.is_dir()))
    if not profiles:
        raise SystemExit("No *ascend_pt directories found. Check worker paths and stop/export logs.")
    failed = False
    analyse = None
    for p in profiles:
        entry = {"path": str(p), "already_has_text_output": has_output(p)}
        if not a.execute:
            entry["action"] = "dry_run"
        elif has_output(p) and not a.force:
            entry["action"] = "skip_existing"
        else:
            try:
                if analyse is None:
                    from torch_npu.profiler.profiler import analyse
                analyse(str(p))
                # Some framework versions log errors instead of raising exceptions.
                entry["action"] = "analysed" if has_output(p) else "check_missing_output"
                failed |= not has_output(p)
            except Exception as exc:
                entry.update(action="failed", error=str(exc))
                failed = True
        print(json.dumps(entry, ensure_ascii=False), flush=True)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
