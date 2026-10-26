#!/usr/bin/env python3
"""AISBench + vLLM-Ascend HTTP profiler. Single file, stdlib, Linux Python >=3.9.
python3 oneclick_aisbench_profile.py --init profile.json
python3 oneclick_aisbench_profile.py --config profile.json --check
python3 oneclick_aisbench_profile.py --config profile.json --execute
"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
import uuid


def utc():
    return datetime.now(timezone.utc).isoformat()

def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def template():
    def target(port):
        return {
            'base_url': 'http://127.0.0.1:' + str(port),
            'metrics_url': 'http://127.0.0.1:' + str(port) + '/metrics',
            'profile_dir': '/data/pd_profile/torch/' + ('P' if port == 8100 else 'D'),
            'active_metric': 'vllm:num_requests_running',
            'capture_delay_s': 0 if port == 8100 else 2,
            'capture_seconds': 5,
            'control_timeout_s': 300,
            'check_openapi': True,
        }
    return {
        'tools_dir': '/EDIT/aisbench_auto_tools_prefix',
        'dataset_pattern': '/EDIT/your_full_C{C}_N{N}.jsonl',
        'out_root': './runs/oneclick_http',
        'concurrencies': [1, 2, 4, 8, 16],
        'input_len': 131072, 'output_len': 1024,
        'dataset_type': 'normal', 'request_rate': '0', 'test_type': 'stream',
        'python': 'python3', 'summarizer': 'default_perf',
        'wrapper_extra_args': [], 'prefix_dataset': '',
        'prefix_warmup_concurrency': 1, 'before_case_argv': [],
        'cache_note': 'EDIT: preserve original prefix/cache preparation; replay may heat the suffix',
        'benchmark_timeout_s': 7200, 'trigger_timeout_s': 1800,
        'success_regex': r'Success Requests\s*[│|]\s*(?:total\s*[│|]\s*)?(\d+)',
        'failed_regex': r'Failed Requests\s*[│|]\s*(?:total\s*[│|]\s*)?(\d+)',
        'targets': {'P': target(8100), 'D': target(8200)},
    }



class Agent:
    """Direct P/D API control; never attaches a PID or invokes msprof."""
    def __init__(self, target, local_dir):
        self.target = dict(target)
        self.base = target['base_url'].rstrip('/')
        self.local_dir = local_dir
        self.possibly_active = False
        self.stop_confirmed = False
        self.events = []

    def request(self, path, method='GET', timeout=None):
        url = self.base + path
        event = {'url': url, 'method': method, 'sent_utc': utc()}
        headers = {}
        token = os.environ.get('PROFILE_API_KEY')
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if method == 'POST':
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(url, headers=headers,
                                     data=b'{}' if method == 'POST' else None,
                                     method=method)
        begin = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout or
                    float(self.target.get('control_timeout_s', 300))) as response:
                raw = response.read(8*1024*1024 + 1)
                if len(raw) > 8*1024*1024:
                    raise RuntimeError('Profiler control response exceeds size limit')
                text = raw.decode('utf-8', errors='replace')
                event.update(http_status=response.status, ok=200 <= response.status < 300)
                if not event['ok']:
                    raise RuntimeError('HTTP status ' + str(response.status))
                event['body_prefix'] = text[:4096]
                return text
        except urllib.error.HTTPError as exc:
            event.update(ok=False, http_status=exc.code)
            raise RuntimeError(f'{method} {url}: HTTP {exc.code}. '
                               'For 404 check --profiler-config and the direct instance API port.') from exc
        except Exception as exc:
            event.update(ok=False, error=str(exc))
            raise
        finally:
            event.update(returned_utc=utc(), elapsed_s=time.monotonic()-begin)
            self.events.append(event)
            dump(self.local_dir / 'http_controls.json', self.events)

    def inspect(self):
        self.request('/health', timeout=10)
        verified = False
        if self.target.get('check_openapi', True):
            data = json.loads(self.request('/openapi.json', timeout=15))
            paths = data.get('paths', {})
            for path in ['/start_profile', '/stop_profile']:
                if 'post' not in paths.get(path, {}):
                    raise RuntimeError(f'{path} POST is missing from OpenAPI. '
                                       'Launch this P/D instance with --profiler-config.')
            verified = True
        return {
            'base_url': self.base,
            'profiler_routes_verified': verified,
            'configured_profile_dir_record': self.target['profile_dir'],
            'profile_dir_verified_from_server': False,
            'note': 'profile_dir only records the startup path; HTTP does not change it. '
                    'No profiling start/stop or inference request was sent during inspection.',
        }

    def call(self, action, config=None, timeout=None):
        if action == 'inspect':
            return self.inspect()
        if action == 'open':
            result = self.inspect()
            result['status'] = 'http_control_ready'
            return result
        if action == 'start':
            # A timed-out start may still have succeeded on the server.
            self.possibly_active = True
            self.stop_confirmed = False
            body = self.request('/start_profile', 'POST', timeout)
            return {'command': 'POST /start_profile', 'http_ok': True, 'body': body[:4096]}
        if action == 'stop':
            body = self.request('/stop_profile', 'POST', timeout)
            self.possibly_active = False
            self.stop_confirmed = True
            return {'command': 'POST /stop_profile', 'http_ok': True, 'body': body[:4096]}
        if action == 'finish':
            if not self.stop_confirmed:
                raise RuntimeError('Cannot finish before a confirmed successful stop')
            return {
                'status': 'HTTP_STOP_OK_VERIFY_SERVER_TRACE',
                'output': self.target['profile_dir'],
                'raw_bytes': None,
                'raw_files_verified': False,
                'kernel_phase_verified': False,
                'note': 'Inspect new ascend_pt data and timestamps in the serving container; '
                        'HTTP success alone does not prove valid NPU kernel data.',
            }
        raise ValueError('Unknown HTTP controller action: ' + action)

    def close(self):
        if self.possibly_active:
            try:
                self.call('stop')
            except Exception as exc:
                return {'status': 'UNCERTAIN_STOP_CHECK_SERVER', 'error': str(exc),
                        'manual_stop_url': self.base + '/stop_profile'}
        return {'status': 'collector_closed'}


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
    token = os.environ.get('PROFILE_METRICS_API_KEY') or os.environ.get('PROFILE_API_KEY')
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
            target['output'] = target['profile_dir']
            report['target'] = {k: target.get(k) for k in ['base_url', 'profile_dir', 'output']}
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
            report['capture'] = agent.call('finish', timeout=float(target.get('control_timeout_s', 300)) +
                                           float(target.get('control_timeout_s', 120)) + 30)
        client.wait(float(cfg['benchmark_timeout_s']))
        client.log.close()
        report['native_counts'] = native_counts((case / 'console.log').read_text(errors='replace'), cfg, 4*c)
        preserve_logs(work, case / 'aisbench')
        report['status'] = 'HTTP_CAPTURE_STOPPED_VERIFY_SERVER_TRACE' if role else 'COUNTS_CHECKED_REVIEW_TOKEN_LENGTHS'
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
        raise ValueError('Keep the confirmed ordinary mode: summarizer must be default_perf')
    if cfg.get('input_len') != 131072 or cfg.get('output_len') != 1024:
        raise ValueError('This script preserves the confirmed 128K/1K workload')
    if not Path(cfg['tools_dir']).expanduser().is_dir():
        raise ValueError('Set tools_dir to the existing working wrapper directory')
    for c in concurrencies:
        if c not in [1, 2, 4, 8, 16]:
            raise ValueError('Use already-tested concurrency values 1,2,4,8,16')
        inspect_jsonl(dataset_path(cfg, c), 4*c)
    wrapper_command(cfg, concurrencies[0], dataset_path(cfg, concurrencies[0]))
    seen = set()
    for role in roles:
        target = cfg['targets'][role]
        base = target.get('base_url', '').rstrip('/')
        u = urlparse(base)
        if (u.scheme not in ('http', 'https') or not u.netloc or u.path or
                u.username or u.password or u.query or u.fragment):
            raise ValueError('base_url must be a direct P/D root API URL without /v1 or credentials')
        if base in seen:
            raise ValueError('P and D must use different instance API URLs')
        seen.add(base)
        if not (0 < float(target.get('capture_seconds', 0)) <= 60):
            raise ValueError('capture_seconds must be in (0,60]')
        if float(target.get('capture_delay_s', 0)) < 0:
            raise ValueError('capture_delay_s must be >=0')
        if float(target.get('control_timeout_s', 300)) <= 0:
            raise ValueError('control_timeout_s must be positive')
        if not target.get('profile_dir', '').startswith('/'):
            raise ValueError('profile_dir must record the absolute path used in server --profiler-config')
        if not target.get('metrics_url', '').startswith(('http://', 'https://')):
            raise ValueError('metrics_url must be reachable from the AISBench client')
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
        print('Created', a.init, '- edit existing paths, dataset_pattern, direct P/D URLs and their startup profile directories.')
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
            print(role, 'HTTP preflight passed (server trace path still requires verification)', flush=True)
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
        writer.writerow(['case', 'C', 'N', 'role', 'status', 'server_profile_dir', 'raw_bytes',
                         'actual_token_lengths_verified', 'kernel_phase_verified'])
        for record in records:
            cap = record.get('capture', {})
            writer.writerow([record['case'], record['C'], record['N'], record['role'], record['status'],
                             cap.get('output', ''), cap.get('raw_bytes', ''), False, False])
    print('DONE:', root / 'index.csv', '\nNative counts/HTTP controls checked; verify server trace files. Review token lengths and '
          'actual kernel/phase in AISBench + Insight; no automatic pure-D or cache-equivalence claim.')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Interrupted. Inspect collector cleanup in case.json.', file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print('STOPPED:', str(exc), file=sys.stderr)
        raise SystemExit(1)
