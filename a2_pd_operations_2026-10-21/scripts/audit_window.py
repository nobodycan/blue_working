"""Check all NPU workers in a chosen wall-clock interval. Does not infer throughput."""
import argparse,collections,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--steps',type=Path,required=True)
p.add_argument('--summary',type=Path,required=True);p.add_argument('--run',type=int,default=1)
p.add_argument('--transfer',type=Path);p.add_argument('--expected-workers',type=int,default=8)
a=p.parse_args();s=next(r for r in json.loads(a.summary.read_text()) if r['run']==a.run)
if not s.get('plateau_valid') or 'plateau_start_ns' not in s:
    print(json.dumps(dict(valid=False,reason='No eligible full-overlap decode window')))
    raise SystemExit(2)
start,end=s['plateau_start_ns'],s['plateau_end_ns'];groups=collections.defaultdict(list)
for path in a.steps.glob('*.jsonl'):
    for line in path.read_text().splitlines():
        r=json.loads(line)
        if start<=r['time_ns']<=end: groups[(r['host'],r['pid'],r['device'])].append(r)
report=[]
for key,rows in sorted(groups.items()):
    counts=collections.Counter(r['batch_size'] for r in rows)
    report.append(dict(worker=key,steps=len(rows),batch_distribution=dict(counts),
        all_decode=all(r['all_decode'] for r in rows),
        constant_target=all(r['batch_size']==s['batch_requested'] for r in rows),
        covered_s=(max(r['time_ns'] for r in rows)-min(r['time_ns'] for r in rows))/1e9))
transfers=[]
if a.transfer:
    for path in a.transfer.glob('*.jsonl'):
        for line in path.read_text().splitlines():
            r=json.loads(line);t1=r['time_ns'];t0=t1-int(r.get('duration_ms',0)*1e6)
            if t0<end and t1>start:transfers.append(r)
valid=(len(report)==a.expected_workers and all(r['steps']>=100 and r['all_decode'] and
    r['constant_target'] and r['covered_s']>=4.5 for r in report) and not transfers and s['plateau_valid'])
print(json.dumps(dict(valid=valid,window_s=(end-start)/1e9,workers=report,
    overlapping_transfer_calls=len(transfers),note='Also inspect KV preemption/logs and transfer kernels in trace'),indent=2))
raise SystemExit(0 if valid else 2)
