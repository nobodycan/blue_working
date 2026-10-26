#!/usr/bin/env python3
"""Read-only environment collection; run inside each actual serving container."""
import argparse
import hashlib
import importlib.metadata as md
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone


def command(argv, timeout=30):
    if not shutil.which(argv[0]):
        return {"argv": argv, "status": "not_found"}
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           errors="replace", timeout=timeout, shell=False)
        return {"argv": argv, "returncode": p.returncode,
                "stdout": p.stdout, "stderr": p.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"argv": argv, "error": str(exc)}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--role", choices=["P", "D", "CLIENT"], required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model-dir", type=Path)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=False)
    versions = {}
    for name in ["vllm", "vllm-ascend", "torch", "torch-npu", "transformers"]:
        try:
            versions[name] = md.version(name)
        except md.PackageNotFoundError:
            versions[name] = None
    data = {"utc": datetime.now(timezone.utc).isoformat(), "role": a.role,
            "hostname": platform.node(), "platform": platform.platform(),
            "python": sys.version, "executable": sys.executable,
            "versions": versions,
            "selected_env": {k: os.environ.get(k) for k in [
                "ASCEND_RT_VISIBLE_DEVICES", "ASCEND_VISIBLE_DEVICES",
                "ASCEND_HOME_PATH", "VLLM_USE_V2_MODEL_RUNNER"]},
            "commands": {}, "cann_versions": {}, "model_configs": {}}
    for name, argv in {
        "npu_smi": ["npu-smi", "info"],
        "msprof": ["msprof", "--version"],
        "clock": ["date", "-u", "+%Y-%m-%dT%H:%M:%S.%NZ"],
        "chrony": ["chronyc", "tracking"],
        "vllm_bench_help": ["vllm", "bench", "serve", "--help"],
        "vllm_serve_help": ["vllm", "serve", "--help"],
    }.items():
        data["commands"][name] = command(argv, timeout=90 if "vllm" in name else 15)
    for raw in ["/usr/local/Ascend/ascend-toolkit/latest/version.cfg",
                "/usr/local/Ascend/ascend-toolkit/latest/version.info",
                "/usr/local/Ascend/cann/latest/version.cfg",
                "/usr/local/Ascend/driver/version.info"]:
        path = Path(raw)
        if path.is_file():
            try:
                data["cann_versions"][raw] = path.read_text(errors="replace")[:32768]
            except OSError as exc:
                data["cann_versions"][raw] = {"error": str(exc)}
    if a.model_dir:
        for name in ["config.json", "quant_model_description.json",
                     "quantize_config.json", "quant_config.json",
                     "tokenizer_config.json", "generation_config.json"]:
            path = a.model_dir / name
            if path.is_file():
                size = path.stat().st_size
                if size > 2 * 1024 * 1024:
                    data["model_configs"][name] = {"size": size, "status": "skipped_large_file"}
                    continue
                raw = path.read_bytes()
                data["model_configs"][name] = {
                    "size": size, "sha256": hashlib.sha256(raw).hexdigest(),
                    "text": raw.decode("utf-8", errors="replace")}
    (a.out / "environment.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(a.out.resolve()), "versions": versions},
                     ensure_ascii=False, indent=2))
    print("Manually record image ID, launch arguments, P/D card mapping, and TP/EP/MTP settings.")


if __name__ == "__main__":
    main()
