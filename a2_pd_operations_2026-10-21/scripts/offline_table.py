"""Read completed offline decode summaries; write an explicitly labelled CSV."""
import argparse,csv,json,statistics
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True)
p.add_argument('--out',type=Path,required=True);a=p.parse_args()
fields=['batch_requested','measured_repeats','all_frontend_plateaus_valid',
        'mean_decode_tps','mean_equivalent_tpot_ms','max_kv_usage',
        'preemptions','worker_audit_still_required','source']
with a.out.open('w',newline='',encoding='utf-8-sig') as f:
    w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
    for path in sorted(a.directory.glob('B*.json')):
        data=json.loads(path.read_text())
        if not isinstance(data,list):continue
        rows=[r for r in data if not r['warmup']]
        if not rows:continue
        good=all(r.get('plateau_valid') for r in rows)
        w.writerow(dict(batch_requested=rows[0]['batch_requested'],measured_repeats=len(rows),
            all_frontend_plateaus_valid=good,
            mean_decode_tps=statistics.mean(r['decode_tokens_per_s'] for r in rows) if good else '',
            mean_equivalent_tpot_ms=statistics.mean(r['tpot_equivalent_ms'] for r in rows) if good else '',
            max_kv_usage=max(r.get('kv_usage_peak',0) for r in rows),
            preemptions=sum(r.get('preemptions',0) for r in rows),
            worker_audit_still_required=True,source=path.name))
print(a.out)
