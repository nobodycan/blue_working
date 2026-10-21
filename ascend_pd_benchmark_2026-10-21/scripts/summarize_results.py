"""CSV summary and latency-only candidates; hardware capacity still requires engine evidence."""
import argparse
import csv
import json
from pathlib import Path
import statistics

p=argparse.ArgumentParser()
p.add_argument('--directory',type=Path,required=True)
p.add_argument('--pattern',default='pd-L*-C*-r*.json')
p.add_argument('--out',type=Path,required=True)
a=p.parse_args()
groups={}
for path in sorted(a.directory.glob(a.pattern)):
    row=json.loads(path.read_text(encoding='utf-8'))
    key=(row['input_tokens'],row['output_tokens'],row['client_concurrency'])
    groups.setdefault(key,[]).append((path,row))
if not groups: raise SystemExit('No matching results')
rows=[]
for (length,output,concurrency),runs in sorted(groups.items()):
    valid=all(r['sufficient_samples'] and r['valid_requests']>=30 and
              not r['all_phase_failures'] and not r['measured_failures'] for _,r in runs)
    ttft=[r['ttft_text_ms']['mean'] for _,r in runs]
    tpot=[r['tpot_request_ms']['mean'] for _,r in runs]
    measured=[v for v in tpot if v is not None]
    qualified=valid and len(runs)>=3 and len(measured)==len(runs)
    rows.append(dict(input_tokens=length,output_tokens=output,client_concurrency=concurrency,
        repetitions=len(runs),all_lengths_and_samples_valid=valid,
        max_end_censored_fraction=max(r.get('end_censored_fraction',0) or 0 for _,r in runs),
        mean_of_run_ttft_ms=statistics.mean(ttft) if all(v is not None for v in ttft) else '',
        mean_of_run_tpot_ms=statistics.mean(measured) if measured else '',
        worst_run_tpot_ms=max(measured) if measured else '',
        candidate_30ms=qualified and max(measured)<=30,
        candidate_50ms=qualified and max(measured)<=50,
        actual_decode_batch='REQUIRES_ENGINE_TRACE',kv_budget_bytes='REQUIRES_CAPACITY_SNAPSHOT',
        queue_preempt_recompute_evidence='REQUIRES_METRICS_AND_TRACE',
        source_files=';'.join(str(path) for path,_ in runs)))
a.out.parent.mkdir(parents=True,exist_ok=True)
with a.out.open('w',newline='',encoding='utf-8-sig') as f:
    writer=csv.DictWriter(f,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
print(a.out)
for length in sorted({r['input_tokens'] for r in rows}):
    for target in (30,50):
        candidates=[r['client_concurrency'] for r in rows if r['input_tokens']==length and r[f'candidate_{target}ms']]
        print(f'L={length}, TPOT<={target}ms: max tested client concurrency candidate = {max(candidates) if candidates else None}; HBM/batch evidence still required')
