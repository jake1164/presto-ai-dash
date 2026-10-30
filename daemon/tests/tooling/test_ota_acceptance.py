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


class PowerCutTests(unittest.TestCase):
    def prompts(self, cut, lines):
        seen = set()
        return [message for message in (acceptance.cut_prompt(cut, seen, line) for line in lines) if message]

    def test_the_download_cut_is_called_once_a_file_stream_is_open(self):
        lines = ['AI Dash: update TLS connect', 'AI Dash: update request sent',
                 'AI Dash: update transfer recorded', 'AI Dash: update request sent',
                 'AI Dash: update request sent']
        messages = self.prompts('download', lines)
        # The manifest request before the transfer is recorded is not a cut point.
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith(acceptance.CUT_NOW))

    def test_the_flash_cut_warns_first_then_calls_when_the_screen_freezes(self):
        messages = self.prompts('flash', ['AI Dash: update transfer recorded',
                                          'AI Dash: update writing slot b',
                                          'AI Dash: update writing slot b'])
        self.assertEqual(len(messages), 2)
        self.assertFalse(messages[0].startswith(acceptance.CUT_NOW))
        self.assertTrue(messages[1].startswith(acceptance.CUT_NOW))

    def test_no_cut_means_no_prompts_and_confirm_is_not_a_serial_prompt(self):
        lines = ['AI Dash: update transfer recorded', 'AI Dash: update writing slot b']
        self.assertEqual(self.prompts(None, lines), [])
        self.assertEqual(self.prompts('confirm', lines), [])

    def journal(self, **overrides):
        value = {'source': 'primary', 'active': 'a', 'confirmed': 'a', 'pending': False,
                 'transfer': False, 'failure': None, 'success': False}
        value.update(overrides)
        return value

    def test_a_download_or_flash_cut_must_leave_a_transfer_and_end_failed_on_the_old_slot(self):
        for cut in ('download', 'flash'):
            with self.subTest(cut=cut):
                cut_state = self.journal(transfer=True)
                recovered = self.journal(failure={'phase': 'failed', 'error': 'connection_lost'})
                self.assertEqual(acceptance.cut_verdict(cut, cut_state, recovered, 'a'), [])

    def test_a_confirm_cut_must_roll_back_to_the_old_slot(self):
        cut_state = self.journal(active='b', pending=True)
        recovered = self.journal(failure={'phase': 'rolled_back', 'error': 'boot_crash'})
        self.assertEqual(acceptance.cut_verdict('confirm', cut_state, recovered, 'a'), [])

    def test_every_wrong_outcome_is_reported(self):
        failed = {'phase': 'failed', 'error': 'connection_lost'}
        cases = {
            'no interrupted transfer': (self.journal(), self.journal(failure=failed), 'a'),
            'wrong failure': (self.journal(transfer=True),
                              self.journal(failure={'phase': 'rolled_back', 'error': 'boot_crash'}), 'a'),
            'transfer left behind': (self.journal(transfer=True),
                                     self.journal(transfer=True, failure=failed), 'a'),
            'pending left behind': (self.journal(transfer=True),
                                    self.journal(pending=True, failure=failed), 'a'),
            'slot changed': (self.journal(transfer=True),
                             self.journal(active='b', confirmed='b', failure=failed), 'a'),
        }
        for name, (cut_state, recovered, before) in cases.items():
            with self.subTest(name):
                self.assertTrue(acceptance.cut_verdict('download', cut_state, recovered, before))
        self.assertTrue(acceptance.cut_verdict('download', None, self.journal(), 'a'))


class UsbProbeTests(unittest.TestCase):
    def test_every_public_probe_first_resets_to_the_launcher(self):
        from unittest.mock import patch
        order = []

        class Done:
            returncode = 0
            stdout = 'OTA_RESULT:{"ok": true}\n'

        def probe(*args, **kwargs):
            order.append('probe')
            return Done()

        with patch.object(acceptance, 'reset_to_launcher', lambda port: order.append('reset')), \
                patch.object(acceptance.subprocess, 'run', probe):
            self.assertEqual(acceptance.public_result('TESTPORT', 'pass'), {'ok': True})
        self.assertEqual(order, ['reset', 'probe'])
