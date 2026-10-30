# Team 9 – Adaptive Traffic Signal Controller

B.Tech CSE Semester 7 IoT mini-project.

An IoT pipeline that adjusts signal timing in real time from observed traffic
demand instead of a fixed schedule. Traffic data is replayed or generated,
published over MQTT, analysed for congestion, and turned into safe signal-timing
decisions by a Python controller driving a simulated actuator. Node-RED ingests
the messages, stores them in SQLite and renders a live control-room dashboard
with an interactive Testing Lab.

> This is a **local software simulation**. It never controls real traffic lights
> and makes no claim of physical hardware control.

For the full file-by-file reference, architecture, execution map and audit
history, see **[PROJECT_FILE_GUIDE.md](PROJECT_FILE_GUIDE.md)**.

## Objectives

- Replay and generate realistic synthetic traffic/sensor data for a junction.
- Publish measurements and commands over MQTT using a documented topic schema.
- Estimate congestion and derive causal features from the traffic data.
- Train and evaluate a lightweight congestion model (decision support only).
- Compute safe, constraint-validated signal timings.
- Compare fixed-time vs adaptive policies under equal exogenous demand.
- Simulate the actuator, persist everything to SQLite, and visualise it live.

## Architecture (short)

```
dataset/*.csv ─► src.simulator ──► MQTT team9/traffic/sensors/#
                                        │
                    ┌───────────────────┴────────────────────┐
                    ▼                                         ▼
   src.service.controller_service                     Node-RED (flows.json)
     AdaptiveController (owns phase machine)            ingest / validate
     decision_engine (demand → green, clamped)          SQLite persistence
     signal/state, timing/decisions, commands           /dashboard + /ui
        │
        ▼  actuator command + ack (acknowledged state, not intended)
```

One source of truth for the active phase: the Python `AdaptiveController` owns
the phase machine; Node-RED only ingests, displays, stores and exposes the
Testing-Lab controls.

## Directory structure

```
config/     settings.yaml (paths, mqtt, controller), mqtt_topics.yaml
dataset/    frozen synthetic data (5 files) + data_dictionary.md
node_red/   flows.json, dashboard.html
src/        common, simulator, mqtt, controller, actuator, analytics, database, service
tests/      12 unittest modules (179 tests)
results/    generated metrics + figures (git-ignored)
models/     trained model artefact (git-ignored)
data/       SQLite database (live + backups)
```

## Prerequisites (tested versions)

- Python 3.14 (works on 3.11+)
- Eclipse Mosquitto broker on `localhost:1883` (or any MQTT broker)
- Node-RED 5.x with `node-red-dashboard` and `node-red-node-sqlite`
- Libraries: PyYAML, paho-mqtt, numpy 2.x, scikit-learn 1.9, matplotlib 3.10

## Install and configure

```cmd
python -m pip install -r requirements.txt
python -m src.database.db          :: create data/traffic.db from schema.sql (never deletes rows)
```

`config/settings.yaml` holds paths, MQTT settings and the controller envelope
(green **10–60 s**, default 25; yellow 3; all-red 2). `config/mqtt_topics.yaml`
holds the topic contract.

## Run the live simulation

One-click Windows startup — double-click `start_all.cmd` in the project root. It
starts Mosquitto if needed, waits for Node-RED, then opens the actuator,
controller and dataset publisher in separate windows, checking for existing
processes so a second click adds no duplicate MQTT clients.

Manual equivalent (each in its own terminal, from the project root, after
Mosquitto and Node-RED are up):

```cmd
python -m src.actuator.actuator_node           :: 1) simulated actuator (acks + heartbeat)
python -m src.service.controller_service      :: 2) controller service (phases, decisions, testing lab)
python -m src.mqtt.publisher --speed 60 --limit 0   :: 3) replay dataset as MQTT readings
```

- <http://127.0.0.1:1880/dashboard> — primary control room (custom, WebSocket-fed)
- <http://127.0.0.1:1880/ui/> — secondary raw-widget component view

## The dashboard

For all four approaches (North/South/East/West): vehicle count, queue length,
arrival rate, average wait, congestion level, current signal colour/phase,
remaining seconds, applied green, last sensor update and a live/stale flag, plus
a junction overview, a timing panel (requested vs applied green + reason), a
queue-trend chart and an event history. State is shown only once **acknowledged**
by the actuator. Actuator health comes from the **real heartbeat**
(`team9/traffic/status`), so a dead actuator can never show as healthy.

The **Traffic Testing Lab** lets you pick a scenario (Low/Normal/Heavy/Custom),
enter per-approach values, and use Start/Pause/Resume/Stop/Reset. Submitted
readings travel the real path (MQTT → controller → actuator → SQLite) and the
result panel shows old → new green times, the decision reason and any validation
warning.

## Fixed-time vs adaptive comparison

```cmd
python -m src.analytics.run_evaluation --train
```

Trains + evaluates the congestion classifier on the frozen dataset (chronological
split, majority-class baseline) and replays the same exogenous arrivals through a
deterministic queue model for both fixed-time (25 s) and adaptive policies.
Outputs land in `results/metrics/` and `results/figures/`. Nothing is written
into `dataset/`.

## ML Model Comparison

The offline comparison evaluates Logistic Regression, Random Forest, Gradient
Boosting and a feedforward MLP against the next-interval congestion label
(LOW/MEDIUM/HIGH). All candidates use the frozen `dataset/traffic_data.csv`,
the existing eight causal features, and one chronological 70/30 split; metrics
include accuracy, balanced accuracy, macro/weighted F1, per-class scores,
confusion matrices and a training-label majority baseline.

The root-level `model_comparison*` reports, charts and artifacts are preserved
as the historical row-split experiment. The current
`python -m src.analytics.compare_models` run writes corrected metrics and
charts under `results/metrics/temporal_corrected/` and
`results/figures/temporal_corrected/`, with fitted candidates under
`models/comparison/temporal_corrected/`. These remain offline decision-support
models and do not control live signal timings.

### Leakage-safe validation and advanced evaluation

The earlier comparison above is historical: it used the legacy row-based
split. Corrected comparison results are stored separately under
`results/metrics/temporal_corrected/` and models under
`models/comparison/temporal_corrected/`. Its split groups identical feature
timestamps and purges training rows whose next-reading target reaches the test
boundary. Rerun with `python -m src.analytics.compare_models`.

On the frozen dataset, the corrected run used 3,492 training and 1,500 test
rows, grouped 1,249 unique feature timestamps, and excluded 4 boundary-crossing
rows. The test boundary was `2026-10-12 17:34:00`; every training target ended
before it. Majority baseline accuracy was 0.6780.

| Model | Accuracy | Balanced accuracy | Macro P/R/F1 | Weighted F1 | HIGH recall |
| --- | ---: | ---: | ---: | ---: | ---: |
| Logistic Regression | 0.6987 | 0.4238 | 0.6023 / 0.4238 / 0.4384 | 0.6398 | 0.1327 |
| Random Forest | 0.6713 | 0.4538 | 0.5362 / 0.4538 / 0.4747 | 0.6428 | 0.2143 |
| Gradient Boosting | 0.7067 | 0.4163 | 0.6689 / 0.4163 / 0.4258 | 0.6481 | 0.0714 |
| MLP Neural Network | 0.6773 | 0.4815 | 0.5460 / 0.4815 / 0.5014 | 0.6539 | 0.2857 |

MLP has the highest macro-F1, with weak HIGH recall (28.57%) and accuracy just
below the majority baseline. Scores reflect this synthetic, imbalanced dataset
and do not establish generalization to real intersections. Per-class
LOW/MEDIUM/HIGH precision, recall, F1 and support are in
`model_comparison_per_class.csv`; model configurations, seeds, split metadata
and confusion matrices are in the JSON output.

Phase 2 performance measurements can be rerun with
`python -m src.analytics.benchmark_models` (three training repetitions by
default; inference counts are configurable). CPU, sampled process RSS, model
loading, latency and throughput are host-specific observations. Phase 3 uses
`python -m src.analytics.closed_loop_evaluation` to exercise the existing
controller service and actuator node over an in-process loopback with a manual
clock. It measures application-level software latency without real MQTT/network
delay. Its queue comparison remains a simplified synthetic saturation-flow
model; neither this nor the frozen synthetic data establishes physical traffic,
hardware, or energy outcomes. Experimental models remain offline only. The
completed loopback run observed 37 actual phase events, 37/37 accepted normal
phase-command acknowledgements, and zero timeline or Stop/Reset safety
violations. Stop and Reset each publish four unique zero-duration RED commands
and are confirmed only after all four matching accepted RED acknowledgements.
Missing, rejected, stale or mismatched ACKs leave the operation visibly pending
or failed and block GREEN restart; Reset changes controller runtime state only
after confirmation and never deletes SQLite history. The dashboard reports
operation status, ACK/pending/failure counts and last acknowledged actuator
states. Fault injection held ACKs after simulated RED application; both
operations timed out as failed and were not reported as confirmed. These are
simulated actuator results, not guarantees about physical signals.

Phase 2 used 3 fit repetitions/model, 20 warmups, 1,000 timed single-row
predictions and 100 held-out batch repetitions. CPU percentage is process
CPU-time divided by fit wall-time (percent of one logical CPU, so it can exceed
100%); peak RSS is sampled every 5 ms and may miss brief peaks. Fit wall time
(mean ± SD), process CPU time and utilization (mean ± SD), peak RSS, single-row
median/p95/p99/max latency, batch throughput, artifact load time and bytes were
measured on Windows 11, Python 3.13.7, Intel64 Family 6 Model 186 (12 logical
CPUs), NumPy 2.2.6 and scikit-learn 1.7.2, CPU only:

| Model | Fit s | CPU s | CPU % | Peak RSS MB | Single-row ms (median/p95/p99/max) | Batch samples/s | Load ms | Artifact bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Logistic Regression | 0.0253 ± 0.0003 | 0.0260 ± 0.0090 | 102.9 ± 35.4 | 153.9 | 0.094 / 0.191 / 0.269 / 0.999 | 1,385,660 | 24.10 | 29,939 |
| Random Forest | 0.7969 ± 0.0459 | 0.7813 ± 0.0563 | 98.0 ± 1.6 | 249.5 | 4.912 / 6.806 / 8.155 / 53.147 | 26,288 | 142.42 | 53,972,454 |
| Gradient Boosting | 0.7152 ± 0.0079 | 0.7031 ± 0.0156 | 98.3 ± 2.0 | 270.7 | 0.342 / 0.611 / 0.743 / 1.588 | 305,656 | 26.45 | 361,387 |
| MLP Neural Network | 2.3105 ± 0.2076 | 2.2396 ± 0.2371 | 96.8 ± 1.9 | 273.5 | 0.110 / 0.128 / 0.209 / 2.069 | 903,250 | 22.75 | 129,478 |

All artifacts passed save/load prediction-consistency checks. Phase 3 command to
ACK median/p95/p99/max processing latency was 0.125/0.167/0.190/0.200 ms in
the in-process loopback. The fixed/adaptive queue model produced average delay
139.399/76.585 s, average queue 7.005/3.848, maximum queue 113/21 and Jain
fairness 0.9572/0.9923; maximum red wait was 95/164 s. These values come from
the simplified queue model, not measured road traffic.

## Tests

```cmd
python -m unittest discover -s tests
```

The regression suite includes controller, service, simulator and actuator-node
safe-stop scenarios. It uses fake MQTT and temporary SQLite databases; no live
broker or physical hardware is required.

## Limitations and safety

- Synthetic data only; no real traffic or hardware is controlled.
- Green allocation is an independent, documented heuristic (demand score →
  requested = previous × score/average, clamped to 10–60 s). It uses the
  dataset's constraints and reason vocabulary but is **not** a bit-for-bit
  reproduction of it.
- The fixed-vs-adaptive queue model is a single-saturation-flow approximation;
  only the relative comparison under equal demand is claimed.
- The congestion classifier is decision support; the live controller keeps its
  deterministic rules and fixed-time fallback and never depends on the model.
- Accuracy (0.6985) beats the majority baseline (0.6778) but the MINORITY classes
  are weak (MEDIUM F1 0.29, HIGH F1 0.21) because the dataset is imbalanced
  (LOW 65.8 %). See the guide for the full metric set.
