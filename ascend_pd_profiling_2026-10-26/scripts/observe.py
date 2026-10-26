#!/usr/bin/env python3
"""Save bounded read-only /metrics and npu-smi snapshots, with timestamps."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--role", choices=["P", "D"], required=True)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--interval", type=float, default=2)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.seconds <= 0 or a.interval < 1:
        ap.error("seconds must be positive; interval must be >= 1")
    a.out.mkdir(parents=True, exist_ok=False)
    deadline = time.monotonic() + a.seconds
    i = 0
    try:
        while time.monotonic() < deadline:
            start = time.monotonic()
            meta = {"role": a.role, "utc": datetime.now(timezone.utc).isoformat()}
            try:
                with urllib.request.urlopen(a.base_url.rstrip("/") + "/metrics", timeout=5) as r:
                    raw = r.read(8 * 1024 * 1024 + 1)
                    meta["metrics_truncated"] = len(raw) > 8 * 1024 * 1024
                    (a.out / f"{i:05d}_metrics.txt").write_bytes(raw[:8 * 1024 * 1024])
                    meta["metrics_status"] = r.status
            except Exception as exc:
                meta["metrics_error"] = str(exc)
            if shutil.which("npu-smi"):
                try:
                    p = subprocess.run(["npu-smi", "info"], capture_output=True, text=True,
                                       errors="replace", timeout=8, shell=False)
                    (a.out / f"{i:05d}_npu-smi.txt").write_text(p.stdout + p.stderr, encoding="utf-8")
                    meta["npu_smi_returncode"] = p.returncode
                except Exception as exc:
                    meta["npu_smi_error"] = str(exc)
            else:
                meta["npu_smi_error"] = "not_found"
            meta["collection_elapsed_s"] = time.monotonic() - start
            (a.out / f"{i:05d}_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            i += 1
            time.sleep(max(0, min(a.interval - (time.monotonic() - start), deadline - time.monotonic())))
    except KeyboardInterrupt:
        print("Stopped; completed snapshots retained.")
    print(f"Saved {i} snapshots to {a.out.resolve()}")


if __name__ == "__main__":
    main()
