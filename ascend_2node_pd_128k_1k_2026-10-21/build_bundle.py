from pathlib import Path
import ast,hashlib,json,re,sys,zipfile
root=Path(r'D:\work\ascend_2node_pd_128k_1k_2026-10-21')
body=(root/'README-body.md').read_text(encoding='utf-8')
# Static syntax inspection only; no benchmark, model loading or test suite runs here.
python_files=list((root/'scripts').glob('*.py'))
for p in python_files:ast.parse(p.read_text(encoding='utf-8'),filename=p.name)
sys.path.insert(0,r'D:\work\ascend_pd_benchmark_2026-10-21\.validation_deps')
import tree_sitter,tree_sitter_bash
parser=tree_sitter.Parser(tree_sitter.Language(tree_sitter_bash.language()))
bash_files=[root/'env.example.sh',*sorted((root/'scripts').glob('*.sh'))]
errors=[]
for p in bash_files:
    if parser.parse(p.read_bytes()).root_node.has_error:errors.append(str(p))
if errors:raise RuntimeError('Bash syntax tree errors: '+str(errors))
print('Static syntax inspection:',len(python_files),'Python,',len(bash_files),'Bash')
# Use relative source link in the archive, while embedding a complete standalone copy below.
appendix=r'''
## 附录：脚本和提取方法

ZIP 中的文件可直接使用。单独 Markdown 也包含全部运行文件，方便只传一个附件。

把本 Markdown 保存为 `manual.md` 后，在 A2 的一个新目录运行以下提取脚本。它只提取有标记的文件，不执行其中命令，也不覆盖已存在的文件。

```bash
python3 - <<'EXTRACT'
from pathlib import Path
import re
src=Path('manual.md').read_text(encoding='utf-8-sig')
root=Path('.').resolve()
pattern=r'<!-- BEGIN_FILE: ([^\n]+) -->\n```[^\n]*\n(.*?)\n```\n<!-- END_FILE -->'
items=re.findall(pattern,src,re.S)
if not items:raise SystemExit('No embedded files found')
for name,content in items:
    path=(root/name).resolve()
    if not path.is_relative_to(root):raise SystemExit('Invalid embedded path')
    if path.exists():raise SystemExit(f'Already exists: {path}; extract into an empty directory')
for name,content in items:
    path=root/name;path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(content+'\n',encoding='utf-8')
print('Extracted',len(items),'files')
EXTRACT
```

### 本地审阅范围

- 已完成 Python AST 与 Bash 语法树静态检查，逐步探针锚点与固定版本 runner 源码对照。
- 核对了固定版本的内部/外部 request ID 行为，离线统计使用外部 ID。
- 本地是 Windows 工作空间，没有 A2、CANN 或模型权重；未声称完成镜像拉取、加载、跨机 KV 正确性或性能验收。
- 提供的阈值（5 秒/100 步/三次、3% 探针开销、10% 缩放偏差等）均为工程规则，可在实验前与导师约定后冻结。

'''
paths=[root/'env.example.sh',*sorted((root/'scripts').glob('*')),root/'upstream/load_balance_proxy_server_example.py']
for p in paths:
    if not p.is_file():continue
    name=p.relative_to(root).as_posix();lang='python' if p.suffix=='.py' else 'bash'
    appendix+=f'\n### `{name}`\n\n<!-- BEGIN_FILE: {name} -->\n```{lang}\n'+p.read_text(encoding='utf-8').rstrip()+'\n```\n<!-- END_FILE -->\n'
manual=body.replace('<!-- SCRIPT_APPENDIX -->',appendix)
(root/'README.md').write_text(manual,encoding='utf-8',newline='\n')
(root/'STATIC_REVIEW.md').write_text(f'''# 静态审阅记录\n\n日期：2026-10-21。\n\nPython 文件 {len(python_files)} 个：AST 可解析。Bash 文件 {len(bash_files)} 个：语法树可解析。固定 v0.23.0 runner 中的 step 插入锚点数量为 1。\n\n本记录不代表 Ascend 实机测试通过。没有运行 NPU benchmark，没有创建性能数值，没有运行实现测试套件。\n''',encoding='utf-8')
(root/'THIRD_PARTY_NOTICES.md').write_text('''# Third-party notice\n\n`upstream/load_balance_proxy_server_example.py` is from vllm-project/vllm-ascend, tag v0.23.0. The original Apache-2.0 SPDX header is preserved. Source: https://github.com/vllm-project/vllm-ascend/blob/v0.23.0/examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py . License: https://www.apache.org/licenses/LICENSE-2.0 .\n\nOriginal source is provided for reproducibility. Optional instrumentation is generated as a separate file at runtime.\n''',encoding='utf-8')
manifest={}
for p in paths:
    manifest[p.relative_to(root).as_posix()]=dict(bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest())
(root/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
name='两台昇腾A2_128K输入1K输出_PD测试步骤.md'
(Path(r'D:\work')/name).write_text(manual,encoding='utf-8',newline='\n')
archive=Path(r'D:\work\ascend_2node_pd_128k_1k_2026-10-21.zip')
include=[root/'README.md',root/'PD-start-commands.md',root/'manifest.json',root/'STATIC_REVIEW.md',root/'THIRD_PARTY_NOTICES.md',*paths]
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
    for p in include:z.write(p,p.relative_to(root).as_posix())
print('Markdown:',name,'bytes=',len(manual.encode()))
print('ZIP:',archive,'bytes=',archive.stat().st_size)
print('Runtime files:',len(paths))
