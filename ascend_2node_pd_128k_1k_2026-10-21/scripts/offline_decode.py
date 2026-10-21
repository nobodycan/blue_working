"""Full-model decode isolation on D hardware. Local prefill is setup, excluded from results.

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
        enable_prefix_caching=False,enable_chunked_prefill=True,disable_hybrid_kv_cache_manager=False,
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
            rid=f'offline-r{run}-q{i}'
            internal_id=engine.add_request(rid,{'prompt_token_ids':prompt},params)
            # add_request returns a randomized INTERNAL ID; RequestOutput uses rid.
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
