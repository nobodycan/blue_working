from pathlib import Path
import shutil
src=Path(r'D:\work\ascend_2node_pd_128k_1k_2026-10-21')
dst=Path(r'D:\work\a2_pd_operations_2026-10-21')
(dst/'scripts').mkdir(parents=True,exist_ok=True)
(dst/'upstream').mkdir(exist_ok=True)
for p in (src/'scripts').glob('*'):
    if p.is_file():shutil.copyfile(p,dst/'scripts'/p.name)
shutil.copyfile(src/'env.example.sh',dst/'env.example.sh')
shutil.copyfile(src/'upstream/load_balance_proxy_server_example.py',dst/'upstream/load_balance_proxy_server_example.py')
(dst/'scripts/wait_ready.py').write_text(r'''"""Wait for HTTP 200, bounded by one overall deadline, bypassing HTTP proxy."""
import argparse,time,urllib.request
p=argparse.ArgumentParser();p.add_argument('--url',action='append',required=True)
p.add_argument('--timeout',type=float,default=1800);a=p.parse_args()
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline=time.monotonic()+a.timeout
pending=set(a.url)
while pending and time.monotonic()<deadline:
    for url in list(pending):
        try:
            with opener.open(url,timeout=min(5,max(.1,deadline-time.monotonic()))) as r:
                if r.status==200:pending.remove(url);print('READY',url,flush=True)
        except Exception:pass
    if pending:time.sleep(min(5,max(0,deadline-time.monotonic())))
if pending:raise SystemExit('TIMEOUT: '+', '.join(sorted(pending)))
''',encoding='utf-8',newline='\n')
(dst/'scripts/write_bs.py').write_text(r'''"""Create selection.env only from three successful fixed-batch PD wave audits."""
import argparse,json,re,shlex
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True)
p.add_argument('--candidate',type=int,required=True);p.add_argument('--out',type=Path,required=True)
a=p.parse_args();b=a.candidate
for repeat in (1,2,3):
    prefix=a.directory/f'C{b}-r{repeat}'
    audit=json.loads(prefix.with_suffix('.audit.json').read_text())
    window=json.loads(prefix.with_suffix('.window.json').read_text())[0]
    assert audit['valid'] is True, f'Worker audit not valid: repeat {repeat}'
    assert window['plateau_valid'] is True and window['batch_requested']==b
    assert len(audit['workers'])==8
    assert all(r['constant_target'] and r['all_decode'] for r in audit['workers'])
if a.out.exists():raise SystemExit('selection.env exists; use a fresh run directory or archive it first')
a.out.write_text(f'export BS={b}\n',encoding='utf-8')
print(f'CONFIRMED BS={b}; saved {a.out}')
''',encoding='utf-8',newline='\n')
(dst/'scripts/offline_table.py').write_text(r'''"""Read completed offline decode summaries; write an explicitly labelled CSV."""
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
''',encoding='utf-8',newline='\n')
(dst/'scripts/capacity_table.py').write_text(r'''"""Export all startup capacity snapshots as CSV; no runtime-peak claim."""
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
''',encoding='utf-8',newline='\n')
print('Prepared independent operations bundle:',dst)
