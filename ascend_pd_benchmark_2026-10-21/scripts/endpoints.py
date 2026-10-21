"""Generate exact host:port lists for monitoring/profiler control. No discovery guesses."""
import argparse
import json
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('--p-hosts', nargs='+', required=True)
p.add_argument('--d-hosts', nargs='+', required=True)
p.add_argument('--out', type=Path, required=True)
a=p.parse_args()
rows=[dict(role=role, host=h, port=port, url=f'http://{h}:{port}')
      for role, hosts in [('prefill',a.p_hosts),('decode',a.d_hosts)]
      for h in hosts for port in range(7100,7108)]
a.out.parent.mkdir(exist_ok=True,parents=True)
a.out.write_text(json.dumps(rows,indent=2),encoding='utf-8')
print(len(rows), 'endpoints written')
