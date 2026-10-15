"""Reproducible analytics/evaluation pipeline (single entry point).

    python -m src.analytics.run_evaluation [--train] [--scenario NAME] [--limit N]

Runs, in order:

1. train + evaluate the congestion classifier on the frozen dataset
   (chronological split, majority-class baseline) and write the metrics;
2. run the fixed-time vs adaptive queue comparison on the same arrivals and
   write the metrics table and a chart.

Everything is written under ``results/`` (never into ``dataset/``). The dataset
is only read. Use ``--train`` to (re)fit the model; otherwise the existing model
is loaded if present.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Optional, Sequence

from src.analytics.congestion import approach_stats
from src.analytics.evaluate_model import EvaluationReport, evaluate
from src.analytics.figures import generate as generate_figures
from src.analytics.simulation import compare, comparison_table
from src.controller.timing_constraints import TimingConfig
from src.simulator.traffic_replay import DEFAULT_CSV_PATH, TrafficReplay

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "results"
METRICS_DIR = RESULTS_DIR / "metrics"
FIGURES_DIR = RESULTS_DIR / "figures"


def _load_readings(scenario: Optional[str], limit: Optional[int]):
    replay = TrafficReplay.from_settings(scenario=scenario)
    readings = replay.readings
    if limit:
        readings = readings[:limit]
    return readings


def _write_csv(path: Path, rows: Sequence[dict], fieldnames: Sequence[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def run_model(readings, *, train: bool) -> Optional[EvaluationReport]:
    """Train (optionally) and evaluate the classifier; None if not enough data."""
    from src.analytics import train_model as trainer
    from src.analytics.feature_engineering import build_features, chronological_split
    from src.analytics.predict import Predictor

    try:
        if train or not trainer.DEFAULT_MODEL_PATH.exists():
            model, train_set, test_set = trainer.train(readings)
        else:
            predictor = Predictor.load()
            model = trainer.TrainedModel(
                pipeline=predictor.pipeline,
                features=predictor.features,
                labels=predictor.labels,
                train_size=0,
                test_size=0,
            )
            train_set, test_set = chronological_split(build_features(readings))
    except ValueError:
        return None

    report = evaluate(model, train_set, test_set)
    (METRICS_DIR).mkdir(parents=True, exist_ok=True)
    (METRICS_DIR / "model_metrics.json").write_text(
        json.dumps(report.as_dict(), indent=2), encoding="utf-8"
    )
    _write_csv(
        METRICS_DIR / "model_per_class.csv",
        [
            {"label": name, **values}
            for name, values in report.per_class.items()
        ],
        ["label", "support", "precision", "recall", "f1"],
    )
    return report


def run_comparison(readings, config: TimingConfig):
    metrics = compare(readings, config)
    rows = comparison_table(metrics)
    _write_csv(METRICS_DIR / "fixed_vs_adaptive.csv", rows, ["metric", "fixed", "adaptive", "delta"])
    _write_csv(
        METRICS_DIR / "approach_stats.csv",
        [s.as_dict() for s in approach_stats(readings).values()],
        ["approach", "samples", "avg_queue", "max_queue", "avg_arrival", "avg_wait", "max_wait", "congestion"],
    )
    _write_csv(
        METRICS_DIR / "approach_fairness.csv",
        [
            {
                "approach": a,
                "fixed_avg_delay": round(metrics["fixed"].per_approach_delay[a], 3),
                "adaptive_avg_delay": round(metrics["adaptive"].per_approach_delay[a], 3),
            }
            for a in metrics["fixed"].per_approach_delay
        ],
        ["approach", "fixed_avg_delay", "adaptive_avg_delay"],
    )
    _plot_comparison(metrics)
    return metrics


def _plot_comparison(metrics) -> Optional[Path]:
    """Bar chart of the key fixed-vs-adaptive metrics (matplotlib, optional)."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:  # pragma: no cover - chart is a nice-to-have
        return None

    fixed = metrics["fixed"].as_dict()
    adaptive = metrics["adaptive"].as_dict()
    grey, teal, red = "#7f909d", "#19e3d0", "#ef6f6c"
    higher_is_better = {"jain_fairness", "served_fraction", "throughput_per_min"}

    def _grouped(ax, keys, title, ylabel, fmt="{:.2f}"):
        x = list(range(len(keys)))
        width = 0.38
        bars_f = ax.bar([i - width / 2 for i in x], [fixed[k] for k in keys], width, label="fixed", color=grey)
        bars_a = ax.bar([i + width / 2 for i in x], [adaptive[k] for k in keys], width, label="adaptive", color=teal)
        for bars in (bars_f, bars_a):
            for bar in bars:
                ax.annotate(fmt.format(bar.get_height()),
                            (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                            ha="center", va="bottom", fontsize=8)
        ax.set_xticks(list(x))
        ax.set_xticklabels([k.replace("_", " ") for k in keys], fontsize=8)
        ax.set_title(title, fontsize=10)
        ax.set_ylabel(ylabel, fontsize=8)
        ax.margins(y=0.18)
        ax.legend(fontsize=8)

    fig, axes = plt.subplots(2, 2, figsize=(11.5, 8.5))
    ax_delay, ax_queue, ax_ratio, ax_delta = axes.ravel()

    _grouped(ax_delay, ["avg_delay_per_vehicle", "worst_approach_delay", "max_red_wait_sec"],
             "Delay / worst wait (lower is better)", "seconds")
    _grouped(ax_queue, ["avg_queue", "max_queue"], "Queues (lower is better)", "vehicles", "{:.0f}")
    _grouped(ax_ratio, ["jain_fairness", "served_fraction"],
             "Fairness / service (higher is better)", "score (0-1)", "{:.3f}")

    # The trade-off, all on one comparable axis: percent change vs fixed-time.
    delta_keys = ["avg_delay_per_vehicle", "vehicle_seconds_delay", "avg_queue", "max_queue",
                  "worst_approach_delay", "max_red_wait_sec", "cycles", "phase_changes",
                  "jain_fairness", "served_fraction"]
    pct = []
    for k in delta_keys:
        base = fixed[k]
        pct.append(0.0 if not base else (adaptive[k] - base) / base * 100.0)
    higher_better = {"jain_fairness", "served_fraction"}
    colors_d = []
    for k, v in zip(delta_keys, pct):
        if v == 0:
            colors_d.append(grey)
        else:
            better = v > 0 if k in higher_better else v < 0
            colors_d.append(teal if better else red)
    ax_delta.barh(range(len(delta_keys)), pct, color=colors_d)
    ax_delta.axvline(0, color="#0b2b33", linewidth=0.8)
    ax_delta.set_yticks(range(len(delta_keys)), [k.replace("_", " ") for k in delta_keys], fontsize=8)
    ax_delta.invert_yaxis()
    ax_delta.set_xlabel("change vs fixed-time (%)", fontsize=8)
    ax_delta.set_title("Adaptive trade-off (teal = better, red = worse)", fontsize=10)
    ax_delta.margins(x=0.18)
    for i, v in enumerate(pct):
        ax_delta.annotate(f"{v:+.0f}%", (v, i), ha="right" if v < 0 else "left",
                          va="center", fontsize=8, color="#0b2b33")

    fig.suptitle("Fixed-time vs adaptive traffic control (identical arrivals)", fontsize=13)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    out = FIGURES_DIR / "fixed_vs_adaptive.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out



def _print_summary(metrics, report: Optional[EvaluationReport]) -> None:
    fixed = metrics["fixed"].as_dict()
    adaptive = metrics["adaptive"].as_dict()
    print("Fixed-time vs adaptive (identical arrivals):")
    print(f"  {'metric':<26}{'fixed':>12}{'adaptive':>12}")
    for key in ("avg_delay_per_vehicle", "avg_queue", "max_queue", "served_fraction", "cycles",
                "throughput_per_min", "jain_fairness", "worst_approach_delay", "max_red_wait_sec"):
        print(f"  {key:<26}{fixed[key]:>12}{adaptive[key]:>12}")
    if report is not None:
        print(
            f"\nCongestion model: accuracy={report.accuracy:.3f} "
            f"baseline={report.baseline_accuracy:.3f} "
            f"f1_macro={report.f1_macro:.3f} beats_baseline={report.beats_baseline}"
        )
        print(f"  train={report.train_size} test={report.test_size}")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the analytics/evaluation pipeline.")
    parser.add_argument("--train", action="store_true", help="(re)fit the congestion model")
    parser.add_argument("--scenario", default=None, help="restrict to one dataset scenario")
    parser.add_argument("--limit", type=int, default=None, help="only use the first N readings")
    parser.add_argument("--no-model", action="store_true", help="skip the ML model step")
    args = parser.parse_args(argv)

    readings = _load_readings(args.scenario, args.limit)
    if not readings:
        print(f"no readings loaded from {DEFAULT_CSV_PATH}")
        return 1

    config = TimingConfig.from_settings()
    report = None if args.no_model else run_model(readings, train=args.train)
    if report is not None:
        generate_figures(report.as_dict())
    metrics = run_comparison(readings, config)
    _print_summary(metrics, report)
    print(f"\nresults written under {RESULTS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
