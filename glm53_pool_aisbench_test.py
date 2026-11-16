#!/usr/bin/env python3
"""Offline checks for the self-contained shell runner; no AISBench/GPU/network needed.
装订版本，work by claude code，2026-11-16
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("glm53_pool_aisbench.sh")
SOURCE = SCRIPT.read_text(encoding="utf-8")
HELPER = SOURCE.split('cat > "$SESSION/helper.py" <<\'PY\'\n', 1)[1].split("\nPY\n", 1)[0]


class FakeTokenizer:
    def encode(self, text, add_special_tokens=True):
        assert add_special_tokens is True
        return list(map(ord, text))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mod = types.ModuleType("pool_helper_test")
        exec(compile(HELPER, "embedded_helper.py", "exec"), self.mod.__dict__)
        self.mod.TARGET = 100
        self.mod.tokenizer = lambda: FakeTokenizer()
        self.tokenizer_dir = self.root / "tokenizer"
        self.tokenizer_dir.mkdir()
        (self.tokenizer_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
        self.env = patch.dict(os.environ, {
            "POOL_TOKENIZER": str(self.tokenizer_dir),
            "POOL_SESSION": str(self.root), "POOL_ALLOW_DRIFT": "0"})
        self.env.start()
        self.data = self.root / "data"
        self.data.mkdir()
        for pn in (1, 10):
            d = self.data / f"p{pn}"
            d.mkdir()
            prefixes = [chr(65 + i) * 90 for i in range(pn)]
            self.write_rows(d / "prefix.jsonl", prefixes)
            self.write_rows(d / "full.jsonl", [prefixes[i % pn] + str(i).zfill(10) for i in range(30)])

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write_rows(self, path, values):
        path.write_text("".join(json.dumps({"question": x, "answer": "none"}) + "\n" for x in values), encoding="utf-8")

    def manifest(self):
        result = self.mod.audit(self.data)
        result["tokenizer_files_sha256"] = self.mod.tokenizer_hashes()
        self.mod.save(self.data / "manifest.json", result)
        return result

    def test_embedded_python_blocks_compile(self):
        for name in ("config.py", "default_api.py"):
            block = SOURCE.split(f'cat > "$WORK/{name}" <<\'PY\'\n', 1)[1].split("\nPY\n", 1)[0]
            compile(block, name, "exec")

    def test_audit_exact_lengths_and_order(self):
        report = self.manifest()
        self.assertTrue(report["sets"]["p10"]["exact_input_length"])
        self.assertEqual(report["sets"]["p10"]["matched_prefix_tokens"], [90] * 30)
        self.assertEqual(report["sets"]["p10"]["max_cross_prefix_common_tokens"], 0)
        self.mod.verify(self.data, 10)

    def test_duplicate_full_request_is_rejected(self):
        path = self.data / "p1" / "full.jsonl"
        values = [r["question"] for r in self.mod.rows(path)]
        values[1] = values[0]
        self.write_rows(path, values)
        with self.assertRaisesRegex(ValueError, "重复"):
            self.mod.audit(self.data)

    def test_missing_rows_are_rejected(self):
        path = self.data / "p10" / "full.jsonl"
        self.write_rows(path, [r["question"] for r in self.mod.rows(path)][:-1])
        with self.assertRaisesRegex(ValueError, "行数"):
            self.mod.audit(self.data)

    def test_wrong_prefix_order_is_rejected(self):
        path = self.data / "p10" / "full.jsonl"
        values = [r["question"] for r in self.mod.rows(path)]
        values[0], values[1] = values[1], values[0]
        self.write_rows(path, values)
        with self.assertRaisesRegex(ValueError, "循环前缀"):
            self.mod.audit(self.data)

    def test_length_drift_requires_explicit_acknowledgement(self):
        path = self.data / "p10" / "full.jsonl"
        self.write_rows(path, [r["question"] + "!" for r in self.mod.rows(path)])
        self.manifest()
        with self.assertRaisesRegex(ValueError, "精确131072"):
            self.mod.verify(self.data, 10)
        with patch.dict(os.environ, {"POOL_ALLOW_DRIFT": "1"}):
            self.mod.verify(self.data, 10)

    def test_file_tampering_is_rejected(self):
        self.manifest()
        path = self.data / "p1" / "full.jsonl"
        values = [r["question"] for r in self.mod.rows(path)]
        values[0] = values[0][:-1] + "x"
        self.write_rows(path, values)
        with self.assertRaisesRegex(ValueError, "sha256"):
            self.mod.verify(self.data, 1)

    def test_changed_tokenizer_file_is_rejected(self):
        self.manifest()
        (self.tokenizer_dir / "tokenizer.json").write_text('{"changed":true}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "tokenizer文件"):
            self.mod.verify(self.data, 10)

    def test_count_parser_accepts_ansi_table(self):
        log = self.root / "log.txt"
        log.write_text("\x1b[32m│ Success Requests │ total │ 30 │\x1b[0m\n│ Failed Requests │ 0 │\n", encoding="utf-8")
        self.mod.counts(log, 30, self.root / "counts.json")
        report = json.loads((self.root / "counts.json").read_text(encoding="utf-8"))
        self.assertTrue(report["request_counts_verified"])
        self.assertFalse(report["actual_server_input_output_lengths_verified"])

    def test_count_parser_fails_closed(self):
        for text in ("no table", "Success Requests: 29\nFailed Requests: 0", "Successful requests: 30\nFailed requests: 1"):
            log = self.root / "log.txt"
            log.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "请求数"):
                self.mod.counts(log, 30, self.root / "counts.json")

    def test_restore_after_failed_run(self):
        restore = "restore() {\n" + SOURCE.split("restore() {\n", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        script = r'''
set -euo pipefail
root=$(mktemp -d)
trap 'rm -rf -- "$root"' EXIT
SESSION="$root/session"
mkdir -p "$SESSION/backup" "$root/ais"
TARGETS=("$root/ais/api" "$root/ais/test" "$root/ais/train")
printf 'original-api\n' > "${TARGETS[0]}"
ln -s original-data.jsonl "${TARGETS[1]}"
cp -a "${TARGETS[0]}" "$SESSION/backup/0"
cp -a "${TARGETS[1]}" "$SESSION/backup/1"
SAVED=3
''' + restore + r'''
set +e
(
  trap restore EXIT
  rm -f "${TARGETS[0]}" "${TARGETS[1]}"
  ln -s temp-api.py "${TARGETS[0]}"
  ln -s temp-data.jsonl "${TARGETS[1]}"
  touch "${TARGETS[2]}"
  exit 7
)
rc=$?
set -e
test "$rc" -eq 7
test ! -L "${TARGETS[0]}"
test "$(cat "${TARGETS[0]}")" = original-api
test "$(readlink "${TARGETS[1]}")" = original-data.jsonl
test ! -e "${TARGETS[2]}"
grep -q 'exit_code=7' "$SESSION/exit_status.txt"
'''
        result = subprocess.run(["bash"], input=script.encode("utf-8"), capture_output=True)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode("utf-8", "replace"))

    def test_restore_after_successful_run(self):
        restore = "restore() {\n" + SOURCE.split("restore() {\n", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        script = r'''
set -euo pipefail
root=$(mktemp -d)
trap 'rm -rf -- "$root"' EXIT
SESSION="$root/session"
mkdir -p "$SESSION/backup"
TARGETS=("$root/new-file")
SAVED=1
''' + restore + r'''
(
  trap restore EXIT
  touch "${TARGETS[0]}"
  exit 0
)
test ! -e "${TARGETS[0]}"
grep -q 'exit_code=0' "$SESSION/exit_status.txt"
'''
        result = subprocess.run(["bash"], input=script.encode("utf-8"), capture_output=True)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode("utf-8", "replace"))

    def test_wrapper_bootstrap_enforces_pipefail(self):
        work = self.root / "work"
        work.mkdir()
        (work / "aisbench_test.py").write_text(
            "import subprocess\nsubprocess.run('false | tee /dev/null', shell=True, check=True)\n", encoding="utf-8")
        calls = []
        cwd, path, argv, run = os.getcwd(), list(sys.path), sys.argv, self.mod.subprocess.run
        try:
            with patch.object(self.mod.subprocess, "run", side_effect=lambda *a, **kw: calls.append((a, kw))):
                self.mod.execute(str(work), [])
            self.assertEqual(calls[0][0][0], "set -o pipefail; false | tee /dev/null")
            self.assertEqual(calls[0][1]["executable"], "/bin/bash")
        finally:
            os.chdir(cwd)
            sys.path[:] = path
            sys.argv = argv
            self.mod.subprocess.run = run


if __name__ == "__main__":
    unittest.main(verbosity=2)
