"""Acceptance variants are complete authenticated bundles from shared source."""
import unittest
import presto_path
import presto_ota_acceptance as acceptance
from presto_ai_dash.device_app import slot_inventory


class CandidateTests(unittest.TestCase):
    def test_each_variant_has_an_exact_verifiable_slot_identity(self):
        for scenario in ('success', 'crash', 'hang'):
            with self.subTest(scenario=scenario):
                bundle = acceptance.candidate(scenario)
                inventory = slot_inventory(bundle)
                self.assertIsNotNone(inventory)
                self.assertEqual(inventory['revision'], bundle['slot']['revision'])
                self.assertNotIn('config.py', inventory['files'])
                self.assertNotIn('secrets.py', inventory['files'])
                for name, data in inventory['files'].items():
                    compile(data, name, 'exec')

    def test_success_variants_are_distinct_without_altering_app_logic(self):
        a, b = acceptance.candidate('success'), acceptance.candidate('success')
        self.assertNotEqual(a['slot']['revision'], b['slot']['revision'])
        self.assertEqual(a['slot']['files']['ai_dash_app.py'], b['slot']['files']['ai_dash_app.py'])
