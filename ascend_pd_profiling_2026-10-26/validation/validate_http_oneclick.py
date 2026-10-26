#!/usr/bin/env python3
"""Offline HTTP-controller checks; no actual P/D service or NPU is contacted."""
from contextlib import contextmanager
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('http_controller', ROOT / 'oneclick_aisbench_profile.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


@contextmanager
def workspace():
    base = ROOT / 'validation'
    with tempfile.TemporaryDirectory(prefix='tmp_http_', dir=base) as raw:
        p = Path(raw).resolve()
        if not p.is_relative_to(base.resolve()) or p == base.resolve():
            raise RuntimeError('Unsafe cleanup target')
        yield p


class Reply:
    status = 200

    def __init__(self, text=''):
        self.raw = text.encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, size=-1):
        return self.raw[:size] if size >= 0 else self.raw


def transport(events, fail_start=False, fail_stop=False, routes=True, timeout_start=False):
    def urlopen(req, timeout=None):
        events.append((req.get_method(), req.full_url))
        path = '/' + req.full_url.rsplit('/', 1)[-1]
        if path == '/openapi.json':
            return Reply(json.dumps({'paths': {
                '/start_profile': {'post': {}}, '/stop_profile': {'post': {}}
            } if routes else {}}))
        if timeout_start and path == '/start_profile':
            raise TimeoutError('mock start response timeout')
        if (fail_start and path == '/start_profile') or (fail_stop and path == '/stop_profile'):
            raise urllib.error.HTTPError(req.full_url, 500, 'mock', {}, io.BytesIO(b'mock'))
        return Reply('')
    return urlopen


class Checks(unittest.TestCase):
    def test_01_new_default_needs_no_pid_or_msprof(self):
        cfg = app.template()
        for target in cfg['targets'].values():
            self.assertIn('base_url', target)
            for key in ['pid', 'ssh_host', 'container', 'msprof']:
                self.assertNotIn(key, target)
        text = (ROOT / 'oneclick_aisbench_profile.py').read_text(encoding='utf-8')
        self.assertNotIn('--dynamic', text)
        self.assertNotIn('PROFILING_MODE', text)

    def test_02_inspect_only_gets_health_and_schema(self):
        with workspace() as temp:
            events = []
            agent = app.Agent(app.template()['targets']['P'], temp)
            with patch.object(app.urllib.request, 'urlopen', transport(events)):
                self.assertTrue(agent.call('inspect')['profiler_routes_verified'])
                self.assertEqual(agent.close()['status'], 'collector_closed')
            self.assertTrue(all(method == 'GET' for method, url in events))

    def test_03_exact_post_endpoints_and_stop(self):
        with workspace() as temp:
            events = []
            agent = app.Agent(app.template()['targets']['P'], temp)
            with patch.object(app.urllib.request, 'urlopen', transport(events)):
                agent.call('open')
                agent.call('start')
                agent.call('stop')
                result = agent.call('finish')
                agent.close()
            self.assertEqual([url.rsplit('/', 1)[-1] for method, url in events if method == 'POST'],
                             ['start_profile', 'stop_profile'])
            self.assertFalse(result['raw_files_verified'])
            self.assertFalse(result['kernel_phase_verified'])

    def test_04_no_routes_fail_before_start(self):
        with workspace() as temp:
            events = []
            agent = app.Agent(app.template()['targets']['D'], temp)
            with patch.object(app.urllib.request, 'urlopen', transport(events, routes=False)):
                with self.assertRaisesRegex(RuntimeError, 'profiler-config'):
                    agent.call('open')
            self.assertTrue(all(method == 'GET' for method, url in events))

    def test_05_start_timeout_attempts_stop(self):
        with workspace() as temp:
            events = []
            agent = app.Agent(app.template()['targets']['P'], temp)
            with patch.object(app.urllib.request, 'urlopen', transport(events, timeout_start=True)):
                with self.assertRaises(TimeoutError):
                    agent.call('start')
                self.assertEqual(agent.close()['status'], 'collector_closed')
            self.assertIn(('POST', 'http://127.0.0.1:8100/stop_profile'), events)

    def test_06_stop_failure_is_uncertain(self):
        with workspace() as temp:
            events = []
            agent = app.Agent(app.template()['targets']['D'], temp)
            with patch.object(app.urllib.request, 'urlopen', transport(events, fail_stop=True)):
                agent.call('start')
                result = agent.close()
            self.assertEqual(result['status'], 'UNCERTAIN_STOP_CHECK_SERVER')

    def test_07_finish_requires_confirmed_stop(self):
        with workspace() as temp:
            agent = app.Agent(app.template()['targets']['P'], temp)
            with self.assertRaisesRegex(RuntimeError, 'confirmed'):
                agent.call('finish')

    def test_08_proxy_url_and_same_instance_rejected(self):
        with workspace() as temp:
            cfg = app.template()
            cfg['tools_dir'] = str(temp)
            cfg['dataset_pattern'] = str(temp / 'C{C}.jsonl')
            (temp / 'C1.jsonl').write_text('{}\n'*4)
            app.validate_config(cfg, [1], ['P', 'D'])
            cfg['targets']['D']['base_url'] = cfg['targets']['P']['base_url']
            with self.assertRaisesRegex(ValueError, 'different'):
                app.validate_config(cfg, [1], ['P', 'D'])
            cfg['targets']['D']['base_url'] = 'http://127.0.0.1:8200/v1'
            with self.assertRaisesRegex(ValueError, 'root API'):
                app.validate_config(cfg, [1], ['P', 'D'])

    def test_09_profile_dir_is_only_a_record(self):
        with workspace() as temp:
            agent = app.Agent(app.template()['targets']['P'], temp)
            with patch.object(app.urllib.request, 'urlopen', transport([])):
                result = agent.call('inspect')
            self.assertFalse(result['profile_dir_verified_from_server'])
            self.assertEqual(result['configured_profile_dir_record'], '/data/pd_profile/torch/P')

    def test_10_standalone_help_and_init(self):
        with workspace() as temp:
            path = ROOT / 'oneclick_aisbench_profile.py'
            result = subprocess.run([sys.executable, '-B', str(path), '--init', str(temp / 'http.json')],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            cfg = json.loads((temp / 'http.json').read_text(encoding='utf-8'))
            self.assertNotIn('pid', cfg['targets']['P'])

    def test_11_workload_is_still_128k_1k_4c(self):
        cfg = app.template()
        for c in [1, 2, 4, 8, 16]:
            cmd = app.wrapper_command(cfg, c, '/data/existing.jsonl')
            self.assertEqual(cmd[cmd.index('--data_num')+1], str(4*c))
            self.assertEqual(cmd[cmd.index('--input_len')+1], '131072')
            self.assertEqual(cmd[cmd.index('--output_len')+1], '1024')
            self.assertNotIn('--profile', cmd)

    def test_12_full_case_uses_http_without_remote_agent(self):
        with workspace() as temp:
            tools = temp / 'tools'
            tools.mkdir()
            for name in ['config.py', 'aisbench_test.py', 'default_api.py']:
                (tools / name).write_text('# original file\n')
            (temp / 'C1.jsonl').write_text('{}\n'*4)
            cfg = app.template()
            cfg.update(tools_dir=str(tools), dataset_pattern=str(temp / 'C{C}.jsonl'))
            cfg['targets']['P'].update(capture_delay_s=0, capture_seconds=0.01)
            root = temp / 'run'
            root.mkdir()

            class Client:
                def __init__(self, argv, cwd, env, log_path):
                    self.log = log_path.open('w')
                    self.log.write('| Success Requests | total | 4 |\n| Failed Requests | total | 0 |\n')
                    self.log.flush()
                    self.proc = Mock()
                    self.proc.poll.return_value = None

                def wait(self, limit):
                    pass

                def cancel(self):
                    self.log.close()

            events = []
            with patch.object(app, 'ClientRun', Client), \
                    patch.object(app, 'read_activity', return_value=False), \
                    patch.object(app, 'await_activity'), patch.object(app, 'wait_client_alive'), \
                    patch.object(app.urllib.request, 'urlopen', transport(events)):
                result = app.do_case(cfg, 1, 'P', root)
            self.assertEqual(result['status'], 'HTTP_CAPTURE_STOPPED_VERIFY_SERVER_TRACE')
            self.assertEqual(result['target']['base_url'], 'http://127.0.0.1:8100')
            self.assertNotIn('pid', result['target'])
            self.assertFalse(result['capture']['raw_files_verified'])
            self.assertEqual([url.rsplit('/', 1)[-1] for method, url in events if method == 'POST'],
                             ['start_profile', 'stop_profile'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
