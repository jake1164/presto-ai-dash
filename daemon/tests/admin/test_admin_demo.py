"""Demo mode: the switch, the payloads it serves, and what it must never write.

Demo mode is a setting of this daemon, not a source and not an account. These
tests prove the switch follows the ingest switch's rules (the environment beats
the stored setting beats the file, off by default, resolved afresh on every
request), that the sample data never comes from a provider, and that the one
write a device can make - a bell tap - never reaches the stored settings.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from envutil import clean_env  # noqa: E402

from admin.test_management import AdminServerBase, ManagementBase  # noqa: E402

WEB_DIR = Path(__file__).resolve().parents[3] / "daemon" / "src" / "presto_ai_dash" / "web"


def read_web(name):
    with open(WEB_DIR / name, encoding="utf-8") as handle:
        return handle.read()

from presto_ai_dash import demo  # noqa: E402
from presto_ai_dash.api import ApiServer  # noqa: E402
from presto_ai_dash.config import (ConfigError, demo_enabled, load_config)  # noqa: E402
from presto_ai_dash.models import SourceSnapshot  # noqa: E402


class DemoSwitchTests(ManagementBase):
    def test_it_is_off_by_default(self):
        config = load_config()
        self.assertIs(config["demo"]["enabled"], False)
        self.assertIs(demo_enabled(config), False)
        self.assertIsNone(self.storage.demo_enabled_setting())

    def test_the_file_can_turn_it_on_and_a_bad_value_is_refused(self):
        path = Path(self.temp.name) / "demo.toml"
        path.write_text("[demo]\nenabled = true\n", encoding="utf-8")
        with clean_env():
            self.assertIs(load_config(path)["demo"]["enabled"], True)
        for text in ('[demo]\nenabled = "yes"', "[demo]\nenabled = 1"):
            with self.subTest(text=text):
                path.write_text(text, encoding="utf-8")
                with clean_env(), self.assertRaises(ConfigError):
                    load_config(path)

    def test_the_environment_accepts_the_documented_words(self):
        for value, expected in (("1", True), ("true", True), ("YES", True), ("On", True),
                                ("0", False), ("false", False), ("No", False), ("OFF", False)):
            with self.subTest(value=value):
                with clean_env(DASH_DEMO=value):
                    self.assertIs(load_config()["demo"]["enabled"], expected)
        for value in ("maybe", "2", ""):
            with self.subTest(value=value):
                with clean_env(DASH_DEMO=value), self.assertRaises(ConfigError):
                    load_config()

    def test_environment_beats_storage_beats_the_file(self):
        config = load_config()
        config["demo"]["enabled"] = True      # as a TOML file could set it
        self.assertIs(demo_enabled(config), True)
        # A stored admin setting beats the file.
        self.assertIs(demo_enabled(config, False), False)
        # The environment beats both, re-read on every call.
        self.assertIs(demo_enabled(config, True, {"DASH_DEMO": "0"}), False)
        self.assertIs(demo_enabled(config, False, {"DASH_DEMO": "yes"}), True)

    def test_a_garbled_environment_value_fails_closed(self):
        config = load_config()
        with self.assertLogs("presto_ai_dash.config", level="ERROR"):
            self.assertIs(demo_enabled(config, True, {"DASH_DEMO": "maybe"}), False)

    def test_the_stored_setting_round_trips_and_refuses_junk(self):
        self.assertIsNone(self.storage.demo_enabled_setting())
        self.storage.save_demo_enabled(True)
        self.assertIs(self.storage.demo_enabled_setting(), True)
        self.storage.save_demo_enabled(False)
        self.assertIs(self.storage.demo_enabled_setting(), False)
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.storage.save_demo_enabled(value)

    def test_the_walkthrough_payload_is_the_demo_module(self):
        """The switch serves demo.py's data, not a hand-written payload."""
        payload = demo.demo_payload(0)
        self.assertIs(payload["demo"], True)
        self.assertEqual({source["id"] for source in payload["sources"]},
                         {source_id for source_id, _type, _name in demo.ACCOUNTS})


def boom(*_args, **_kwargs):
    raise AssertionError("the real path was used in demo mode")


class DemoApiTests(ManagementBase):
    """The API serves the demo on both daemon paths, and writes nothing."""

    def serve(self, **kwargs):
        api = ApiServer("127.0.0.1", 0, self.storage, "device-token", **kwargs)
        api.start()
        self.addCleanup(api.stop)
        return "http://127.0.0.1:%d" % api.actual_port

    def managed(self, second=0, **extra):
        return self.serve(status_provider=self.manager.status_payload,
                          card_provider=self.manager.card_detail,
                          alert_writer=self.manager.set_service_alert_enabled,
                          demo_enabled=lambda: True,
                          demo_second=lambda: second, **extra)

    def unmanaged(self, second=0):
        return self.serve(demo_enabled=lambda: True, demo_second=lambda: second)

    def get(self, base, path):
        request = Request(base + path, headers={"Authorization": "Bearer device-token"})
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def post_alert(self, base, instance_id, enabled):
        body = json.dumps({"enabled": enabled}).encode("utf-8")
        request = Request(base + "/api/v1/alerts/" + instance_id, data=body, method="POST",
                          headers={"Authorization": "Bearer device-token",
                                   "Content-Type": "application/json"})
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def stored_settings(self):
        """Everything the daemon stores, so a write of any kind is visible."""
        with self.storage._lock:
            meta = {row["name"]: bytes(row["value"]) for row in
                    self.storage._conn.execute("SELECT name, value FROM configuration_meta")}
            instances = [dict(row) for row in
                         self.storage._conn.execute("SELECT * FROM source_instances"
                                                    " ORDER BY position, rowid")]
            snapshots = self.storage._conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        return meta, instances, snapshots

    # -- the payload ------------------------------------------------------

    def test_the_managed_path_serves_the_demo_payload(self):
        status, payload = self.get(self.managed(), "/api/v1/status")
        self.assertEqual(status, 200)
        self.assertIs(payload["demo"], True)
        self.assertEqual({source["id"] for source in payload["sources"]},
                         {source_id for source_id, _type, _name in demo.ACCOUNTS})
        # The device's cards are the demo's own, and its Home card is first.
        self.assertEqual(len(payload["cards"]), len(demo.ACCOUNTS) + 1)

    def test_the_unmanaged_path_serves_the_same_demo_payload(self):
        status, payload = self.get(self.unmanaged(second=48), "/api/v1/status")
        self.assertEqual(status, 200)
        self.assertIs(payload["demo"], True)
        # The readings are the loop's at 48 s. Only the timestamps differ:
        # the daemon stamps the payload with the moment it answered.
        at_48 = demo.demo_payload(48)
        self.assertEqual([source["id"] for source in payload["sources"]],
                         [source["id"] for source in at_48["sources"]])
        self.assertEqual(payload["overall_status"], at_48["overall_status"])
        self.assertEqual(
            [(window["label"], window["used_percent"])
             for source in payload["sources"]
             for window in source.get("quota", {}).get("windows", ())],
            [(window["label"], window["used_percent"])
             for source in at_48["sources"]
             for window in source.get("quota", {}).get("windows", ())])

    def test_the_lazy_poll_carries_no_card_lines(self):
        base = self.managed()
        status, full = self.get(base, "/api/v1/status")
        self.assertEqual(status, 200)
        self.assertTrue(any(card.get("detail") for card in full["cards"]))
        status, lazy = self.get(base, "/api/v1/status?cards=lazy")
        self.assertEqual(status, 200)
        self.assertTrue(all("detail" not in card for card in lazy["cards"]))
        self.assertEqual(len(lazy["cards"]), len(full["cards"]))

    def test_a_demo_card_detail_is_served_and_other_ids_are_not_found(self):
        base = self.managed()
        status, detail = self.get(base, "/api/v1/cards/demo-claude")
        self.assertEqual(status, 200)
        self.assertEqual(detail["id"], "demo-claude")
        self.assertIsInstance(detail["tiles"], list)
        self.assertIsInstance(detail["lines"], list)
        self.assertEqual(self.get(base, "/api/v1/cards/demo-nope")[0], 404)
        # A real account's id is not a demo card.
        real = self.manager.save({"type": "litellm", "name": "Gateway",
                                  "config": {"base_url": "http://localhost:4000"}})
        self.assertEqual(self.get(base, "/api/v1/cards/" + real["id"])[0], 404)

    # -- what demo mode must never do -------------------------------------

    def test_demo_ids_never_equal_a_real_source_id(self):
        real = self.manager.save({"type": "litellm", "name": "Gateway",
                                  "config": {"base_url": "http://localhost:4000"}})
        demo_ids = {source_id for source_id, _type, _name in demo.ACCOUNTS}
        self.assertNotIn(real["id"], demo_ids)
        self.assertTrue(all(instance_id.startswith("demo-") for instance_id in demo_ids))
        _, payload = self.get(self.managed(), "/api/v1/status")
        self.assertNotIn(real["id"], {source["id"] for source in payload["sources"]})

    def test_a_status_request_asks_no_real_provider_and_writes_nothing(self):
        before = self.stored_settings()
        base = self.serve(status_provider=boom, card_provider=boom,
                          alert_writer=boom, demo_enabled=lambda: True,
                          demo_second=lambda: 0)
        self.assertEqual(self.get(base, "/api/v1/status")[0], 200)
        self.assertEqual(self.get(base, "/api/v1/status?cards=lazy")[0], 200)
        self.assertEqual(self.get(base, "/api/v1/cards/demo-claude")[0], 200)
        self.assertEqual(self.stored_settings(), before)

    def test_a_bell_tap_changes_no_stored_setting(self):
        row = self.manager.save({"type": "litellm", "name": "Gateway", "enabled": True,
                                 "config": {"base_url": "http://localhost:4000"}})
        self.storage.save_snapshot(SourceSnapshot(row["id"], "ok",
                                                  metrics={"requests_today": 3}))
        before = self.stored_settings()
        base = self.managed()
        status, response = self.post_alert(base, "demo-claude", False)
        self.assertEqual(status, 200)
        self.assertEqual(response, {"ok": True, "alerts": False, "master": True})
        self.assertEqual(self.stored_settings(), before,
                         "a demo bell tap must not store anything")
        # The next poll carries the tap, in memory only.
        _, payload = self.get(base, "/api/v1/status")
        claude = next(source for source in payload["sources"]
                      if source["id"] == "demo-claude")
        self.assertIs(claude["alerts"], False)
        other = next(source for source in payload["sources"]
                     if source["id"] == "demo-codex")
        self.assertIs(other["alerts"], True)

    def test_a_tap_on_a_real_account_is_still_refused_in_demo_mode(self):
        real = self.manager.save({"type": "litellm", "name": "Gateway",
                                  "config": {"base_url": "http://localhost:4000"}})
        status, _ = self.post_alert(self.managed(), real["id"], False)
        self.assertEqual(status, 404)

    def test_demo_bell_updates_home_and_aggregate_without_hiding_service_status(self):
        base = self.managed(second=54)
        before = self.stored_settings()
        _, original = self.get(base, "/api/v1/status")
        self.assertEqual(original["overall_status"], "critical")
        self.assertEqual(self.post_alert(base, "demo-claude", False)[0], 200)
        _, payload = self.get(base, "/api/v1/status")
        self.assertEqual(payload["overall_status"], "ok")
        self.assertEqual(payload["cards"][0]["status"], "ok")
        self.assertEqual(payload["cards"][0]["accent"], "green")
        claude = next(source for source in payload["sources"] if source["id"] == "demo-claude")
        self.assertEqual(claude["status"], "critical")
        self.assertIs(claude["alerts"], False)
        self.assertEqual(self.stored_settings(), before)
        self.post_alert(base, "demo-claude", True)
        self.assertEqual(self.get(base, "/api/v1/status")[1]["overall_status"], "critical")

    def test_a_demo_prefixed_id_the_demo_does_not_produce_is_refused(self):
        """The in-memory map only ever holds ids demo.py really serves."""
        before = self.stored_settings()
        base = self.managed()
        for made_up in ("demo-unknown", "demo-", "demo-claude-2"):
            with self.subTest(instance_id=made_up):
                status, body = self.post_alert(base, made_up, False)
                self.assertEqual(status, 404)
                self.assertEqual(body, {"error": "not_found"})
        # A refusal changes nothing, and the next poll is unchanged.
        _, payload = self.get(base, "/api/v1/status")
        for source in payload["sources"]:
            self.assertIs(source["alerts"], True, source["id"])
        self.assertEqual(self.stored_settings(), before)

    def test_turning_demo_off_restores_the_real_payload_without_a_restart(self):
        state = {"on": True}
        row = self.manager.save({"type": "litellm", "name": "Gateway", "enabled": True,
                                 "config": {"base_url": "http://localhost:4000"}})
        self.storage.save_snapshot(SourceSnapshot(row["id"], "ok",
                                                  metrics={"requests_today": 3}))
        base = self.serve(status_provider=self.manager.status_payload,
                          demo_enabled=lambda: state["on"], demo_second=lambda: 0)
        _, payload = self.get(base, "/api/v1/status")
        self.assertIs(payload["demo"], True)
        state["on"] = False
        _, payload = self.get(base, "/api/v1/status")
        self.assertNotIn("demo", payload)
        self.assertEqual([source["id"] for source in payload["sources"]], [row["id"]])
        state["on"] = True
        _, payload = self.get(base, "/api/v1/status")
        self.assertIs(payload["demo"], True)


class DemoAdminRouteTests(AdminServerBase):
    """The admin page's demo switch: its route, its lock, and its controls."""

    def test_the_switch_round_trips_and_is_served_by_the_api(self):
        status, _, state = self.request("/api/admin/demo")
        self.assertEqual(status, 200)
        self.assertEqual(state, {"enabled": False, "locked_by_environment": False})
        status, _, state = self.request("/api/admin/demo", "PUT", {"enabled": True})
        self.assertEqual(status, 200)
        self.assertEqual(state, {"enabled": True, "locked_by_environment": False})
        self.assertIs(self.storage.demo_enabled_setting(), True)
        self.assertIs(self.manager.demo_state()["enabled"], True)
        status, _, state = self.request("/api/admin/demo", "PUT", {"enabled": False})
        self.assertEqual(status, 200)
        self.assertEqual(state["enabled"], False)

    def test_the_environment_locks_the_switch(self):
        with patch.dict(os.environ, {"DASH_DEMO": "1"}):
            status, _, state = self.request("/api/admin/demo")
            self.assertEqual(status, 200)
            self.assertEqual(state, {"enabled": True, "locked_by_environment": True})
            status, _, body = self.request("/api/admin/demo", "PUT", {"enabled": False})
            self.assertEqual(status, 409)
            self.assertIn("DASH_DEMO", body["error"])

    def test_only_a_boolean_is_accepted(self):
        for body in ({"enabled": "yes"}, {"enabled": 1}, {}, {"on": True}):
            with self.subTest(body=body):
                status, _, _ = self.request("/api/admin/demo", "PUT", body)
                self.assertEqual(status, 400)
        self.assertIsNone(self.storage.demo_enabled_setting())

    def test_the_page_carries_the_switch_the_banner_and_its_guide(self):
        status, _, html = self.request("/")
        self.assertEqual(status, 200)
        for marker in (b'id="demo-enabled"', b'id="demo-lock"', b'id="demo-banner"',
                       b'data-help="demo-mode"'):
            self.assertIn(marker, html)
        self.assertIn(b"Demo mode is ON", html)

    def test_the_empty_list_offers_sample_data(self):
        """A new reader finds demo mode without opening Settings."""
        _, _, html = self.request("/")
        for marker in (b'id="demo-callout"', b'id="demo-callout-button"',
                       b"Want to see how it works first?", b"Show sample data",
                       b'id="demo-callout-lock"'):
            self.assertIn(marker, html)
        js = read_web("admin.js")
        # The callout only exists while no account is switched on, and its
        # button is the same writer as the Settings switch.
        self.assertIn("sources.filter((source) => source.enabled).length", js)
        self.assertIn("enabled === 0)", js)
        self.assertIn(
            'element("demo-callout-button").addEventListener("click", '
            '() => setDemoEnabled(true))', js)
        self.assertIn('demo-callout-button").disabled = locked', js)

    def test_the_banner_says_services_are_hidden_and_can_turn_demo_off(self):
        _, _, html = self.request("/")
        for marker in (b'id="demo-banner-off"', b'id="demo-banner-hidden"',
                       b"Your services are hidden while demo is on.",
                       b'id="demo-banner-lock"'):
            self.assertIn(marker, html)
        js = read_web("admin.js")
        self.assertIn('demo-banner-hidden").hidden = !(on && sources.length > 0)', js)
        self.assertIn(
            'element("demo-banner-off").addEventListener("click", '
            '() => setDemoEnabled(false))', js)

    def test_adding_a_source_never_turns_demo_off(self):
        """One writer only: nothing in the save path touches demo mode."""
        js = read_web("admin.js")
        occurrences = js.count("setDemoEnabled(")
        # The definition plus one call for each of the three controls.
        self.assertEqual(occurrences, 4, "demo mode must be changed by its controls only")
        for call in ('setDemoEnabled(true))', 'setDemoEnabled(false))',
                     '() => setDemoEnabled(element("demo-enabled").checked))'):
            self.assertEqual(js.count(call), 1, call)
