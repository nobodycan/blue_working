"""Launch eight external DP ranks on one A2 host, using the documented DP8/DP32 layout."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument('role', choices=['prefill', 'decode'])
    p.add_argument('--master', required=True)
    p.add_argument('--node-id', type=int, required=True)
    p.add_argument('--rank-start', type=int, default=0)
    a = p.parse_args()
    assert 0 <= a.node_id < 4
    assert a.rank_start == (0 if a.role == 'prefill' else 8 * a.node_id)
    root = Path('/bench/logs'); root.mkdir(exist_ok=True, parents=True)
    processes, files = [], []
    try:
        for local in range(8):
            rank = a.rank_start + local
            env = dict(os.environ, NODE_ID=str(a.node_id), DP_RANK=str(rank),
                       DP_MASTER=a.master, API_PORT=str(7100 + local),
                       ASCEND_RT_VISIBLE_DEVICES=str(local))
            path = root / f'{a.role}-{a.node_id}-rank{rank}.log'
            f = path.open('a'); files.append(f)
            process = subprocess.Popen(['bash', '/bench/scripts/serve.sh', a.role],
                env=env, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
            processes.append(process)
            print(f'local={local} rank={rank} pid={process.pid} log={path}', flush=True)
        while True:
            for process in processes:
                if process.poll() is not None:
                    raise RuntimeError(f'Rank process {process.pid} exited with {process.returncode}')
            time.sleep(1)
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        for f in files:
            f.close()


if __name__ == '__main__':
    main()
