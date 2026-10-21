"""Create selection.env only from three successful fixed-batch PD wave audits."""
import argparse,json,re,shlex
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True)
p.add_argument('--candidate',type=int,required=True);p.add_argument('--out',type=Path,required=True)
a=p.parse_args();b=a.candidate
for repeat in (1,2,3):
    prefix=a.directory/f'C{b}-r{repeat}'
    audit=json.loads(prefix.with_suffix('.audit.json').read_text())
    window=json.loads(prefix.with_suffix('.window.json').read_text())[0]
    assert audit['valid'] is True, f'Worker audit not valid: repeat {repeat}'
    assert window['plateau_valid'] is True and window['batch_requested']==b
    assert len(audit['workers'])==8
    assert all(r['constant_target'] and r['all_decode'] for r in audit['workers'])
if a.out.exists():raise SystemExit('selection.env exists; use a fresh run directory or archive it first')
a.out.write_text(f'export BS={b}\n',encoding='utf-8')
print(f'CONFIRMED BS={b}; saved {a.out}')
