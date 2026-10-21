from pathlib import Path
root=Path(r'D:\work\ascend_2node_pd_128k_1k_2026-10-21')
p=root/'scripts/step_probe.py'
s=p.read_text(encoding='utf-8').replace('_buffers={}','_buffers={}\n_last_flush=0.0').replace('    if len(buf)>=128: flush()','    global _last_flush\n    now=time.monotonic()\n    if len(buf)>=32 or now-_last_flush>=0.25:\n        flush(); _last_flush=now')
p.write_text(s,encoding='utf-8',newline='\n')
(root/'scripts/pd_plateau.py').write_text(r'''"""Build a candidate pure-D window from a finite PD wave with token ID events.
Worker audit and absence of KV transfer must still be checked separately.
"""
import argparse,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--summary',type=Path,required=True)
p.add_argument('--out',type=Path,required=True);a=p.parse_args()
s=json.loads(a.summary.read_text());assert s['mode']=='wave' and s['return_token_ids']
rows=[json.loads(x) for x in a.summary.with_suffix('.requests.jsonl').read_text().splitlines()]
assert len(rows)==s['client_concurrency'] and all(not r['error'] for r in rows)
events=sorted((e['t_s'],i,e['tokens']) for i,r in enumerate(rows) for e in r['token_events'])
counts=[0]*len(rows);groups=[];win=[]
for t,i,n in events:
    counts[i]+=n
    if all(32<=k<=896 for k in counts): win.append((t,list(counts)))
    else:
        if win:groups.append(win)
        win=[]
if win:groups.append(win)
best=max(groups,key=lambda g:g[-1][0]-g[0][0],default=[])
result=dict(run=0,batch_requested=len(rows),plateau_valid=False,source=str(a.summary))
if len(best)>=2:
    dt=best[-1][0]-best[0][0];tokens=sum(best[-1][1])-sum(best[0][1])
    if dt>0 and tokens>0:
        result.update(plateau_valid=dt>=5,plateau_s=dt,
            plateau_start_ns=s['start_wall_ns']+round(best[0][0]*1e9),
            plateau_end_ns=s['start_wall_ns']+round(best[-1][0]*1e9),
            decode_tokens_per_s=tokens/dt,tpot_equivalent_ms=1000*len(rows)*dt/tokens)
a.out.write_text(json.dumps([result],indent=2));print(json.dumps(result,indent=2))
raise SystemExit(0 if result['plateau_valid'] else 2)
''',encoding='utf-8',newline='\n')
