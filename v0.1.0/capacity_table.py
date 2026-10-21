"""Export all startup capacity snapshots as CSV; no runtime-peak claim."""
import argparse,csv,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True)
p.add_argument('--out',type=Path,required=True);a=p.parse_args()
fields=['host','pid','device','time_ns','num_blocks','kv_pool_gib',
        'model_storage_gib','startup_reserved_gib','startup_free_gib','total_gib']
with a.out.open('w',newline='',encoding='utf-8-sig') as f:
    w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
    for path in sorted(a.directory.glob('*.jsonl')):
        for line in path.read_text().splitlines():
            r=json.loads(line)
            if r.get('kind')!='capacity_post_kv_before_graph':continue
            row={k:r.get(k) for k in fields[:5]}
            for dst,src in [('kv_pool_gib','actual_unique_cache_storage_bytes'),
                ('model_storage_gib','model_parameter_buffer_storage_bytes'),
                ('startup_reserved_gib','reserved_bytes'),('startup_free_gib','free_bytes'),('total_gib','total_bytes')]:
                row[dst]=r[src]/1024**3
            w.writerow(row)
print(a.out)
