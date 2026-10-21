"""Measure ONLY manually selected device streams in a selected stage/window.

PID/TID meaning must first be checked in MindStudio Insight. Do not select host
threads or a trace already aggregating multiple devices. Times are Chrome trace us.
"""
import argparse
import json
from pathlib import Path
import re


def union_length(intervals):
    total=0.0; end=None
    for start, stop in sorted(intervals):
        if end is None or start>end:
            total+=stop-start; end=stop
        elif stop>end:
            total+=stop-end; end=stop
    return total


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--trace',type=Path,required=True)
    p.add_argument('--pid',required=True)
    p.add_argument('--tids',nargs='*')
    p.add_argument('--start-us',type=float,required=True)
    p.add_argument('--end-us',type=float,required=True)
    p.add_argument('--name-regex',default='.*')
    p.add_argument('--out',type=Path,required=True)
    a=p.parse_args()
    if a.end_us<=a.start_us: p.error('end must be greater than start')
    data=json.loads(a.trace.read_text(encoding='utf-8'))
    events=data['traceEvents'] if isinstance(data,dict) else data
    spans=[]; names=set()
    for e in events:
        if e.get('ph')!='X' or str(e.get('pid'))!=a.pid: continue
        if a.tids and str(e.get('tid')) not in a.tids: continue
        if not re.search(a.name_regex,e.get('name','')): continue
        start=max(float(e['ts']),a.start_us)
        stop=min(float(e['ts'])+float(e.get('dur',0)),a.end_us)
        if stop>start: spans.append((start,stop)); names.add(e.get('name',''))
    if not spans: raise RuntimeError('No matching intervals; inspect trace device PID/TID selection')
    result=dict(events=len(spans),pid=a.pid,tids=a.tids,
        window_ms=(a.end_us-a.start_us)/1000,
        active_union_ms=union_length(spans)/1000,
        kernel_sum_ms=sum(t-s for s,t in spans)/1000,
        first_to_last_span_ms=(max(t for s,t in spans)-min(s for s,t in spans))/1000,
        distinct_names=sorted(names))
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__': main()
