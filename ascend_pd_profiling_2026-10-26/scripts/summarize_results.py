#!/usr/bin/env python3
"""Collect completed baseline vLLM result JSONs without inventing missing values."""
import argparse
import csv
import json
import math
from pathlib import Path
import sys


FIELDS = ["run_id", "status", "notes", "concurrency", "expected_input_len", "expected_output_len",
          "seed", "num_prompts", "completed", "failed", "duration_s", "total_input_tokens",
          "total_output_tokens", "request_throughput_rps", "output_throughput_tok_s",
          "request_goodput_rps", "mean_ttft_ms", "p50_ttft_ms", "p95_ttft_ms", "p99_ttft_ms",
          "mean_tpot_ms", "p50_tpot_ms", "p95_tpot_ms", "p99_tpot_ms",
          "mean_itl_ms", "p95_itl_ms", "p99_itl_ms", "mean_e2el_ms", "p99_e2el_ms", "source"]


def flatten(manifest, data, source):
    c = manifest["configuration"]
    row = {"run_id": manifest["run_id"], "status": "REVIEW", "notes": "",
           "concurrency": c["concurrency"], "expected_input_len": c["input_len"],
           "expected_output_len": c["output_len"], "seed": c["seed"],
           "num_prompts": c["num_prompts"], "source": str(source.resolve())}
    remap = {"duration_s": "duration", "request_throughput_rps": "request_throughput",
             "output_throughput_tok_s": "output_throughput", "request_goodput_rps": "request_goodput"}
    for name in FIELDS:
        if name not in row:
            row[name] = data.get(remap.get(name, name), "")
            if row[name] is None:
                row[name] = ""
    for metric in ("ttft", "tpot"):
        key = f"p50_{metric}_ms"
        if row[key] == "":
            row[key] = data.get(f"median_{metric}_ms", "")
    notes = []
    completed, failed = data.get("completed"), data.get("failed")
    if not isinstance(completed, int) or completed < 1:
        notes.append("no_valid_completed_count")
    if failed != 0:
        notes.append("failures_or_missing_failed_count")
    if isinstance(completed, int) and isinstance(failed, int) and completed + failed != c["num_prompts"]:
        notes.append("request_count_mismatch")
    for metric in ["duration", "output_throughput", "mean_ttft_ms", "mean_tpot_ms"]:
        value = data.get(metric)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            notes.append("check_" + metric)
    # Fixed-length intent is not a substitute for actual successful token totals.
    if isinstance(completed, int):
        if data.get("total_input_tokens") != completed * c["input_len"]:
            notes.append("input_length_total_unverified_or_mismatch")
        if data.get("total_output_tokens") != completed * c["output_len"]:
            notes.append("output_length_total_unverified_or_mismatch")
    row["status"] = "CHECKED_FIELDS" if not notes else "REVIEW"
    row["notes"] = ";".join(notes) if notes else "manual_trace_and_length_audit_still_required"
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if not a.root.is_dir():
        ap.error("root directory does not exist")
    rows, errors = [], []
    for path in sorted(a.root.rglob("run.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if manifest.get("purpose") != "baseline":
                continue
            if manifest.get("status") != "completed" or manifest.get("returncode") != 0:
                errors.append(f"{path}: baseline did not finish successfully")
                continue
            source = path.parent / "result.json"
            data = json.loads(source.read_text(encoding="utf-8"))
            rows.append(flatten(manifest, data, source))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"{path}: {exc}")
    if not rows:
        raise SystemExit("No completed baseline results found. No summary written.")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("x", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows to {a.out.resolve()}. CHECKED_FIELDS is not a benchmark certification.")
    for error in errors:
        print("ERROR:", error, file=sys.stderr)
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
