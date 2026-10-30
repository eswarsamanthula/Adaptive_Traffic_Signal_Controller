"""Isolated application-level closed-loop evaluation; no broker or live DB."""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from src.actuator.actuator_node import ActuatorNode
from src.actuator.command_validator import validate_no_conflict
from src.common.time_utils import ManualClock, add_seconds
from src.controller.adaptive_controller import AdaptiveController
from src.controller.timing_constraints import SignalState, TimingConfig
from src.analytics.compare_models import PROJECT_ROOT, _save_json, _write_csv
from src.analytics.simulation import compare
from src.mqtt.topics import load_topics
from src.service.controller_service import ControllerService
from src.simulator.traffic_replay import TrafficReplay

METRICS = PROJECT_ROOT / "results" / "metrics" / "temporal_corrected"
FIGURES = PROJECT_ROOT / "results" / "figures" / "temporal_corrected"
ORIGIN = "2026-10-30 00:00:00"


class LoopbackBus:
    """Synchronous in-process topic router; deliberately has no network layer."""
    def __init__(self):
        self.handlers = defaultdict(list)
        self.commands = {}
        self.ack_rows = []
        self.held_acks = []
        self.hold_acks = False
        self.publications = []
        self.pending_lookup = None

    def subscribe(self, topic, callback):
        self.handlers[topic].append(callback)

    def publish(self, topic, payload):
        self.publications.append({"topic": topic, "payload": payload})
        try:
            obj = json.loads(payload)
        except (ValueError, TypeError):
            obj = {}
        if "/commands/" in topic and obj.get("command_id"):
            self.commands[obj["command_id"]] = {"issued_ns": time.perf_counter_ns(), "payload": obj}
        if "/acks/" in topic and self.hold_acks:
            self.held_acks.append((topic, payload))
            return
        for callback in list(self.handlers[topic]):
            if "/acks/" in topic:
                begin = time.perf_counter_ns()
                matched = bool(self.pending_lookup and self.pending_lookup(json.loads(payload).get("command_id")))
                callback(topic, payload)
                end = time.perf_counter_ns()
                ack = json.loads(payload)
                command = self.commands.get(ack.get("command_id"))
                if command is not None:
                    self.ack_rows.append({"command_id": ack.get("command_id"), "command_to_ack_ms": (end-command["issued_ns"])/1e6,
                                          "status": ack.get("status") if matched else "late_unmatched_ack", "ack_processing_ms": (end-begin)/1e6,
                                          "matched_pending_command": matched,
                                          "source": command["payload"].get("source", "")})
            else:
                callback(topic, payload)

    def deliver_held(self):
        held, self.held_acks = self.held_acks, []
        self.hold_acks = False
        for topic, payload in held:
            self.publish(topic, payload)


class LoopbackClient:
    def __init__(self, bus): self.bus, self.is_connected = bus, False
    def connect(self): self.is_connected = True
    def disconnect(self): self.is_connected = False
    def subscribe(self, topic, callback): self.bus.subscribe(topic, callback)
    def publish(self, topic, payload, **kwargs): self.bus.publish(topic, payload)


def _summary(values: list[float], failed: int = 0) -> dict:
    if not values:
        return {"sample_count": failed, "successful_samples": 0, "failed_or_timed_out": failed,
                "min_ms": None, "median_ms": None, "p95_ms": None, "p99_ms": None, "max_ms": None}
    values = sorted(values)
    def q(p):
        i = (len(values)-1)*p; lo=int(i); hi=min(lo+1, len(values)-1)
        return values[lo]+(values[hi]-values[lo])*(i-lo)
    return {"sample_count": len(values)+failed, "successful_samples": len(values), "failed_or_timed_out": failed,
            "min_ms": min(values), "median_ms": statistics.median(values), "p95_ms": q(.95), "p99_ms": q(.99), "max_ms": max(values)}


def run_closed_loop(*, cycles: int = 3, output_dir: Path = METRICS, figure_dir: Path = FIGURES) -> dict:
    if cycles < 1: raise ValueError("cycles must be at least 1")
    config = TimingConfig(tick_seconds=3600)
    topics = load_topics()
    clock = ManualClock()
    bus = LoopbackBus()
    service_client, actuator_client = LoopbackClient(bus), LoopbackClient(bus)
    controller = AdaptiveController(config, clock)
    service = ControllerService(service_client, topics, repository=None, controller=controller, config=config)
    bus.pending_lookup = lambda command_id: command_id in service._pending_acks
    actuator = ActuatorNode(actuator_client, topics, config, heartbeat_interval=3600)
    # Use actual application subscriptions and handlers. No persistent DB is
    # attached, so the evaluator cannot accidentally open the project's DB.
    initial_events = []
    controller_start = controller.start
    def capture_initial_events(*, at=None):
        emitted = controller_start(at=at)
        initial_events.extend(emitted)
        return emitted
    controller.start = capture_initial_events
    actuator_client.connect()
    for t in [topics.command_topic(a) for a in config.phase_order]: bus.subscribe(t, actuator.handle_message)
    service.start()
    actuator.stop_heartbeat()
    phase_rows, stability_rows, safety_checks = [], [], []
    latency_rows, stimulus_rows = [], []
    decisions_by_approach = defaultdict(list)
    stimulus_pending = False
    stimulus_started_ns = None

    # The service startup starts the actual controller and dispatches its
    # initial GREEN through the normal command/ACK path.
    if not initial_events:
        raise RuntimeError("AdaptiveController.start emitted no initial phase event")
    initial = initial_events[0]
    initial.timestamp = ORIGIN
    phase_rows.append({"sequence": 1, "simulated_seconds": initial.at, "simulated_timestamp": ORIGIN,
                       "cycle_no": initial.cycle_no, "approach": initial.approach,
                       "state": initial.state.value, "duration_sec": initial.duration_sec, "scenario": "baseline"})
    decisions_by_approach[initial.approach].append(initial.duration_sec)
    initial_greens = sum(s["state"] == "GREEN" for s in controller.snapshot()["signals"].values())
    actuator_greens = sum(s == "GREEN" for s in actuator.actuator.snapshot().values())
    safety_checks.append({"sequence": 1, "controller_green_count": initial_greens,
                          "actuator_green_count": actuator_greens,
                          "timing_ok": config.min_green_seconds <= initial.duration_sec <= config.max_green_seconds,
                          "no_controller_conflict": initial_greens <= 1, "no_actuator_conflict": actuator_greens <= 1,
                          "acknowledged_state_matches_actuator": all(
                              actuator.actuator.states[a] == s for a, s in service._acked_states.items())})

    # Exercise the real Testing-Lab reading intake and adaptive decision engine.
    stimuli = [
        {"North": (22, 12), "South": (5, 2), "East": (7, 3), "West": (4, 2)},
        {"North": (8, 4), "South": (18, 11), "East": (6, 3), "West": (5, 2)},
        {"North": (8, 4), "South": (18, 11), "East": (6, 3), "West": (5, 2)},
    ]
    # Include one following GREEN after the requested complete cycles so the
    # stop fault check can observe a genuinely applied green on the actuator.
    target_events = cycles * len(config.phase_order) * 3 + 1
    seq = 1
    # Feed distinct stimuli before each cycle boundary, then advance the real
    # ManualClock to each actual controller phase deadline.
    current_scenario = "baseline"
    while len(phase_rows) < target_events:
        if controller.state == SignalState.GREEN and controller.active_index == 0 and controller.phase_elapsed == 0:
            cycle_idx = min(max(controller.cycle_no - 1, 0), len(stimuli)-1)
            current_scenario = ("baseline", "rush", "steady")[cycle_idx]
            raw = stimuli[cycle_idx]
            readings = {a: {"queue_length": q, "arrival_rate": ar, "avg_wait_time": 8}
                        for a, (q, ar) in raw.items()}
            stamp = add_seconds(ORIGIN, clock.now())
            payload = json.dumps({"timestamp": stamp, "scenario": current_scenario, "readings": readings})
            stimulus_started_ns = time.perf_counter_ns()
            service.handle_message(topics.test_input_topic, payload)
            stimulus_pending = True
        deadline = controller.phase_started_at + controller.phase_duration
        clock.set(deadline)
        tick_start = time.perf_counter_ns()
        prior_commands = set(bus.commands)
        events = service.tick_once(at=clock.now())
        tick_end = time.perf_counter_ns()
        issued = [v for command_id, v in bus.commands.items()
                  if command_id not in prior_commands and tick_start <= v["issued_ns"] <= tick_end]
        for index, command in enumerate(issued):
            latency_rows.append({"sample_id": command["payload"]["command_id"],
                                 "latency_type": "controller_tick_to_command_publication",
                                 "latency_ms": (command["issued_ns"]-tick_start)/1e6, "status": "success",
                                 "scope": "application-level software latency without real broker/network delay"})
            if stimulus_pending and index == 0 and stimulus_started_ns is not None:
                stimulus_rows.append({"input_id": f"stimulus-{max(controller.cycle_no-1, 0)+1}",
                                      "latency_type": "controlled_stimulus_to_command_publication",
                                      "latency_ms": (command["issued_ns"]-stimulus_started_ns)/1e6, "status": "success",
                                      "scope": "application-level software latency without real broker/network delay"})
                stimulus_pending = False
        # Decision/command processing is measured around the actual tick path;
        # it is application software time with no network/broker delay.
        for event in events:
            seq += 1
            stamp = add_seconds(ORIGIN, event.at)
            event_scenario = ("baseline", "rush", "steady")[min(max(event.cycle_no-1, 0), 2)]
            phase_rows.append({"sequence": seq, "simulated_seconds": event.at, "simulated_timestamp": stamp,
                               "cycle_no": event.cycle_no, "approach": event.approach,
                               "state": event.state.value, "duration_sec": event.duration_sec,
                               "scenario": event_scenario})
            if event.state == SignalState.GREEN:
                decisions_by_approach[event.approach].append(event.duration_sec)
            snapshot = controller.snapshot()
            green_count = sum(s["state"] == "GREEN" for s in snapshot["signals"].values())
            actuator_green_count = sum(s == "GREEN" for s in actuator.actuator.snapshot().values())
            timing_ok = (event.state != SignalState.GREEN or config.min_green_seconds <= event.duration_sec <= config.max_green_seconds)
            timing_ok &= (event.state != SignalState.YELLOW or event.duration_sec == config.yellow_seconds)
            timing_ok &= (event.state != SignalState.ALL_RED or event.duration_sec == config.all_red_seconds)
            safety_checks.append({"sequence": seq, "controller_green_count": green_count,
                                  "actuator_green_count": actuator_green_count, "timing_ok": bool(timing_ok),
                                  "no_controller_conflict": green_count <= 1,
                                  "no_actuator_conflict": actuator_green_count <= 1,
                                  "acknowledged_state_matches_actuator": all(
                                      actuator.actuator.states[a] == service._acked_states.get(a, "RED")
                                      for a in service._acked_states)})

    for row in bus.ack_rows:
        latency_rows.append({"sample_id": row["command_id"], "latency_type": "command_to_acknowledgement",
                             "latency_ms": row["command_to_ack_ms"], "status": row["status"],
                             "scope": "application-level software latency without real broker/network delay"})

    normal_cycle_command_ids = {
        cid for cid, command in bus.commands.items()
        if command["payload"].get("source") == "controller"
    }

    # Exercise successful Stop and Reset through the service, actuator node,
    # and correlated ACK callback. The safe operation is synchronous only
    # because this evaluator's in-process broker delivers ACKs inline.
    service.handle_message(topics.test_input_topic, json.dumps({"command": "stop"}))
    stopped_red = all(v["state"] == "RED" for v in controller.snapshot()["signals"].values())
    stop_actuator_states = actuator.actuator.snapshot()
    stop_actuator_green_count = sum(s == "GREEN" for s in stop_actuator_states.values())
    stop_actuator_all_red = all(s == "RED" for s in stop_actuator_states.values())
    stop_safe_result = service._safe_operation_view()
    service.handle_message(topics.test_input_topic, json.dumps({"command": "reset"}))
    reset_cycle = controller.cycle_no == 0
    reset_actuator_states = actuator.actuator.snapshot()
    reset_actuator_green_count = sum(s == "GREEN" for s in reset_actuator_states.values())
    reset_safe_result = service._safe_operation_view()

    # Separate fault-injection operations: the simulated node applies RED but
    # the broker holds the ACK beyond the configured timeout. Neither Stop nor
    # Reset may claim success; late ACKs remain stale after timeout.
    bus.hold_acks = True
    service.handle_message(topics.test_input_topic, json.dumps({"command": "start"}))
    service.handle_message(topics.test_input_topic, json.dumps({"command": "stop"}))
    fault_stop_pending = service._safe_operation_view()
    service.check_ack_timeouts(now=max(p["issued_epoch"] for p in service._pending_acks.values()) + 5, timeout=1)
    fault_stop_result = service._safe_operation_view()
    bus.deliver_held()
    bus.hold_acks = True
    service.handle_message(topics.test_input_topic, json.dumps({"command": "reset"}))
    fault_reset_pending = service._safe_operation_view()
    service.check_ack_timeouts(now=max(p["issued_epoch"] for p in service._pending_acks.values()) + 5, timeout=1)
    fault_reset_result = service._safe_operation_view()
    bus.deliver_held()
    bus.hold_acks = False

    # Expected negative cases traverse the actual actuator validator/node.
    negative_cases = [
        ("invalid_state", {"approach": "North", "state": "BLUE", "duration_sec": 25}),
        ("out_of_range_green", {"approach": "North", "state": "GREEN", "duration_sec": 999}),
        ("unknown_approach", {"approach": "Up", "state": "GREEN", "duration_sec": 25}),
    ]
    negative_results = []
    for idx, (case, command) in enumerate(negative_cases, 1):
        cid = f"negative-{case}-{idx}"
        before = actuator.actuator.snapshot()
        command.update({"command_id": cid, "cycle_no": controller.cycle_no, "source": "evaluation-negative-test"})
        service._pending_acks[cid] = {"approach": command["approach"], "state": command["state"], "issued_epoch": time.time()}
        topic = topics.command_topic("North")
        actuator.handle_message(topic, json.dumps(command))
        ack = next((a for a in reversed(bus.publications) if "/acks/" in a["topic"] and cid in a["payload"]), None)
        obj = json.loads(ack["payload"]) if ack else {}
        unchanged = actuator.actuator.snapshot() == before
        negative_results.append({"case": case, "expected": "rejected", "actual": obj.get("status", "no_ack"),
                                 "applied_state_unchanged": unchanged,
                                 "passed": obj.get("status") == "rejected" and unchanged})
    before_states = actuator.actuator.snapshot()
    conflicting = [{"approach": "North", "state": "GREEN", "duration_sec": 25},
                   {"approach": "South", "state": "GREEN", "duration_sec": 25}]
    conflict_result = actuator.actuator.apply_snapshot(conflicting)
    negative_results.append({"case": "conflicting_green_snapshot", "expected": "rejected", "actual": "rejected" if not conflict_result["applied"] else "accepted",
                             "passed": not conflict_result["applied"]})
    if actuator.actuator.snapshot() != before_states:
        negative_results[-1]["passed"] = False

    # Missing ack timeout and delayed stale ack: hold the real node's ack, mark
    # its controller command timed out, then deliver it late through the same path.
    bus.hold_acks = True
    missing_id = f"missing-ack-{controller.cycle_no}"
    missing_command = {"command_id": missing_id, "cycle_no": controller.cycle_no,
                       "approach": "North", "state": "YELLOW", "duration_sec": config.yellow_seconds,
                       "issued_at": ORIGIN, "source": "controller"}
    service._pending_acks[missing_id] = {"approach": "North", "state": "YELLOW", "issued_epoch": time.time()}
    previous_acked_state = service._acked_states.get("North", "RED")
    service_client.publish(topics.command_topic("North"), json.dumps(missing_command))
    actuator_applied_before_ack = actuator.actuator.states["North"] == "YELLOW"
    controller_state_not_applied_before_ack = service._acked_states.get("North", "RED") == previous_acked_state
    timeout_ids = service.check_ack_timeouts(now=time.time()+5, timeout=1)
    timeout_count = len(timeout_ids)
    delayed = bool(bus.held_acks)
    bus.deliver_held()
    latency_rows.append({"sample_id": missing_id, "latency_type": "command_to_acknowledgement",
                         "latency_ms": "", "status": "timed_out",
                         "scope": "application-level software latency without real broker/network delay"})
    before_mismatched_ack = dict(service._acked_states)
    service.handle_message(topics.ack_topic("North"), json.dumps({
        "command_id": "mismatched-command-id", "approach": "North", "status": "accepted",
        "applied_state": "GREEN", "applied_at": ORIGIN,
    }))
    mismatched_ack_ignored = service._acked_states == before_mismatched_ack
    latency_rows.append({"sample_id": "mismatched-command-id", "latency_type": "command_to_acknowledgement",
                         "latency_ms": "", "status": "unmatched_command_id",
                         "scope": "application-level software latency without real broker/network delay"})

    # Use the frozen readings and existing deterministic queue approximation.
    readings = TrafficReplay.from_settings().readings
    queue_results = compare(readings, config)
    fixed, adaptive = queue_results["fixed"].as_dict(), queue_results["adaptive"].as_dict()
    queue_rows = [{"metric": k, "fixed": fixed[k], "adaptive": adaptive[k], "delta_adaptive_minus_fixed": adaptive[k]-fixed[k]}
                  for k in ("avg_delay_per_vehicle", "avg_queue", "max_queue", "worst_approach_delay", "max_red_wait_sec", "jain_fairness", "served_fraction")]
    total_h = (max((r["simulated_seconds"] for r in phase_rows), default=0)/3600)
    approaches = [r for r in phase_rows if r["state"] == "GREEN"]
    switches = sum(1 for a,b in zip(approaches, approaches[1:]) if a["approach"] != b["approach"])
    green_changes = []
    oscillation_reversals = 0
    for approach, values in decisions_by_approach.items():
        deltas = [b-a for a,b in zip(values, values[1:])]
        green_changes.extend(abs(delta) for delta in deltas)
        oscillation_reversals += sum(1 for a,b in zip(deltas,deltas[1:]) if a*b < 0)
    normal_acks = [r for r in bus.ack_rows if r["status"] == "accepted" and r["command_id"] in normal_cycle_command_ids]
    rejected_normal = [r for r in bus.ack_rows if r["status"] == "rejected" and r["command_id"] in normal_cycle_command_ids]
    normal_issued = len(normal_cycle_command_ids)
    expected_negative_count = len(negative_results)
    metrics = {
        "latency": {"command_to_acknowledgement_application_ms": _summary([r["command_to_ack_ms"] for r in normal_acks], failed=timeout_count),
                    "controller_tick_to_command_publication_ms": _summary([r["latency_ms"] for r in latency_rows if r["latency_type"] == "controller_tick_to_command_publication"]),
                    "stimulus_to_command_publication_ms": _summary([r["latency_ms"] for r in stimulus_rows]),
                    "scope": "application-level software latency without real broker/network delay"},
        "acknowledgements": {"normal_accepted": len(normal_acks), "normal_rejected": len(rejected_normal),
                             "normal_controller_commands_issued": normal_issued,
                             "normal_commands_missing_ack": normal_issued-len(normal_acks)-len(rejected_normal),
                             "negative_command_tests_issued": len(negative_cases),
                             "conflicting_snapshot_tests_issued": 1,
                             "negative_test_rejections": sum(r["actual"] == "rejected" for r in negative_results),
                             "acknowledgement_success_rate": len(normal_acks)/(len(normal_acks)+len(rejected_normal)) if normal_acks or rejected_normal else None,
                             "acknowledgement_success_rate_of_all_normal_commands": len(normal_acks)/normal_issued if normal_issued else None,
                             "negative_test_rejection_rate": sum(r["actual"] == "rejected" for r in negative_results)/expected_negative_count if expected_negative_count else None,
                             "timeout_count": timeout_count, "delayed_ack_late_detected": delayed,
                             "actuator_applied_before_ack": actuator_applied_before_ack,
                             "controller_state_not_applied_before_ack": controller_state_not_applied_before_ack,
                             "pending_commands_at_completion": len(service._pending_acks),
                             "stale_or_unmatched_ack_count": sum(not r["matched_pending_command"] for r in bus.ack_rows) + 1,
                             "mismatched_command_id_count": 1, "mismatched_ack_ignored_without_state_change": mismatched_ack_ignored},
        "phase_events": {"actual_state_transitions": len(phase_rows), "green_entries": sum(r["state"]=="GREEN" for r in phase_rows),
                         "yellow_transitions": sum(r["state"]=="YELLOW" for r in phase_rows), "all_red_transitions": sum(r["state"]=="ALL_RED" for r in phase_rows),
                         "approach_switches": switches, "switches_per_simulated_hour": switches/total_h if total_h else None,
                         "completed_cycles": sum(any(r["cycle_no"] == cycle and r["approach"] == config.phase_order[-1] and r["state"] == "ALL_RED" for r in phase_rows)
                                                  for cycle in {r["cycle_no"] for r in phase_rows}),
                         "mean_green_change_seconds": statistics.mean(green_changes) if green_changes else None,
                         "max_green_change_seconds": max(green_changes) if green_changes else None,
                         "adjustments_exceeding_hold_band": sum(v > config.hold_band_seconds for v in green_changes),
                         "oscillation_indicator": "count and magnitude of sign reversals in consecutive per-approach green adjustments",
                         "oscillation_sign_reversals": oscillation_reversals},
        "control_stability": {"max_red_wait_seconds": adaptive["max_red_wait_sec"], "max_green_duration_by_approach": {a:max(v) for a,v in decisions_by_approach.items() if v},
                              "mean_green_duration_by_approach": {a:statistics.mean(v) for a,v in decisions_by_approach.items() if v},
                              "unnecessary_changes_constant_demand": "not measurable; the three-cycle scenario includes only one steady-demand cycle"},
        "queue_comparison": {"model": "existing simplified deterministic saturation-flow approximation", "same_frozen_exogenous_arrivals": True, "fixed": fixed, "adaptive": adaptive},
        "safety": {"checks_performed": len(safety_checks), "timeline_violations": sum(not (r["timing_ok"] and r["no_controller_conflict"] and r["no_actuator_conflict"] and r["acknowledged_state_matches_actuator"]) for r in safety_checks),
                   "stop_reset_actuator_violations": int(not (stop_safe_result and stop_safe_result["status"] == "stop_confirmed" and stop_safe_result["red_acknowledgements"] == 4 and stop_actuator_all_red)) + int(not (reset_safe_result and reset_safe_result["status"] == "reset_confirmed" and reset_safe_result["red_acknowledgements"] == 4 and all(v == "RED" for v in reset_actuator_states.values()))),
                   "violations": sum(not (r["timing_ok"] and r["no_controller_conflict"] and r["no_actuator_conflict"] and r["acknowledged_state_matches_actuator"]) for r in safety_checks) + int(not (stop_safe_result and stop_safe_result["status"] == "stop_confirmed" and stop_safe_result["red_acknowledgements"] == 4 and stop_actuator_all_red)) + int(not (reset_safe_result and reset_safe_result["status"] == "reset_confirmed" and reset_safe_result["red_acknowledgements"] == 4 and all(v == "RED" for v in reset_actuator_states.values()))),
                   "timing_envelope_violations": sum(not r["timing_ok"] for r in safety_checks), "conflicting_green_violations": sum(not (r["no_controller_conflict"] and r["no_actuator_conflict"]) for r in safety_checks),
                   "expected_negative_tests": negative_results, "all_negative_tests_rejected": all(r["passed"] for r in negative_results), "stop_all_red": stopped_red, "reset_cycle_zero": reset_cycle,
                   "stop_actuator_green_count": stop_actuator_green_count, "stop_actuator_all_red": stop_actuator_all_red,
                   "reset_actuator_green_count": reset_actuator_green_count,
                   "successful_stop": stop_safe_result, "successful_reset": reset_safe_result,
                   "fault_injection_stop_pending": fault_stop_pending, "fault_injection_stop_result": fault_stop_result,
                   "fault_injection_reset_pending": fault_reset_pending, "fault_injection_reset_result": fault_reset_result,
                   "scope": "observed controller snapshots, actuator states and transition records; no physical signal hardware"},
        "environment": {"transport": "synchronous in-process loopback", "database": "none opened or written; service persistence disabled", "real_mqtt_network": False, "physical_hardware": False},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _save_json(output_dir/"closed_loop_summary.json", metrics)
    _write_csv(output_dir/"closed_loop_summary.csv", [{"section": section, "metric": key, "value": json.dumps(value) if isinstance(value,(dict,list)) else value}
                                                       for section,obj in metrics.items() if isinstance(obj,dict) for key,value in obj.items()])
    latency_samples = latency_rows + stimulus_rows
    latency_columns = list(dict.fromkeys(key for row in latency_samples for key in row))
    output_dir.joinpath("closed_loop_latency_samples.csv").parent.mkdir(parents=True, exist_ok=True)
    with (output_dir/"closed_loop_latency_samples.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=latency_columns); writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in latency_columns} for row in latency_samples)
    _write_csv(output_dir/"closed_loop_phase_events.csv", phase_rows)
    _write_csv(output_dir/"closed_loop_control_stability.csv", [{"approach": a,"green_durations_seconds":json.dumps(v),"changes_seconds":json.dumps([b-a for a,b in zip(v,v[1:])])} for a,v in decisions_by_approach.items()])
    _save_json(output_dir/"closed_loop_safety_checks.json", {"summary":metrics["safety"],"checks":safety_checks})
    _write_csv(output_dir/"closed_loop_fixed_vs_adaptive.csv", queue_rows)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    figure_dir.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(9,5)); vals=[r["latency_ms"] for r in latency_rows if r["status"]=="success"]
    ax.plot(range(1,len(vals)+1),vals,marker="."); ax.set_xlabel("Sample"); ax.set_ylabel("Application latency (ms)"); ax.set_title("In-process software latency (no network)"); fig.tight_layout(); fig.savefig(figure_dir/"closed_loop_latency.png",dpi=140); plt.close(fig)
    fig, ax = plt.subplots(figsize=(9,5)); counts={state:sum(r["state"]==state for r in phase_rows) for state in ("GREEN","YELLOW","ALL_RED","RED")}; ax.bar(counts.keys(),counts.values()); ax.set_ylabel("Observed entries"); ax.set_title("Actual controller phase transitions"); fig.tight_layout(); fig.savefig(figure_dir/"closed_loop_phase_transitions.png",dpi=140); plt.close(fig)
    fig, ax = plt.subplots(figsize=(9,5)); ax.bar([r["metric"] for r in queue_rows[:3]],[r["adaptive"] for r in queue_rows[:3]],label="Adaptive"); ax.bar([r["metric"] for r in queue_rows[:3]],[r["fixed"] for r in queue_rows[:3]],alpha=.6,label="Fixed"); ax.tick_params(axis="x",rotation=20); ax.legend(); ax.set_title("Queue model trade-offs (same arrivals)"); fig.tight_layout(); fig.savefig(figure_dir/"closed_loop_fixed_vs_adaptive.png",dpi=140); plt.close(fig)
    fig, ax = plt.subplots(figsize=(9,5)); ax.plot([r["sequence"] for r in phase_rows],[r["duration_sec"] for r in phase_rows],marker="."); ax.set_xlabel("Recorded event"); ax.set_ylabel("Phase duration (s)"); ax.set_title("Green and clearance timing stability"); fig.tight_layout(); fig.savefig(figure_dir/"closed_loop_control_stability.png",dpi=140); plt.close(fig)
    service.stop(); actuator.stop_heartbeat()
    print(f"Closed-loop evaluation: {metrics['phase_events']['actual_state_transitions']} observed phase events; {metrics['safety']['checks_performed']} safety checks; outputs in {output_dir}")
    return metrics


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description="Run isolated closed-loop evaluation")
    parser.add_argument("--cycles",type=int,default=3)
    args=parser.parse_args(argv)
    try: run_closed_loop(cycles=args.cycles); return 0
    except Exception as exc: print(f"Closed-loop evaluation failed: {exc}"); return 1


if __name__ == "__main__": raise SystemExit(main())
