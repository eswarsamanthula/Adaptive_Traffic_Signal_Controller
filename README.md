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

## Tests

```cmd
python -m unittest discover -s tests
```

179 tests, pure standard library — no broker, browser or Node-RED required.

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
