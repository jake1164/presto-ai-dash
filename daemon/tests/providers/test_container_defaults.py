"""The source form in a container: URLs on this computer must not say localhost."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.sources import CONTAINER_HOST, in_container, source_catalog


def url_fields(catalog):
    return {(entry["id"], field["key"]): field
            for entry in catalog for field in entry["fields"] if field["type"] == "url"}


class ContainerDefaultsTests(unittest.TestCase):
    def test_a_container_is_told_by_the_image_or_a_marker_file(self):
        never = lambda path: False
        self.assertFalse(in_container({}, exists=never))
        self.assertTrue(in_container({"DASH_IN_CONTAINER": "1"}, exists=never))
        self.assertFalse(in_container({"DASH_IN_CONTAINER": "0"}, exists=never))
        self.assertTrue(in_container({}, exists=lambda path: path == "/.dockerenv"))
        self.assertTrue(in_container({}, exists=lambda path: path == "/run/.containerenv"))

    def test_outside_a_container_the_defaults_are_localhost(self):
        fields = url_fields(source_catalog(container=False))
        self.assertEqual(fields[("ollama", "base_url")]["default"], "http://localhost:11434")
        self.assertTrue(all("note" not in field for field in fields.values()))

    def test_inside_a_container_the_defaults_reach_the_host_and_say_why(self):
        fields = url_fields(source_catalog(container=True))
        self.assertEqual(fields[("ollama", "base_url")]["default"],
                         "http://%s:11434" % CONTAINER_HOST)
        self.assertEqual(fields[("litellm", "base_url")]["default"],
                         "http://%s:4000" % CONTAINER_HOST)
        for field in fields.values():
            if "localhost" in field["default"]:
                self.fail("a localhost default is left: %r" % field)
            if CONTAINER_HOST in field["default"]:
                self.assertIn(CONTAINER_HOST, field["note"])

    def test_the_class_fields_are_not_changed(self):
        source_catalog(container=True)
        fields = url_fields(source_catalog(container=False))
        self.assertEqual(fields[("ollama", "base_url")]["default"], "http://localhost:11434")


class ClaudeLocalFieldsTests(unittest.TestCase):
    """The Claude fields that read this machine's files, in a container."""

    LOCAL = {"settings_path", "activity_logs", "active_seconds",
             "usage_cache", "usage_cache_path"}

    def claude(self, container, present):
        from presto_ai_dash.sources import claude
        original = claude.ClaudeSource.local_files_present
        claude.ClaudeSource.local_files_present = staticmethod(lambda *a, **k: present)
        try:
            entry = next(e for e in source_catalog(container=container) if e["id"] == "claude")
        finally:
            claude.ClaudeSource.local_files_present = original
        return entry

    def hidden(self, entry):
        return {f["key"] for f in entry["fields"] if f.get("hidden")}

    def test_a_container_without_claude_files_hides_the_local_readers(self):
        entry = self.claude(container=True, present=False)
        self.assertTrue(entry["collector_only"])
        self.assertEqual(self.hidden(entry), self.LOCAL)

    def test_the_stale_thresholds_stay_and_say_they_are_a_collectors(self):
        entry = self.claude(container=True, present=False)
        fields = {f["key"]: f for f in entry["fields"]}
        for key in ("stale_after_seconds", "usage_cache_stale_seconds"):
            self.assertNotIn("hidden", fields[key])
        self.assertIn("collector", fields["usage_cache_stale_seconds"]["label"])

    def test_files_the_user_mounted_keep_every_field_shown(self):
        entry = self.claude(container=True, present=True)
        self.assertFalse(entry["collector_only"])
        self.assertEqual(self.hidden(entry), set())

    def test_the_files_decide_not_the_container(self):
        # A server that is not a container and has no Claude profile is treated
        # the same way; a machine with one keeps the fields.
        gone = self.claude(container=False, present=False)
        self.assertTrue(gone["collector_only"])
        self.assertEqual(self.hidden(gone), self.LOCAL)
        here = self.claude(container=False, present=True)
        self.assertFalse(here["collector_only"])
        self.assertEqual(self.hidden(here), set())

    def test_every_source_declares_collector_only(self):
        for entry in source_catalog(container=True):
            self.assertIn("collector_only", entry)
        self.assertFalse(any(e["collector_only"] for e in source_catalog(container=True)
                             if e["id"] != "claude"))

    def test_local_files_are_found_by_existence_only(self):
        import tempfile
        from presto_ai_dash.sources.claude import ClaudeSource
        with tempfile.TemporaryDirectory() as home:
            self.assertFalse(ClaudeSource.local_files_present({}, home))
            Path(home, ".claude").mkdir()
            self.assertTrue(ClaudeSource.local_files_present({}, home))
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as custom:
            self.assertTrue(ClaudeSource.local_files_present({"CLAUDE_CONFIG_DIR": custom}, home))


if __name__ == "__main__":
    unittest.main()
