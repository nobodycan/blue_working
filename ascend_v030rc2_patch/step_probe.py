"""CPU request-state snapshots for MRV1; never synchronize NPU per step."""
import atexit
import json
import os
from pathlib import Path
import socket
import time

_buffers = {}
_last_flush = 0.0


def flush():
    for path, rows in _buffers.items():
        if rows:
            with path.open("a", encoding="utf-8") as f:
                f.write("".join(rows))
            rows.clear()


atexit.register(flush)


def snapshot(runner, out):
    directory = os.getenv("AUDIT_STEP_DIR")
    if not directory:
        return
    rows = []
    for rid, scheduled in out.num_scheduled_tokens.items():
        scheduled = int(scheduled)
        if scheduled <= 0:
            continue
        state = runner.requests.get(rid)
        plen = getattr(state, "num_prompt_tokens", None)
        computed = getattr(state, "num_computed_tokens", None)
        previous_drafts = getattr(state, "prev_num_draft_len", None)
        known = plen is not None and computed is not None and previous_drafts is not None
        lower_bound = int(computed) - max(0, int(previous_drafts)) if known else None
        rows.append(dict(request_id=rid, scheduled_tokens=scheduled,
                         prompt_tokens=plen, computed_tokens=computed,
                         previous_draft_tokens=previous_drafts,
                         conservative_computed_tokens=lower_bound,
                         decode=bool(lower_bound >= plen) if known else None))
    if sum(r["scheduled_tokens"] for r in rows) != int(out.total_num_scheduled_tokens):
        raise RuntimeError("Scheduler token counts disagree; diagnostic schema mismatch")
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname()
    row = dict(kind="worker_step", time_ns=time.time_ns(), host=host,
               pid=os.getpid(), device=str(runner.device),
               runner_module=type(runner).__module__, batch_size=len(rows),
               scheduled_tokens=int(out.total_num_scheduled_tokens),
               all_decode=bool(rows) and all(r["decode"] is True for r in rows),
               classification="conservative CPU state after _update_states", requests=rows)
    path = root / f"{host}-{os.getpid()}.jsonl"
    buffer = _buffers.setdefault(path, [])
    buffer.append(json.dumps(row, default=str) + "\n")
    global _last_flush
    now = time.monotonic()
    if len(buffer) >= 32 or now - _last_flush >= 0.25:
        flush()
        _last_flush = now
