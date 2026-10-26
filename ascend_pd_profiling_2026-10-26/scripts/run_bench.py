#!/usr/bin/env python3
"""One benchmark case using the installed official vLLM CLI; dry-run by default."""
import argparse
from datetime import datetime, timezone
import importlib.metadata as md
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from urllib.parse import urlparse


def positive(raw):
    n = int(raw)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def utc():
    return datetime.now(timezone.utc).isoformat()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", required=True, help="PD proxy root URL, without /v1")
    ap.add_argument("--model", required=True, help="local model/tokenizer directory on client")
    ap.add_argument("--served-model", required=True, help="API model alias")
    ap.add_argument("--tokenizer-mode", default="deepseek_v4")
    ap.add_argument("--concurrency", type=positive, required=True)
    ap.add_argument("--input-len", type=positive, default=1024)
    ap.add_argument("--output-len", type=positive, default=128)
    ap.add_argument("--num-prompts", type=positive, default=100)
    ap.add_argument("--warmups", type=int, default=8)
    ap.add_argument("--seed", type=int, default=1001)
    ap.add_argument("--purpose", choices=["smoke", "baseline", "diagnostic"], required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out-root", type=Path, default=Path("runs"))
    ap.add_argument("--execute", action="store_true")
    a = ap.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", a.run_id):
        ap.error("run-id must be a simple unique name (letters, digits, _, -, .)")
    u = urlparse(a.base_url)
    if u.scheme not in ("http", "https") or not u.netloc or u.path not in ("", "/"):
        ap.error("base-url must be a root http(s) URL without /v1 or another path")
    if u.username or u.password or u.query or u.fragment:
        ap.error("do not put credentials, query strings or fragments in base-url")
    if a.warmups < 0 or a.num_prompts < a.concurrency:
        ap.error("warmups must be >=0 and num-prompts must be >= concurrency")
    out = a.out_root.resolve() / a.run_id
    cmd = ["vllm", "bench", "serve",
           "--backend", "openai", "--base-url", a.base_url.rstrip("/"),
           "--endpoint", "/v1/completions", "--model", a.model,
           "--served-model-name", a.served_model,
           "--tokenizer-mode", a.tokenizer_mode,
           "--dataset-name", "random", "--random-input-len", str(a.input_len),
           "--random-output-len", str(a.output_len),
           "--random-range-ratio", "0", "--random-prefix-len", "0",
           "--request-rate", "inf", "--max-concurrency", str(a.concurrency),
           "--num-prompts", str(a.num_prompts), "--num-warmups", str(a.warmups),
           "--seed", str(a.seed), "--temperature", "0", "--ignore-eos",
           "--percentile-metrics", "ttft,tpot,itl,e2el",
           "--metric-percentiles", "50,95,99", "--save-result", "--save-detailed",
           "--result-dir", str(out), "--result-filename", "result.json"]
    print("Linux command:\n" + shlex.join(cmd), flush=True)
    if not a.execute:
        print("DRY RUN: no requests sent; add --execute on the NPU client.")
        return
    if not Path(a.model).is_dir():
        ap.error("--model must point to a local directory with the matching tokenizer")
    exe = shutil.which("vllm")
    if not exe:
        ap.error("vllm CLI not found in this environment")
    help_run = subprocess.run([exe, "bench", "serve", "--help"],
                              capture_output=True, text=True, errors="replace", timeout=120)
    if help_run.returncode:
        raise SystemExit("vllm bench serve --help failed; check client environment")
    help_text = help_run.stdout + help_run.stderr
    missing = sorted({x for x in cmd if x.startswith("--") and x not in help_text})
    if missing:
        raise SystemExit("Installed CLI is missing required options: " + ", ".join(missing))
    out.mkdir(parents=True, exist_ok=False)
    (out / "cli_help.txt").write_text(help_text, encoding="utf-8")
    versions = {}
    for name in ["vllm", "vllm-ascend", "torch", "torch-npu"]:
        try:
            versions[name] = md.version(name)
        except md.PackageNotFoundError:
            versions[name] = None
    info = {"schema": 1, "purpose": a.purpose, "run_id": a.run_id,
            "started_utc": utc(), "status": "running", "versions": versions,
            "configuration": {k: str(v) if isinstance(v, Path) else v
                              for k, v in vars(a).items()},
            "argv": cmd, "note": "No --profile flag is used. Verify profiling state separately."}
    manifest = out / "run.json"

    def save():
        manifest.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

    save()
    cmd[0] = exe
    code = 1
    try:
        with (out / "benchmark.log").open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace", bufsize=1, shell=False)
            try:
                for line in proc.stdout:
                    print(line, end="", flush=True)
                    log.write(line)
                    log.flush()
                code = proc.wait()
            except BaseException:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise
        info["status"] = "completed" if code == 0 and (out / "result.json").is_file() else "failed"
        if info["status"] == "failed" and code == 0:
            code = 1
    except BaseException as exc:
        info["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        info["error"] = str(exc)
        raise
    finally:
        info["finished_utc"] = utc()
        info["returncode"] = code
        save()
    print("Result directory:", out)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
