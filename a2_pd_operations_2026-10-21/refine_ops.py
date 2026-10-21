from pathlib import Path
root=Path(r'D:\work\a2_pd_operations_2026-10-21')
(root/'scripts/check_runs.py').write_text(r'''"""Check a completed run set before summarizing; no hardware calls."""
import argparse,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True)
p.add_argument('--runs',type=int,required=True);p.add_argument('--concurrency',type=int,required=True)
p.add_argument('--mode',choices=['wave','steady'],default='wave');a=p.parse_args()
for i in range(1,a.runs+1):
    path=a.directory/f'r{i}.json'
    s=json.loads(path.read_text())
    assert s['mode']==a.mode and s['client_concurrency']==a.concurrency, f'Config mismatch: {path}'
    assert s['input_tokens']==131072 and s['output_tokens']==1024
    assert s['all_phase_failures']==0 and s['sufficient_samples'], f'Failed samples: {path}'
    rows=[json.loads(x) for x in path.with_suffix('.requests.jsonl').read_text().splitlines()]
    assert rows and all(not r['error'] for r in rows), f'Request failure: {path}'
    assert all(r['usage']['prompt_tokens']==131072 and r['usage']['completion_tokens']==1024 for r in rows)
    if a.mode=='wave':assert len(rows)==a.concurrency, f'Wave size mismatch: {path}'
    else:assert s['end_censored_fraction'] is not None and s['end_censored_fraction']<=.10, f'Extend duration: {path}'
print(f'PASS: {a.runs} {a.mode} runs, concurrency={a.concurrency}, all length/failure checks passed')
''',encoding='utf-8',newline='\n')
p=root/'env.example.sh';s=p.read_text(encoding='utf-8').replace('/data/v4flash-2node','/data/v4flash-ops').replace('CONTAINER=v4flash-2node','CONTAINER=v4flash-ops');p.write_text(s,encoding='utf-8',newline='\n')
p=root/'README-body.md';s=p.read_text(encoding='utf-8-sig').replace('/data/v4flash-2node','/data/v4flash-ops').replace('`v4flash-2node`','`v4flash-ops`')
s=s.replace('确认有 30 个 summary，每个 all_phase_failures=0。保留对应 requests.jsonl。','''执行检查，必须显示 PASS：

```bash
docker exec "$CONTAINER" python /bench/scripts/check_runs.py \\
  --directory "/bench/results/$RUN_ID/single" --runs 30 --concurrency 1
```
''')
s=s.replace('确认有 10 个 summary，每轮 valid_requests=实测BS、all_phase_failures=0。','''执行检查，必须显示 PASS：

```bash
docker exec "$CONTAINER" python /bench/scripts/check_runs.py \\
  --directory "/bench/results/$RUN_ID/batch" --runs 10 --concurrency "$BS"
```
''')
s=s.replace('确认三轮 all_phase_failures=0、sufficient_samples=true。样本不足或 end_censored_fraction>0.10 时，使用新输出目录，将 duration 增至 3600 或更长后重测。','''执行检查，必须显示 PASS：

```bash
docker exec "$CONTAINER" python /bench/scripts/check_runs.py \\
  --directory "/bench/results/$RUN_ID/steady" --runs 3 --concurrency "$BS" --mode steady
```

样本不足或 end_censored_fraction>0.10 时，使用新输出目录，将 duration 增至 3600 或更长后重测。''')
s=s.replace('确认 30/10 次全部成功后再汇总。将正式与诊断结果分开保存，记录两组差异。','''再执行两项检查，均须显示 PASS 后才使用正式对比结果：

```bash
docker exec "$CONTAINER" python /bench/scripts/check_runs.py \\
  --directory "/bench/results/$RUN_ID/formal-single" --runs 30 --concurrency 1
docker exec "$CONTAINER" python /bench/scripts/check_runs.py \\
  --directory "/bench/results/$RUN_ID/formal-batch" --runs 10 --concurrency "$BS"
```

将正式与诊断结果分开保存，记录两组差异。''')
s=s.replace('首次 SSH 按集群要求核实主机指纹。保存管理员的专网/RoCE 验收记录到 bootstrap 目录。','首次 SSH 按集群要求核实主机指纹。保存管理员的专网/RoCE 验收记录到 bootstrap 目录。')
s=s.replace('## 1. 两台机器：上传并解压操作包','''## 1. 两台机器：上传并解压操作包''')
s=s.replace('检查四个文件均存在。已有旧 `env.sh` 时先归档旧文件；不要覆盖旧结果。','检查四个文件均存在。本清单使用新的 /data/v4flash-ops 目录和 v4flash-ops 容器名。已有同名目录/配置时先归档，或统一换成未使用的目录；不要将新脚本包与旧版文件混装。')
s=s.replace('重启本测试容器。','重启本测试容器。',1)
p.write_text(s,encoding='utf-8',newline='\n')
print('Updated fresh workspace/container names and result-set checks.')
