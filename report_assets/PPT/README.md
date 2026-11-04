# Presentation asset selections

This folder contains **unchanged copies** of the strongest existing report assets for the 16-slide Adaptive Traffic Signal Controller presentation. Originals remain in their original locations under `report_assets/`.

## Selected assets and slide mapping

| Selected file | Recommended slide | Why it fits | Use guidance |
|---|---:|---|---|
| `diagrams/fig_architecture.png` | 6 — System architecture | Shows the simulator, MQTT broker, adaptive controller, Node-RED, SQLite dashboard, and simulated signal/actuator paths in one project-specific diagram. | Use as the architecture visual **instead of** the deck's editable architecture drawing; do not show both. The PPT's native drawing is preferable when editability is required. |
| `diagrams/fig_workflow.png` | 7 — Workflow | Summarizes the implemented stages from data preparation and simulation through MQTT, Node-RED, ML analysis, and evaluation. | Use as a visual replacement for the editable workflow only if the raster image remains readable at presentation size. Do not place both. |
| `graphs/model_comparison_metrics.png` | 12 — ML results | Gives a compact comparison of accuracy, balanced accuracy, and macro F1 across the four models. | The slide table must remain the source for HIGH recall and exact percentages; this graph does not show HIGH recall. Replace the matching chart rather than adding a duplicate chart. |
| `graphs/benchmark_latency.png` | 13 — Computational benchmark | Shows p50 and p95 prediction latency for all four models, making the latency trade-off easy to compare. | Keep the benchmark table for fit time, RSS, and artifact size; use this graph in place of a latency chart. |
| `graphs/benchmark_peak_rss.png` | 13 — Computational benchmark | Shows the verified 153.9, 249.5, 270.7, and 273.5 decimal-MB process peak RSS values. | Label as whole-process RSS in decimal MB. Do not describe it as model-only memory. Avoid repeating the same bars if the chart is already on the slide. |
| `graphs/fixed_vs_adaptive.png` | 14 — Closed-loop evaluation | Combines fixed/adaptive delay, queue, maximum queue, Jain fairness, and maximum red wait, including the red-wait trade-off. | Use in place of the slide's matching evaluation chart, not alongside a second copy of the same metrics. Keep the ACK/fault-handling evidence and its separate explanation. |
| `screenshots/dashboard_overview.png` | 15 — Dashboard | Shows the real application overview, live traffic, signal phase/handshake, queue history, and timing decisions. | Use as the primary overview screenshot. |
| `screenshots/dashboard_testing_lab.png` | 15 — Dashboard | Shows the actual Testing Lab and configurable approach inputs/control area. | Use as the Testing Lab screenshot. |
| `screenshots/dashboard_ack_fault_handling.png` | 15 — Dashboard | Shows actual signal-phase and handshake status alongside the dashboard/testing interface. | Treat this as ACK-status evidence. It does **not** by itself prove a missing-ACK fault scenario; explain that fault behavior from the verified test/evaluation evidence. |

## Review of other source assets

The source `report_assets/` tree was inventoried and its 18 raster images reviewed. The following were not copied because they add redundancy or are less necessary for a readable 16-slide deck:

- `graphs/benchmark_fit_time.png` and `graphs/benchmark_artifact_size.png`: their exact values are already in the benchmark table; the selected latency and RSS graphs add more distinct context.
- `graphs/model_comparison_confusion_matrices.png`, `graphs/model_comparison_per_class_f1.png`, and `graphs/model_vs_baseline.png`: useful for a deeper ML appendix, but redundant for the main 16-slide narrative, which already has the metric comparison table/chart and baseline callout.
- `dashboard/dashboard_signal_timing.png` and `dashboard/dashboard_stop_reset_ack.png`: overlap substantially with the selected overview and ACK-status screenshots.
- `dashboard/dashboard_traffic_analytics.png`: narrower crop that repeats the queue-history information visible in the overview.
- `amrita-logo.png`: branding asset, not a graph, chart, diagram, or application screenshot; the title slide already identifies the institution.
- `IOT_Report.pdf` and `IOT_Report.tex`: report documents, not presentation image assets.

The selected assets are copied byte-for-byte. No image was created, cropped, recolored, relabeled, or otherwise modified. The PPTX uses editable native elements for several diagrams and charts; use the copied raster versions as alternatives, not as extra duplicate visuals.
