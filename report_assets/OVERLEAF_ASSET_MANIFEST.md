## Organized project paths

Graph PNGs: report_assets/graphs/. Dashboard captures: report_assets/dashboard/. Diagrams, logo and report files: directly under report_assets/.

# Overleaf asset manifest

Dashboard captures are copied byte-for-byte from real project screenshots. Corrected ML plots use results/metrics/temporal_corrected. Benchmark and traffic comparison graphs are generated from saved CSV/JSON outputs.

| Filename | Purpose / suggested caption | Chapter | Source | Priority |
|---|---|---|---|---|
| amrita-logo.png | Institution mark on cover/declaration | Front matter | Existing report asset | Required if referenced |
| fig_architecture.png | Simulated controller system architecture | Architecture | Existing diagram | Required |
| fig_workflow.png | Readings-to-actuation workflow | Architecture | Existing diagram | Required; correction advised |
| dashboard_overview.png | Running dashboard overview | Dashboard evidence | Genuine capture | Required |
| dashboard_traffic_analytics.png | Queue and per-approach analytics | Dashboard evidence | Genuine capture | Recommended |
| dashboard_signal_timing.png | Active phase, timing and approach states | Dashboard evidence | Genuine capture | Recommended |
| dashboard_testing_lab.png | Testing scenario and observed output | Dashboard evidence | Genuine capture | Recommended |
| dashboard_stop_reset_ack.png | Stop/Reset and actuator-reported RED states | Safety | Genuine capture; UI has no command-ID ledger | Recommended |
| dashboard_ack_fault_handling.png | Withheld ACK leaves Stop unconfirmed | Safety | Genuine controlled fault capture | Optional; call it unconfirmed |
| model_comparison_metrics.png | Four-model accuracy, balanced accuracy and macro F1 | ML | Corrected temporal outputs | Required |
| model_comparison_confusion_matrices.png | Four held-out confusion matrices | ML | Corrected temporal outputs | Recommended |
| model_comparison_per_class_f1.png | Per-class F1, including weak HIGH detection | ML | Corrected temporal outputs | Recommended |
| model_vs_baseline.png and PDF | Test accuracy vs LOW-only baseline 0.678 | ML | Corrected temporal metrics CSV | Recommended |
| fixed_vs_adaptive.png and PDF | Delay, average/max queue, Jain fairness, max red wait | Closed-loop | Corrected summary JSON | Required; simulation |
| benchmark_fit_time.png and PDF | Mean fit wall time, three runs | Benchmark | Benchmark CSV | Recommended |
| benchmark_peak_rss.png and PDF | Peak process RSS in MiB | Benchmark | Benchmark CSV | Recommended; whole process RSS |
| benchmark_latency.png and PDF | p50/p95 single-record latency | Benchmark | Benchmark CSV | Recommended |
| benchmark_artifact_size.png and PDF | Serialized artifact size in decimal MB | Benchmark | Benchmark CSV | Recommended |

## Accuracy notes

The existing fig_workflow.png places ML analysis in the live path to adaptive control. The implementation uses a deterministic rule-based live controller; ML is offline decision-support. Keep the filename but revise the diagram to show ML as a separate offline branch. The existing fig_architecture.png separates the analytics/dashboard read path from the controller/actuator and is consistent with implementation.

Do not use the old root fixed_vs_adaptive.png: values differ from final output (including 161 s max red wait) and it omits fairness. The packaged graph uses the final 164 s trade-off. Do not use old root model_vs_baseline.png (train=3497/test=1499); this package uses the corrected temporal results. Throughput is excluded because the batch denominator is not clearly defined in the available benchmark output. The fault screenshot shows an unconfirmed operation; it must not be captioned as an acknowledged-safe transition.


## Existing LaTeX reference audit

The current IOT_Report.tex has figure calls for fig_architecture.png, fig_workflow.png, report_assets/dashboard/dashboard_overview.png, ml_confusion_matrices.png, ml_accuracy_comparison.png, ml_latency_comparison.png, and dashboard evidence under report_assets/dashboard/. The two diagram basenames and dashboard overview are available. The ML basenames are missing from the Overleaf root; use the corrected assets in this package and the supplied snippets. The source also refers to dashboard paths under report_assets/dashboard/, while this upload bundle is intentionally flat; snippets use the flat filenames.

Figure labels in the supplied snippets were checked for duplicates; none were found. Each snippet image exists in the package. The source's existing figure captions are present; the proposed fixed/adaptive result and benchmark graph insertions add new evidence without replacing sections. Unselected old figures excluded: root confusion_matrix.png and per_class_metrics.png are single/old-split versions; root model_vs_baseline.png is outdated; root fixed_vs_adaptive.png conflicts with corrected metrics and omits the fairness measure.
