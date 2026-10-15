"""Static validation of both the classic Node-RED UI and custom /dashboard page."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FLOWS_PATH = PROJECT_ROOT / "node_red" / "flows.json"
PYTHON_BASE_CLIENT_ID = "team9-traffic-sim"


class FlowValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nodes = json.loads(FLOWS_PATH.read_text(encoding="utf-8"))
        cls.by_id = {n["id"]: n for n in cls.nodes}

    def test_every_node_has_id_and_type(self):
        for node in self.nodes:
            self.assertIn("id", node, node)
            self.assertIn("type", node, node)

    def test_no_id_equals_own_type(self):
        self.assertEqual([n for n in self.nodes if n["id"] == n["type"]], [])

    def test_no_duplicate_ids(self):
        ids = [n["id"] for n in self.nodes]
        self.assertEqual({i for i in ids if ids.count(i) > 1}, set())

    def test_wires_reference_existing_nodes(self):
        ids = set(self.by_id)
        for node in self.nodes:
            for output in node.get("wires", []) or []:
                for target in output:
                    self.assertIn(target, ids, f"{node['id']} wires to missing {target}")

    def test_required_dashboard_nodes_are_restored(self):
        expected = {
            "ui_base_config": "ui_base",
            "ui_board": "ui_template",
            "ui_lab": "ui_template",
            "ui_signal_panel": "ui_template",
        }
        for node_id, node_type in expected.items():
            self.assertEqual(self.by_id[node_id]["type"], node_type)
        self.assertNotEqual(self.by_id["ui_base_config"]["id"], "ui_base")
        self.assertEqual(self.by_id["ui_group_lab"]["tab"], "ui_tab_traffic")
        self.assertEqual(self.by_id["ui_board"]["group"], "ui_group_live")
        self.assertEqual(self.by_id["ui_lab"]["group"], "ui_group_lab")
        self.assertEqual(self.by_id["ui_signal_panel"]["group"], "ui_group_phase")

    def test_dashboard_widgets_are_wired_to_live_data(self):
        expected_targets = {
            "fn_dash": {"ui_board", "ui_chart_q", "ui_strip"},
            "fn_signal_parse": {"ui_signal_panel", "ui_strip"},
            "fn_timing_parse": {"ui_timing_panel"},
            "fn_test_parse": {"ui_lab"},
            "fn_status": {"ui_strip"},
            "fn_act_cmd": {"ui_signal_panel", "ui_strip"},
            "fn_act_ack": {"ui_signal_panel", "ui_strip"},
        }
        for source, targets in expected_targets.items():
            actual = {target for output in self.by_id[source]["wires"] for target in output}
            self.assertTrue(targets <= actual, f"{source}: missing {targets - actual}")
        self.assertEqual(self.by_id["ui_lab"]["wires"], [["mqtt_out_test"]])

    def test_sensor_and_handshake_subscriptions_present(self):
        topics = {n.get("topic", "") for n in self.nodes if n["type"] == "mqtt in"}
        self.assertTrue(any("sensors/#" in t for t in topics), topics)
        self.assertTrue(any("commands/#" in t for t in topics), topics)
        self.assertTrue(any("acks/#" in t for t in topics), topics)

    def test_actuator_health_is_heartbeat_driven(self):
        # A real heartbeat topic feeds a single health function that drives both
        # the Node-RED strip and the custom page, so they cannot disagree.
        self.assertEqual(self.by_id["mqtt_in_status"]["topic"], "team9/traffic/status")
        self.assertIn("fn_health", self.by_id["mqtt_in_status"]["wires"][0])
        self.assertIn("fn_health", self.by_id["inject_health"]["wires"][0])
        targets = {t for out in self.by_id["fn_health"]["wires"] for t in out}
        self.assertTrue({"ui_strip", "fn_ws_pack"} <= targets)
        fn = self.by_id["fn_health"]["func"]
        self.assertIn("actuator_beat", fn)
        self.assertIn('value = "stale"', fn)
        self.assertIn('value = "disconnected"', fn)
        self.assertIn('not responding', fn)
        # acks no longer drive the actuator chip on the strip
        strip = self.by_id["ui_strip"]["format"]
        self.assertIn("s.chip==='actuator'", strip)
        self.assertNotIn("not an actuator heartbeat", strip)

    def test_broker_client_id_does_not_collide_with_python_roles(self):
        for broker in (n for n in self.nodes if n["type"] == "mqtt-broker"):
            cid = broker.get("clientid") or ""
            self.assertNotEqual(cid, PYTHON_BASE_CLIENT_ID)
            self.assertFalse(cid.startswith(PYTHON_BASE_CLIENT_ID + "-"))


class DashboardPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nodes = json.loads(FLOWS_PATH.read_text(encoding="utf-8"))
        cls.by_id = {n["id"]: n for n in cls.nodes}
        cls.page = cls.by_id["tpl_dash"]["template"]

    def test_dashboard_route_serves_html(self):
        route = self.by_id["http_in_dash"]
        self.assertEqual(route["url"], "/dashboard")
        self.assertEqual(route["method"], "get")
        self.assertEqual(self.by_id["tpl_dash"]["syntax"], "plain")
        self.assertEqual(self.by_id["tpl_dash"]["format"], "html")
        self.assertEqual(self.by_id["tpl_dash"]["wires"], [["http_res_dash"]])

    def test_websocket_feed_and_input_share_one_listener(self):
        self.assertEqual(self.by_id["ws_listener"]["path"], "/ws/traffic")
        self.assertEqual(self.by_id["ws_out"]["server"], "ws_listener")
        self.assertEqual(self.by_id["ws_in"]["server"], "ws_listener")
        self.assertIn("/ws/traffic", self.page)

    def test_websocket_input_decodes_json_envelopes(self):
        # Keep wholemsg false so websocket-out sends the page's expected
        # {type, data} shape, and decode the incoming JSON string in the flow.
        self.assertEqual(self.by_id["ws_listener"]["wholemsg"], "false")
        fn = self.by_id["fn_ws_in"]["func"]
        self.assertIn("JSON.parse(p)", fn)
        self.assertIn('p.type === "publish"', fn)

    def test_all_feeds_reach_the_browser(self):
        for src in ("fn_dash", "fn_signal_parse", "fn_timing_parse", "fn_test_parse",
                    "fn_act_cmd", "fn_act_ack", "fn_status"):
            flat = [t for out in self.by_id[src]["wires"] for t in out]
            self.assertIn("fn_ws_pack", flat, src)
        self.assertEqual(self.by_id["fn_ws_pack"]["wires"], [["ws_out"]])

    def test_lab_publishes_to_test_input(self):
        self.assertEqual(self.by_id["fn_ws_in"]["wires"], [["mqtt_out_test"], ["ws_out"]])
        self.assertEqual(self.by_id["mqtt_out_test"]["topic"], "team9/traffic/test/input")

    def test_ui_is_the_secondary_component_view(self):
        # /ui is kept for raw widget debugging but is clearly secondary, and the
        # two surfaces point at each other so there is one obvious primary view.
        self.assertIn("component view", self.by_id["ui_tab_traffic"]["name"].lower())
        self.assertIn("/dashboard", self.by_id["ui_text_note"]["format"])
        self.assertIn('href="/ui"', self.page)

    def test_page_renders_heartbeat_health_and_timeouts(self):
        for needle in ("'health'", "S.health", "TIMED OUT", "command timed out"):
            self.assertIn(needle, self.page, needle)

    def test_page_renders_handshake_and_validates_lab(self):
        for needle in ("intended", "actuator", "APPLIED", "PENDING", "REJECTED",
                       "ARR/MIN", "WAIT s", 'step="1"', "r.ok===false", "nothing was applied"):
            self.assertIn(needle, self.page, needle)
        self.assertNotIn("<select", self.page)


if __name__ == "__main__":
    unittest.main()
