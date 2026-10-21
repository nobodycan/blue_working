import ast
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT=Path(__file__).resolve().parents[1]
marker='<!-- SCRIPT_APPENDIX -->'
readme=ROOT/'README.md'
text=readme.read_text(encoding='utf-8').split(marker)[0]+marker+'\n\n'
text+='## 附录：完整脚本源码\n\n以下代码与配套 `scripts/` 文件逐字一致。\n\n'
scripts=sorted(p for p in (ROOT/'scripts').iterdir() if p.suffix in ('.sh','.py'))
for path in scripts:
    source=path.read_text(encoding='utf-8').rstrip('\n')+'\n'
    path.write_text(source,encoding='utf-8',newline='\n')
    language='bash' if path.suffix=='.sh' else 'python'
    name='scripts/'+path.name
    text+=f'<!-- BEGIN_SCRIPT: {name} -->\n```{language}\n'+source+'```\n<!-- END_SCRIPT -->\n\n'
readme.write_text(text,encoding='utf-8',newline='\n')
pattern=r'^<!-- BEGIN_SCRIPT: (scripts/[A-Za-z0-9_.-]+) -->\n```(?:python|bash)\n(.*?)\n```\n<!-- END_SCRIPT -->'
embedded=re.findall(pattern,text,re.M|re.S)
assert len(embedded)==len(scripts)
for name,body in embedded:
    assert (ROOT/name).read_text(encoding='utf-8')==body+'\n'
    if name.endswith('.py'): ast.parse(body,filename=name)
assert text.count('\n```')%2==0, 'Unbalanced fences'
sources={
'model-v0.23.0.md':'https://raw.githubusercontent.com/vllm-project/vllm-ascend/v0.23.0/docs/source/tutorials/models/DeepSeek-V4-Flash.md',
'installation-v0.23.0.md':'https://raw.githubusercontent.com/vllm-project/vllm-ascend/v0.23.0/docs/source/installation.md',
'pd-v0.23.0.md':'https://raw.githubusercontent.com/vllm-project/vllm-ascend/v0.23.0/docs/source/tutorials/features/pd_disaggregation_mooncake_multi_node.md',
'proxy-v0.23.0.py':'https://raw.githubusercontent.com/vllm-project/vllm-ascend/v0.23.0/examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py',
'worker-v0.23.0.py':'https://raw.githubusercontent.com/vllm-project/vllm-ascend/v0.23.0/vllm_ascend/worker/worker.py',
'runner-v0.23.0.py':'https://raw.githubusercontent.com/vllm-project/vllm-ascend/v0.23.0/vllm_ascend/worker/model_runner_v1.py',
'hybrid-v0.23.0.py':'https://raw.githubusercontent.com/vllm-project/vllm-ascend/v0.23.0/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_hybrid_connector.py',
'cache-v0.23.0.py':'https://raw.githubusercontent.com/vllm-project/vllm/v0.23.0/vllm/v1/kv_cache_interface.py',
'profiling-v0.23.0.md':'https://raw.githubusercontent.com/vllm-project/vllm-ascend/v0.23.0/docs/source/developer_guide/performance_and_debug/service_profiling_guide.md'}
evidence=[dict(url=url,snapshot_sha256=hashlib.sha256((ROOT/'references'/name).read_bytes()).hexdigest(),
               fetched_date='2026-10-21',version_ref='v0.23.0') for name,url in sources.items()]
(ROOT/'source-evidence.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8',newline='\n')
counts=dict(python=sum(p.suffix=='.py' for p in scripts),bash=sum(p.suffix=='.sh' for p in scripts))
validation=ROOT/'VALIDATION.md'
v=validation.read_text(encoding='utf-8')
v=re.sub(r'配套\d+个Python脚本',f"配套{counts['python']}个Python脚本",v)
validation.write_text(v,encoding='utf-8',newline='\n')
deliver=[readme,validation,ROOT/'source-evidence.json',*scripts]
manifest=[dict(path=str(p.relative_to(ROOT)).replace('\\','/'),bytes=p.stat().st_size,
               sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in deliver]
(ROOT/'manifest.json').write_text(json.dumps(dict(counts=counts,files=manifest),indent=2),encoding='utf-8')
target=ROOT.parent/'昇腾A2_DeepSeekV4Flash_PD_HBM测试手册.md'
target.write_bytes(readme.read_bytes())
archive=ROOT.parent/'ascend_pd_benchmark_2026-10-21.zip'
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
    for path in [*deliver,ROOT/'manifest.json']:
        z.write(path,arcname=str(path.relative_to(ROOT)))
print(json.dumps(dict(markdown=str(target),markdown_bytes=target.stat().st_size,
                     archive=str(archive),archive_bytes=archive.stat().st_size,counts=counts,
                     embedded_scripts_verified=len(embedded)),ensure_ascii=False))
