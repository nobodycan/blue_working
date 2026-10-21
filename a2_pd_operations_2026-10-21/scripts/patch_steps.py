"""Guarded v0.23.0 diagnostic patch; run AFTER audit_patch.py; restore in reverse order."""
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
    anchor="""                deferred_state_corrections_fn = self._update_states(
                    scheduler_output
                )"""
    assert src.count(anchor)==1, 'Source mismatch: stop and inspect installed runner'
    replacement=anchor+'\n                # V4_2NODE_STEP_AUDIT\n                from step_probe import snapshot\n                snapshot(self, scheduler_output)'
    modified=src.replace(anchor,replacement); ast.parse(modified)
    assert not backup.exists(), 'Existing backup: inspect first'
    backup.write_text(src,encoding='utf-8'); path.write_text(modified,encoding='utf-8')
    print(path)
if __name__=='__main__': main()
