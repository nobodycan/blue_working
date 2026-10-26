#!/usr/bin/env python3
# Legacy alternative only. Current user workflow: use oneclick_aisbench_profile.py (HTTP).
"""AISBench 128K/1K N=4C + msprof controller. Linux, Python >=3.9, stdlib only.

python3 oneclick_aisbench_msprof.py --init profile.json
python3 oneclick_aisbench_msprof.py --config profile.json --check
python3 oneclick_aisbench_msprof.py --config profile.json --execute
"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

# This small agent is sent through stdin-controlled Python over local/docker/SSH.
# msprof gets a genuine Linux PTY; the AISBench client never runs inside msprof.
AGENT_SOURCE = r'''
import base64, glob, json, os, re, select, signal, stat, subprocess, sys, time
from pathlib import Path

def emit(obj):
    print(json.dumps(obj, ensure_ascii=True), flush=True)

class Session:
    def __init__(self):
        self.proc = None
        self.master = None
        self.log = None
        self.active = False
        self.cfg = None
        self.out = None

    def inspect(self, cfg):
        self.cfg = cfg
        pid = int(cfg['pid'])
        if pid < 2:
            raise RuntimeError('Set an actual NPU worker PID, not 0/API frontend.')
        root = Path('/proc') / str(pid)
        if root.stat().st_uid != os.geteuid():
            raise RuntimeError('Run the collector as the same user as the worker.')
        env = (root / 'environ').read_bytes().split(b'\0')
        if b'PROFILING_MODE=dynamic' not in env:
            raise RuntimeError('Worker was not started with PROFILING_MODE=dynamic. '
                               'Prepare/restart the service with its original settings first.')
        help_run = subprocess.run([cfg['msprof'], '--help'], capture_output=True,
                                  text=True, errors='replace', timeout=30)
        help_text = help_run.stdout + help_run.stderr
        for flag in ['--dynamic', '--pid', '--output', '--task-time']:
            if flag not in help_text:
                raise RuntimeError('Current msprof help does not expose ' + flag)
        if cfg.get('export', True) and '--export' not in help_text:
            raise RuntimeError('Auto export requested but --export is absent in help.')
        sockets = [p for p in glob.glob(str(Path.home() / 'dynamic_profiling_socket_*'))
                   if re.search(r'(?:^|_)' + str(pid) + r'(?:_|$)', Path(p).name)]
        explicit = cfg.get('socket_path')
        if explicit:
            sockets = [explicit]
        sockets = [p for p in sockets if Path(p).exists() and stat.S_ISSOCK(Path(p).stat().st_mode)]
        if not sockets:
            raise RuntimeError('No live dynamic socket found for this PID. Check CANN/init/user, '
                               'or set socket_path to its real location. Nothing was deleted.')
        return {'pid': pid, 'uid': os.geteuid(), 'sockets': sockets,
                'msprof_help': help_text,
                'worker_comm': (root / 'comm').read_text().strip(),
                'note': 'PID/device mapping must still be verified using worker logs/npu-smi.'}

    def pump(self, timeout):
        import errno
        ready, _, _ = select.select([self.master], [], [], timeout)
        if not ready:
            return ''
        try:
            raw = os.read(self.master, 65536)
        except OSError as exc:
            if exc.errno == errno.EIO:
                return ''
            raise
        if raw and self.log:
            self.log.write(raw)
            self.log.flush()
        return raw.decode('utf-8', errors='replace')

    def prompt(self, timeout):
        deadline = time.monotonic() + timeout
        data = ''
        pattern = self.cfg.get('prompt_regex', r'(?m)(?:\(msprof\)\s*|^\s*>\s*)$')
        while time.monotonic() < deadline:
            data += self.pump(min(0.2, max(0, deadline - time.monotonic())))
            data = data[-262144:]
            if re.search(pattern, data):
                failure = self.cfg.get('failure_regex', r'(?i)\b(?:failed|failure|error|exception)\b|失败|错误')
                if re.search(failure, data):
                    raise RuntimeError('msprof reported an error; inspect msprof.log: ' + data[-3000:])
                return data[-3000:]
            if self.proc.poll() is not None:
                raise RuntimeError('msprof exited before its prompt: ' + data[-3000:])
        raise RuntimeError('Timed out waiting for msprof prompt. Inspect log and adjust prompt_regex '
                           'only to match the real CLI. Tail: ' + data[-3000:])

    def open(self, cfg):
        import pty, termios
        info = self.inspect(cfg)
        self.out = Path(cfg['output'])
        if not self.out.is_absolute():
            raise RuntimeError('Collector output must be an absolute path inside its container.')
        self.out.mkdir(parents=True, exist_ok=False)
        self.out.chmod(0o700)
        (self.out / 'msprof_help.txt').write_text(info['msprof_help'])
        self.log = (self.out / 'msprof.log').open('wb')
        self.master, slave = pty.openpty()
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        argv = [cfg['msprof'], '--dynamic=on', '--pid=' + str(cfg['pid']),
                '--output=' + str(self.out), '--task-time=on']
        try:
            self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                         start_new_session=True, close_fds=True)
        finally:
            os.close(slave)
        self.prompt(float(cfg.get('control_timeout_s', 120)))
        return {'status': 'attached_prompt_seen', 'output': str(self.out), 'pid': cfg['pid']}

    def command(self, action):
        if self.proc is None or self.proc.poll() is not None:
            raise RuntimeError('No running msprof session.')
        # Discard only already-written terminal output before sending a new command.
        while select.select([self.master], [], [], 0)[0]:
            if not self.pump(0):
                break
        if action == 'start':
            self.active = True  # State may be active even when acknowledgement times out.
        os.write(self.master, (action + '\n').encode())
        text = self.prompt(float(self.cfg.get('control_timeout_s', 120)))
        if action == 'stop':
            self.active = False
        return {'command': action, 'prompt_returned': True, 'tail': text}

    def close_cli(self):
        if self.proc is not None and self.proc.poll() is None:
            if self.active:
                self.command('stop')
            os.write(self.master, b'quit\n')
            deadline = time.monotonic() + float(self.cfg.get('control_timeout_s', 120))
            while self.proc.poll() is None and time.monotonic() < deadline:
                self.pump(0.2)
            if self.proc.poll() is None:
                raise RuntimeError('msprof quit timed out; check collector state manually.')
            if self.proc.returncode != 0:
                raise RuntimeError('msprof quit returned ' + str(self.proc.returncode))
        if self.master is not None:
            try:
                self.pump(0)
            except Exception:
                pass
            os.close(self.master)
            self.master = None
        if self.log:
            self.log.close()
            self.log = None

    def finish(self):
        self.close_cli()
        raw_dirs = [p for p in self.out.rglob('PROF_*') if p.is_dir()]
        if not raw_dirs:
            raise RuntimeError('No PROF_* output. Capture is NOT accepted as successful.')
        total = sum(p.stat().st_size for d in raw_dirs for p in d.rglob('*') if p.is_file())
        if total <= 0:
            raise RuntimeError('PROF_* directories contain no data.')
        if self.cfg.get('export', True):
            with (self.out / 'export.log').open('w') as log:
                p = subprocess.run([self.cfg['msprof'], '--export=on', '--output=' + str(self.out)],
                                   stdout=log, stderr=subprocess.STDOUT,
                                   timeout=float(self.cfg.get('export_timeout_s', 600)))
            if p.returncode:
                raise RuntimeError('msprof export failed; raw capture is retained. Read export.log.')
        exported = [str(p.relative_to(self.out)) for p in self.out.rglob('*')
                    if p.is_file() and (p.name.startswith(('op_summary', 'op_statistic', 'msprof_'))
                                        and p.suffix in ('.csv', '.json', '.db'))]
        return {'status': 'RAW_PRESENT_REVIEW_TIMELINE', 'output': str(self.out),
                'raw_dirs': [str(p) for p in raw_dirs], 'raw_bytes': total,
                'exported_files': exported[:100],
                'kernel_phase_verified': False}

    def cleanup(self):
        try:
            self.close_cli()
            return {'status': 'collector_closed'}
        except Exception as exc:
            # Only this msprof CLI may be terminated; never signal the model worker.
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
            self.active = False
            return {'status': 'UNCERTAIN_STOP_CHECK_SERVER', 'error': str(exc)}

session = Session()
try:
    for line in sys.stdin:
        request = json.loads(line)
        action = request['action']
        try:
            if action == 'inspect':
                result = session.inspect(request['config'])
            elif action == 'open':
                result = session.open(request['config'])
            elif action in ('start', 'stop'):
                result = session.command(action)
            elif action == 'finish':
                result = session.finish()
            elif action == 'close':
                result = session.cleanup()
            else:
                raise RuntimeError('Unknown agent action')
            emit({'id': request['id'], 'ok': True, 'result': result})
        except Exception as exc:
            emit({'id': request['id'], 'ok': False, 'error': str(exc)})
        if action == 'close':
            break
finally:
    session.cleanup()
'''


def utc():
    return datetime.now(timezone.utc).isoformat()


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def template():
    target = {'ssh_host': '', 'container': '', 'python': 'python3',
              'env_script': '', 'msprof': 'msprof', 'pid': 0,
              'socket_path': '', 'output_root': '/data/pd_profile_oneclick',
              'metrics_url': '', 'active_metric': 'vllm:num_requests_running',
              'capture_delay_s': 0, 'capture_seconds': 5, 'export': True}
    return {
        'tools_dir': '/EDIT/aisbench_auto_tools_prefix',
        'dataset_pattern': '/EDIT/your_full_C{C}_N{N}.jsonl',
        'out_root': './runs/oneclick',
        'concurrencies': [1, 2, 4, 8, 16],
        'input_len': 131072, 'output_len': 1024,
        'dataset_type': 'normal', 'request_rate': '0', 'test_type': 'stream',
        'python': 'python3', 'summarizer': 'default_perf',
        'wrapper_extra_args': [],
        'prefix_dataset': '', 'prefix_warmup_concurrency': 1,
        'before_case_argv': [],
        'cache_note': 'EDIT: record original P/D cache and equivalent preparation; '
                      'without a reset hook repeated full prompts may become hotter',
        'benchmark_timeout_s': 7200, 'trigger_timeout_s': 1800,
        'success_regex': r'Success Requests\s*[│|]\s*(?:total\s*[│|]\s*)?(\d+)',
        'failed_regex': r'Failed Requests\s*[│|]\s*(?:total\s*[│|]\s*)?(\d+)',
        'targets': {
            'P': dict(target, metrics_url='http://127.0.0.1:8100/metrics'),
            'D': dict(target, metrics_url='http://127.0.0.1:8200/metrics', capture_delay_s=2),
        },
    }


def filled(value, variables):
    result = str(value)
    for key, val in variables.items():
        result = result.replace('{' + key + '}', str(val))
    return result


def dataset_path(cfg, c):
    return Path(filled(cfg['dataset_pattern'], {'C': c, 'N': 4*c,
                     'ISL': cfg['input_len'], 'OSL': cfg['output_len']})).expanduser().resolve()


def inspect_jsonl(path, expected=None):
    count = 0
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for line in stream:
            digest.update(line)
            if line.strip():
                json.loads(line.decode('utf-8'))
                count += 1
    if expected is not None and count != expected:
        raise ValueError(f'{path}: actual JSONL rows={count}, expected={expected}; --data_num does not trim a file')
    if not count:
        raise ValueError(f'{path}: empty dataset')
    return {'path': str(path), 'rows': count, 'sha256': digest.hexdigest(),
            'actual_token_lengths_verified': False}


def wrapper_command(cfg, c, data, warmup=False, rows=None):
    cmd = [cfg.get('python', 'python3'), 'aisbench_test.py', '--dataset', str(data),
           '--dataset_type', 'normal' if warmup else cfg.get('dataset_type', 'normal'),
           '--input_len', str(cfg['input_len']), '--output_len', '1' if warmup else str(cfg['output_len']),
           '--data_num', str(rows if warmup else 4*c),
           '--concurrency', str(cfg.get('prefix_warmup_concurrency', 1) if warmup else c),
           '--request_rate', str(cfg.get('request_rate', '0')),
           '--test_type', cfg.get('test_type', 'stream'), '--repeat', '1']
    extra = cfg.get('wrapper_extra_args', [])
    forbidden = {'--dataset', '--dataset_type', '--data_num', '--concurrency', '--input_len',
                 '--output_len', '--repeat', '--prefix_test', '--request_rate', '--test_type'}
    if any(x.split('=', 1)[0] in forbidden for x in extra):
        raise ValueError('Do not override generated workload flags or add --prefix_test in wrapper_extra_args')
    return cmd + list(extra)


def transport_command(target):
    argv = [target.get('python', 'python3'), '-u', '-c', AGENT_SOURCE]
    container = target.get('container', '')
    if container:
        if container.startswith('-'):
            raise ValueError('Invalid container name')
        argv = ['docker', 'exec', '-i', container] + argv
    # Source CANN in the actual service container, before launching agent Python.
    env_script = target.get('env_script', '')
    if env_script:
        inner = [target.get('python', 'python3'), '-u', '-c', AGENT_SOURCE]
        shell = ['bash', '-lc', 'source ' + shlex.quote(env_script) + ' >&2 && exec ' + shlex.join(inner)]
        argv = ['docker', 'exec', '-i', container] + shell if container else shell
    host = target.get('ssh_host', '')
    if host:
        if host.startswith('-') or not re.fullmatch(r'[A-Za-z0-9_.@:\[\]-]+', host):
            raise ValueError('Invalid ssh_host; use a configured SSH host alias if needed')
        argv = ['ssh', '-T', '-o', 'BatchMode=yes', host, shlex.join(argv)]
    return argv


class Agent:
    def __init__(self, target, local_dir):
        self.target = target
        self.seq = 0
        self.messages = queue.Queue()
        self.stderr = (local_dir / 'agent_stderr.log').open('w', encoding='utf-8')
        self.proc = subprocess.Popen(transport_command(target), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self.stderr,
                                     text=True, encoding='utf-8', errors='replace', bufsize=1)

        def read():
            for line in self.proc.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    self.messages.put({'ok': False, 'error': 'Unexpected agent stdout: ' + line[:1000]})
            self.messages.put({'ok': False, 'error': 'Agent connection closed; inspect agent_stderr.log'})
        self.thread = threading.Thread(target=read, daemon=True)
        self.thread.start()

    def call(self, action, config=None, timeout=180):
        self.seq += 1
        payload = {'id': self.seq, 'action': action}
        if config is not None:
            payload['config'] = config
        self.proc.stdin.write(json.dumps(payload) + '\n')
        self.proc.stdin.flush()
        try:
            reply = self.messages.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError(f'{action}: agent timeout; remote collector state must be checked')
        if reply.get('id') != self.seq or not reply.get('ok'):
            raise RuntimeError(reply.get('error', 'Agent protocol mismatch; check collector state'))
        return reply['result']

    def close(self):
        result = {'status': 'connection_already_closed_check_collector'}
        try:
            if self.proc.poll() is None:
                result = self.call('close', timeout=float(self.target.get('control_timeout_s', 120)) + 30)
                self.proc.stdin.close()
                self.proc.wait(timeout=10)
        except Exception as exc:
            result = {'status': 'UNCERTAIN_STOP_CHECK_SERVER', 'error': str(exc)}
            # Close transport stdin so the agent finally block can try cleanup.
            try:
                self.proc.stdin.close()
            except Exception:
                pass
        finally:
            if self.proc.poll() is not None:
                self.thread.join(timeout=2)
                if not self.proc.stdin.closed:
                    self.proc.stdin.close()
                if not self.thread.is_alive():
                    self.proc.stdout.close()
            self.stderr.close()
        return result


class ClientRun:
    def __init__(self, argv, cwd, env, log_path):
        self.started = time.monotonic()
        self.log_path = log_path
        self.log = log_path.open('w', encoding='utf-8')
        self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=self.log,
                                     stderr=subprocess.STDOUT, start_new_session=True)

    def wait(self, limit):
        remaining = max(0.1, limit - (time.monotonic() - self.started))
        try:
            rc = self.proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            self.cancel()
            raise RuntimeError('AISBench wrapper timed out; its own process group was stopped')
        finally:
            self.log.flush()
        if rc:
            raise RuntimeError(f'AISBench wrapper returned {rc}; inspect {self.log_path}')

    def cancel(self):
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.proc.wait(timeout=10)
        self.log.close()


def native_counts(text, cfg, expected):
    ok = re.findall(cfg['success_regex'], text)
    bad = re.findall(cfg['failed_regex'], text)
    if len(ok) != 1 or len(bad) != 1:
        raise ValueError('Cannot unambiguously verify native AISBench Success/Failed Requests. '
                         'Inspect the report and adapt success_regex/failed_regex to its actual format.')
    completed, failed = int(ok[0]), int(bad[0])
    if completed != expected or failed != 0:
        raise ValueError(f'Native AISBench reports success={completed}, failed={failed}, expected={expected}')
    return {'success': completed, 'failed': failed, 'source': 'native_console_table'}


def metric_active(text, name):
    found = False
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        m = re.match(r'^([^\s{]+)(?:\{.*\})?\s+([^\s]+)', line)
        if m and m.group(1) == name:
            found = True
            value = float(m.group(2))
            if not math.isfinite(value) or value < 0:
                raise ValueError('Running-request gauge must be finite and nonnegative')
            if value > 0:
                return True
    if not found:
        raise ValueError(f'Metric {name!r} is absent; set active_metric to the actual running-request gauge')
    return False


def read_activity(target):
    req = urllib.request.Request(target['metrics_url'])
    token = os.environ.get('PROFILE_METRICS_API_KEY')
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    with urllib.request.urlopen(req, timeout=5) as response:
        text = response.read(8*1024*1024).decode(errors='replace')
    return metric_active(text, target.get('active_metric', 'vllm:num_requests_running'))


def await_activity(target, client, timeout, events):
    deadline = time.monotonic() + timeout
    url = target['metrics_url']
    last_error = None
    while time.monotonic() < deadline:
        if client.proc.poll() is not None:
            raise RuntimeError('AISBench ended before capture trigger; no useful window is claimed')
        try:
            if read_activity(target):
                events.append({'event': 'running_gauge_trigger', 'utc': utc(), 'url': url,
                               'note': 'Running gauge does not prove pure P/D kernel phase.'})
                return
        except ValueError:
            raise
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError('No activity trigger before timeout. Last metrics error: ' + str(last_error))


def wait_client_alive(client, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if client.proc.poll() is not None:
            raise RuntimeError('AISBench ended before the requested capture window completed')
        time.sleep(min(0.2, max(0, deadline-time.monotonic())))


def copy_tools(cfg, case):
    source = Path(cfg['tools_dir']).expanduser().resolve()
    work = case / 'tool_work'
    work.mkdir()
    for path in source.glob('*.py'):
        shutil.copy2(path, work / path.name)
    for name in ['aisbench_test.py', 'config.py', 'default_api.py']:
        if not (work / name).is_file():
            raise ValueError('Missing wrapper file: ' + name)
    # Existing datasets are always passed explicitly; no GSM8K regeneration/picked-id mutation.
    return work


def set_output(work, path, summarizer):
    path.mkdir(parents=True, exist_ok=False)
    with (work / 'config.py').open('a', encoding='utf-8') as stream:
        stream.write('\n# oneclick: only output/statistics overrides; service/model template is preserved.\n')
        stream.write('OUTPUT_DIR = ' + repr(str(path.resolve())) + '\n')
        stream.write('DEFAULT_PERFORMANCE_TEST = ' + repr(summarizer) + '\n')


def preserve_logs(work, dest):
    for name in ['aisbench.log', 'temp_api.py']:
        path = work / name
        if path.is_file():
            shutil.copy2(path, dest / name)


def do_case(cfg, c, role, run_root):
    phase = 'ON_' + role if role else 'OFF'
    case = run_root / f'C{c}_N{4*c}_{phase}'
    case.mkdir()
    events = []
    report = {'case': case.name, 'C': c, 'N': 4*c, 'role': role or 'PD', 'phase': phase,
              'input_target': cfg['input_len'], 'output_target': cfg['output_len'],
              'summarizer': cfg['summarizer'], 'started_utc': utc(), 'status': 'RUNNING',
              'cache_note': cfg.get('cache_note', ''), 'cache_equivalence_verified': False,
              'kernel_phase_verified': False}
    agent = None
    client = None
    cleanup = None
    try:
        work = copy_tools(cfg, case)
        data = dataset_path(cfg, c)
        report['dataset'] = inspect_jsonl(data, 4*c)
        variables = {'C': c, 'N': 4*c, 'ROLE': role or 'PD', 'PHASE': phase,
                     'CASE_DIR': str(case.resolve())}
        env = dict(os.environ)
        env.update({k: str(v) for k, v in variables.items()})
        env['PYTHONUNBUFFERED'] = '1'
        hook = [filled(x, variables) for x in cfg.get('before_case_argv', [])]
        if hook:
            with (case / 'before_case.log').open('w') as log:
                subprocess.run(hook, env=env, cwd=cfg['tools_dir'], stdout=log,
                               stderr=subprocess.STDOUT, check=True,
                               timeout=float(cfg.get('benchmark_timeout_s', 7200)))
        prefix = cfg.get('prefix_dataset', '')
        if prefix:
            prefix_path = Path(filled(prefix, variables)).expanduser().resolve()
            report['prefix_dataset'] = inspect_jsonl(prefix_path)
            set_output(work, case / 'prefix_warmup', 'default_perf')
            client = ClientRun(wrapper_command(cfg, c, prefix_path, warmup=True,
                                               rows=report['prefix_dataset']['rows']),
                               work, env, case / 'prefix_console.log')
            client.wait(float(cfg['benchmark_timeout_s']))
            client.log.close()
            native_counts((case / 'prefix_console.log').read_text(errors='replace'),
                          cfg, report['prefix_dataset']['rows'])
            preserve_logs(work, case / 'prefix_warmup')
            client = None
        set_output(work, case / 'aisbench', cfg['summarizer'])
        if role:
            target = dict(cfg['targets'][role])
            if read_activity(target):
                raise RuntimeError('Target is already busy before this case. Drain other test traffic first; '
                                   'its running gauge cannot be used as this run\'s trigger.')
            target['output'] = target['output_root'].rstrip('/') + '/' + run_root.name + '/' + case.name
            report['target'] = {k: target.get(k) for k in ['ssh_host', 'container', 'pid', 'output']}
            agent = Agent(target, case)
            report['collector_open'] = agent.call('open', target)
        argv = wrapper_command(cfg, c, data)
        report['client_argv'] = argv
        dump(case / 'case.json', report)
        print(f'[{case.name}] AISBench started; log: {case / "console.log"}', flush=True)
        client = ClientRun(argv, work, env, case / 'console.log')
        events.append({'event': 'client_started', 'utc': utc()})
        if role:
            await_activity(target, client, float(cfg['trigger_timeout_s']), events)
            wait_client_alive(client, float(target.get('capture_delay_s', 0)))
            events.append({'event': 'start_sent', 'utc': utc()})
            report['collector_start'] = agent.call('start')
            events.append({'event': 'start_prompt_returned', 'utc': utc()})
            print(f'[{case.name}] CAPTURE ACTIVE ({target["capture_seconds"]} s)', flush=True)
            wait_client_alive(client, float(target['capture_seconds']))
            report['collector_stop'] = agent.call('stop')
            events.append({'event': 'stop_prompt_returned', 'utc': utc()})
            report['capture'] = agent.call('finish', timeout=float(target.get('export_timeout_s', 600)) +
                                           float(target.get('control_timeout_s', 120)) + 30)
        client.wait(float(cfg['benchmark_timeout_s']))
        client.log.close()
        report['native_counts'] = native_counts((case / 'console.log').read_text(errors='replace'), cfg, 4*c)
        preserve_logs(work, case / 'aisbench')
        report['status'] = 'CAPTURED_NEEDS_INSIGHT_REVIEW' if role else 'COUNTS_CHECKED_REVIEW_TOKEN_LENGTHS'
    except BaseException as exc:
        report['status'] = 'FAILED'
        report['error'] = str(exc) or type(exc).__name__
        raise
    finally:
        # Stop collectors first; never send signals to model PIDs.
        if agent:
            cleanup = agent.close()
            report['collector_cleanup'] = cleanup
            if cleanup.get('status') != 'collector_closed':
                report['status'] = 'FAILED_UNCERTAIN_STOP'
        if client:
            client.cancel()
        report['finished_utc'] = utc()
        dump(case / 'events.json', events)
        dump(case / 'case.json', report)
        if cleanup and cleanup.get('status') != 'collector_closed':
            raise RuntimeError('Collector stop/connection is uncertain. Inspect case.json and server before continuing.')
    return report


def validate_config(cfg, concurrencies, roles):
    if cfg.get('summarizer') != 'default_perf':
        raise ValueError('This script preserves the original ordinary mode: summarizer must be default_perf')
    if cfg.get('input_len') != 131072 or cfg.get('output_len') != 1024:
        raise ValueError('This script targets the confirmed 128K/1K workload')
    if not Path(cfg['tools_dir']).expanduser().is_dir():
        raise ValueError('Set tools_dir to the existing working wrapper directory')
    for c in concurrencies:
        if c not in [1, 2, 4, 8, 16]:
            raise ValueError('Use the already-tested concurrency values 1,2,4,8,16')
        inspect_jsonl(dataset_path(cfg, c), 4*c)
    wrapper_command(cfg, concurrencies[0], dataset_path(cfg, concurrencies[0]))
    seen_targets = set()
    for role in roles:
        target = cfg['targets'][role]
        if int(target.get('pid', 0)) < 2:
            raise ValueError(f'Set targets.{role}.pid to the verified NPU worker PID')
        identity = (target.get('ssh_host', ''), target.get('container', ''), int(target['pid']))
        if identity in seen_targets:
            raise ValueError('P and D point to the same PID in the same environment; verify role mapping')
        seen_targets.add(identity)
        if not (0 < float(target.get('capture_seconds', 0)) <= 60):
            raise ValueError('capture_seconds must be in (0,60]')
        if float(target.get('capture_delay_s', 0)) < 0:
            raise ValueError('capture_delay_s must be >= 0')
        if not target.get('metrics_url', '').startswith(('http://', 'https://')):
            raise ValueError(f'Set targets.{role}.metrics_url reachable from the AISBench client')
        transport_command(target)
    for pattern in [cfg['success_regex'], cfg['failed_regex']]:
        if re.compile(pattern).groups != 1:
            raise ValueError('Each native counts regex must contain exactly one capture group')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--init', type=Path, help='create editable config without overwriting an existing file')
    ap.add_argument('--config', type=Path)
    ap.add_argument('--concurrencies', help='e.g. 1 or 1,2,4,8,16; defaults to config')
    ap.add_argument('--roles', default='P,D', help='P,D or P or D; separate diagnostic runs')
    ap.add_argument('--mode', choices=['diagnostic', 'baseline', 'both'], default='diagnostic')
    action = ap.add_mutually_exclusive_group()
    action.add_argument('--check', action='store_true', help='read-only collector preflight; no inference/capture')
    action.add_argument('--execute', action='store_true', help='run configured workload and capture')
    a = ap.parse_args()
    if a.init:
        with a.init.open('x', encoding='utf-8') as f:
            json.dump(template(), f, ensure_ascii=False, indent=2)
        print('Created', a.init, '- edit existing paths, dataset_pattern, target PID/transport/metrics URLs.')
        return
    if not a.config:
        ap.error('Use --init profile.json first, then --config profile.json')
    cfg = json.loads(a.config.read_text(encoding='utf-8-sig'))
    cfg['tools_dir'] = str(Path(cfg['tools_dir']).expanduser().resolve())
    cs = [int(x) for x in a.concurrencies.split(',')] if a.concurrencies else cfg['concurrencies']
    roles = list(dict.fromkeys(a.roles.split(',')))
    if not cs or any(r not in ['P', 'D'] for r in roles) or len(cs) != len(set(cs)):
        ap.error('Use unique concurrencies and roles P,D / P / D')
    selected_roles = roles if a.mode != 'baseline' else []
    validate_config(cfg, cs, selected_roles)
    plan = [{'C': c, 'N': 4*c, 'role': role or 'PD', 'capture': bool(role),
             'dataset': str(dataset_path(cfg, c))}
            for c in cs for role in (([None] if a.mode in ['baseline', 'both'] else []) +
                                    (roles if a.mode in ['diagnostic', 'both'] else []))]
    print(json.dumps({'mode': a.mode, 'plan': plan}, ensure_ascii=True, indent=2))
    if not (a.check or a.execute):
        print('DRY RUN. Add --check for read-only remote checks or --execute to run.')
        return
    if sys.platform != 'linux':
        raise SystemExit('Execute this script on the Linux AISBench client. Windows supports config/dry-run only.')
    import fcntl
    lock = (Path(cfg['tools_dir']) / '.oneclick_msprof.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Another oneclick run is using this wrapper. Do not run multiple wrappers concurrently.')
    run_id = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
    root = Path(cfg['out_root']).expanduser().resolve() / run_id
    root.mkdir(parents=True, exist_ok=False)
    # Config may contain internal paths/commands; keep output private to the user.
    os.chmod(root, 0o700)
    dump(root / 'config_snapshot.json', cfg)
    dump(root / 'plan.json', plan)
    print('Run directory:', root, flush=True)
    def interrupted(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    for role in selected_roles:
        check_dir = root / ('preflight_' + role)
        check_dir.mkdir()
        agent = Agent(cfg['targets'][role], check_dir)
        try:
            result = agent.call('inspect', cfg['targets'][role])
            dump(check_dir / 'result.json', result)
            print(role, 'preflight passed (PID/device mapping remains your recorded input)', flush=True)
        finally:
            close = agent.close()
            if close.get('status') != 'collector_closed':
                raise RuntimeError('Preflight agent did not close cleanly: ' + str(close))
    if a.check:
        print('CHECK ONLY complete. No model request, profiler start, or service restart was issued.')
        return
    records = []
    for item in plan:
        role = item['role'] if item['capture'] else None
        records.append(do_case(cfg, item['C'], role, root))
        dump(root / 'completed_cases.json', records)
    with (root / 'index.csv').open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['case', 'C', 'N', 'role', 'status', 'remote_capture_path', 'raw_bytes',
                         'actual_token_lengths_verified', 'kernel_phase_verified'])
        for record in records:
            cap = record.get('capture', {})
            writer.writerow([record['case'], record['C'], record['N'], record['role'], record['status'],
                             cap.get('output', ''), cap.get('raw_bytes', ''), False, False])
    print('DONE:', root / 'index.csv', '\nNative counts/raw presence checked. Review token lengths and '
          'actual kernel/phase in AISBench + Insight; no automatic pure-D or cache-equivalence claim.')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Interrupted. Completed files retained; inspect collector cleanup in case.json.', file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print('STOPPED:', str(exc), file=sys.stderr)
        raise SystemExit(1)
