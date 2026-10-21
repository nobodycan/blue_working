"""Wait for HTTP 200, bounded by one overall deadline, bypassing HTTP proxy."""
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
