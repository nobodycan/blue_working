#!/usr/bin/env python3
"""Analyze the documented canonical CSVs, NOT arbitrary aisbench files.
Python 3.9+, standard library only. Does not upload, install, or plot anything.
"""
import argparse
import csv
import hashlib
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

RUN_FIELDS = [
    "run_id", "analysis_group", "mode", "concurrency", "target_input_tokens",
    "target_output_tokens", "repeat_id", "pair_id", "workload", "scope",
    "run_status", "expected_requests", "source_file",
]
REQUEST_FIELDS = [
    "run_id", "request_id", "status", "is_warmup", "input_tokens",
    "output_tokens", "decode_ms", "decode_time_source", "ttft_ms", "e2e_ms",
    "source_file", "source_locator",
]
METRICS = ["request_mean_tpot_ms", "token_weighted_tpot_ms"]
MIN_REPEATS = 5


class Issues:
    def __init__(self):
        self.errors = []
        self.warnings = []
        self.error_count = 0
        self.warning_count = 0

    def error(self, message):
        self.error_count += 1
        if len(self.errors) < 200:
            self.errors.append(message)

    def warn(self, message):
        self.warning_count += 1
        if len(self.warnings) < 200:
            self.warnings.append(message)

    def as_dict(self):
        return dict(error_count=self.error_count, warning_count=self.warning_count,
                    errors=self.errors, warnings=self.warnings,
                    messages_capped_at=200)


def csv_rows(path, required, issues):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        missing = sorted(set(required) - set(fields))
        if missing or len(fields) != len(set(fields)):
            issues.error(f"{path}: missing columns {missing} or duplicate header")
            return
        for row in reader:
            line = reader.line_num
            if None in row:
                issues.error(f"{path}:{line}: too many CSV cells")
                continue
            if not any(str(v or "").strip() for v in row.values()):
                continue
            yield line, {k: str(v or "").strip() for k, v in row.items()}


def integer(value, name, minimum=0, optional=False):
    if value == "" and optional:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: expected integer, got {value!r}")
    if result < minimum:
        raise ValueError(f"{name}: must be >= {minimum}")
    return result


def number(value, name, optional=True):
    if value == "" and optional:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: expected number, got {value!r}")
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name}: must be finite and nonnegative")
    return result


def required_text(row, fields):
    for field in fields:
        if not row[field]:
            raise ValueError(f"{field}: required nonempty text")


def quantile(values, probability):
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low = int(math.floor(position))
    high = int(math.ceil(position))
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarize_inputs(runs_path, requests_path):
    issues = Issues()
    runs = {}
    repeat_keys = set()
    group_design = {}
    for line, row in csv_rows(runs_path, RUN_FIELDS, issues):
        try:
            required_text(row, ["run_id", "analysis_group", "repeat_id", "source_file"])
            if row["run_id"] in runs:
                raise ValueError("duplicate run_id")
            for field, allowed in [
                ("mode", {"mixed", "disagg"}),
                ("workload", {"burst", "closed_loop", "open_loop", "unknown"}),
                ("scope", {"full_requests"}),
                ("run_status", {"completed", "failed", "interrupted"}),
            ]:
                if row[field] not in allowed:
                    raise ValueError(f"{field}: allowed values {sorted(allowed)}")
            for field in ["concurrency", "target_input_tokens", "target_output_tokens"]:
                row[field] = integer(row[field], field, 1)
            row["expected_requests"] = integer(
                row["expected_requests"], "expected_requests", optional=True)
            signature = (row["target_input_tokens"], row["workload"], row["scope"])
            group = row["analysis_group"]
            if group in group_design and group_design[group] != signature:
                raise ValueError("analysis_group mixes input targets/workloads/scopes")
            group_design[group] = signature
            key = (group, row["mode"], row["concurrency"],
                   row["target_output_tokens"], row["repeat_id"])
            if key in repeat_keys:
                raise ValueError("duplicate repeat_id within same experimental condition")
            repeat_keys.add(key)
            runs[row["run_id"]] = row
        except ValueError as exc:
            issues.error(f"runs line {line}: {exc}")

    accum = {}
    for run_id in runs:
        accum[run_id] = dict(warmup=0, measured=0, success=0, failed=0,
                             invalid_tpot=0, tpot=[], decode_sum=0.0,
                             intervals_sum=0, outputs=[], input_mismatch=0,
                             input_unknown=0, sources=set())
    seen = set()
    group_sources = defaultdict(set)
    for line, row in csv_rows(requests_path, REQUEST_FIELDS, issues):
        try:
            required_text(row, ["run_id", "request_id", "source_file", "source_locator"])
            run_id = row["run_id"]
            if run_id not in runs:
                raise ValueError("unknown run_id")
            key = (run_id, row["request_id"])
            if key in seen:
                raise ValueError("duplicate (run_id, request_id)")
            seen.add(key)
            if row["status"] not in {"success", "failed", "timeout", "cancelled", "incomplete"}:
                raise ValueError("unknown request status")
            if row["is_warmup"] not in {"0", "1"}:
                raise ValueError("is_warmup must be 0 or 1")
            actual_input = integer(row["input_tokens"], "input_tokens", optional=True)
            output = integer(row["output_tokens"], "output_tokens", optional=True)
            decode = number(row["decode_ms"], "decode_ms")
            ttft = number(row["ttft_ms"], "ttft_ms")
            e2e = number(row["e2e_ms"], "e2e_ms")
            if ttft is not None and e2e is not None:
                if e2e < ttft:
                    raise ValueError("e2e_ms < ttft_ms")
                if decode is not None:
                    tolerance = max(1.0, decode * 0.001)
                    if abs(e2e - ttft - decode) > tolerance:
                        raise ValueError("decode_ms inconsistent with e2e_ms - ttft_ms; audit boundaries")
            if row["status"] == "success":
                if output is None:
                    raise ValueError("success missing actual output_tokens")
                if output >= 2:
                    if decode is None:
                        raise ValueError("success with L>=2 missing decode_ms")
                    if row["decode_time_source"] not in {
                        "last_minus_first_token", "e2e_minus_ttft"
                    }:
                        raise ValueError("missing/unsupported decode_time_source")
                    if row["decode_time_source"] == "e2e_minus_ttft" and (ttft is None or e2e is None):
                        raise ValueError("e2e_minus_ttft requires both original timing values")
            a = accum[run_id]
            if row["is_warmup"] == "1":
                a["warmup"] += 1
                continue
            a["measured"] += 1
            if actual_input is None:
                a["input_unknown"] += 1
            elif actual_input != runs[run_id]["target_input_tokens"]:
                a["input_mismatch"] += 1
            if row["status"] != "success":
                a["failed"] += 1
                continue
            a["success"] += 1
            if output < 2:
                a["invalid_tpot"] += 1
                continue
            a["tpot"].append(decode / (output - 1))
            a["decode_sum"] += decode
            a["intervals_sum"] += output - 1
            a["outputs"].append(output)
            a["sources"].add(row["decode_time_source"])
            group_sources[runs[run_id]["analysis_group"]].add(row["decode_time_source"])
        except ValueError as exc:
            issues.error(f"requests line {line}: {exc}")

    result = []
    for run_id, run in runs.items():
        a = accum[run_id]
        measured = a["measured"]
        if run["run_status"] != "completed":
            issues.warn(f"{run_id}: excluded non-completed run ({run['run_status']})")
        elif measured == 0:
            issues.error(f"{run_id}: completed run has no measured request records")
        expected = run["expected_requests"]
        if expected is None:
            issues.warn(f"{run_id}: expected_requests unknown; missing failures cannot be ruled out")
        elif measured != expected:
            message = f"{run_id}: observed measured requests {measured} != expected {expected}"
            if run["run_status"] == "completed":
                issues.error(message)
            else:
                issues.warn(message)
        if a["failed"]:
            issues.warn(f"{run_id}: {a['failed']} failed/non-completed requests; TPOT is conditional on success")
        if a["invalid_tpot"]:
            issues.warn(f"{run_id}: {a['invalid_tpot']} successes have L<2 and undefined TPOT")
        if a["input_mismatch"] or a["input_unknown"]:
            issues.warn(f"{run_id}: actual input mismatches={a['input_mismatch']}, unknown={a['input_unknown']}")
        if run["workload"] == "unknown":
            issues.warn(f"{run_id}: workload unknown; mechanism conclusions blocked")
        valid = len(a["tpot"])
        if run["run_status"] == "completed" and not valid:
            issues.warn(f"{run_id}: no valid TPOT requests; excluded from conditional estimates")
        metric_row = dict(run)
        metric_row.update(
            measured_requests=measured, warmup_requests=a["warmup"],
            success_requests=a["success"], failed_requests=a["failed"],
            undefined_tpot_requests=a["invalid_tpot"], valid_tpot_requests=valid,
            failure_rate=a["failed"] / measured if measured else None,
            actual_input_mismatches=a["input_mismatch"], actual_input_unknown=a["input_unknown"],
            request_mean_tpot_ms=statistics.mean(a["tpot"]) if valid else None,
            token_weighted_tpot_ms=a["decode_sum"] / a["intervals_sum"] if valid else None,
            request_tpot_p50_ms=quantile(a["tpot"], 0.50) if valid else None,
            request_tpot_p95_ms=quantile(a["tpot"], 0.95) if valid else None,
            actual_output_min=min(a["outputs"]) if valid else None,
            actual_output_max=max(a["outputs"]) if valid else None,
            actual_output_mean=statistics.mean(a["outputs"]) if valid else None,
            full_target_fraction=sum(v == run["target_output_tokens"] for v in a["outputs"]) / valid if valid else None,
            fixed_actual_target=bool(valid and all(v == run["target_output_tokens"] for v in a["outputs"])),
        )
        result.append(metric_row)
    for group, sources in group_sources.items():
        if len(sources) > 1:
            issues.error(f"group {group}: mixed decode timing sources {sorted(sources)}; split or calibrate")
    if not runs:
        issues.error("no valid runs")
    return result, issues


def bootstrap_group(values, count, ci, rng):
    if len(values) < MIN_REPEATS:
        return None, None
    means = [statistics.mean(rng.choices(values, k=len(values))) for _ in range(count)]
    alpha = (1 - ci) / 2
    return quantile(means, alpha), quantile(means, 1 - alpha)


def pairing(mixed, disagg):
    values = [r["pair_id"] for r in mixed + disagg]
    if not any(values):
        return "unpaired", None
    if not all(values):
        return "invalid_partial_pair_ids", None
    left = {r["pair_id"]: r for r in mixed}
    right = {r["pair_id"]: r for r in disagg}
    if len(left) != len(mixed) or len(right) != len(disagg):
        return "invalid_duplicate_pair_ids", None
    if set(left) != set(right):
        return "invalid_unmatched_pair_ids", None
    ids = sorted(left)
    return "paired", [(left[k], right[k]) for k in ids]


def bootstrap_comparison(mixed, disagg, metric, count, ci, rng):
    design, pairs = pairing(mixed, disagg)
    if design.startswith("invalid"):
        return design, None
    if min(len(mixed), len(disagg)) < MIN_REPEATS:
        return design + "_insufficient_repeats", None
    if any(r[metric] <= 0 for r in disagg):
        return design + "_nonpositive_baseline", None
    deltas, relatives = [], []
    for _ in range(count):
        if pairs is not None:
            sampled = rng.choices(pairs, k=len(pairs))
            m = statistics.mean(p[0][metric] for p in sampled)
            d = statistics.mean(p[1][metric] for p in sampled)
        else:
            m = statistics.mean(r[metric] for r in rng.choices(mixed, k=len(mixed)))
            d = statistics.mean(r[metric] for r in rng.choices(disagg, k=len(disagg)))
        deltas.append(m - d)
        relatives.append(m / d - 1)
    alpha = (1 - ci) / 2
    return design, (quantile(deltas, alpha), quantile(deltas, 1 - alpha),
                    quantile(relatives, alpha), quantile(relatives, 1 - alpha))


def linear_fit(xs, ys):
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if not sxx:
        raise ValueError("identical inverse lengths")
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    residuals = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
    sse = sum(v * v for v in residuals)
    sst = sum((y - my) ** 2 for y in ys)
    return dict(A_ms=slope, delta_ms_per_token=intercept,
                r_squared=1 - sse / sst if sst else None,
                rmse_ms_per_token=math.sqrt(sse / len(ys)))


def model_diagnostics(comparisons, issues):
    groups = defaultdict(list)
    for row in comparisons:
        if row["fixed_actual_target"] and row["target_output_tokens"] >= 2:
            groups[(row["analysis_group"], row["concurrency"])].append(row)
    fits, residual_rows = [], []
    for (group, concurrency), rows in sorted(groups.items()):
        rows.sort(key=lambda r: r["target_output_tokens"])
        if len(rows) < 4:
            issues.warn(f"{group}/C={concurrency}: <4 fixed-length points; model fit skipped")
            continue
        xs = [1 / (r["target_output_tokens"] - 1) for r in rows]
        ys = [r["delta_tpot_ms"] for r in rows]
        fit = linear_fit(xs, ys)
        inverse_loo, constant_loo = [], []
        for i in range(len(rows)):
            train_x = xs[:i] + xs[i + 1:]
            train_y = ys[:i] + ys[i + 1:]
            temp = linear_fit(train_x, train_y)
            inverse_loo.append(ys[i] - (temp["A_ms"] * xs[i] + temp["delta_ms_per_token"]))
            constant_loo.append(ys[i] - statistics.mean(train_y))
            prediction = fit["A_ms"] * xs[i] + fit["delta_ms_per_token"]
            residual_rows.append(dict(analysis_group=group, concurrency=concurrency,
                                      target_output_tokens=rows[i]["target_output_tokens"],
                                      inverse_intervals=xs[i], observed_delta_ms=ys[i],
                                      predicted_delta_ms=prediction, residual_ms=ys[i] - prediction))
        without_short = linear_fit(xs[1:], ys[1:])
        without_long = linear_fit(xs[:-1], ys[:-1])
        fit.update(analysis_group=group, concurrency=concurrency, n_lengths=len(rows),
                   inverse_loo_rmse=math.sqrt(statistics.mean(v*v for v in inverse_loo)),
                   constant_loo_rmse=math.sqrt(statistics.mean(v*v for v in constant_loo)),
                   A_without_shortest=without_short["A_ms"],
                   delta_without_shortest=without_short["delta_ms_per_token"],
                   A_without_longest=without_long["A_ms"],
                   delta_without_longest=without_long["delta_ms_per_token"],
                   inference="descriptive_shape_only_no_parameter_CI")
        fits.append(fit)
    return fits, residual_rows


def analyze(run_rows, issues, metric, count, ci, seed, margin, prespecified):
    rng = random.Random(seed)
    cells = defaultdict(list)
    for row in run_rows:
        if row["run_status"] == "completed" and row[metric] is not None:
            cells[(row["analysis_group"], row["concurrency"],
                   row["target_output_tokens"], row["mode"])].append(row)
    groups = []
    for (group, concurrency, length, mode), rows in sorted(cells.items()):
        rows.sort(key=lambda r: r["run_id"])
        values = [r[metric] for r in rows]
        low, high = bootstrap_group(values, count, ci, rng)
        groups.append(dict(
            analysis_group=group, concurrency=concurrency, target_output_tokens=length,
            mode=mode, metric=metric, n_runs=len(rows), mean_tpot_ms=statistics.mean(values),
            run_sd_ms=statistics.stdev(values) if len(values) > 1 else None,
            ci_low_ms=low, ci_high_ms=high,
            valid_requests=sum(r["valid_tpot_requests"] for r in rows),
            measured_requests=sum(r["measured_requests"] for r in rows),
            failed_requests=sum(r["failed_requests"] for r in rows),
            mean_run_failure_rate=statistics.mean(r["failure_rate"] for r in rows),
            fixed_actual_target=all(r["fixed_actual_target"] for r in rows),
            run_ids=";".join(r["run_id"] for r in rows),
        ))
    comparisons = []
    conditions = sorted(set(key[:3] for key in cells))
    for group, concurrency, length in conditions:
        mixed = cells.get((group, concurrency, length, "mixed"), [])
        disagg = cells.get((group, concurrency, length, "disagg"), [])
        if not mixed or not disagg:
            issues.warn(f"{group}/C={concurrency}/L={length}: missing usable counterpart")
            continue
        m = statistics.mean(r[metric] for r in mixed)
        d = statistics.mean(r[metric] for r in disagg)
        if d <= 0:
            issues.warn(f"{group}/C={concurrency}/L={length}: nonpositive baseline; comparison skipped")
            continue
        design, interval = bootstrap_comparison(mixed, disagg, metric, count, ci, rng)
        if interval is None:
            issues.warn(f"{group}/C={concurrency}/L={length}: no comparison CI ({design})")
        low, high, rlow, rhigh = interval or (None, None, None, None)
        fixed = all(r["fixed_actual_target"] for r in mixed + disagg)
        if not fixed:
            issues.warn(f"{group}/C={concurrency}/L={length}: variable actual length; E and inverse-length fit excluded")
        if margin is None:
            equivalence = "not_assessed_no_margin"
        elif interval is None:
            equivalence = "insufficient_evidence_no_CI"
        elif rlow >= -margin and rhigh <= margin:
            equivalence = "CI_inside_prespecified_margin" if prespecified else "CI_inside_exploratory_margin"
        else:
            equivalence = "CI_not_contained_in_margin"
        comparisons.append(dict(
            analysis_group=group, concurrency=concurrency, target_output_tokens=length,
            metric=metric, n_mixed_runs=len(mixed), n_disagg_runs=len(disagg),
            mixed_tpot_ms=m, disagg_tpot_ms=d, delta_tpot_ms=m-d,
            relative_gap=m/d-1, delta_ci_low_ms=low, delta_ci_high_ms=high,
            relative_ci_low=rlow, relative_ci_high=rhigh,
            design=design, fixed_actual_target=fixed,
            extra_decode_ms=(m-d)*(length-1) if fixed and length >= 2 else None,
            extra_decode_ci_low_ms=low*(length-1) if fixed and low is not None and length >= 2 else None,
            extra_decode_ci_high_ms=high*(length-1) if fixed and high is not None and length >= 2 else None,
            margin=margin, margin_prespecified=prespecified,
            equivalence_evidence=equivalence,
        ))
    fits, residuals = model_diagnostics(comparisons, issues)
    return groups, comparisons, fits, residuals


def write_csv(path, rows, fields=None):
    rows = list(rows)
    fields = fields or (list(rows[0]) if rows else ["no_rows"])
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_test():
    """Numerical fixtures only, never benchmark observations."""
    import tempfile
    import unittest

    class Tests(unittest.TestCase):
        def fixture(self, directory, duplicate=False, inconsistent=False):
            runs, requests = [], []
            for mode in ["mixed", "disagg"]:
                for length in [512, 1024, 2048, 4096]:
                    for repeat in range(5):
                        run_id = f"{mode}_{length}_{repeat}"
                        run = dict(run_id=run_id, analysis_group="TEST_FIXTURE_NOT_MEASURED",
                                   mode=mode, concurrency=4, target_input_tokens=512,
                                   target_output_tokens=length, repeat_id=str(repeat), pair_id=str(repeat),
                                   workload="burst", scope="full_requests", run_status="completed",
                                   expected_requests=2, source_file="SYNTHETIC_NUMERICAL_FIXTURE")
                        runs.append(run)
                        tpot = 10 + repeat * 0.01 + (1000 / (length - 1) if mode == "mixed" else 0)
                        for req in range(2):
                            decode = tpot * (length - 1)
                            requests.append(dict(run_id=run_id, request_id=str(req), status="success",
                                                 is_warmup=0, input_tokens=512, output_tokens=length,
                                                 decode_ms=decode, decode_time_source="e2e_minus_ttft",
                                                 ttft_ms=100, e2e_ms=decode+100,
                                                 source_file="SYNTHETIC_NUMERICAL_FIXTURE", source_locator=str(req)))
            if duplicate:
                requests.append(dict(requests[0]))
            if inconsistent:
                requests[0]["e2e_ms"] += 100
            rp, qp = Path(directory)/"runs.csv", Path(directory)/"requests.csv"
            write_csv(rp, runs, RUN_FIELDS)
            write_csv(qp, requests, REQUEST_FIELDS)
            return rp, qp

        def test_numeric_fixture(self):
            with tempfile.TemporaryDirectory() as tmp:
                rp, qp = self.fixture(tmp)
                rows, issues = summarize_inputs(rp, qp)
                self.assertEqual(issues.error_count, 0)
                groups, comparisons, fits, residuals = analyze(
                    rows, issues, METRICS[0], 100, .95, 1, .05, False)
                self.assertEqual(len(groups), 8)
                self.assertEqual(len(comparisons), 4)
                self.assertEqual(len(residuals), 4)
                self.assertAlmostEqual(fits[0]["A_ms"], 1000, places=7)
                self.assertAlmostEqual(fits[0]["delta_ms_per_token"], 0, places=10)
                self.assertAlmostEqual(comparisons[0]["extra_decode_ms"], 1000, places=7)
                self.assertEqual(comparisons[0]["design"], "paired")

        def test_duplicate_rejected(self):
            with tempfile.TemporaryDirectory() as tmp:
                paths = self.fixture(tmp, duplicate=True)
                _, issues = summarize_inputs(*paths)
                self.assertGreater(issues.error_count, 0)

        def test_boundary_mismatch_rejected(self):
            with tempfile.TemporaryDirectory() as tmp:
                paths = self.fixture(tmp, inconsistent=True)
                _, issues = summarize_inputs(*paths)
                self.assertGreater(issues.error_count, 0)

        def test_insufficient_repeats_no_interval(self):
            row = dict(pair_id="", request_mean_tpot_ms=10)
            design, interval = bootstrap_comparison([row], [row], METRICS[0], 100, .95, random.Random(1))
            self.assertIsNone(interval)
            self.assertIn("insufficient", design)

        def test_invalid_pairing_no_interval(self):
            a = dict(pair_id="a", request_mean_tpot_ms=10)
            b = dict(pair_id="b", request_mean_tpot_ms=10)
            design, interval = bootstrap_comparison([a]*5, [b]*5, METRICS[0], 100, .95, random.Random(1))
            self.assertIsNone(interval)
            self.assertIn("invalid", design)

        def test_weighting_definitions(self):
            request_mean = statistics.mean([10, 20])
            token_weighted = (10*10 + 20*100) / (10+100)
            self.assertEqual(request_mean, 15)
            self.assertNotEqual(request_mean, token_weighted)

        def test_variable_length_excludes_extra_time_and_fit(self):
            with tempfile.TemporaryDirectory() as tmp:
                paths = self.fixture(tmp)
                rows, issues = summarize_inputs(*paths)
                for row in rows:
                    row["fixed_actual_target"] = False
                _, comparisons, fits, _ = analyze(rows, issues, METRICS[0], 100, .95, 1, None, False)
                self.assertFalse(fits)
                self.assertTrue(all(r["extra_decode_ms"] is None for r in comparisons))

        def test_quantile(self):
            self.assertEqual(quantile([0, 10], .5), 5)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Tests)
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    for name in ["validate", "analyze"]:
        p = sub.add_parser(name)
        p.add_argument("--runs", required=True)
        p.add_argument("--requests", required=True)
        p.add_argument("--out", required=True, help="New output directory; must not already exist")
        if name == "analyze":
            p.add_argument("--metric", required=True, choices=METRICS)
            p.add_argument("--bootstrap", type=int, default=5000)
            p.add_argument("--ci", type=float, default=.95)
            p.add_argument("--seed", type=int, default=20261111)
            p.add_argument("--margin", type=float, help="Relative fraction, e.g. 0.05 for 5%%")
            p.add_argument("--margin-prespecified", action="store_true",
                           help="Only if documentary evidence shows margin was chosen before results")
    args = parser.parse_args()
    if args.command == "self-test":
        return self_test()
    if args.command == "analyze":
        if args.bootstrap < 1000 or not 0 < args.ci < 1:
            parser.error("bootstrap must be >=1000; ci must be between 0 and 1")
        if args.margin is not None and not 0 < args.margin < 1:
            parser.error("margin must be a fraction between 0 and 1")
        if args.margin_prespecified and args.margin is None:
            parser.error("--margin-prespecified requires --margin")
    output = Path(args.out).resolve()
    # Refuse reuse, including a previous failed validation directory.
    output.mkdir(parents=True, exist_ok=False)
    rows, issues = summarize_inputs(args.runs, args.requests)
    if not issues.error_count and args.command == "analyze":
        group_rows, comparisons, fits, residuals = analyze(
            rows, issues, args.metric, args.bootstrap, args.ci, args.seed,
            args.margin, args.margin_prespecified)
        if not group_rows:
            issues.error("no analyzable completed runs")
        if not comparisons:
            issues.warn("no matched mode conditions available; no deployment comparison")
        if not issues.error_count:
            write_csv(output/"run_metrics.csv", rows)
            write_csv(output/"group_metrics.csv", group_rows)
            write_csv(output/"comparisons.csv", comparisons)
            write_csv(output/"fit_diagnostics.csv", fits)
            write_csv(output/"fit_residuals.csv", residuals)
    report = issues.as_dict()
    report.update(command=args.command, valid=not issues.error_count,
                  run_count=len(rows), minimum_repeats_for_CI=MIN_REPEATS,
                  created_utc=datetime.now(timezone.utc).isoformat(),
                  arguments=vars(args), script_sha256=file_hash(__file__),
                  inputs={str(Path(p).resolve()): file_hash(p) for p in [args.runs, args.requests]},
                  limitations=["Canonical-input checks cannot verify raw-field semantics or independence.",
                               "No causal inference; model fit has no parameter confidence intervals.",
                               "Equivalence flags concern each cell, not simultaneous whole-curve coverage.",
                               "Failure rates and actual batch/resource confounds require human review."])
    save_json(output/"validation.json", report)
    print(json.dumps(dict(valid=report["valid"], errors=issues.error_count,
                          warnings=issues.warning_count, output=str(output)), ensure_ascii=False))
    return 0 if report["valid"] else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, UnicodeError, csv.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
