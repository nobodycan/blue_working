"""Read health or toggle profiler on EVERY explicitly listed vLLM API instance."""
import argparse
import concurrent.futures
import json
from pathlib import Path
import urllib.request

p=argparse.ArgumentParser()
p.add_argument('--endpoints', type=Path, required=True)
p.add_argument('--action', choices=['health','start_profile','stop_profile','metrics'], required=True)
p.add_argument('--role', choices=['all','prefill','decode'], default='all')
p.add_argument('--out', type=Path)
a=p.parse_args()
rows=[r for r in json.loads(a.endpoints.read_text()) if a.role=='all' or r['role']==a.role]

def call(row):
    url=row['url']+'/'+a.action
    method='POST' if a.action in ('start_profile','stop_profile') else 'GET'
    try:
        with urllib.request.urlopen(urllib.request.Request(url,method=method), timeout=300) as response:
            return dict(**row, status=response.status, body=response.read().decode())
    except Exception as e: return dict(**row,status=None,error=repr(e))

with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool: result=list(pool.map(call,rows))
payload=json.dumps(result,indent=2)
if a.out:
    a.out.parent.mkdir(exist_ok=True,parents=True); a.out.write_text(payload,encoding='utf-8')
print('\n'.join(f"{r['url']}: {r['status']} {r.get('error','')}" for r in result))
raise SystemExit(0 if all(r['status']==200 for r in result) else 2)
