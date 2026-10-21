from pathlib import Path
import ast,hashlib,json,re,sys,zipfile
root=Path(r'D:\work\a2_pd_operations_2026-10-21')
p=root/'README-body.md';body=p.read_text(encoding='utf-8-sig')
body=body.replace('| NPU | HBM 总量 | 该 BS 下最高占用 | 最小余量 |','| NPU | HBM 总量 | 该 BS 下 2 秒采样最高占用 | 采样最小余量 |')
body=body.replace('- HBM 峰值从运行日志填写，不用 startup CSV 代填。','- HBM 数值注明“2 秒采样最高占用”，不用 startup CSV 代填；接近边界的点补充 profiler 内存记录。')
p.write_text(body,encoding='utf-8',newline='\n')
appendix='''## 附录 A. P 机器完整启动脚本

以下文件已包含在操作包中。按第 17 步调用，不需要再次手工创建。

```bash
'''+(root/'scripts/launch_prefill.sh').read_text().rstrip()+'''
```

## 附录 B. D 机器完整启动脚本

以下文件已包含在操作包中。按第 18 步调用。

```bash
'''+(root/'scripts/launch_decode.sh').read_text().rstrip()+'''
```

## 附录 C. 代理完整启动命令

在 P 容器中单独前台排查时使用。执行前先停止已运行的同端口代理；不要同时执行两份。

```bash
source /bench/scripts/common.sh
python /bench/upstream/load_balance_proxy_server_example.py \\
  --host 0.0.0.0 --port "$PROXY_PORT" --workers 1 --max-retries 1 \\
  --prefiller-hosts "$P_IP" --prefiller-ports 8100 \\
  --decoder-hosts "$D_IP" --decoder-ports 8200
```
'''
manual=body.replace('<!-- LAUNCH_APPENDIX -->',appendix)
(root/'README.md').write_text(manual,encoding='utf-8',newline='\n')
# Static inspection only, without running model/service/benchmark code.
python_files=sorted((root/'scripts').glob('*.py'))
for p in python_files:ast.parse(p.read_text(encoding='utf-8'),filename=p.name)
sys.path.insert(0,r'D:\work\ascend_pd_benchmark_2026-10-21\.validation_deps')
import tree_sitter,tree_sitter_bash
parser=tree_sitter.Parser(tree_sitter.Language(tree_sitter_bash.language()))
bash_files=[root/'env.example.sh',*sorted((root/'scripts').glob('*.sh'))]
for p in bash_files:
    assert not parser.parse(p.read_bytes()).root_node.has_error, f'Bash syntax: {p}'
blocks=re.findall(r'```bash\n(.*?)\n```',manual,re.S)
for i,block in enumerate(blocks,1):
    assert not parser.parse(block.encode()).root_node.has_error, f'Bash block: {i}'
for name in set(re.findall(r'/bench/scripts/([A-Za-z0-9_.-]+)',manual)):
    assert (root/'scripts'/name).is_file(),f'Missing script: {name}'
headings=[int(x) for x in re.findall(r'^## (\d+)\.',manual,re.M)]
assert headings==list(range(49)),headings
assert '/data/v4flash-2node' not in manual
assert '<!-- LAUNCH_APPENDIX -->' not in manual
# Parse embedded Python snippets without executing them.
heredocs=re.findall(r"<<'PY'\n(.*?)\nPY",manual,re.S)
for src in heredocs:ast.parse(src)
notes=f'''# 交付检查\n\n日期：2026-10-21。\n\n- 操作步骤：0～48，连续无缺号。\n- Python 脚本：{len(python_files)} 个，AST 可解析。\n- Bash 文件：{len(bash_files)} 个，语法树可解析。\n- Markdown 内 Bash 命令块：{len(blocks)} 个，语法树可解析。\n- 内嵌 Python 片段：{len(heredocs)} 个，AST 可解析。\n- 文档引用的脚本文件均已附带。\n\n仅执行静态检查，没有运行 A2 服务、NPU benchmark 或实现测试套件；不代表实机验收完成。\n'''
(root/'DELIVERY_CHECK.md').write_text(notes,encoding='utf-8')
(root/'THIRD_PARTY_NOTICES.md').write_text('''# Third-party notice\n\nupstream/load_balance_proxy_server_example.py is from vllm-project/vllm-ascend v0.23.0; its Apache-2.0 SPDX header is preserved.\n\nSource: https://github.com/vllm-project/vllm-ascend/blob/v0.23.0/examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py\nLicense: https://www.apache.org/licenses/LICENSE-2.0\n''',encoding='utf-8')
files=[root/'README.md',root/'env.example.sh',root/'DELIVERY_CHECK.md',root/'THIRD_PARTY_NOTICES.md',*sorted((root/'scripts').glob('*')),*sorted((root/'upstream').glob('*'))]
files=[p for p in files if p.is_file()]
manifest={p.relative_to(root).as_posix():{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files}
(root/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
files.append(root/'manifest.json')
target=Path(r'D:\work\两台昇腾A2_PD测试_逐步操作清单.md')
target.write_text(manual,encoding='utf-8',newline='\n')
archive=Path(r'D:\work\a2_pd_operations_2026-10-21.zip')
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
    for p in files:z.write(p,p.relative_to(root).as_posix())
print(notes)
print('Markdown bytes:',target.stat().st_size,'Archive bytes:',archive.stat().st_size,'Files:',len(files))
