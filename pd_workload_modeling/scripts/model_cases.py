#!/usr/bin/env python3
"""Fixed-case workload modeling, NOT an arbitrary aisbench file parser.
Python 3.9+, standard library. Inputs follow references/DATA_INTERFACE.md.
No network, profiler, server changes, confidence intervals, or causal claims.
"""
import argparse
import csv
import hashlib
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

LENGTHS = [512, 1024, 2048, 4096, 8192, 16384, 32768, 65536]
CONCURRENCIES = [1, 4, 8, 16, 32]
MODES = ["mixed", "disagg"]
FIELDS = ["run_id", "analysis_group", "mode", "concurrency", "target_input_tokens",
          "target_output_tokens", "repeat_id", "run_status", "scope", "workload",
          "request_mean_tpot_ms", "valid_tpot_requests", "success_requests",
          "failed_requests", "actual_output_min", "actual_output_max",
          "actual_input_mismatches", "actual_input_unknown", "source_file"]
MODELS = {"K0_constant": ["constant"], "K1_inverse": ["constant", "inverse"],
          "K2_context": ["constant", "context"],
          "K3_combined": ["constant", "inverse", "context"]}


def write_csv(path, rows, fields=None):
    fields = fields or (list(rows[0]) if rows else ["no_rows"])
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024*1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def new_directory(path):
    directory = Path(path).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def features(length, input_length, terms):
    values = {"constant": 1.0, "inverse": 512.0/(length-1),
              "context": (input_length+length/2.0)/1000.0}
    return [values[t] for t in terms]


def least_squares(matrix, ys):
    """Modified Gram-Schmidt with reorthogonalization; at most three columns."""
    n, p = len(matrix), len(matrix[0])
    if n < p:
        raise ValueError("not enough observations")
    q, r = [], [[0.0]*p for _ in range(p)]
    for j in range(p):
        column = [matrix[i][j] for i in range(n)]
        norm0 = math.sqrt(sum(v*v for v in column))
        for _ in range(2):
            for k in range(j):
                projection = sum(a*b for a, b in zip(q[k], column))
                r[k][j] += projection
                column = [a-projection*b for a, b in zip(column, q[k])]
        norm = math.sqrt(sum(v*v for v in column))
        if norm0 == 0 or norm <= norm0*1e-10:
            raise ValueError("rank-deficient design matrix")
        r[j][j] = norm
        q.append([v/norm for v in column])
    qty = [sum(a*b for a, b in zip(column, ys)) for column in q]
    coefficients = [0.0]*p
    for j in reversed(range(p)):
        coefficients[j] = (qty[j]-sum(r[j][k]*coefficients[k] for k in range(j+1, p)))/r[j][j]
    if not all(math.isfinite(v) for v in coefficients):
        raise ValueError("nonfinite fitted coefficient")
    return coefficients


def fit(rows, terms):
    return least_squares([features(r["length"], r["input"], terms) for r in rows],
                         [r["value"] for r in rows])


def predict(row, terms, coefficients):
    return sum(a*b for a, b in zip(features(row["length"], row["input"], terms), coefficients))


def named_parameters(terms, coefficients):
    raw = dict(zip(terms, coefficients))
    return dict(a_ms=raw["constant"],
                finite_overhead_ms=raw["inverse"]*512 if "inverse" in raw else None,
                context_slope_ms_per_1k=raw.get("context"))


def parse_integer(row, field, minimum=0):
    value = int(row[field])
    if value < minimum:
        raise ValueError(f"{field} below {minimum}")
    return value


def read_runs(path):
    accepted, excluded, errors = [], [], []
    seen_ids, seen_repeats = set(), set()
    group_signatures = {}
    group_actual_inputs = {}
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        missing = set(FIELDS)-set(header)
        if missing or len(header) != len(set(header)):
            return [], [], [f"missing fields {sorted(missing)} or duplicate header"]
        for raw in reader:
            line = reader.line_num
            try:
                if None in raw:
                    raise ValueError("too many CSV cells")
                row = {k: str(v or "").strip() for k, v in raw.items()}
                for f in ["run_id", "analysis_group", "repeat_id", "source_file"]:
                    if not row[f]:
                        raise ValueError(f"empty {f}")
                if row["run_id"] in seen_ids:
                    raise ValueError("duplicate run_id")
                seen_ids.add(row["run_id"])
                if row["mode"] not in MODES:
                    raise ValueError("mode must be mixed or disagg")
                for f in ["concurrency", "target_input_tokens", "target_output_tokens"]:
                    row[f] = parse_integer(row, f, 1)
                if row["target_output_tokens"] < 2:
                    raise ValueError("target output must be >=2")
                if row["scope"] != "full_requests":
                    raise ValueError("scope must be full_requests")
                if row["workload"] not in {"burst", "closed_loop", "open_loop", "unknown"}:
                    raise ValueError("invalid workload")
                if row["run_status"] not in {"completed", "failed", "interrupted"}:
                    raise ValueError("invalid run_status")
                signature = (row["target_input_tokens"], row["workload"], row["scope"])
                g = row["analysis_group"]
                if g in group_signatures and group_signatures[g] != signature:
                    raise ValueError("analysis_group mixes input/workload/scope")
                group_signatures[g] = signature
                repeat = (g, row["mode"], row["concurrency"], row["target_output_tokens"], row["repeat_id"])
                if repeat in seen_repeats:
                    raise ValueError("duplicate repeat_id in condition")
                seen_repeats.add(repeat)
                for f in ["valid_tpot_requests", "success_requests", "failed_requests",
                          "actual_input_mismatches", "actual_input_unknown"]:
                    row[f] = parse_integer(row, f)
                if row["valid_tpot_requests"] > row["success_requests"]:
                    raise ValueError("valid TPOT count exceeds successes")
                reasons = []
                if row["run_status"] != "completed":
                    reasons.append("run_not_completed")
                if not row["valid_tpot_requests"]:
                    reasons.append("no_valid_tpot")
                if row["valid_tpot_requests"] != row["success_requests"]:
                    reasons.append("some_successes_have_undefined_tpot")
                row["model_input_tokens"] = row["target_input_tokens"]
                actual_lo, actual_hi = row.get("actual_input_min", ""), row.get("actual_input_max", "")
                if bool(actual_lo) != bool(actual_hi):
                    raise ValueError("both actual_input_min and actual_input_max are required when supplied")
                if actual_lo:
                    input_lo = parse_integer(row, "actual_input_min", 1)
                    input_hi = parse_integer(row, "actual_input_max", 1)
                    if input_lo > input_hi:
                        raise ValueError("actual input min exceeds max")
                    if input_lo == input_hi:
                        row["model_input_tokens"] = input_lo
                    else:
                        reasons.append("actual_input_not_fixed")
                    if not row["actual_input_mismatches"] and (input_lo != row["target_input_tokens"] or input_hi != row["target_input_tokens"]):
                        raise ValueError("actual input range contradicts zero input mismatches")
                if row["actual_input_unknown"] or (row["actual_input_mismatches"] and not actual_lo):
                    reasons.append("actual_input_not_verified_fixed")
                if row["actual_output_min"] == "" or row["actual_output_max"] == "":
                    reasons.append("actual_output_unknown")
                else:
                    lo = parse_integer(row, "actual_output_min")
                    hi = parse_integer(row, "actual_output_max")
                    if lo > hi:
                        raise ValueError("actual output min exceeds max")
                    if lo != row["target_output_tokens"] or hi != row["target_output_tokens"]:
                        reasons.append("actual_output_not_fixed_target")
                if row["request_mean_tpot_ms"] == "":
                    reasons.append("metric_missing")
                else:
                    y = float(row["request_mean_tpot_ms"])
                    if not math.isfinite(y) or y <= 0:
                        raise ValueError("TPOT must be finite and positive")
                    row["value"] = y
                if reasons:
                    excluded.append(dict(line=line, run_id=row["run_id"], reason=";".join(reasons),
                                         source_file=row["source_file"]))
                else:
                    actual_s = row["model_input_tokens"]
                    if g in group_actual_inputs and group_actual_inputs[g] != actual_s:
                        raise ValueError("analysis_group mixes actual fixed input lengths; split groups")
                    group_actual_inputs[g] = actual_s
                    accepted.append(row)
            except (ValueError, TypeError) as exc:
                if len(errors) < 200:
                    errors.append(f"line {line}: {exc}")
    return accepted, excluded, errors


def aggregate(runs):
    cells = defaultdict(list)
    for run in runs:
        cells[(run["analysis_group"], run["concurrency"], run["target_output_tokens"], run["mode"])].append(run)
    output, series = [], defaultdict(list)
    by_condition = defaultdict(dict)
    for (group, concurrency, length, mode), rs in sorted(cells.items()):
        value = statistics.mean(r["value"] for r in rs)
        inputs = rs[0]["model_input_tokens"]
        output.append(dict(analysis_group=group, concurrency=concurrency, target_output_tokens=length,
                           mode=mode, target_input_tokens=rs[0]["target_input_tokens"],
                           actual_model_input_tokens=inputs, mean_tpot_ms=value,
                           n_runs=len(rs), run_sd_ms=statistics.stdev(r["value"] for r in rs) if len(rs)>1 else None,
                           success_requests=sum(r["success_requests"] for r in rs),
                           failed_requests=sum(r["failed_requests"] for r in rs),
                           workload=rs[0]["workload"], run_ids=";".join(sorted(r["run_id"] for r in rs))))
        point = dict(length=length, input=inputs, value=value)
        series[(group, concurrency, mode)].append(point)
        by_condition[(group, concurrency, length)][mode] = point
    for (group, concurrency, length), sides in sorted(by_condition.items()):
        if "mixed" in sides and "disagg" in sides:
            point = dict(length=length, input=sides["mixed"]["input"],
                         value=sides["mixed"]["value"]-sides["disagg"]["value"])
            series[(group, concurrency, "gap_mixed_minus_disagg")].append(point)
    return output, series


def model_series(series):
    fits, predictions, residuals, tails, notes = [], [], [], [], []
    for (group, concurrency, label), points in sorted(series.items()):
        points.sort(key=lambda r: r["length"])
        base = dict(analysis_group=group, concurrency=concurrency, series=label)
        if label in MODES:
            for left, right in zip(points, points[1:]):
                proxy = ((right["length"]-1)*right["value"]-(left["length"]-1)*left["value"])/(right["length"]-left["length"])
                tails.append(dict(base, lower_output=left["length"], upper_output=right["length"],
                                  tail_proxy_ms=proxy, interpretation="different_case_secant_not_token_trace"))
        for model, terms in MODELS.items():
            p = len(terms)
            if len(points) < max(4, p+2):
                notes.append(dict(base, model=model, note="insufficient_lengths_for_fit_and_LOO"))
                continue
            try:
                coefficients = fit(points, terms)
                ys = [r["value"] for r in points]
                errors = [r["value"]-predict(r, terms, coefficients) for r in points]
                loo_errors = []
                for index, row in enumerate(points):
                    trained = fit(points[:index]+points[index+1:], terms)
                    pred = predict(row, terms, trained)
                    loo_errors.append(row["value"]-pred)
                    predictions.append(dict(base, model=model, split="leave_one_length_out",
                                            held_out_output=row["length"], observed_ms=row["value"],
                                            predicted_ms=pred, error_ms=pred-row["value"],
                                            n_train_lengths=len(points)-1))
                long_errors = []
                if len(points)-2 >= p+1:
                    trained = fit(points[:-2], terms)
                    for row in points[-2:]:
                        pred = predict(row, terms, trained)
                        long_errors.append(row["value"]-pred)
                        predictions.append(dict(base, model=model, split="holdout_largest_two_lengths",
                                                held_out_output=row["length"], observed_ms=row["value"],
                                                predicted_ms=pred, error_ms=pred-row["value"],
                                                n_train_lengths=len(points)-2))
                mean_y = statistics.mean(ys)
                sst = sum((y-mean_y)**2 for y in ys)
                params = named_parameters(terms, coefficients)
                without_shortest = named_parameters(terms, fit(points[1:], terms))
                without_longest = named_parameters(terms, fit(points[:-1], terms))
                fits.append(dict(base, model=model, n_lengths=len(points), n_parameters=p, **params,
                                 train_rmse_ms=math.sqrt(statistics.mean(e*e for e in errors)),
                                 train_r_squared=1-sum(e*e for e in errors)/sst if sst else None,
                                 loo_mae_ms=statistics.mean(abs(e) for e in loo_errors),
                                 loo_rmse_ms=math.sqrt(statistics.mean(e*e for e in loo_errors)),
                                 long_holdout_mae_ms=statistics.mean(abs(e) for e in long_errors) if long_errors else None,
                                 without_shortest_parameters=json.dumps(without_shortest, sort_keys=True),
                                 without_longest_parameters=json.dumps(without_longest, sort_keys=True),
                                 inference="descriptive_no_parameter_CI_no_causal_claim"))
                for row, error in zip(points, errors):
                    residuals.append(dict(base, model=model, output_tokens=row["length"],
                                          observed_ms=row["value"], predicted_ms=row["value"]-error,
                                          residual_ms=error))
            except ValueError as exc:
                notes.append(dict(base, model=model, note=str(exc)))
    return fits, predictions, residuals, tails, notes


def make_plan(out):
    rows = []
    for mode in MODES:
        for concurrency in CONCURRENCIES:
            for length in LENGTHS:
                rows.append(dict(case_id=f"{mode}_C{concurrency:02d}_I00512_O{length:05d}",
                                 mode=mode, target_input_tokens=512, target_output_tokens=length,
                                 concurrency=concurrency, planned_machine_count=1 if mode=="mixed" else 2,
                                 observation_status="not_observed_by_this_tool",
                                 long_holdout="yes" if length in LENGTHS[-2:] else "no"))
    write_csv(out/"planned_cases.csv", rows)
    write_csv(out/"model_runs_header.csv", [], FIELDS)
    write_json(out/"plan_manifest.json", dict(kind="planned_conditions_not_measurements", n_conditions=len(rows),
                                             holdout=LENGTHS[-2:], input=512, lengths=LENGTHS,
                                             concurrencies=CONCURRENCIES))


def self_test():
    import tempfile
    import unittest

    class Tests(unittest.TestCase):
        def points(self):
            # Explicit mathematical fixture, NOT benchmark observations.
            return [dict(length=l, input=512, value=10+1200/(l-1)+.2*(512+l/2)/1000) for l in LENGTHS]

        def test_qr_known_coefficients(self):
            result = fit(self.points(), MODELS["K3_combined"])
            self.assertAlmostEqual(result[0], 10, places=8)
            self.assertAlmostEqual(result[1]*512, 1200, places=7)
            self.assertAlmostEqual(result[2], .2, places=8)

        def test_length_holdout(self):
            result = model_series({("NUMERICAL_FIXTURE", 4, "mixed"): self.points()})
            fits, predictions, _, _, _ = result
            combined = next(r for r in fits if r["model"] == "K3_combined")
            self.assertLess(combined["long_holdout_mae_ms"], 1e-8)
            holdout = [r["held_out_output"] for r in predictions if r["split"]=="holdout_largest_two_lengths"]
            self.assertEqual(set(holdout), {32768, 65536})

        def test_rank_deficiency(self):
            with self.assertRaises(ValueError):
                least_squares([[1, 1], [1, 1]], [1, 2])

        def test_plan_80(self):
            with tempfile.TemporaryDirectory() as tmp:
                make_plan(Path(tmp))
                with (Path(tmp)/"planned_cases.csv").open(encoding="utf-8-sig", newline="") as f:
                    rows = list(csv.DictReader(f))
                self.assertEqual(len(rows), 80)
                self.assertEqual(len(set(r["case_id"] for r in rows)), 80)

        def test_small_grid_no_model(self):
            fits, _, _, _, notes = model_series({("FIXTURE", 1, "mixed"): self.points()[:3]})
            self.assertFalse(fits)
            self.assertEqual(len(notes), 4)

        def test_tail_constant_overhead_cancels(self):
            rows = [dict(length=l, input=512, value=10+1200/(l-1)) for l in LENGTHS]
            _, _, _, tails, _ = model_series({("FIXTURE", 1, "mixed"): rows})
            self.assertTrue(all(abs(r["tail_proxy_ms"]-10)<1e-9 for r in tails))

        def fixture_row(self):
            return dict(run_id="FIXTURE_NOT_MEASURED", analysis_group="FIXTURE", mode="mixed",
                        concurrency=1, target_input_tokens=512, target_output_tokens=512,
                        repeat_id="r1", run_status="completed", scope="full_requests", workload="burst",
                        request_mean_tpot_ms=10, valid_tpot_requests=2, success_requests=2,
                        failed_requests=0, actual_output_min=512, actual_output_max=512,
                        actual_input_mismatches=0, actual_input_unknown=0, source_file="MATHEMATICAL_FIXTURE")

        def test_accept_input(self):
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/"fixture.csv"
                write_csv(path, [self.fixture_row()], FIELDS)
                accepted, excluded, errors = read_runs(path)
                self.assertEqual(len(accepted), 1)
                self.assertFalse(excluded or errors)

        def test_fixed_chat_template_input_preserved(self):
            row=self.fixture_row()
            row.update(actual_input_mismatches=2, actual_input_min=528, actual_input_max=528)
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/"fixture.csv"
                write_csv(path, [row], FIELDS+["actual_input_min", "actual_input_max"])
                accepted, excluded, errors=read_runs(path)
                self.assertFalse(excluded or errors)
                self.assertEqual(accepted[0]["target_input_tokens"], 512)
                self.assertEqual(accepted[0]["model_input_tokens"], 528)
                cells, _=aggregate(accepted)
                self.assertEqual(cells[0]["actual_model_input_tokens"], 528)

        def test_unknown_input_excluded(self):
            row=self.fixture_row()
            row["actual_input_unknown"]=2
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/"fixture.csv"
                write_csv(path, [row], FIELDS)
                accepted, excluded, errors=read_runs(path)
                self.assertFalse(accepted or errors)
                self.assertEqual(len(excluded), 1)

        def test_duplicate_rejected(self):
            row=self.fixture_row()
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/"fixture.csv"
                write_csv(path, [row, row], FIELDS)
                _, _, errors=read_runs(path)
                self.assertTrue(errors)

        def test_cli_full_matrix(self):
            import contextlib
            import io
            from unittest.mock import patch
            rows=[]
            for mode in MODES:
                for concurrency in CONCURRENCIES:
                    for length in LENGTHS:
                        row=self.fixture_row()
                        row.update(run_id=f"FIXTURE_{mode}_{concurrency}_{length}", mode=mode,
                                   concurrency=concurrency, target_output_tokens=length,
                                   actual_output_min=length, actual_output_max=length,
                                   request_mean_tpot_ms=10+.1*(512+length/2)/1000+
                                   (1200/(length-1)+.1*(512+length/2)/1000 if mode=="mixed" else 0))
                        rows.append(row)
            with tempfile.TemporaryDirectory() as tmp:
                source=Path(tmp)/"MATHEMATICAL_FIXTURES.csv"
                destination=Path(tmp)/"result"
                write_csv(source, rows, FIELDS)
                argv=["model_cases.py", "analyze", "--runs", str(source), "--out", str(destination),
                      "--metric-semantics", "aisbench_response_tpot", "--timing-audited"]
                with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(), 0)
                manifest=json.loads((destination/"model_manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["accepted_runs"], 80)
                self.assertEqual(manifest["n_fits"], 60)
                self.assertEqual(manifest["metric_semantics"], "aisbench_response_tpot")
                with (destination/"model_fits.csv").open(encoding="utf-8-sig", newline="") as f:
                    self.assertEqual(len(list(csv.DictReader(f))), 60)

        def test_nonfinite_rejected(self):
            row=self.fixture_row()
            row["request_mean_tpot_ms"]="nan"
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/"fixture.csv"
                write_csv(path, [row], FIELDS)
                _, _, errors=read_runs(path)
                self.assertTrue(errors)

    suite=unittest.defaultTestLoader.loadTestsFromTestCase(Tests)
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    plan=sub.add_parser("make-plan")
    plan.add_argument("--out", required=True)
    analyze=sub.add_parser("analyze")
    analyze.add_argument("--runs", required=True)
    analyze.add_argument("--out", required=True)
    analyze.add_argument("--metric-semantics", required=True,
                         choices=["aisbench_response_tpot", "canonical_content_tpot"],
                         help="Keep response-event metrics separate from verified content-token metrics")
    analyze.add_argument("--timing-audited", action="store_true",
                         help="Only set after verifying the chosen metric formula, units, and timing endpoints")
    args=parser.parse_args()
    if args.command=="self-test":
        return self_test()
    if args.command=="analyze" and not args.timing_audited:
        parser.error("Audit metric semantics first; --timing-audited is factual, not a bypass switch")
    out=new_directory(args.out)
    if args.command=="make-plan":
        make_plan(out)
        print(f"Generated 80 planned conditions (NOT observations): {out}")
        return 0
    runs, excluded, errors=read_runs(args.runs)
    manifest=dict(created_utc=datetime.now(timezone.utc).isoformat(), arguments=vars(args),
                  input_sha256=sha256(args.runs), script_sha256=sha256(__file__),
                  accepted_runs=len(runs), excluded_runs=len(excluded), errors=errors,
                  caveats=["Metric semantics/independence cannot be verified from CSV alone.",
                           "All results are conditional on selected successful requests.",
                           "No bootstrap CI, proof of equivalence, or causal bottleneck attribution.",
                           "Tail proxies are different-case secants, not measured token suffixes."])
    write_csv(out/"excluded_runs.csv", excluded)
    if not errors and runs:
        cells, series=aggregate(runs)
        fits, predictions, residuals, tails, notes=model_series(series)
        for table in [cells, fits, predictions, residuals, tails, notes]:
            for row in table:
                row["metric_semantics"] = args.metric_semantics
        manifest["metric_semantics"] = args.metric_semantics
        manifest["physical_timing_warning"] = (
            "Response-event timing can include role/usage/network/handoff events; coefficients are not pure kernel costs."
            if args.metric_semantics == "aisbench_response_tpot" else
            "Content-token timing still includes client transport unless server timestamps are used.")
        write_csv(out/"case_means.csv", cells)
        write_csv(out/"model_fits.csv", fits)
        write_csv(out/"length_holdout_predictions.csv", predictions)
        write_csv(out/"model_residuals.csv", residuals)
        write_csv(out/"tail_proxy.csv", tails)
        write_csv(out/"model_notes.csv", notes)
        manifest.update(n_fits=len(fits), failed_requests_in_accepted_runs=sum(r["failed_requests"] for r in runs),
                        unknown_workload_runs=sum(r["workload"]=="unknown" for r in runs))
    elif not errors:
        errors.append("no eligible fixed-length, verified-input runs; retain descriptive analysis separately")
    manifest["valid"]=not errors
    write_json(out/"model_manifest.json", manifest)
    print(json.dumps(dict(valid=manifest["valid"], accepted_runs=len(runs), excluded_runs=len(excluded),
                          errors=errors[:10], output=str(out)), ensure_ascii=False))
    return 0 if manifest["valid"] else 2


if __name__=="__main__":
    try:
        sys.exit(main())
    except (OSError, UnicodeError, csv.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
