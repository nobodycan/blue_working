"""Compare equal-size simultaneous waves to idle-server single-request repetitions."""
import argparse,json,statistics
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--single',type=Path,nargs='+',required=True)
p.add_argument('--batch',type=Path,nargs='+',required=True);p.add_argument('--bs',type=int,required=True)
p.add_argument('--tolerance',type=float,default=.10);a=p.parse_args()
def read(paths):
    runs=[]
    for path in paths:
        summary=json.loads(path.read_text());raw=path.with_suffix('.requests.jsonl')
        rows=[json.loads(line) for line in raw.read_text().splitlines()]
        assert summary['mode']=='wave' and not summary['all_phase_failures']
        assert all(not r['error'] for r in rows)
        assert all(r['usage']['prompt_tokens']==131072 and r['usage']['completion_tokens']==1024 for r in rows)
        runs.append(rows)
    return runs
def ttft(r):
    value=r['ttft_token_ms'] if r['ttft_token_ms'] is not None else r['ttft_text_ms']
    assert value is not None
    return value
single=read(a.single);batch=read(a.batch)
assert all(len(r)==1 for r in single), 'Use separate idle-server single-request runs'
assert all(len(r)==a.bs for r in batch)
t1=statistics.mean(ttft(run[0]) for run in single)
means=[statistics.mean(ttft(r) for r in run) for run in batch]
lasts=[max(ttft(r) for r in run) for run in batch]
spreads=[1000*(max(r['start_s'] for r in run)-min(r['start_s'] for r in run)) for run in batch]
makespans=[max(r['start_s']*1000+ttft(r) for r in run)-min(r['start_s']*1000 for r in run) for run in batch]
mean=statistics.mean(means);last=statistics.mean(lasts)
print(json.dumps(dict(bs=a.bs,single_repetitions=len(single),batch_repetitions=len(batch),
    single_mean_ttft_ms=t1,batch_mean_ttft_ms=mean,batch_mean_last_ttft_ms=last,
    request_start_spread_ms=spreads,wave_first_token_makespan_mean_ms=statistics.mean(makespans),
    batch_to_single=mean/t1,last_to_single=last/t1,
    mentor_prediction_ms=a.bs*t1,relative_error_to_mentor_prediction=mean/(a.bs*t1)-1,
    within_declared_tolerance=abs(mean/(a.bs*t1)-1)<=a.tolerance,
    ideal_serial_equal_service_mean_prediction_ms=(a.bs+1)/2*t1,
    tolerance=a.tolerance,note='Descriptive comparison; no causal conclusion from free HBM alone'),indent=2))
