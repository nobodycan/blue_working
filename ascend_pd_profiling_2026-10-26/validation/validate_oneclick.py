#!/usr/bin/env python3
"""Offline checks for the single-file controller; no real NPU/SSH/Docker/msprof."""
import ast
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('oneclick', ROOT / 'oneclick_aisbench_msprof.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


@contextmanager
def workspace():
    base = ROOT / 'validation'
    with tempfile.TemporaryDirectory(prefix='tmp_oneclick_', dir=base) as raw:
        p = Path(raw).resolve()
        if not p.is_relative_to(base.resolve()) or p == base.resolve():
            raise RuntimeError('Unsafe cleanup target')
        yield p


def setup_case(tmp, c=1):
    tools = tmp / 'tools'
    tools.mkdir()
    (tools / 'aisbench_test.py').write_text('# fake upstream entry\n')
    (tools / 'default_api.py').write_text('# original API template\n')
    (tools / 'config.py').write_text("MODEL_NAME='unchanged'\nOUTPUT_DIR='old'\n")
    data = tmp / f'full_C{c}_N{4*c}.jsonl'
    data.write_text('\n'.join(json.dumps({'question': str(i)}) for i in range(4*c)) + '\n')
    cfg = app.template()
    cfg.update(tools_dir=str(tools), dataset_pattern=str(tmp / 'full_C{C}_N{N}.jsonl'),
               concurrencies=[c], trigger_timeout_s=1, benchmark_timeout_s=5)
    for i, target in enumerate(cfg['targets'].values()):
        target.update(pid=1234+i, capture_delay_s=0, capture_seconds=0.01)
    return cfg


class FakeAgent:
    actions = []
    start_fails = False
    uncertain = False

    def __init__(self, target, local_dir):
        self.target = target

    def call(self, action, config=None, timeout=None):
        self.actions.append(action)
        if action == 'start' and self.start_fails:
            raise RuntimeError('mock start failure')
        if action == 'finish':
            return {'output': self.target['output'], 'raw_bytes': 99, 'status': 'RAW_PRESENT_REVIEW_TIMELINE'}
        return {'status': 'mock'}

    def close(self):
        self.actions.append('close')
        return {'status': 'UNCERTAIN_STOP_CHECK_SERVER' if self.uncertain else 'collector_closed'}


class FakeClient:
    wait_fails = False
    cancelled = 0

    def __init__(self, argv, cwd, env, log_path):
        self.proc = Mock()
        self.proc.poll.return_value = None
        n = int(argv[argv.index('--data_num') + 1])
        self.log = log_path.open('w', encoding='utf-8')
        self.log.write(f'| Success Requests | total | {n} |\n| Failed Requests | total | 0 |\n')
        self.log.flush()

    def wait(self, limit):
        if self.wait_fails:
            raise RuntimeError('mock client failure')

    def cancel(self):
        type(self).cancelled += 1
        self.log.close()


class Checks(unittest.TestCase):
    def setUp(self):
        FakeAgent.actions = []
        FakeAgent.start_fails = False
        FakeAgent.uncertain = False
        FakeClient.wait_fails = False
        FakeClient.cancelled = 0

    def test_01_python_and_agent_syntax(self):
        ast.parse((ROOT / 'oneclick_aisbench_msprof.py').read_text(encoding='utf-8'))
        ast.parse(app.AGENT_SOURCE)
        p = subprocess.run([sys.executable, '-B', str(ROOT / 'oneclick_aisbench_msprof.py'), '--help'],
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_02_template_has_original_load(self):
        cfg = app.template()
        self.assertEqual(cfg['concurrencies'], [1, 2, 4, 8, 16])
        self.assertEqual((cfg['input_len'], cfg['output_len'], cfg['summarizer']),
                         (131072, 1024, 'default_perf'))

    def test_03_dataset_count_is_checked(self):
        with workspace() as temp:
            cfg = setup_case(temp)
            app.validate_config(cfg, [1], ['P', 'D'])
            with self.assertRaises(ValueError):
                app.inspect_jsonl(app.dataset_path(cfg, 1), 8)

    def test_04_no_override_or_implicit_prefix_test(self):
        cfg = app.template()
        for flag in ['--concurrency=8', '--prefix_test', '--dataset', '--repeat=2']:
            cfg['wrapper_extra_args'] = [flag]
            with self.assertRaises(ValueError):
                app.wrapper_command(cfg, 1, '/data/f.jsonl')

    def test_05_command_uses_actual_dataset_and_4C(self):
        cfg = app.template()
        cmd = app.wrapper_command(cfg, 8, '/data/full data.jsonl')
        self.assertEqual(cmd[cmd.index('--data_num') + 1], '32')
        self.assertEqual(cmd[cmd.index('--concurrency') + 1], '8')
        self.assertEqual(cmd[cmd.index('--dataset') + 1], '/data/full data.jsonl')
        self.assertNotIn('--profile', cmd)
        warm = app.wrapper_command(cfg, 8, '/data/prefix.jsonl', warmup=True, rows=2)
        self.assertEqual(warm[warm.index('--output_len') + 1], '1')
        self.assertEqual(warm[warm.index('--data_num') + 1], '2')

    def test_06_native_counts_fail_closed(self):
        cfg = app.template()
        self.assertEqual(app.native_counts('| Success Requests | total | 4 |\n| Failed Requests | total | 0 |',
                                           cfg, 4)['success'], 4)
        for text in ['', '| Success Requests | total | 4 |',
                     '| Success Requests | total | 4 |\n| Failed Requests | total | 1 |',
                     '| Success Requests | total | 2 |\n| Failed Requests | total | 0 |']:
            with self.assertRaises(ValueError):
                app.native_counts(text, cfg, 4)

    def test_07_metric_labels_exact_name_and_invalid_values(self):
        self.assertTrue(app.metric_active('vllm:num_requests_running{engine="0"} 2',
                                          'vllm:num_requests_running'))
        self.assertFalse(app.metric_active('vllm:num_requests_running 0', 'vllm:num_requests_running'))
        for text in ['other_metric 2', 'vllm:num_requests_running NaN', 'vllm:num_requests_running -1']:
            with self.assertRaises(ValueError):
                app.metric_active(text, 'vllm:num_requests_running')

    def test_08_transport_quotes_code_for_ssh_and_container(self):
        target = dict(app.template()['targets']['P'], ssh_host='user@host', container='pd-p',
                      env_script='/cann/path with space/set_env.sh')
        cmd = app.transport_command(target)
        self.assertEqual(cmd[:5], ['ssh', '-T', '-o', 'BatchMode=yes', 'user@host'])
        self.assertIn('docker exec -i pd-p', cmd[-1])
        self.assertIn('>&2', cmd[-1])
        self.assertNotIn('--version', cmd[-1])

    def test_09_source_template_is_preserved(self):
        with workspace() as temp:
            cfg = setup_case(temp)
            case = temp / 'case'
            case.mkdir()
            work = app.copy_tools(cfg, case)
            app.set_output(work, case / 'aisbench', 'default_perf')
            self.assertEqual((work / 'default_api.py').read_bytes(),
                             (Path(cfg['tools_dir']) / 'default_api.py').read_bytes())
            self.assertNotIn('oneclick', (Path(cfg['tools_dir']) / 'config.py').read_text())
            self.assertIn("MODEL_NAME='unchanged'", (work / 'config.py').read_text())

    def test_10_diagnostic_lifecycle_and_summary(self):
        with workspace() as temp:
            cfg = setup_case(temp)
            root = temp / 'run'
            root.mkdir()
            with patch.object(app, 'Agent', FakeAgent), patch.object(app, 'ClientRun', FakeClient), \
                    patch.object(app, 'read_activity', return_value=False), \
                    patch.object(app, 'await_activity'), patch.object(app, 'wait_client_alive'):
                result = app.do_case(cfg, 1, 'P', root)
            self.assertEqual(FakeAgent.actions, ['open', 'start', 'stop', 'finish', 'close'])
            self.assertEqual(result['status'], 'CAPTURED_NEEDS_INSIGHT_REVIEW')
            self.assertFalse(result['kernel_phase_verified'])
            self.assertFalse(result['cache_equivalence_verified'])

    def test_11_start_failure_closes_and_cancels(self):
        with workspace() as temp:
            cfg = setup_case(temp)
            root = temp / 'run'
            root.mkdir()
            FakeAgent.start_fails = True
            with patch.object(app, 'Agent', FakeAgent), patch.object(app, 'ClientRun', FakeClient), \
                    patch.object(app, 'read_activity', return_value=False), \
                    patch.object(app, 'await_activity'), patch.object(app, 'wait_client_alive'):
                with self.assertRaises(RuntimeError):
                    app.do_case(cfg, 1, 'P', root)
            self.assertIn('close', FakeAgent.actions)
            self.assertEqual(FakeClient.cancelled, 1)
            self.assertEqual(json.loads((root / 'C1_N4_ON_P/case.json').read_text())['status'], 'FAILED')

    def test_12_uncertain_stop_prevents_success(self):
        with workspace() as temp:
            cfg = setup_case(temp)
            root = temp / 'run'
            root.mkdir()
            FakeAgent.uncertain = True
            with patch.object(app, 'Agent', FakeAgent), patch.object(app, 'ClientRun', FakeClient), \
                    patch.object(app, 'read_activity', return_value=False), \
                    patch.object(app, 'await_activity'), patch.object(app, 'wait_client_alive'):
                with self.assertRaisesRegex(RuntimeError, 'uncertain'):
                    app.do_case(cfg, 1, 'D', root)
            self.assertEqual(json.loads((root / 'C1_N4_ON_D/case.json').read_text())['status'],
                             'FAILED_UNCERTAIN_STOP')

    def test_13_busy_target_prevents_benchmark(self):
        with workspace() as temp:
            cfg = setup_case(temp)
            root = temp / 'run'
            root.mkdir()
            with patch.object(app, 'read_activity', return_value=True), patch.object(app, 'ClientRun') as client:
                with self.assertRaisesRegex(RuntimeError, 'already busy'):
                    app.do_case(cfg, 1, 'P', root)
                client.assert_not_called()

    def test_14_agent_stop_failure_retains_uncertain_state(self):
        ns = {}
        exec(app.AGENT_SOURCE.split('\nsession = Session()')[0], ns)
        session = ns['Session']()
        session.proc = Mock()
        session.proc.poll.return_value = None
        session.active = True
        session.command = Mock(side_effect=RuntimeError('stop timeout'))
        result = session.cleanup()
        self.assertEqual(result['status'], 'UNCERTAIN_STOP_CHECK_SERVER')
        session.command.assert_called_once_with('stop')
        session.proc.terminate.assert_called_once()

    def test_15_init_never_overwrites(self):
        with workspace() as temp:
            path = temp / 'profile.json'
            argv = [sys.executable, '-B', str(ROOT / 'oneclick_aisbench_msprof.py'), '--init', str(path)]
            self.assertEqual(subprocess.run(argv, capture_output=True, timeout=10).returncode, 0)
            self.assertNotEqual(subprocess.run(argv, capture_output=True, timeout=10).returncode, 0)

    def test_16_agent_inspects_existing_dynamic_worker_without_version(self):
        ns = {}
        exec(app.AGENT_SOURCE.split('\nsession = Session()')[0], ns)
        with workspace() as temp:
            proc = temp / '1234'
            proc.mkdir()
            (proc / 'environ').write_bytes(b'OTHER=not_printed\0PROFILING_MODE=dynamic\0')
            (proc / 'comm').write_text('vllm-worker')
            sock = temp / 'dynamic_profiling_socket_1234'
            sock.write_text('mock socket')

            class Paths:
                def __call__(self, value):
                    return temp if str(value) == '/proc' else Path(value)

                def home(self):
                    return temp

            ns['Path'] = Paths()
            cfg = dict(app.template()['targets']['P'], pid=1234)
            fake_help = subprocess.CompletedProcess([], 0, '--dynamic --pid --output --task-time --export', '')
            with patch.object(ns['os'], 'geteuid', return_value=proc.stat().st_uid, create=True), \
                    patch.object(ns['stat'], 'S_ISSOCK', return_value=True), \
                    patch.object(ns['subprocess'], 'run', return_value=fake_help) as command:
                result = ns['Session']().inspect(cfg)
                self.assertEqual(result['pid'], 1234)
                self.assertEqual(command.call_args.args[0], ['msprof', '--help'])

    def test_17_finished_client_cannot_trigger_capture(self):
        client = Mock()
        client.proc.poll.return_value = 0
        with patch.object(app, 'read_activity') as activity:
            with self.assertRaisesRegex(RuntimeError, 'ended before capture'):
                app.await_activity(app.template()['targets']['P'], client, 1, [])
            activity.assert_not_called()

    def test_18_real_agent_json_transport_no_profiler_started(self):
        with workspace() as temp:
            cfg = dict(app.template()['targets']['P'], python=sys.executable, pid=0)
            agent = app.Agent(cfg, temp)
            try:
                with self.assertRaisesRegex(RuntimeError, 'actual NPU worker PID'):
                    agent.call('inspect', cfg, timeout=10)
            finally:
                self.assertEqual(agent.close()['status'], 'collector_closed')


if __name__ == '__main__':
    unittest.main(verbosity=2)
