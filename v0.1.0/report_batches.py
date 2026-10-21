"""Actual worker scheduling-batch distribution during one client measurement window.
Idle time has no execute_model event; use running/waiting metrics for occupancy.
"""
import argparse,collections,json,statistics
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--steps',type=Path,required=True)
p.add_argument('--summary',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
a=p.parse_args();s=json.loads(a.summary.read_text())
start=s['start_wall_ns']+int(s['window_start_s']*1e9)
end=s['start_wall_ns']+int(s['window_end_s']*1e9)
groups=collections.defaultdict(list)
for path in a.steps.glob('*.jsonl'):
    for line in path.read_text().splitlines():
        r=json.loads(line)
        if start<=r['time_ns']<end:groups[(r['host'],r['pid'],r['device'])].append(r)
def q(xs,p):
    xs=sorted(xs);return xs[round((len(xs)-1)*p)]
report=[]
for key,rows in sorted(groups.items()):
    sizes=[r['batch_size'] for r in rows]
    report.append(dict(worker=key,scheduled_iterations=len(rows),min=min(sizes),p50=q(sizes,.5),
        p95=q(sizes,.95),max=max(sizes),iteration_weighted_mean=statistics.mean(sizes),
        distribution=dict(collections.Counter(sizes)),all_decode_fraction=sum(r['all_decode'] for r in rows)/len(rows)))
a.out.write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
if len(report)!=8: raise SystemExit('Expected 8 NPU worker series; check window and source logs')
