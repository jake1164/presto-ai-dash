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


if __name__ == "__main__":
    unittest.main()
