"""Model evaluation figures rendered from the saved metrics (no retraining).

The classifier's metrics are already written to ``results/metrics`` by
``run_evaluation``. Those numbers are hard to read as a JSON blob, so this
module turns them into the standard evaluation pictures:

* ``confusion_matrix.png`` - where the predictions actually go;
* ``per_class_metrics.png`` - precision/recall/F1 per congestion level;
* ``model_vs_baseline.png`` - accuracy next to the majority-class baseline.

Run standalone to (re)draw them from an existing metrics file::

    python -m src.analytics.figures

Nothing is trained or re-evaluated here and the dataset is never touched; the
figures are a pure function of ``model_metrics.json``.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIGURES_DIR = PROJECT_ROOT / "results" / "figures"
METRICS_PATH = PROJECT_ROOT / "results" / "metrics" / "model_metrics.json"

LABEL_COLORS = ("#19e3d0", "#f6b73c", "#ef6f6c")


def _pyplot():
    """Import matplotlib lazily; return the pyplot module or None if absent."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        return plt
    except Exception:
        return None


def plot_confusion_matrix(report: Dict, out_dir: Path) -> Optional[Path]:
    plt = _pyplot()
    if plt is None:
        return None
    labels: Sequence[str] = report["labels"]
    grid = report["confusion"]
    size = len(labels)
    fig, ax = plt.subplots(figsize=(5.5, 5))
    matrix = [[grid[r][c] for c in range(size)] for r in range(size)]
    image = ax.imshow(matrix, cmap="YlGnBu")
    ax.set_xticks(range(size), labels)
    ax.set_yticks(range(size), labels)
    ax.set_xlabel("predicted")
    ax.set_ylabel("actual")
    ax.set_title(
        f"Congestion confusion matrix\n"
        f"accuracy {report['accuracy']:.3f} vs baseline {report['baseline_accuracy']:.3f}"
    )
    peak = max((v for row in matrix for v in row), default=0)
    for r in range(size):
        for c in range(size):
            value = matrix[r][c]
            ax.text(
                c, r, str(value),
                ha="center", va="center",
                color="white" if peak and value > peak / 2 else "#0b2b33",
                fontweight="bold" if r == c else "normal",
            )
    fig.colorbar(image, ax=ax, fraction=0.046, pad=0.04, label="count")
    fig.tight_layout()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "confusion_matrix.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def plot_per_class_metrics(report: Dict, out_dir: Path) -> Optional[Path]:
    plt = _pyplot()
    if plt is None:
        return None
    per_class = report["per_class"]
    names = list(report["labels"])
    metrics = ("precision", "recall", "f1")
    width = 0.25
    fig, ax = plt.subplots(figsize=(7, 4.5))
    for i, metric in enumerate(metrics):
        values = [per_class[n][metric] for n in names]
        offsets = [x + (i - 1) * width for x in range(len(names))]
        bars = ax.bar(offsets, values, width, label=metric, color=LABEL_COLORS[i % len(LABEL_COLORS)])
        for bar, value in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, value + 0.02, f"{value:.2f}",
                    ha="center", va="bottom", fontsize=8)
    supports = [per_class[n]["support"] for n in names]
    ax.set_xticks(range(len(names)), [f"{n}\n(n={s})" for n, s in zip(names, supports)])
    ax.set_ylim(0, 1.08)
    ax.set_ylabel("score")
    ax.set_title("Per-class precision / recall / F1 (held-out test set)")
    ax.legend(ncol=3, loc="upper right")
    fig.tight_layout()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "per_class_metrics.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def plot_model_vs_baseline(report: Dict, out_dir: Path) -> Optional[Path]:
    plt = _pyplot()
    if plt is None:
        return None
    names = ["accuracy", "macro F1"]
    model = [report["accuracy"], report["f1_macro"]]
    baseline = [report["baseline_accuracy"], 0.0]
    width = 0.35
    fig, ax = plt.subplots(figsize=(6, 4.5))
    left = ax.bar([x - width / 2 for x in range(len(names))], model, width,
                  label="model", color="#19e3d0")
    right = ax.bar([x + width / 2 for x in range(len(names))], baseline, width,
                   label=f"baseline ({report['baseline_label']})", color="#7f909d")
    for bars in (left, right):
        for bar in bars:
            value = bar.get_height()
            if value:
                ax.text(bar.get_x() + bar.get_width() / 2, value + 0.01, f"{value:.3f}",
                        ha="center", va="bottom", fontsize=8)
    ax.set_xticks(range(len(names)), names)
    ax.set_ylim(0, 1.0)
    ax.set_ylabel("score")
    verdict = "beats baseline" if report.get("beats_baseline") else "does not beat baseline"
    ax.set_title(
        f"Model vs majority-class baseline - {verdict}\n"
        f"train={report['train_size']} test={report['test_size']}"
    )
    ax.legend(loc="upper right")
    fig.tight_layout()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "model_vs_baseline.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def generate(report: Dict, out_dir: Path = FIGURES_DIR) -> List[Path]:
    """Render every model figure; returns the paths actually written."""
    written: List[Path] = []
    for render in (plot_confusion_matrix, plot_per_class_metrics, plot_model_vs_baseline):
        path = render(report, out_dir)
        if path is not None:
            written.append(path)
    return written


def load_metrics(path: Path = METRICS_PATH) -> Optional[Dict]:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Render model evaluation figures from saved metrics.")
    parser.add_argument("--metrics", type=Path, default=METRICS_PATH, help="path to model_metrics.json")
    args = parser.parse_args(argv)
    report = load_metrics(args.metrics)
    if report is None:
        print(f"no metrics at {args.metrics}; run src.analytics.run_evaluation --train first")
        return 1
    written = generate(report)
    if not written:
        print("matplotlib is unavailable; no figures written")
        return 0
    for path in written:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
