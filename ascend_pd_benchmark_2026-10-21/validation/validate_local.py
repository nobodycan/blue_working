"""Local-only checks; deliberately does not claim NPU/RDMA/deployment validation."""
import argparse
import ast
import asyncio
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))


def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/f'{name}.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def check_static():
    for path in (ROOT/'scripts').glob('*.py'):
        ast.parse(path.read_text(encoding='utf-8'),filename=str(path))
    import tree_sitter, tree_sitter_bash
    parser=tree_sitter.Parser(tree_sitter.Language(tree_sitter_bash.language()))
    for path in (ROOT/'scripts').glob('*.sh'):
        tree=parser.parse(path.read_bytes())
        if tree.root_node.has_error:
            def errors(node):
                if node.type=='ERROR' or node.is_missing: print(path.name,node)
                for child in node.children: errors(child)
            errors(tree.root_node)
            raise AssertionError('Bash parse error: '+str(path))
    trace=load('trace_intervals')
    assert trace.union_length([(0,10),(5,15),(20,25)])==20


def check_patches():
    with tempfile.TemporaryDirectory(dir=ROOT/'validation') as folder:
        base=Path(folder)
        runner=base/'worker/model_runner_v1.py'
        connector=base/'distributed/kv_transfer/kv_p2p/mooncake_hybrid_connector.py'
        runner.parent.mkdir(parents=True); connector.parent.mkdir(parents=True)
        shutil.copyfile(ROOT/'references/runner-v0.23.0.py',runner)
        shutil.copyfile(ROOT/'references/hybrid-v0.23.0.py',connector)
        original=[p.read_text(encoding='utf-8') for p in (runner,connector)]
        subprocess.run([sys.executable,str(ROOT/'scripts/audit_patch.py'),'--root',str(base)],check=True)
        for path in (runner,connector): ast.parse(path.read_text(encoding='utf-8'))
        assert 'dump_capacity(self, kv_caches)' in runner.read_text(encoding='utf-8')
        assert connector.read_text(encoding='utf-8').count('ret = measured_read(')==2
        subprocess.run([sys.executable,str(ROOT/'scripts/audit_patch.py'),'--root',str(base),'--restore'],check=True)
        assert [p.read_text(encoding='utf-8') for p in (runner,connector)]==original
        output=base/'proxy_instrumented.py'
        subprocess.run([sys.executable,str(ROOT/'scripts/patch_proxy.py'),
                        str(ROOT/'references/proxy-v0.23.0.py'),str(output)],check=True)
        ast.parse(output.read_text(encoding='utf-8'))


def check_report():
    import csv
    with tempfile.TemporaryDirectory(dir=ROOT/'validation') as folder:
        base=Path(folder)
        for concurrency,tpots in ((8,[29,29.5,29.9]),(16,[45,49,51])):
            for repeat,tpot in enumerate(tpots,1):
                row=dict(input_tokens=65536,output_tokens=1024,client_concurrency=concurrency,
                         sufficient_samples=True,valid_requests=30,all_phase_failures=0,
                         measured_failures=0,ttft_text_ms={'mean':1000},tpot_request_ms={'mean':tpot})
                (base/f'pd-L65536-C{concurrency}-r{repeat}.json').write_text(json.dumps(row))
        output=base/'result.csv'
        subprocess.run([sys.executable,str(ROOT/'scripts/summarize_results.py'),
                        '--directory',str(base),'--out',str(output)],check=True)
        rows=list(csv.DictReader(output.open(encoding='utf-8-sig')))
        assert rows[0]['candidate_30ms']=='True'
        assert rows[1]['candidate_50ms']=='False'


async def check_client():
    from aiohttp import web
    client=load('bench_client')
    bad=False
    observed_bodies=[]
    async def handler(request):
        body=await request.json(); observed_bodies.append(body)
        response=web.StreamResponse(headers={'Content-Type':'text/event-stream'})
        await response.prepare(request)
        for pos in range(4):
            choice=dict(index=0,text=f'word{pos}',finish_reason='length' if pos==3 else None)
            if body['return_token_ids']: choice['token_ids']=[20+2*pos,21+2*pos]
            frame=('data: '+json.dumps({'choices':[choice]})+'\n\n').encode()
            await response.write(frame[:7]); await response.write(frame[7:])
            await asyncio.sleep(.015)
        usage={'prompt_tokens':len(body['prompt'])+(1 if bad else 0),'completion_tokens':8}
        await response.write(('data: '+json.dumps({'choices':[],'usage':usage})+'\n\n').encode())
        await response.write(b'data: [DONE]\n\n'); await response.write_eof()
        return response
    app=web.Application(); app.router.add_post('/v1/completions',handler)
    runner=web.AppRunner(app); await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0); await site.start()
    port=site._server.sockets[0].getsockname()[1]
    try:
        with tempfile.TemporaryDirectory(dir=ROOT/'validation') as folder:
            path=Path(folder)
            prompts=path/'prompt.json'
            prompts.write_text(json.dumps({'length':64,'prompt_token_ids':[10]*64,'pool':list(range(10,60))}))
            args=argparse.Namespace(base_url=f'http://127.0.0.1:{port}',model='mock',
                prompts=prompts,out=path/'wave.json',concurrency=2,mode='wave',requests=4,
                burnin=0,duration=1,timeout=10,output_length=8,min_samples=4,return_token_ids=True)
            with contextlib.redirect_stdout(io.StringIO()): result=await client.main(args)
            assert result==0
            data=json.loads(args.out.read_text()); assert data['valid_requests']==4
            records=[json.loads(line) for line in args.out.with_suffix('.requests.jsonl').read_text().splitlines()]
            assert all(r['output_ids']==8 and len(r['token_events'])==4 for r in records)
            assert all(r['first_event_tokens']==2 for r in records)
            assert all(len(b['prompt'])==64 and b['add_special_tokens'] is False for b in observed_bodies)
            assert len({tuple(b['prompt'][:32]) for b in observed_bodies})==4
            args.mode='steady'; args.burnin=.1; args.duration=.45
            args.min_samples=3; args.return_token_ids=False; args.out=path/'steady.json'
            with contextlib.redirect_stdout(io.StringIO()): result=await client.main(args)
            assert result==0
            data=json.loads(args.out.read_text()); assert data['ttft_token_ms']['mean'] is None
            records=[json.loads(line) for line in args.out.with_suffix('.requests.jsonl').read_text().splitlines()]
            included=[r for r in records if r['in_measure_window']]
            assert len(included)==data['valid_requests'] and len(included)>=3
            assert all(.1<=r['start_s']<=r['end_s']<=.55 for r in included)
            assert any(not r['in_measure_window'] for r in records)
            bad=True; args.mode='wave'; args.out=path/'bad.json'; args.requests=1; args.min_samples=1
            with contextlib.redirect_stdout(io.StringIO()): result=await client.main(args)
            assert result==2
            data=json.loads(args.out.read_text()); assert data['all_phase_failures']==1
            assert data['valid_requests']==0
    finally:
        await runner.cleanup()


if __name__=='__main__':
    check_static(); print('Python AST, Bash grammar, interval union: PASS')
    check_patches(); print('Patch anchors / AST / restore against downloaded sources: PASS')
    check_report(); print('Report candidates reject a failed repetition: PASS')
    asyncio.run(check_client()); print('Mock SSE, MTP-style grouped output, length checks, steady-window filtering: PASS')
