"""CPU-only worker snapshots; opt in for diagnostic runs, not device timing."""
import atexit
import json
import os
from pathlib import Path
import socket
import time
_buffers={}
_last_flush=0.0

def flush():
    for path,rows in _buffers.items():
        if rows:
            with path.open('a',encoding='utf-8') as f: f.write(''.join(rows))
            rows.clear()
atexit.register(flush)

def snapshot(runner,out):
    directory=os.getenv('AUDIT_STEP_DIR')
    if not directory: return
    root=Path(directory); root.mkdir(parents=True,exist_ok=True)
    rows=[]
    for rid,n in out.num_scheduled_tokens.items():
        state=runner.requests.get(rid)
        prompt=getattr(state,'prompt_token_ids',None)
        plen=getattr(state,'num_prompt_tokens',None)
        if plen is None and prompt is not None: plen=len(prompt)
        computed=getattr(state,'num_computed_tokens',None)
        rows.append(dict(request_id=rid,scheduled_tokens=int(n),prompt_tokens=plen,
            computed_tokens=computed,
            decode=bool(computed>=plen) if computed is not None and plen is not None else None))
    row=dict(kind='worker_step',time_ns=time.time_ns(),pid=os.getpid(),
        host=socket.gethostname(),device=str(runner.device),
        batch_size=len(rows),scheduled_tokens=int(out.total_num_scheduled_tokens),
        all_decode=bool(rows) and all(x['decode'] is True for x in rows),requests=rows)
    path=root/f'{socket.gethostname()}-{os.getpid()}.jsonl'
    buf=_buffers.setdefault(path,[]); buf.append(json.dumps(row,default=str)+'\n')
    global _last_flush
    now=time.monotonic()
    if len(buf)>=32 or now-_last_flush>=0.25:
        flush(); _last_flush=now
