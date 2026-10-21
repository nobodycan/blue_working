from pathlib import Path
root=Path(r'D:\work\ascend_2node_pd_128k_1k_2026-10-21')
p=root/'scripts/offline_decode.py'
s=p.read_text(encoding='utf-8')
s=s.replace("enable_prefix_caching=False,disable_hybrid_kv_cache_manager=False,", "enable_prefix_caching=False,enable_chunked_prefill=True,disable_hybrid_kv_cache_manager=False,")
s=s.replace("            rid=engine.add_request(f'offline-r{run}-q{i}',{'prompt_token_ids':prompt},params)\n            assert rid not in counts\n            ids.append(rid);counts[rid]=0", "            rid=f'offline-r{run}-q{i}'\n            internal_id=engine.add_request(rid,{'prompt_token_ids':prompt},params)\n            # add_request returns a randomized INTERNAL ID; RequestOutput uses rid.\n            assert rid not in counts\n            ids.append(rid);counts[rid]=0")
p.write_text(s,encoding='utf-8',newline='\n')
p=root/'scripts/serve.sh';s=p.read_text(encoding='utf-8').replace('--no-enable-prefix-caching --no-disable-hybrid-kv-cache-manager','--no-enable-prefix-caching --enable-chunked-prefill --no-disable-hybrid-kv-cache-manager');p.write_text(s,encoding='utf-8',newline='\n')
p=root/'scripts/compare_ttft.py';s=p.read_text(encoding='utf-8')
s=s.replace("mean=statistics.mean(means);last=statistics.mean(lasts)","makespans=[max(r['start_s']*1000+ttft(r) for r in run)-min(r['start_s']*1000 for r in run) for run in batch]\nmean=statistics.mean(means);last=statistics.mean(lasts)")
s=s.replace('request_start_spread_ms=spreads,batch_to_single=mean/t1,','request_start_spread_ms=spreads,wave_first_token_makespan_mean_ms=statistics.mean(makespans),\n    batch_to_single=mean/t1,last_to_single=last/t1,')
p.write_text(s,encoding='utf-8',newline='\n')
p=root/'README-body.md';s=p.read_text(encoding='utf-8-sig')
wrong='''```bash
docker exec "$CONTAINER" python - <<'PY'
from pathlib import Path
from torch_npu.profiler.profiler import analyse
for path in Path('/bench/profiles').rglob('*ascend_pt'):
    analyse(str(path))
PY
```

如果从 shell 使用带 stdin 的 heredoc，`docker exec` 要加 `-i`；推荐直接按下面这条执行：

'''
s=s.replace(wrong,'带 stdin 的 heredoc 使用 `docker exec -i`：\n\n')
s=s.replace('2. 环境预检、短请求语义检查、128K/1K 精确计数检查。','2. 环境预检、生成精确 token 输入；先通过离线 B=1 运行闸门。')
s=s.replace('5. 启动真实 1P+1D；单并发热身后测 30 次独立请求。','5. 启动真实 1P+1D，完成短请求语义与 128K/1K 精确计数检查；单并发热身后测 30 次独立请求。')
p.write_text(s,encoding='utf-8',newline='\n')
(root/'scripts/report_batches.py').write_text(r'''"""Actual worker scheduling-batch distribution during one client measurement window.
Idle time has no execute_model event; use running/waiting metrics for occupancy.
"""
import argparse,collections,json,statistics
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--steps',type=Path,required=True)
p.add_argument('--summary',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
a=p.parse_args();s=json.loads(a.summary.read_text())
start=s['start_wall_ns']+int(s['window_start_s']*1e9)
end=s['start_wall_ns']+int(s['window_end_s']*1e9)
groups=collections.defaultdict(list)
for path in a.steps.glob('*.jsonl'):
    for line in path.read_text().splitlines():
        r=json.loads(line)
        if start<=r['time_ns']<end:groups[(r['host'],r['pid'],r['device'])].append(r)
def q(xs,p):
    xs=sorted(xs);return xs[round((len(xs)-1)*p)]
report=[]
for key,rows in sorted(groups.items()):
    sizes=[r['batch_size'] for r in rows]
    report.append(dict(worker=key,scheduled_iterations=len(rows),min=min(sizes),p50=q(sizes,.5),
        p95=q(sizes,.95),max=max(sizes),iteration_weighted_mean=statistics.mean(sizes),
        distribution=dict(collections.Counter(sizes)),all_decode_fraction=sum(r['all_decode'] for r in rows)/len(rows)))
a.out.write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
if len(report)!=8: raise SystemExit('Expected 8 NPU worker series; check window and source logs')
''',encoding='utf-8',newline='\n')
