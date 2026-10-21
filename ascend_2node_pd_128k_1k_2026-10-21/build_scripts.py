from pathlib import Path
import shutil
root=Path(r'D:\work\ascend_2node_pd_128k_1k_2026-10-21')
old=Path(r'D:\work\ascend_pd_benchmark_2026-10-21')
for name in ['start_container.sh','preflight.sh','generate_prompts.py','bench_client.py','capacity_probe.py','audit_patch.py','patch_proxy.py','metrics_monitor.py','trace_intervals.py']:
    (root/'scripts'/name).write_text((old/'scripts'/name).read_text(encoding='utf-8'),encoding='utf-8',newline='\n')
(root/'upstream').mkdir(exist_ok=True)
shutil.copyfile(old/'references'/'proxy-v0.23.0.py',root/'upstream'/'load_balance_proxy_server_example.py')
files={
'env.example.sh':r'''#!/usr/bin/env bash
export BENCH_ROOT=/data/v4flash-2node
export MODEL_DIR=/data/models/DeepSeek-V4-Flash-w8a8-mtp
export MODEL=/models/DeepSeek-V4-Flash-w8a8-mtp
export IMAGE=quay.io/ascend/vllm-ascend:v0.23.0
export CONTAINER=v4flash-2node
export SERVED_MODEL=dsv4
export P_IP=10.10.0.11 D_IP=10.10.0.12
export PROXY_PORT=1999
export MAX_MODEL_LEN=133120 MAX_SEQS=32 MEM_UTIL=0.90
export P_TOKEN_BUDGET=8192 D_TOKEN_BUDGET=512
# Per-NPU KV pool budget in BYTES; empty means auto sizing.
export KV_BYTES=${KV_BYTES:-}
export PROFILE=${PROFILE:-0}
export AUDIT_CAPACITY_DIR=${AUDIT_CAPACITY_DIR:-/bench/results/capacity}
export AUDIT_TRANSFER_DIR=${AUDIT_TRANSFER_DIR:-}
export AUDIT_STEP_DIR=${AUDIT_STEP_DIR:-}
# Host-specific NIC and IP are set in env.local.sh, never guessed.
if [[ -f /bench/env.local.sh ]]; then source /bench/env.local.sh; fi
''',
'scripts/common.sh':r'''#!/usr/bin/env bash
source /bench/env.sh
export OMP_PROC_BIND=false OMP_NUM_THREADS=10
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export HCCL_BUFFSIZE=1024 TASK_QUEUE_ENABLE=1 HCCL_OP_EXPANSION_MODE=AIV
export VLLM_RPC_TIMEOUT=3600000 VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=30000
export HCCL_EXEC_TIMEOUT=204 HCCL_CONNECT_TIMEOUT=1200
export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7
export PYTHONPATH="/bench/scripts${PYTHONPATH:+:$PYTHONPATH}"
: "${LOCAL_IP:?set env.local.sh}" "${NIC:?set env.local.sh}"
export HCCL_IF_IP="$LOCAL_IP" GLOO_SOCKET_IFNAME="$NIC"
export HCCL_SOCKET_IFNAME="$NIC" TP_SOCKET_IFNAME="$NIC"
if [[ -f /usr/lib/aarch64-linux-gnu/libjemalloc.so.2 ]]; then
  export LD_PRELOAD="/usr/lib/aarch64-linux-gnu/libjemalloc.so.2${LD_PRELOAD:+:$LD_PRELOAD}"
fi
export NO_PROXY="${NO_PROXY:-},127.0.0.1,localhost,$P_IP,$D_IP"
export no_proxy="$NO_PROXY"
''',
'scripts/serve.sh':r'''#!/usr/bin/env bash
set -euo pipefail
source /bench/scripts/common.sh
role=${1:?prefill_or_decode}
args=( "$MODEL" --host 0.0.0.0 --served-model-name "$SERVED_MODEL"
 --data-parallel-size 1 --tensor-parallel-size 8 --enable-expert-parallel
 --max-model-len "$MAX_MODEL_LEN" --max-num-seqs "$MAX_SEQS"
 --gpu-memory-utilization "$MEM_UTIL" --seed 1024 --quantization ascend
 --tokenizer-mode deepseek_v4 --trust-remote-code --block-size 128
 --no-enable-prefix-caching --no-disable-hybrid-kv-cache-manager
 --async-scheduling --safetensors-load-strategy prefetch
 --speculative-config '{"method":"mtp","num_speculative_tokens":1,"enforce_eager":true}'
)
if [[ "$role" == prefill ]]; then
  args+=(--port 8100 --max-num-batched-tokens "$P_TOKEN_BUDGET" --enforce-eager
    --additional-config '{"enable_cpu_binding":true,"enable_dsa_cp":false}')
  kv_role=kv_producer; kv_port=30000; engine_id=0
elif [[ "$role" == decode ]]; then
  args+=(--port 8200 --max-num-batched-tokens "$D_TOKEN_BUDGET"
    --compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY"}'
    --additional-config '{"ascend_compilation_config":{"enable_npugraph_ex":true,"enable_static_kernel":false},"enable_cpu_binding":true,"enable_dsa_cp":false,"multistream_overlap_shared_expert":true,"recompute_scheduler_enable":true}')
  kv_role=kv_consumer; kv_port=30100; engine_id=1
else
  echo 'Usage: serve.sh prefill|decode'; exit 2
fi
transfer=$(python - "$kv_role" "$kv_port" "$engine_id" <<'PYKV'
import json,sys
print(json.dumps(dict(kv_connector='MooncakeHybridConnector',kv_role=sys.argv[1],
 kv_port=sys.argv[2],engine_id=sys.argv[3],kv_connector_extra_config={
 'prefill':{'dp_size':1,'tp_size':8},'decode':{'dp_size':1,'tp_size':8}})))
PYKV
)
args+=(--kv-transfer-config "$transfer")
if [[ -n "$KV_BYTES" ]]; then args+=(--kv-cache-memory-bytes "$KV_BYTES"); fi
if [[ "$PROFILE" == 1 ]]; then
  args+=(--profiler-config "{\"profiler\":\"torch\",\"torch_profiler_dir\":\"/bench/profiles/$role\",\"torch_profiler_with_stack\":false}")
fi
mkdir -p /bench/results /bench/logs /bench/profiles
printf '%q ' vllm serve "${args[@]}" > "/bench/results/$role-command.txt"
printf '\n' >> "/bench/results/$role-command.txt"
exec vllm serve "${args[@]}"
''',
'scripts/start_proxy.sh':r'''#!/usr/bin/env bash
set -euo pipefail
source /bench/scripts/common.sh
proxy=/bench/upstream/load_balance_proxy_server_example.py
if [[ "${PROXY_AUDIT:-0}" == 1 ]]; then
  python /bench/scripts/patch_proxy.py "$proxy" /bench/upstream/proxy_instrumented.py
  proxy=/bench/upstream/proxy_instrumented.py
  export PROXY_AUDIT_DIR=/bench/results/proxy-audit
fi
exec python "$proxy" --host 0.0.0.0 --port "$PROXY_PORT" --workers 1 --max-retries 1 \
 --prefiller-hosts "$P_IP" --prefiller-ports 8100 \
 --decoder-hosts "$D_IP" --decoder-ports 8200
''',
'scripts/step_probe.py':r'''"""CPU-only worker snapshots; opt in for diagnostic runs, not device timing."""
import atexit
import json
import os
from pathlib import Path
import socket
import time
_buffers={}

def flush():
    for path,rows in _buffers.items():
        if rows:
            with path.open('a',encoding='utf-8') as f: f.write(''.join(rows))
            rows.clear()
atexit.register(flush)

def snapshot(runner,out):
    directory=os.getenv('AUDIT_STEP_DIR')
    if not directory: return
    root=Path(directory); root.mkdir(parents=True,exist_ok=True)
    rows=[]
    for rid,n in out.num_scheduled_tokens.items():
        state=runner.requests.get(rid)
        prompt=getattr(state,'prompt_token_ids',None)
        plen=getattr(state,'num_prompt_tokens',None)
        if plen is None and prompt is not None: plen=len(prompt)
        computed=getattr(state,'num_computed_tokens',None)
        rows.append(dict(request_id=rid,scheduled_tokens=int(n),prompt_tokens=plen,
            computed_tokens=computed,
            decode=bool(computed>=plen) if computed is not None and plen is not None else None))
    row=dict(kind='worker_step',time_ns=time.time_ns(),pid=os.getpid(),
        host=socket.gethostname(),device=str(runner.device),
        batch_size=len(rows),scheduled_tokens=int(out.total_num_scheduled_tokens),
        all_decode=bool(rows) and all(x['decode'] is True for x in rows),requests=rows)
    path=root/f'{socket.gethostname()}-{os.getpid()}.jsonl'
    buf=_buffers.setdefault(path,[]); buf.append(json.dumps(row,default=str)+'\n')
    if len(buf)>=128: flush()
''',
'scripts/patch_steps.py':r'''"""Guarded v0.23.0 diagnostic patch; run AFTER audit_patch.py; restore in reverse order."""
import argparse,ast,importlib.util
from pathlib import Path

def main():
    p=argparse.ArgumentParser(); p.add_argument('--restore',action='store_true'); a=p.parse_args()
    path=Path(importlib.util.find_spec('vllm_ascend').origin).parent/'worker/model_runner_v1.py'
    backup=Path(str(path)+'.steps.bak')
    if a.restore:
        if backup.exists():
            path.write_text(backup.read_text(encoding='utf-8'),encoding='utf-8'); backup.unlink()
        return
    src=path.read_text(encoding='utf-8')
    if '# V4_2NODE_STEP_AUDIT' in src: return
    anchor='''+'"""'+r'''                deferred_state_corrections_fn = self._update_states(
                    scheduler_output
                )'''+'"""'+r'''
    assert src.count(anchor)==1, 'Source mismatch: stop and inspect installed runner'
    replacement=anchor+'\n                # V4_2NODE_STEP_AUDIT\n                from step_probe import snapshot\n                snapshot(self, scheduler_output)'
    modified=src.replace(anchor,replacement); ast.parse(modified)
    assert not backup.exists(), 'Existing backup: inspect first'
    backup.write_text(src,encoding='utf-8'); path.write_text(modified,encoding='utf-8')
    print(path)
if __name__=='__main__': main()
''',
'scripts/offline_decode.py':r'''"""Full-model decode isolation on D hardware. Local prefill is setup, excluded from results.

Uses public EngineArgs/LLMEngine interfaces for the pinned v0.23.0 stack. Keeps all
request outputs cumulative so accepted MTP output tokens are counted exactly.
No device synchronization is inserted in decode iterations. This is NOT PD TTFT.
"""
import argparse,json,os,random,statistics,time
from pathlib import Path

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--batch',type=int,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--prompts',type=Path,default=Path('/bench/data/prompt-131072.json'))
    p.add_argument('--repeats',type=int,default=3);p.add_argument('--warmup',type=int,default=1)
    p.add_argument('--profile',action='store_true');a=p.parse_args()
    from vllm.engine.arg_utils import EngineArgs
    from vllm.v1.engine.llm_engine import LLMEngine
    from vllm import SamplingParams
    from vllm.sampling_params import RequestOutputKind
    data=json.loads(a.prompts.read_text()); assert data['length']==131072
    assert 1<=a.batch<=int(os.environ['MAX_SEQS'])
    cfg=dict(model=os.environ['MODEL'],tensor_parallel_size=8,data_parallel_size=1,
        enable_expert_parallel=True,quantization='ascend',tokenizer_mode='deepseek_v4',
        trust_remote_code=True,seed=1024,max_model_len=int(os.environ['MAX_MODEL_LEN']),
        max_num_seqs=int(os.environ['MAX_SEQS']),
        max_num_batched_tokens=int(os.environ['D_TOKEN_BUDGET']),
        gpu_memory_utilization=float(os.environ['MEM_UTIL']),block_size=128,
        enable_prefix_caching=False,disable_hybrid_kv_cache_manager=False,
        async_scheduling=True,safetensors_load_strategy='prefetch',disable_log_stats=False,
        speculative_config={'method':'mtp','num_speculative_tokens':1,'enforce_eager':True},
        compilation_config={'cudagraph_mode':'FULL_DECODE_ONLY'},
        additional_config={'ascend_compilation_config':{'enable_npugraph_ex':True,'enable_static_kernel':False},
            'enable_cpu_binding':True,'enable_dsa_cp':False,'multistream_overlap_shared_expert':True})
    if os.getenv('KV_BYTES'): cfg['kv_cache_memory_bytes']=int(os.environ['KV_BYTES'])
    if a.profile: cfg['profiler_config']={'profiler':'torch','torch_profiler_dir':'/bench/profiles/offline',
                                        'torch_profiler_with_stack':False}
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.with_suffix('.config.json').write_text(json.dumps(cfg,indent=2))
    engine=LLMEngine.from_engine_args(EngineArgs(**cfg))
    cache={}; preemptions=[0]
    old_update=engine.output_processor.update_scheduler_stats
    def update(s):
        if s is not None:
            cache.update({k:getattr(s,k,None) for k in ['num_running_reqs','num_waiting_reqs',
                        'num_skipped_waiting_reqs','kv_cache_usage','step_counter']})
        return old_update(s)
    engine.output_processor.update_scheduler_stats=update
    old_process=engine.output_processor.process_outputs
    def process(*args,**kwargs):
        result=old_process(*args,**kwargs)
        stats=kwargs.get('iteration_stats')
        preemptions[0]+=int(getattr(stats,'num_preempted_reqs',0))
        return result
    engine.output_processor.process_outputs=process
    params=SamplingParams(temperature=0,ignore_eos=True,max_tokens=1024,min_tokens=1024,
        detokenize=False,output_kind=RequestOutputKind.CUMULATIVE)
    summaries=[]
    for run in range(a.warmup+a.repeats):
        counts={}; first={}; last={}; frames=[]; ids=[]; pre0=preemptions[0]
        for i in range(a.batch):
            prompt=list(data['prompt_token_ids'])
            prompt[:32]=random.Random(20261021+run*10000+i).choices(data['pool'],k=32)
            rid=engine.add_request(f'offline-r{run}-q{i}',{'prompt_token_ids':prompt},params)
            assert rid not in counts
            ids.append(rid);counts[rid]=0
        if a.profile and run>=a.warmup: engine.start_profile()
        start=time.perf_counter();start_ns=time.time_ns()
        while engine.has_unfinished_requests():
            outputs=engine.step();now=time.perf_counter();t_ns=time.time_ns()
            delta=0
            for item in outputs:
                if item.request_id not in counts: raise RuntimeError('Unexpected request ID')
                assert item.prompt_token_ids is not None and len(item.prompt_token_ids)==131072
                n=len(item.outputs[0].token_ids)
                assert n>=counts[item.request_id], 'Cumulative outputs required'
                delta+=n-counts[item.request_id];counts[item.request_id]=n
                if n:
                    first.setdefault(item.request_id,now);last[item.request_id]=now
            frames.append(dict(time_ns=t_ns,t_s=now-start,counts=[counts[rid] for rid in ids],
                accepted_delta=delta,stats=dict(cache),preemptions=preemptions[0]-pre0))
        if a.profile and run>=a.warmup: engine.stop_profile()
        if any(n!=1024 for n in counts.values()): raise RuntimeError(f'Output length mismatch: {counts}')
        # A bounded, fully overlapping decode plateau, away from first/last output.
        groups=[]; current=[]
        for frame in frames:
            valid=(all(32<=n<=896 for n in frame['counts']) and
                frame['stats'].get('num_waiting_reqs')==0 and frame['preemptions']==0)
            if valid: current.append(frame)
            else:
                if current: groups.append(current)
                current=[]
        if current: groups.append(current)
        win=max(groups,key=lambda g:g[-1]['t_s']-g[0]['t_s'],default=[])
        row=dict(run=run,warmup=run<a.warmup,batch_requested=a.batch,start_wall_ns=start_ns,
            all_output_tokens=1024,preemptions=preemptions[0]-pre0,profiled=a.profile,
            plateau_valid=False,client_frontend_tpot_ms=statistics.mean(
                (last[r]-first[r])*1000/1023 for r in ids))
        if len(win)>=2:
            dt=win[-1]['t_s']-win[0]['t_s']
            tokens=sum(win[-1]['counts'])-sum(win[0]['counts'])
            if dt>0 and tokens>0:
                row.update(plateau_start_ns=win[0]['time_ns'],plateau_end_ns=win[-1]['time_ns'],
                    plateau_s=dt,frontend_output_frames=len(win),accepted_tokens=tokens,
                    decode_tokens_per_s=tokens/dt,tpot_equivalent_ms=1000*a.batch*dt/tokens,
                    kv_usage_peak=max(f['stats'].get('kv_cache_usage') or 0 for f in win),
                    running_min=min(f['stats'].get('num_running_reqs') or 0 for f in win),
                    running_max=max(f['stats'].get('num_running_reqs') or 0 for f in win),
                    plateau_valid=dt>=5 and len(win)>=100 and preemptions[0]==pre0,
                    worker_batch_audit_required=True)
        summaries.append(row)
        framepath=a.out.with_name(a.out.stem+f'.r{run}.frames.jsonl')
        framepath.write_text(''.join(json.dumps(f)+'\n' for f in frames),encoding='utf-8')
        a.out.write_text(json.dumps(summaries,indent=2),encoding='utf-8')
        print(json.dumps(row),flush=True)
    if any(not r['plateau_valid'] for r in summaries if not r['warmup']):
        raise SystemExit('No valid full-overlap decode plateau in every measured repeat; do not report requested B as measured B')
if __name__=='__main__': main()
''',
'scripts/audit_window.py':r'''"""Check all NPU workers in a chosen wall-clock interval. Does not infer throughput."""
import argparse,collections,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--steps',type=Path,required=True)
p.add_argument('--summary',type=Path,required=True);p.add_argument('--run',type=int,default=1)
p.add_argument('--transfer',type=Path);p.add_argument('--expected-workers',type=int,default=8)
a=p.parse_args();s=next(r for r in json.loads(a.summary.read_text()) if r['run']==a.run)
start,end=s['plateau_start_ns'],s['plateau_end_ns'];groups=collections.defaultdict(list)
for path in a.steps.glob('*.jsonl'):
    for line in path.read_text().splitlines():
        r=json.loads(line)
        if start<=r['time_ns']<=end: groups[(r['host'],r['pid'],r['device'])].append(r)
report=[]
for key,rows in sorted(groups.items()):
    counts=collections.Counter(r['batch_size'] for r in rows)
    report.append(dict(worker=key,steps=len(rows),batch_distribution=dict(counts),
        all_decode=all(r['all_decode'] for r in rows),
        constant_target=all(r['batch_size']==s['batch_requested'] for r in rows),
        covered_s=(max(r['time_ns'] for r in rows)-min(r['time_ns'] for r in rows))/1e9))
transfers=[]
if a.transfer:
    for path in a.transfer.glob('*.jsonl'):
        for line in path.read_text().splitlines():
            r=json.loads(line);t1=r['time_ns'];t0=t1-int(r.get('duration_ms',0)*1e6)
            if t0<end and t1>start:transfers.append(r)
valid=(len(report)==a.expected_workers and all(r['steps']>=100 and r['all_decode'] and
    r['constant_target'] and r['covered_s']>=4.5 for r in report) and not transfers and s['plateau_valid'])
print(json.dumps(dict(valid=valid,window_s=(end-start)/1e9,workers=report,
    overlapping_transfer_calls=len(transfers),note='Also inspect KV preemption/logs and transfer kernels in trace'),indent=2))
raise SystemExit(0 if valid else 2)
''',
'scripts/compare_ttft.py':r'''"""Compare equal-size simultaneous waves to idle-server single-request repetitions."""
import argparse,json,statistics
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--single',type=Path,nargs='+',required=True)
p.add_argument('--batch',type=Path,nargs='+',required=True);p.add_argument('--bs',type=int,required=True)
p.add_argument('--tolerance',type=float,default=.10);a=p.parse_args()
def read(paths):
    runs=[]
    for path in paths:
        summary=json.loads(path.read_text());raw=path.with_suffix('.requests.jsonl')
        rows=[json.loads(line) for line in raw.read_text().splitlines()]
        assert summary['mode']=='wave' and not summary['all_phase_failures']
        assert all(not r['error'] for r in rows)
        assert all(r['usage']['prompt_tokens']==131072 and r['usage']['completion_tokens']==1024 for r in rows)
        runs.append(rows)
    return runs
def ttft(r):
    value=r['ttft_token_ms'] if r['ttft_token_ms'] is not None else r['ttft_text_ms']
    assert value is not None
    return value
single=read(a.single);batch=read(a.batch)
assert all(len(r)==1 for r in single), 'Use separate idle-server single-request runs'
assert all(len(r)==a.bs for r in batch)
t1=statistics.mean(ttft(run[0]) for run in single)
means=[statistics.mean(ttft(r) for r in run) for run in batch]
lasts=[max(ttft(r) for r in run) for run in batch]
spreads=[1000*(max(r['start_s'] for r in run)-min(r['start_s'] for r in run)) for run in batch]
mean=statistics.mean(means);last=statistics.mean(lasts)
print(json.dumps(dict(bs=a.bs,single_repetitions=len(single),batch_repetitions=len(batch),
    single_mean_ttft_ms=t1,batch_mean_ttft_ms=mean,batch_mean_last_ttft_ms=last,
    request_start_spread_ms=spreads,batch_to_single=mean/t1,
    mentor_prediction_ms=a.bs*t1,relative_error_to_mentor_prediction=mean/(a.bs*t1)-1,
    within_declared_tolerance=abs(mean/(a.bs*t1)-1)<=a.tolerance,
    ideal_serial_equal_service_mean_prediction_ms=(a.bs+1)/2*t1,
    tolerance=a.tolerance,note='Descriptive comparison; no causal conclusion from free HBM alone'),indent=2))
''',
'scripts/host_monitor.sh':r'''#!/usr/bin/env bash
set -euo pipefail
out=${1:?output_path}
mkdir -p "$(dirname "$out")"
while true; do
  date -u +'%Y-%m-%dT%H:%M:%S.%NZ'
  npu-smi info
  sleep 2
done >> "$out" 2>&1
'''
}
for name,content in files.items():
    (root/name).write_text(content,encoding='utf-8',newline='\n')
print('Created',len(files),'new files and copied reusable client/monitor/source files')
