#!/usr/bin/env python3
"""Offline validation only. Synthetic fixtures are NOT NPU benchmark results."""
import ast
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import csv
import io
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]


def run(name, *args):
    env = dict(os.environ)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = env["NO_PROXY"]
    return subprocess.run([sys.executable, str(ROOT / "scripts" / name), *map(str, args)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=20, env=env)


@contextmanager
def workspace():
    base = ROOT / "validation"
    with tempfile.TemporaryDirectory(prefix="tmp_offline_", dir=base) as raw:
        path = Path(raw).resolve()
        # Check absolute cleanup target before TemporaryDirectory's recursive cleanup.
        if not path.is_relative_to(base.resolve()) or path == base.resolve():
            raise RuntimeError("Unsafe temporary path")
        yield path


@contextmanager
def servers(fail_start=None, fail_stop=None):
    events = []
    urls = ["http://127.0.0.1:8100", "http://127.0.0.1:8200"]

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size=-1):
            return b'{"mock":true}'

    def urlopen(req, timeout=None):
        role = "P" if req.full_url.startswith(urls[0] + "/") else "D"
        action = "/" + req.full_url.rsplit("/", 1)[1]
        assert req.get_method() == "POST" and req.data == b"{}"
        events.append((role, action))
        if ((role == fail_start and action == "/start_profile") or
                (role == fail_stop and action == "/stop_profile")):
            raise urllib.error.HTTPError(req.full_url, 500, "mock_failure", {}, io.BytesIO(b"mock"))
        return Response()

    # Intercept transport in process; no sockets/network permissions needed.
    with patch("urllib.request.urlopen", urlopen):
        yield urls, events


def run_profile(*args):
    stdout, stderr = io.StringIO(), io.StringIO()
    code = 0
    with patch.object(sys, "argv", ["profile_window.py", *map(str, args)]), \
            redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            runpy.run_path(str(ROOT / "scripts/profile_window.py"), run_name="__main__")
        except SystemExit as exc:
            code = exc.code or 0
    return subprocess.CompletedProcess([], code, stdout.getvalue(), stderr.getvalue())


class Checks(unittest.TestCase):
    def test_01_syntax_and_help(self):
        scripts = list((ROOT / "scripts").glob("*.py"))
        self.assertEqual(len(scripts), 6)
        for path in scripts:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            p = run(path.name, "--help")
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_02_documents_and_configs(self):
        for path in ROOT.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            for link in re.findall(r"\]\(([^)]+)\)", text):
                if "://" not in link and not link.startswith("#"):
                    self.assertTrue((path.parent / link.split("#")[0]).exists(), (path, link))
        json.loads((ROOT / "configs/environment_template.json").read_text(encoding="utf-8"))
        with (ROOT / "configs/experiment_matrix.csv").open(encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        self.assertTrue(all(len(row) == len(rows[0]) for row in rows))

    def test_03_benchmark_dry_run(self):
        p = run("run_bench.py", "--base-url", "http://127.0.0.1:1999",
                "--model", "/model/with space", "--served-model", "dsv4",
                "--concurrency", "4", "--purpose", "baseline", "--run-id", "check")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("DRY RUN", p.stdout)
        self.assertIn("--max-concurrency 4", p.stdout)
        self.assertIn("--random-range-ratio 0", p.stdout)
        self.assertNotIn(" --profile ", p.stdout)

    def test_04_benchmark_invalid_run_id(self):
        p = run("run_bench.py", "--base-url", "http://127.0.0.1:1999",
                "--model", "/model", "--served-model", "dsv4", "--concurrency", "1",
                "--purpose", "baseline", "--run-id", "../escape")
        self.assertNotEqual(p.returncode, 0)

    def test_05_capture_success(self):
        with workspace() as temp, servers() as (urls, events):
            p = run_profile("--p-url", urls[0], "--d-url", urls[1],
                    "--seconds", "0.05", "--out", temp / "capture", "--execute")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr +
                             (temp / "capture/events.json").read_text(encoding="utf-8"))
            self.assertEqual(set(events), {(r, a) for r in ["P", "D"]
                                         for a in ["/start_profile", "/stop_profile"]})
            log = json.loads((temp / "capture/events.json").read_text(encoding="utf-8"))
            self.assertTrue(any(x["action"] == "window_active" for x in log))

    def test_06_capture_partial_start_failure_cleans_both(self):
        with workspace() as temp, servers(fail_start="D") as (urls, events):
            p = run_profile("--p-url", urls[0], "--d-url", urls[1],
                    "--seconds", "0.05", "--out", temp / "capture", "--execute")
            self.assertNotEqual(p.returncode, 0)
            self.assertIn(("P", "/stop_profile"), events)
            self.assertIn(("D", "/stop_profile"), events)
            log = json.loads((temp / "capture/events.json").read_text(encoding="utf-8"))
            self.assertFalse(any(x["action"] == "window_active" for x in log))

    def test_07_capture_stop_failure_is_error(self):
        with workspace() as temp, servers(fail_stop="P") as (urls, events):
            p = run_profile("--p-url", urls[0], "--d-url", urls[1],
                    "--seconds", "0.05", "--out", temp / "capture", "--execute")
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("stop manually", p.stdout)
            self.assertIn("CAPTURE ACTIVE", p.stdout)

    def test_08_capture_same_endpoint_rejected(self):
        p = run("profile_window.py", "--p-url", "http://127.0.0.1:1234",
                "--d-url", "http://127.0.0.1:1234", "--out", "unused")
        self.assertNotEqual(p.returncode, 0)

    def test_09_analysis_dry_run_no_npu_import(self):
        with workspace() as temp:
            (temp / "fake_rank0_ascend_pt").mkdir()
            p = run("analyze_profiles.py", temp)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("dry_run", p.stdout)
            self.assertNotIn("ModuleNotFoundError", p.stderr)

    def test_10_summary_excludes_diagnostic_and_preserves_missing(self):
        with workspace() as temp:
            config = {"concurrency": 1, "input_len": 1024, "output_len": 128,
                      "seed": 1, "num_prompts": 2}
            for name, purpose in [("base", "baseline"), ("diag", "diagnostic")]:
                folder = temp / name
                folder.mkdir()
                (folder / "run.json").write_text(json.dumps({
                    "purpose": purpose, "run_id": name, "status": "completed",
                    "returncode": 0, "configuration": config}), encoding="utf-8")
                # Intentionally missing p99 fields; fixture is synthetic.
                (folder / "result.json").write_text(json.dumps({
                    "duration": 2, "completed": 2, "failed": 0, "total_input_tokens": 2048,
                    "total_output_tokens": 256, "output_throughput": 128,
                    "mean_ttft_ms": 10, "mean_tpot_ms": 5}), encoding="utf-8")
            p = run("summarize_results.py", temp, "--out", temp / "summary.csv")
            self.assertEqual(p.returncode, 0, p.stderr)
            with (temp / "summary.csv").open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["run_id"], "base")
            self.assertEqual(rows[0]["p99_ttft_ms"], "")
            self.assertNotEqual(run("summarize_results.py", temp, "--out",
                                    temp / "summary.csv").returncode, 0)

    def test_11_original_aisbench_matrix_is_preserved(self):
        with (ROOT / "configs/experiment_matrix.csv").open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        original = [r for r in rows if r["family"] == "ORIGINAL"]
        self.assertEqual([(int(r["concurrency"]), int(r["request_count"])) for r in original],
                         [(1, 4), (2, 8), (4, 16), (8, 32), (16, 64)])
        for row in original:
            self.assertEqual(row["input_tokens"], "131072")
            self.assertEqual(row["output_tokens"], "1024")
            self.assertEqual(row["summarizer"], "default_perf")
            self.assertEqual(row["profiler"], "off")


if __name__ == "__main__":
    unittest.main(verbosity=2)
