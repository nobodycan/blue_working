#!/usr/bin/env python3
"""Control a short vLLM-Ascend P/D capture window; does not generate model traffic."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse


def utc():
    return datetime.now(timezone.utc).isoformat()


def post(base, action, timeout, key):
    begin = time.monotonic()
    event = {"url": base + "/" + action, "action": action, "sent_utc": utc()}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(event["url"], data=b"{}", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            event["http_status"] = res.status
            event["body"] = res.read(4096).decode(errors="replace")
            event["ok"] = 200 <= res.status < 300
    except urllib.error.HTTPError as exc:
        event.update(ok=False, http_status=exc.code,
                     error=exc.read(4096).decode(errors="replace"))
    except Exception as exc:
        event.update(ok=False, error=str(exc))
    event["returned_utc"] = utc()
    event["elapsed_s"] = time.monotonic() - begin
    return event


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--p-url", required=True)
    ap.add_argument("--d-url", required=True)
    ap.add_argument("--seconds", type=float, default=5)
    ap.add_argument("--http-timeout", type=float, default=300,
                    help="control request timeout; export on stop may take time")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--execute", action="store_true")
    a = ap.parse_args()
    if not (0 < a.seconds <= 60) or a.http_timeout <= 0:
        ap.error("seconds must be in (0,60]; http-timeout must be positive")
    urls = {"P": a.p_url.rstrip("/"), "D": a.d_url.rstrip("/")}
    if len(set(urls.values())) < 2:
        ap.error("P and D must be different API endpoints")
    for url in urls.values():
        u = urlparse(url)
        if (u.scheme not in ("http", "https") or not u.netloc or u.path
                or u.username or u.password or u.query or u.fragment):
            ap.error("P/D URLs must be root http(s) API URLs without credentials or /v1")
    if not a.execute:
        print(json.dumps({"mode": "dry_run", "targets": urls, "seconds": a.seconds,
                          "warning": "Direct P/D control only; start traffic separately. "
                          "Use distinct physical devices and an exclusive capture session."}, indent=2))
        return
    a.out.mkdir(parents=True, exist_ok=False)
    key = os.environ.get("PROFILE_API_KEY", "")
    events = []
    interrupted = False

    def save():
        (a.out / "events.json").write_text(
            json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")

    def phase(action):
        collected = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = {pool.submit(post, url, action, a.http_timeout, key): role
                    for role, url in urls.items()}
            for job in as_completed(jobs):
                event = job.result()
                event["role"] = jobs[job]
                events.append(event)
                collected.append(event)
                save()
                print(f"{event['role']} {action}: {'OK' if event['ok'] else 'FAILED'}", flush=True)
        return collected

    def handle_signal(signum, frame):
        nonlocal interrupted
        interrupted = True
        print("Stop requested; waiting for control calls, then attempting cleanup.", flush=True)

    old_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[sig] = signal.signal(sig, handle_signal)
    errors = []
    try:
        print("Starting both profilers. This controller does NOT send inference requests.", flush=True)
        starts = phase("start_profile")
        if any(not x["ok"] for x in starts):
            errors.append("Start failed or state uncertain. Check URLs/configuration and server logs.")
        elif not interrupted:
            events.append({"action": "window_active", "utc": utc(), "seconds": a.seconds})
            save()
            print("CAPTURE ACTIVE: traffic must be running through the PD proxy.", flush=True)
            deadline = time.monotonic() + a.seconds
            while time.monotonic() < deadline and not interrupted:
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    except Exception as exc:
        errors.append(str(exc))
    finally:
        # Attempt both stops even if a start timed out: the server may have started it.
        print("Stopping both profilers; export may take longer than capture.", flush=True)
        stops = phase("stop_profile")
        for event in stops:
            if not event["ok"]:
                errors.append(f"{event['role']} stop failed/uncertain: stop manually before benchmarking.")
        events.append({"action": "controller_done", "utc": utc(),
                       "interrupted": interrupted, "errors": errors})
        save()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    if interrupted or errors:
        print("\n".join(errors) or "Interrupted; inspect captured window before use.")
        raise SystemExit(1)
    print("Capture controls completed. Verify actual P/D rank trace files and useful activity.")


if __name__ == "__main__":
    main()
