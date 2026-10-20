"""Boolean source-config fields: validation, admin HTTP round trip, page assets."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import unittest
import json

from presto_ai_dash import sources
from presto_ai_dash.management import validate_settings
from presto_ai_dash.sources import Source, source_catalog
from test_management import AdminServerBase, ManagementBase

FIELDS = (
    {"key": "profile", "label": "Profile path", "type": "string", "default": "~/p.json"},
    {"key": "flag", "label": "Read local activity", "type": "bool", "default": False},
    {"key": "seconds", "label": "In-use window", "type": "int", "default": 300,
     "minimum": 30, "maximum": 3600},
)


class FlagSource(Source):
    """A test-only source type carrying one bool field, registered per test."""
    id = "boolflagtest"
    name = "Bool flag test"
    config_fields = FIELDS


class RegistrationBase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        sources._REGISTRY[FlagSource.id] = FlagSource
        self.addCleanup(sources._REGISTRY.pop, FlagSource.id, None)


class BoolValidationTests(unittest.TestCase):
    def test_real_booleans_are_accepted(self):
        for value in (True, False):
            with self.subTest(value=value):
                settings = validate_settings(FlagSource, {"profile": "~/p.json",
                                                          "flag": value, "seconds": 300})
                self.assertIs(settings["flag"], value)

    def test_missing_key_falls_back_to_the_field_default(self):
        self.assertIs(validate_settings(FlagSource, {})["flag"], False)

    def test_strings_numbers_and_none_are_rejected(self):
        for value in ("true", "False", "1", 1, 0, None, [], {}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_settings(FlagSource, {"flag": value})

    def test_other_field_types_still_validate(self):
        # Adding the bool branch must not disturb string, url or int handling.
        self.assertEqual(validate_settings(FlagSource, {"seconds": 60})["seconds"], 60)
        with self.assertRaises(ValueError):
            validate_settings(FlagSource, {"seconds": 29})
        with self.assertRaises(ValueError):
            validate_settings(FlagSource, {"profile": 5})


class BoolCatalogTests(RegistrationBase):
    def test_source_catalog_exposes_the_bool_field_type(self):
        entry = next(item for item in source_catalog() if item["id"] == FlagSource.id)
        field = next(item for item in entry["fields"] if item["key"] == "flag")
        self.assertEqual(field["type"], "bool")
        self.assertIs(field["default"], False)


class BoolAdminRoundTripTests(RegistrationBase, ManagementBase):
    def test_bool_settings_round_trip_through_save_and_list(self):
        self.disable_fake()
        row = self.manager.save({"type": FlagSource.id, "name": "Flagged", "enabled": True,
                                 "config": {"profile": "x", "flag": True, "seconds": 120}})
        self.assertIs(row["config"]["flag"], True)
        listed = next(item for item in self.manager.list_sources()["sources"]
                      if item["type"] == FlagSource.id)
        self.assertIs(listed["config"]["flag"], True)
        edited = self.manager.save({"revision": row["revision"],
                                    "config": {"profile": "x", "flag": False, "seconds": 120}},
                                   row["id"])
        self.assertIs(edited["config"]["flag"], False)
        with self.assertRaises(ValueError):
            self.manager.save({"type": FlagSource.id, "config": {"flag": "true"}})


class BoolAdminHttpTests(RegistrationBase, AdminServerBase):
    def test_create_edit_and_catalog_keep_a_real_boolean_over_http(self):
        status, _, listed = self.request()
        self.assertEqual(status, 200)
        field = next(item for item in
                     next(t for t in listed["types"] if t["id"] == FlagSource.id)["fields"]
                     if item["key"] == "flag")
        self.assertEqual(field["type"], "bool")
        status, _, row = self.request(method="POST", data={
            "type": FlagSource.id, "name": "HTTP flag",
            "config": {"profile": "x", "flag": True, "seconds": 120}})
        self.assertEqual(status, 201)
        self.assertIs(row["config"]["flag"], True)
        path = "/api/admin/sources/" + row["id"]
        status, _, edited = self.request(path, "PUT", {
            "revision": row["revision"], "config": {"profile": "x", "flag": False,
                                                    "seconds": 120}})
        self.assertEqual(status, 200)
        self.assertIs(edited["config"]["flag"], False)
        status, _, body = self.request(method="POST", data={
            "type": FlagSource.id, "name": "Bad flag", "config": {"flag": "true"}})
        self.assertEqual(status, 400)
        self.assertNotIn("Bad flag", json.dumps(body))
        self.assertEqual([item["name"] for item in self.manager.list_sources()["sources"]
                          if item["type"] == FlagSource.id], ["HTTP flag"])


class BoolPageAssetTests(AdminServerBase):
    def test_page_asset_renders_bool_fields_as_checkboxes(self):
        _, _, script = self.request("/admin.js")
        for marker in (b'"bool"', b'"checkbox"', b".checked"):
            self.assertIn(marker, script)


if __name__ == "__main__":
    unittest.main()
