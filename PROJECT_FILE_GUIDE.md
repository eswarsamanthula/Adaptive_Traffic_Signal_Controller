# Project File Guide — Team 9 Adaptive Traffic Signal Controller

The single detailed reference for this project: what every file is, how the
system runs, what data/artefacts exist, what was removed and why, and the current
status. For the short version see [README.md](README.md).

Facts in this guide were verified on 2026-10-15 against the working tree and a
green test run (`python -m unittest discover -s tests` → **179 tests OK**).

---

## A. Overview and architecture

A **local, synthetic-data** IoT pipeline. A frozen dataset is replayed (or
generated) over MQTT, a Python service makes safe adaptive signal-timing
decisions, a simulated actuator acknowledges them, and Node-RED stores and
displays the result. No real traffic hardware is controlled.

```
dataset/*.csv ──► src.simulator (replay + scenarios)
                        │  team9/traffic/sensors/#            (MQTT)
                        ▼
        ┌───────────────┴───────────────────────────────┐
        │                                                │
   src.service.controller_service                 Node-RED (node_red/flows.json)
     • AdaptiveController (owns phase machine)        • ingest / validate
     • decision_engine (demand → green, clamped)      • SQLite persistence
     • src.analytics (model, decision support)        • /dashboard (primary) + /ui (secondary)
        │  signal/state, signal/phase, timing/decisions
        │  commands/{approach}  ⇄  acks/{approach}
        ▼
   src.actuator.actuator_node  (validate → apply/reject → ack → heartbeat)
```

**Single source of truth.** The Python `AdaptiveController` owns the phase
machine. Node-RED only ingests, validates, stores, displays and exposes the
Testing-Lab controls; it does **not** run a second controller.

**Data flow, end to end**

1. `src.simulator` replays or generates readings.
2. `src.mqtt` publishes them to `team9/traffic/sensors/#`.
3. `controller_service` subscribes, updates per-approach readings, and on each
   tick runs the phase machine.
4. On a phase change it publishes an actuator command and records a `pending`
   row; the actuator validates, applies/rejects, and publishes an ack.
5. The controller matches the ack, updates the row and the acknowledged-state
   map, and republishes the `signal/state` snapshot.
6. Node-RED stores valid readings (and system events) in SQLite and renders both
   dashboards. Dashboards show a state as *applied* only once acknowledged.

**Safety envelope** (`config/settings.yaml`): green **10–60 s** (default 25),
yellow **3 s**, all-red **2 s**, sensor stale after **180 s**. Requested green is
free (observed 0–153 s) but is always clamped to 10–60 s before it is applied —
in the live database there are **0 clamp violations**.

---

## B. Current file tree

79 files, 10,578,464 bytes (~10.09 MB), excluding `__pycache__` (regenerated on
run).

```
Adaptive-Traffic-Signal-Controller/
├── .gitignore
├── README.md
├── PROJECT_FILE_GUIDE.md
├── requirements.txt
├── start_all.cmd
├── config/          settings.yaml, mqtt_topics.yaml
├── data/            traffic.db, traffic.db-wal, traffic.db-shm + 3 .bak   (protected)
├── dataset/         5 data files + data_dictionary.md                     (protected)
├── models/          congestion_model.joblib, .gitkeep                     (git-ignored)
├── node_red/        flows.json, dashboard.html, flows.json.bak-20261015-102311
├── results/         figures/ (4 png), metrics/ (5 files)                  (git-ignored)
├── scripts/         check_live_run.py, start_demo.cmd
├── src/             28 .py + database/schema.sql
└── tests/           12 .py
```

Only three Markdown files remain: `README.md`, this guide, and
`dataset/data_dictionary.md` (protected data documentation).

---

## C. File-by-file inventory

### Root

| File | Purpose |
|---|---|
| `README.md` | Concise overview, architecture, quickstart, tests, limitations. |
| `PROJECT_FILE_GUIDE.md` | This document. |
| `requirements.txt` | PyYAML, paho-mqtt, numpy, scikit-learn, matplotlib (optional). |
| `start_all.cmd` | One-click launcher: starts Mosquitto/Node-RED if needed, then actuator + controller + publisher; avoids duplicate processes. |
| `.gitignore` | Ignores `__pycache__/`, `*.pyc`, `results/`, `models/`. |

### config/

| File | Purpose |
|---|---|
| `settings.yaml` | Paths (`dataset_csv`, `dataset_workbook`, `models_dir`, `results_dir`), `database.path`, MQTT broker, simulator, `controller` envelope, `actuator.ack_timeout_seconds` (2) and `actuator.heartbeat_interval_seconds` (5). |
| `mqtt_topics.yaml` | Topic catalog (base `team9/traffic`) and approach slug map; documents the retained actuator heartbeat payload on `status`. |

### src/common/

| File | Purpose |
|---|---|
| `schemas.py` | `SensorReading`, `APPROACHES`, `TRAFFIC_COLUMNS` shared definitions. |
| `logging_config.py` | Consistent logger setup. |
| `time_utils.py` | `Clock` / `ManualClock` for testable time. |

### src/simulator/

| File | Purpose |
|---|---|
| `traffic_replay.py` | Loads/validates `traffic_data.csv`, replays it (start/pause/resume/stop/reset, position, status); `readings`, `scenarios` helpers. |
| `sensor_generator.py` | Synthesises readings; `ReadingValidationError` used by the repository and the Testing Lab. |
| `scenario_manager.py` | Read-only scenario helpers: `scenarios()`, `filter()`. |

### src/mqtt/

| File | Purpose |
|---|---|
| `mqtt_client.py` | paho wrapper with a distinct `client_id` per role so processes never evict each other. |
| `publisher.py` | CLI that replays the dataset onto `sensors/#` (`--speed`, `--limit`). |
| `topics.py` | Resolves topic templates from `mqtt_topics.yaml`. |

### src/controller/

| File | Purpose |
|---|---|
| `timing_constraints.py` | `SignalState`, `TimingConfig`, `PHASE_STATES`, green allocation/clamp helpers. |
| `decision_engine.py` | Demand score → requested green + `decision_reason` vocabulary. |
| `adaptive_controller.py` | Phase state machine (the single source of truth); snapshot. |
| `fixed_time_controller.py` | 25 s fixed-time baseline used by tests/comparison. |

### src/actuator/

| File | Purpose |
|---|---|
| `command_validator.py` | `validate_command` (per-command) and `validate_no_conflict` (no two greens) — pure, no I/O. |
| `actuator_simulator.py` | Validates and applies/rejects commands; keeps per-approach state and history; `apply_snapshot` (whole-intersection safety gate) and `snapshot`. |
| `actuator_node.py` | The actuator process: subscribes to `commands/#`, acks on `acks/#`, publishes a retained liveness heartbeat on `team9/traffic/status`. |

### src/analytics/

| File | Purpose |
|---|---|
| `congestion.py` | Congestion index and approach stats. |
| `feature_engineering.py` | 8 causal features; `FEATURE_NAMES`, `LABELS`; chronological split. |
| `train_model.py` | `StandardScaler → LogisticRegression` (`random_state=2026`); `DEFAULT_MODEL_PATH`. |
| `evaluate_model.py` | Writes `model_metrics.json`, `model_per_class.csv`. |
| `predict.py` | `Predictor.load()` — loads the trained model bundle. |
| `simulation.py` | Deterministic single-saturation-flow queue model for fixed vs adaptive. |
| `figures.py` | Evaluation figures from the stored metrics (no retraining). |
| `run_evaluation.py` | CLI: train/evaluate + fixed-vs-adaptive comparison, writes `results/`. |

### src/database/

| File | Purpose |
|---|---|
| `db.py` | `DatabaseConfig`, `connect` (WAL + busy timeout), `init_db`; runnable as `python -m src.database.db`. |
| `repository.py` | `TrafficRepository`: idempotent inserts, historical queries, bounded retry on SQLite lock errors. |
| `schema.sql` | Tables `sensor_readings`, `signal_decisions`, `signal_events`, `actuator_commands`, `system_events` (all `IF NOT EXISTS`). |

### src/service/

| File | Purpose |
|---|---|
| `controller_service.py` | Orchestrator: subscribes to sensors/test input, runs the controller, publishes signal/timing/test topics, persists decisions/events/commands, tracks acks and timeouts. |

### tests/ (179 tests)

`test_dataset.py`, `test_mqtt.py`, `test_decision_engine.py`, `test_starvation.py`,
`test_timing_safety.py`, `test_actuator_ack.py`, `test_database.py`,
`test_end_to_end.py`, `test_analytics.py`, `test_node_red_flow.py`,
`test_testing_lab_validation.py`, plus `__init__.py`.

### node_red/

| File | Purpose |
|---|---|
| `flows.json` | Importable flow: MQTT ingest → validation → SQLite (`INSERT OR IGNORE`) → dashboards; health via heartbeat; Testing-Lab nodes; error `catch`. |
| `dashboard.html` | Mirror of the served custom `/dashboard` template (WebSocket-fed). |
| `flows.json.bak-20261015-102311` | Newest pre-edit backup (kept for reference). |

### scripts/

| File | Purpose |
|---|---|
| `check_live_run.py` | Acceptance health check against the live DB (needs Mosquitto + Node-RED). |
| `start_demo.cmd` | Compatibility shim that forwards to `start_all.cmd`. |

---

## D. Execution and dependency map

**Processes (runtime)**

| Process | Command | Role | MQTT client id |
|---|---|---|---|
| Dataset publisher | `python -m src.mqtt.publisher --speed 60 --limit 0` | Replays `traffic_data.csv` to `sensors/#` | `team9-traffic-sim-publisher` |
| Controller service | `python -m src.service.controller_service` | Phase machine, decisions, persistence, Testing Lab | `team9-traffic-sim-controller` |
| Actuator node | `python -m src.actuator.actuator_node` | Validate/apply/reject, acks, heartbeat | `team9-traffic-sim-actuator` |
| Node-RED | `node-red` (+ import `flows.json`) | Ingest/store/display | `team9-nodered` |

**CLI entry points**

| Command | Does |
|---|---|
| `python -m src.database.db` | Create/migrate `data/traffic.db` from `schema.sql` (idempotent). |
| `python -m src.analytics.run_evaluation --train` | Train + evaluate + write `results/`. |
| `python -m src.analytics.figures` | Regenerate evaluation figures only. |
| `python start_all.cmd` (or double-click) | Launch the whole stack. |
| `python scripts/check_live_run.py` | Live acceptance check. |
| `python -m unittest discover -s tests` | Full test suite. |

**MQTT topic contract** (base `team9/traffic`)

| Topic | Direction | Purpose |
|---|---|---|
| `sensors/{approach}` | publisher → controller + Node-RED | sensor readings |
| `signal/state` | controller → Node-RED | live snapshot (all approaches, intended vs actuator) |
| `signal/phase` | controller → Node-RED | one phase-change event |
| `timing/decisions` | controller → Node-RED | per-cycle green decisions |
| `commands/{approach}` | controller → actuator | actuator command |
| `acks/{approach}` | actuator → controller | acknowledgement |
| `status` | actuator → all | retained liveness heartbeat |
| `test/input` | dashboard → controller | Testing-Lab submissions / control |
| `test/result` | controller → dashboard | old → new timing + reason |

**Command / acknowledgement handshake**

1. On a phase change the controller publishes a command (UUID `command_id`) and
   records a `pending` row in `actuator_commands`.
2. The actuator validates (`ActuatorSimulator`), applies or rejects, and publishes
   an ack (`status` = accepted/rejected, `applied_state`, `reason`).
3. The controller matches the ack to its pending command and updates the row;
   unmatched acks are logged `stale-ack`; pending older than
   `actuator.ack_timeout_seconds` (2 s) become `unacknowledged`.
4. The `signal/state` snapshot carries `intended`, `actuator`, `pending_commands`
   and `ack_timeouts`, so the dashboards can show intended vs acknowledged state
   and distinguish *not applied* from *not responding*.

**Module dependency direction** (no cycles):
`common` → `simulator`/`mqtt`/`database` → `controller`/`analytics`/`actuator` →
`service` → entry points (`actuator_node`, `controller_service`, `publisher`,
`run_evaluation`).

---

## E. Dataset, database, model and results

### dataset/ (protected — never edited)

Synthetic data, seed 2026, one clock `2026-10-12 03:00 → 23:49`.

| File | Content |
|---|---|
| `traffic_data.csv` | 5000 rows (4 approaches × 1250 min). Demand/state: vehicle_count, queue_length, avg_wait_time, arrival_rate, congestion_level, scenario. |
| `signal_cycle.csv` | Adaptive run signal events (GREEN = applied green 10–60, YELLOW 3, ALL_RED 2). |
| `adaptive_decisions.csv` | Per-approach per-cycle decision incl. requested vs applied green and reason. |
| `fixed_baseline_signal.csv` | Fixed baseline: 25 s green, 3 s yellow, 2 s all-red, 120 s cycle. |
| `adaptive_traffic_project_dataset.xlsx` | Source workbook (all sheets). |
| `data_dictionary.md` | Column reference and shared rules (protected documentation). |

### data/ (protected — live state)

| File | Note |
|---|---|
| `traffic.db` | Live SQLite DB (WAL). Tables: `sensor_readings`, `signal_decisions`, `signal_events`, `actuator_commands`, `system_events`. |
| `traffic.db-wal`, `traffic.db-shm` | Live WAL sidecar state — do not touch while services exist. |
| `traffic.db.bak-20261012-115627`, `traffic.db.bak-20261014-170508`, `traffic.db.pre-audit-cleanup-20261012-1526.bak` | Protected backups. |

Persistence rules: `init_db` is idempotent; `_migrate` only ever
`ALTER TABLE ... ADD COLUMN` (never drops/rewrites rows). The dual-writer
contract is intentional: Node-RED stores readings with `INSERT OR IGNORE`, the
Python layer persists decisions/events/commands; WAL + busy-timeout + bounded
retry make concurrent writers safe (`tests/test_database.py::ConcurrentWriterTests`).

### models/

`congestion_model.joblib` (`train_model.DEFAULT_MODEL_PATH`) — `StandardScaler →
LogisticRegression`, `random_state=2026`. Decision support only; the live
controller never imports it.

### results/ (generated; git-ignored)

| Path | Content |
|---|---|
| `metrics/model_metrics.json` | accuracy 0.6985, baseline 0.6778, beats_baseline true, confusion matrix, per-class F1. |
| `metrics/model_per_class.csv` | Per-class precision/recall/F1. |
| `metrics/fixed_vs_adaptive.csv` | Delay/queue/fairness/max-red-wait for fixed vs adaptive. |
| `metrics/approach_stats.csv`, `metrics/approach_fairness.csv` | Approach-level summaries. |
| `figures/confusion_matrix.png` | Confusion-matrix heatmap. |
| `figures/per_class_metrics.png` | Per-class metric bars. |
| `figures/model_vs_baseline.png` | Model vs baseline. |
| `figures/fixed_vs_adaptive.png` | Four-panel honest comparison. |

**Key ML facts.** Accuracy 0.6985 vs baseline 0.6778 (`beats_baseline = true`,
`random_state = 2026`); confusion matrix `[[959,49,8],[306,75,4],[78,7,13]]`;
per-class F1 LOW 0.8131 / MEDIUM 0.2907 / HIGH 0.2114. The thin +2.07-point win
and weak minority-class F1 are a **dataset-imbalance** result
(LOW 65.8 % / MEDIUM 26.7 % / HIGH 7.4 %), not a modelling breakthrough. No data
leakage: causal features, train-only scaling, chronological split.

**Fixed vs adaptive** (`fixed → adaptive`): avg delay 139.399 → 74.854; max queue
113 → 30; Jain fairness 0.9572 → 0.9997; **max red wait 95 → 161** (Δ +66 — the
honest trade-off, now shown in the figure).

---

## F. Dead-code and redundancy report

Audited by reference count across every `.py` file (imports, calls, tests). Only
code that is **definitively unreferenced** was removed, and nothing on a
safety/validation path was touched.

### Removed (unreferenced, non-safety)

| Location | Symbol | Evidence |
|---|---|---|
| `src/simulator/scenario_manager.py` | `readings` (property) | 0 callers (tests use `TrafficReplay.readings`). |
| `src/simulator/scenario_manager.py` | `scenario_at` | 0 callers. |
| `src/simulator/scenario_manager.py` | `counts` | 0 callers. |
| `src/simulator/scenario_manager.py` | `transitions` | 0 callers. |
| `src/simulator/scenario_manager.py` | `timeline` | 0 callers. |
| `src/analytics/predict.py` | `Predictor.predict_row` | 0 callers. |
| `src/analytics/predict.py` | `Predictor.predict` | 0 callers (`run_evaluation` uses `pipeline.predict` directly). |
| `src/analytics/predict.py` | `Predictor.rule_baseline` | 0 callers. |
| `src/simulator/traffic_replay.py` | `TrafficReplay.scenario_manager()` | 0 callers (the `scenarios` property covers the used case). |
| `src/simulator/traffic_replay.py` | `TrafficReplay.set_speed` | 0 callers. |
| `src/database/repository.py` | `READING_COLUMNS` | 0 references. |

After removal: `ScenarioManager` keeps its used API (`scenarios`, `filter`);
`Predictor` is a bundle loader (`Predictor.load`) used by `run_evaluation`;
`traffic_replay` keeps `readings`/`scenarios`; imports were pruned to match
(`Dict`/`Optional`/`Tuple` in `scenario_manager.py`; `List`/`build_features`/
`classify_congestion`/`SensorReading` in `predict.py`).

### Preserved intentionally (unreferenced but kept)

| Location | Symbol | Why kept |
|---|---|---|
| `src/actuator/actuator_simulator.py` | `apply_snapshot`, `snapshot` | Whole-intersection **safety gate** (refuses conflicting greens); removing it would drop the only caller of `validate_no_conflict`. |
| `src/actuator/command_validator.py` | `validate_no_conflict` | The "no two greens at once" safety invariant — must not be removed. |

### Confirmed NOT dead

`PHASE_STATES` (imported by `tests/test_timing_safety.py`); `FixedTimeController`
(used by tests); every source module is imported somewhere (no wholly dead file).
`AdaptiveController.snapshot` is used (controller_service + two tests) and is
distinct from `ActuatorSimulator.snapshot`.

### Redundancy noted, not changed (protected files)

`config/*.yaml` and `node_red/flows.json` / `dashboard.html` are protected and
were **not** modified. A static pass flagged these Node-RED candidates for a
future manual review (unverified, report only):

- `ui_lab`: unused helper `function num(v)`.
- `ui_signal_panel`: `scope.score = p.average_score` written but never read.
- `fn_health`: `age_s` / `interval_s` locals that do not affect the result.
- `fn_act_cmd`: an if/else where both branches set the same value.

---

## G. Removed-files inventory

All removals to date, with sizes. No protected file was ever touched.

**Pass 1 — cleanup (caches, superseded reports, backups): 87 files / 1,220,832 B**

- 9 `__pycache__/` directories (75 `.pyc`) — 883,704 B; regenerated on run.
- 4 superseded reports — `PROJECT_STATUS_AUDIT.md`, `REMAINING_WORK.md`,
  `UI_RESTYLING_REPORT.md`, `PROJECT_PROGRESS.md` — ~32,725 B.
- 5 old `node_red/flows.json.bak-*` — 289,502 B (newest kept).
- 3 `tests/test_node_red_flow.py.bak-*` — 14,901 B.

**Pass 2 — documentation consolidation (this task): 5 files**

- `FINAL_IMPLEMENTATION_REPORT.md` (5,894 B) — migrated into §A/§D/§F/§H.
- `PROJECT_ARCHITECTURE.md` (5,864 B) — migrated into §A/§D.
- `PROJECT_CLEANUP_AUDIT.md` (18,744 B) — migrated into §C/§E/§G.
- `PROJECT_GRAPH_ML_IOT_AUDIT.md` (23,882 B) — migrated into §E/§F/§H.
- `node_red/README.md` — migrated into §D/§H (import steps, gotcha, which dashboard).

**Kept (protected, do not delete):** `dataset/data_dictionary.md`;
`data/traffic.db*` + 3 DB backups; the dataset; `models/congestion_model.joblib`;
`node_red/flows.json` + `dashboard.html`; `results/`; `requirements.txt`;
`start_all.cmd`; `scripts/*`; `.gitignore`.

---

## H. Final status

- **Files:** 79 (excluding regenerated `__pycache__`), **10,578,464 B (~10.09 MB)**.
- **Markdown:** `README.md`, `PROJECT_FILE_GUIDE.md`, `dataset/data_dictionary.md`.
- **Tests:** `python -m unittest discover -s tests` → **179 OK** (after the
  dead-code removals).
- **Database:** live `data/traffic.db` at schema, untouched; backups intact;
  WAL + busy-timeout + bounded retry in place.
- **Protected files:** all present and unmodified (dataset, DB + backups, model,
  flow, dashboard, configs, `schema.sql`, tests).

**Known, intentionally unfixed (pre-existing, out of scope):**

- Type-checker noise in `src/service/controller_service.py` (~line 296,
  `_pending_acks.pop(command_id, None)`), `src/analytics/run_evaluation.py`
  (`pipeline: object` vs `Pipeline`), `src/simulator/traffic_replay.py` (~line 126),
  and `src/database/repository.py` (~line 319, `int | None`). None affect runtime
  or tests.
- `scripts/check_live_run.py` reports 2 historical live-run rows (one early-run
  unacknowledged command, ids 32/35 in `system_events`); these require a fresh
  live run to clear and were not part of this task. No services were started.
- `.gitignore` references a non-existent `results/.gitkeep` (harmless).

**Node-RED / dashboards:** `/dashboard` is the primary control room; `/ui` is the
kept secondary component view. Import `node_red/flows.json` (Menu → Import →
Deploy). The `ui_base` config node must keep `"id": "ui_base_config"`
(`id ≠ type`) or deploy aborts with a circular-dependency error and `/ui` stays
blank — enforced by `tests/test_database.py`.

### Verification commands

```powershell
$env:PYTHONPATH="."; python -m unittest discover -s tests      # 179 OK
python -c "import json; json.load(open('node_red/flows.json', encoding='utf-8')); print('flows ok')"
python -m py_compile src/simulator/scenario_manager.py src/analytics/predict.py src/simulator/traffic_replay.py src/database/repository.py
```
