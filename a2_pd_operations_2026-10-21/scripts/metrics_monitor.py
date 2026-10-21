"""Save raw Prometheus scrapes without assuming metric names or summing DP duplicates."""
import argparse
import concurrent.futures
import json
from pathlib import Path
import time
import urllib.request

p=argparse.ArgumentParser()
p.add_argument('--endpoints',type=Path,required=True)
p.add_argument('--out',type=Path,required=True)
p.add_argument('--interval',type=float,default=2)
a=p.parse_args()
rows=json.loads(a.endpoints.read_text())
a.out.parent.mkdir(parents=True,exist_ok=True)

def scrape(row):
    try:
        with urllib.request.urlopen(row['url']+'/metrics',timeout=5) as r:
            return dict(**row,time_ns=time.time_ns(),status=r.status,text=r.read().decode())
    except Exception as e: return dict(**row,time_ns=time.time_ns(),error=repr(e))

with a.out.open('a',encoding='utf-8') as f, concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
    try:
        while True:
            start=time.monotonic()
            for row in pool.map(scrape,rows): f.write(json.dumps(row)+'\n')
            f.flush()
            time.sleep(max(0,a.interval-(time.monotonic()-start)))
    except KeyboardInterrupt: pass
