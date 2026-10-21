"""Check a completed run set before summarizing; no hardware calls."""
import argparse,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True)
p.add_argument('--runs',type=int,required=True);p.add_argument('--concurrency',type=int,required=True)
p.add_argument('--mode',choices=['wave','steady'],default='wave');a=p.parse_args()
for i in range(1,a.runs+1):
    path=a.directory/f'r{i}.json'
    s=json.loads(path.read_text())
    assert s['mode']==a.mode and s['client_concurrency']==a.concurrency, f'Config mismatch: {path}'
    assert s['input_tokens']==131072 and s['output_tokens']==1024
    assert s['all_phase_failures']==0 and s['sufficient_samples'], f'Failed samples: {path}'
    rows=[json.loads(x) for x in path.with_suffix('.requests.jsonl').read_text().splitlines()]
    assert rows and all(not r['error'] for r in rows), f'Request failure: {path}'
    assert all(r['usage']['prompt_tokens']==131072 and r['usage']['completion_tokens']==1024 for r in rows)
    if a.mode=='wave':assert len(rows)==a.concurrency, f'Wave size mismatch: {path}'
    else:assert s['end_censored_fraction'] is not None and s['end_censored_fraction']<=.10, f'Extend duration: {path}'
print(f'PASS: {a.runs} {a.mode} runs, concurrency={a.concurrency}, all length/failure checks passed')
