"""Repeatable CPU/RSS and inference benchmark for offline traffic models."""
from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import statistics
import threading
import time
from pathlib import Path
from typing import Any

import joblib
import numpy
import psutil
import sklearn

from src.analytics.compare_models import (MODEL_CONFIGS, PROJECT_ROOT, SEED,
    build_estimators, generate_comparison_figures, _save_json, _write_csv)
from src.analytics.feature_engineering import (build_features,
    leakage_safe_chronological_split, to_matrix)
from src.simulator.traffic_replay import TrafficReplay

OUT = PROJECT_ROOT / "results" / "metrics" / "temporal_corrected"
FIG = PROJECT_ROOT / "results" / "figures" / "temporal_corrected"
MODEL_OUT = PROJECT_ROOT / "models" / "comparison" / "temporal_corrected" / "benchmark"


def _quantile(values: list[float], q: float) -> float:
    if not values:
        raise ValueError("cannot calculate latency percentiles from no samples")
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _measure_fit(estimator: Any, x: list[list[float]], y: list[int], sample_interval: float = 0.005) -> dict:
    try:
        process = psutil.Process()
        cpu_before = process.cpu_times()
        cpu_error = None
    except Exception as exc:
        process = None
        cpu_before = None
        cpu_error = str(exc)
    rss_samples: list[int] = []
    stop = threading.Event()
    sampling_errors: list[str] = []

    def sample_rss() -> None:
        while not stop.is_set():
            try:
                if process is None:
                    sampling_errors.append("psutil process handle unavailable")
                    return
                rss_samples.append(process.memory_info().rss)
            except Exception as exc:  # reported as unavailable below
                sampling_errors.append(str(exc))
                return
            stop.wait(sample_interval)

    sampler = threading.Thread(target=sample_rss, name="benchmark-rss", daemon=True)
    wall_start = time.perf_counter_ns()
    sampler.start()
    estimator.fit(x, y)
    wall_end = time.perf_counter_ns()
    stop.set(); sampler.join()
    try:
        cpu_after = process.cpu_times() if process is not None else None
    except Exception as exc:
        cpu_after = None
        cpu_error = str(exc)
    wall_s = (wall_end - wall_start) / 1e9
    cpu_s = ((cpu_after.user + cpu_after.system) - (cpu_before.user + cpu_before.system)) if cpu_after is not None and cpu_before is not None else None
    mlp = estimator.named_steps["clf"] if hasattr(estimator, "named_steps") and "clf" in estimator.named_steps else estimator
    iterations = getattr(mlp, "n_iter_", getattr(mlp, "n_estimators_", None))
    if iterations is not None:
        arr = numpy.asarray(iterations)
        iterations = int(arr.item()) if arr.size == 1 else [int(v) for v in arr.ravel()]
    convergence = (iterations < getattr(mlp, "max_iter")) if isinstance(iterations, int) and hasattr(mlp, "max_iter") else None
    return {
        "training_wall_seconds": wall_s,
        "process_cpu_seconds": cpu_s,
        "cpu_measurement_error": cpu_error,
        "average_process_cpu_percent": (cpu_s / wall_s * 100.0) if wall_s and cpu_s is not None else None,
        "peak_process_rss_bytes": max(rss_samples) if rss_samples else None,
        "rss_sampling_error": "; ".join(sampling_errors) if sampling_errors else None,
        "converged": convergence,
        "iterations": iterations,
    }


def run_benchmark(readings=None, *, repetitions: int = 3, warmups: int = 20,
                  inference_repetitions: int = 1000, batch_repetitions: int = 100,
                  output_dir: Path = OUT, figure_dir: Path = FIG, model_dir: Path = MODEL_OUT) -> dict:
    if repetitions < 1 or warmups < 0 or inference_repetitions < 1 or batch_repetitions < 1:
        raise ValueError("repetitions and measured inference counts must be positive; warmups cannot be negative")
    readings = readings if readings is not None else TrafficReplay.from_settings().readings
    features = build_features(readings)
    train, test, split = leakage_safe_chronological_split(features)
    x_train, x_test = to_matrix(train), to_matrix(test)
    if not x_test:
        raise ValueError("empty held-out test partition")
    # Build candidates before timers so imports/construction are outside every fit boundary.
    factories = build_estimators()
    raw_runs, summaries, fitted = [], {}, {}
    model_dir.mkdir(parents=True, exist_ok=True)
    for name, prototype in factories.items():
        runs = []
        estimator = prototype
        for run_no in range(1, repetitions + 1):
            if run_no > 1:
                estimator = build_estimators()[name]
            measured = _measure_fit(estimator, x_train, list(train.labels))
            measured.update({"model": name, "run": run_no})
            runs.append(measured); raw_runs.append(measured)
        artifact = model_dir / MODEL_CONFIGS[name]["artifact"]
        expected_predictions = list(map(int, estimator.predict(x_test)))
        joblib.dump({"model_name": name, "configuration": MODEL_CONFIGS[name]["parameters"], "estimator": estimator,
                     "features": list(__import__("src.analytics.feature_engineering", fromlist=["FEATURE_NAMES"]).FEATURE_NAMES),
                     "train_size": len(train), "test_size": len(test), "split_metadata": split.as_dict(), "random_seed": SEED}, artifact)
        loaded_at = time.perf_counter_ns(); loaded = joblib.load(artifact); load_ms = (time.perf_counter_ns() - loaded_at) / 1e6
        estimator = loaded["estimator"]
        if list(map(int, estimator.predict(x_test))) != expected_predictions:
            raise RuntimeError(f"benchmark artifact prediction round-trip failed for {name}")
        for i in range(warmups): estimator.predict([x_test[i % len(x_test)]])
        single_ms = []
        for i in range(inference_repetitions):
            row = x_test[i % len(x_test)]
            begin = time.perf_counter_ns(); estimator.predict([row]); single_ms.append((time.perf_counter_ns() - begin) / 1e6)
        batch = x_test
        for _ in range(min(warmups, batch_repetitions)): estimator.predict(batch)
        batch_elapsed_s = []
        for _ in range(batch_repetitions):
            begin = time.perf_counter_ns(); estimator.predict(batch); batch_elapsed_s.append((time.perf_counter_ns() - begin) / 1e9)
        batch_total_s = sum(batch_elapsed_s)
        model_runs = runs
        vals = lambda key: [r[key] for r in model_runs if r[key] is not None]
        def aggregate(key):
            values = vals(key)
            return {"mean": statistics.mean(values), "stdev": statistics.stdev(values) if len(values) > 1 else 0.0, "runs": len(values)} if values else {"mean": None, "stdev": None, "runs": 0, "unavailable_reason": "measurement unavailable for all repetitions; see raw run error fields"}
        summaries[name] = {
            "training": {k: aggregate(k) for k in ("training_wall_seconds", "process_cpu_seconds", "average_process_cpu_percent", "peak_process_rss_bytes")},
            "convergence": {"converged": runs[-1]["converged"], "iterations": runs[-1]["iterations"]},
            "measurement_errors": {"cpu": [r["cpu_measurement_error"] for r in runs if r["cpu_measurement_error"]],
                                    "rss": [r["rss_sampling_error"] for r in runs if r["rss_sampling_error"]]},
            "inference": {"single_row_ms": {"median": statistics.median(single_ms), "p95": _quantile(single_ms, .95), "p99": _quantile(single_ms, .99), "max": max(single_ms), "samples": len(single_ms)}, "batch_ms_mean": statistics.mean(batch_elapsed_s) * 1000, "batch_samples_per_second": len(batch) * batch_repetitions / batch_total_s if batch_total_s else None, "batch_size": len(batch), "batch_repetitions": batch_repetitions, "model_load_ms": load_ms},
            "artifact": {"path": str(artifact), "size_bytes": artifact.stat().st_size, "size_mb": artifact.stat().st_size / (1024 * 1024)},
            "macro_f1": None,
        }
        fitted[name] = estimator
    # Macro F1 uses the same corrected held-out evaluation and model configs.
    from src.analytics.compare_models import calculate_metrics
    for name, estimator in fitted.items():
        metric = calculate_metrics(list(test.labels), list(map(int, estimator.predict(x_test))), list(train.labels))
        summaries[name]["macro_f1"] = metric["macro_f1"]
    cpu = platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER") or "unavailable"
    payload = {
        "metadata": {"python": platform.python_version(), "operating_system": platform.platform(), "cpu_count_logical": psutil.cpu_count(logical=True), "processor": cpu, "numpy": numpy.__version__, "scikit_learn": sklearn.__version__, "gpu_used": False, "gpu_usage": "not applicable; scikit-learn candidates ran on CPU", "split": split.as_dict(), "train_size": len(train), "test_size": len(test)},
        "methodology": {"training_repetitions": repetitions, "warmup_predictions": warmups, "single_row_inference_repetitions": inference_repetitions, "batch_repetitions": batch_repetitions, "training_boundary": "estimator.fit only; imports, feature construction and estimator construction excluded for every candidate", "wall_clock": "perf_counter_ns", "process_cpu": "process user+system CPU-time delta divided by fit wall-time; percent of one logical CPU and may exceed 100%", "peak_rss": "psutil RSS sampled every 5 ms during fit; sampling includes whole process and may miss shorter peaks", "inference": "single-row predictions are warmed up before timing; batch uses the whole held-out test partition", "machine_observations_only": True},
        "models": summaries,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _save_json(output_dir / "model_performance_benchmark.json", payload)
    _write_csv(output_dir / "model_performance_benchmark.csv", [{"model": n, "macro_f1": v["macro_f1"], **v["training"], **v["inference"]["single_row_ms"], "batch_samples_per_second": v["inference"]["batch_samples_per_second"], "model_load_ms": v["inference"]["model_load_ms"], "model_size_bytes": v["artifact"]["size_bytes"]} for n, v in summaries.items()])
    _write_csv(output_dir / "model_benchmark_runs.csv", raw_runs)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    names = list(summaries)
    fig, axes = plt.subplots(2, 3, figsize=(15, 8))
    charts = [("training_wall_seconds", "Training wall time (s)"), ("p50", "Single-row p50 latency (ms)"), ("p95", "Single-row p95 latency (ms)"), ("batch_samples_per_second", "Batch throughput (samples/s)"), ("peak_process_rss_bytes", "Peak process RSS (bytes)"), ("size_bytes", "Artifact size (bytes)")]
    for ax, (key, label) in zip(axes.flat, charts):
        if key == "training_wall_seconds": vals = [summaries[n]["training"][key]["mean"] for n in names]
        elif key in ("p50", "p95"): vals = [summaries[n]["inference"]["single_row_ms"]["median" if key == "p50" else "p95"] for n in names]
        elif key == "batch_samples_per_second": vals = [summaries[n]["inference"][key] for n in names]
        elif key == "peak_process_rss_bytes": vals = [summaries[n]["training"][key]["mean"] for n in names]
        else: vals = [summaries[n]["artifact"][key] for n in names]
        ax.bar(names, vals); ax.set_title(label); ax.tick_params(axis="x", rotation=25); ax.set_ylabel(label)
    fig.tight_layout(); figure_dir.mkdir(parents=True, exist_ok=True); p = figure_dir / "model_performance_benchmark.png"; fig.savefig(p, dpi=140); plt.close(fig)
    fig, ax = plt.subplots(figsize=(8, 5)); ax.scatter([summaries[n]["inference"]["single_row_ms"]["median"] for n in names], [summaries[n]["macro_f1"] for n in names])
    for n in names: ax.annotate(n, (summaries[n]["inference"]["single_row_ms"]["median"], summaries[n]["macro_f1"]))
    ax.set_xlabel("Single-row inference median (ms)"); ax.set_ylabel("Macro F1"); ax.set_title("Macro F1 vs inference latency"); fig.tight_layout(); p2 = figure_dir / "model_macro_f1_vs_latency.png"; fig.savefig(p2, dpi=140); plt.close(fig)
    print(f"Benchmark complete: {output_dir}; {repetitions} training runs/model; CPU RSS measurements are host-specific.")
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark offline traffic models")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=20)
    parser.add_argument("--inference-repetitions", type=int, default=1000)
    parser.add_argument("--batch-repetitions", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        run_benchmark(repetitions=args.repetitions, warmups=args.warmups,
                      inference_repetitions=args.inference_repetitions, batch_repetitions=args.batch_repetitions)
        return 0
    except Exception as exc:
        print(f"Benchmark failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
