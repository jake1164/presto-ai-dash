"""Regression checks using only temporary profiles and synthetic HTTP responses."""

import json
from pathlib import Path
from unittest.mock import patch

from providers.test_claude_oauth_usage import Base, NOW, SECRET, Opener, Reply, http_error, oauth


class IdentityTests(Base):
    def test_no_attestation_fails_closed_even_with_cached_account(self):
        Path(self.poller().identity_file.path).unlink()
        self.assertEqual(self.poll()["reason"], "identity_unverified")
        self.assertEqual(self.count(), 0)

    def test_account_switch_latches_across_restart_and_reversion(self):
        path = self.profile / ".claude.json"
        original = path.read_bytes()
        path.write_text(json.dumps({"cachedUsageUtilization": {"accountUuid": "other"}}))
        self.assertEqual(self.poll()["reason"], "identity_unverified")
        path.write_bytes(original)
        self.poller().reenable()  # lifting a provider stop is not identity attestation
        self.assertEqual(self.poll()["reason"], "identity_unverified")
        self.assertTrue(self.poller().attest(NOW))
        self.assertTrue(self.poll()["attempted"])

    def test_token_switch_requires_explicit_reattestation(self):
        self.write_credentials(token="different-synthetic-token")
        self.assertEqual(self.poll()["reason"], "identity_unverified")
        self.assertTrue(self.poller().attest(NOW))
        self.assertTrue(self.poll()["attempted"])

    def test_changed_daemon_destination_suspends_mapping(self):
        self.assertEqual(self.poll(self.poller(destination=lambda: "other-destination"))["reason"],
                         "identity_unverified")
        self.assertEqual(self.poll()["reason"], "identity_unverified")
        self.assertEqual(self.count(), 0)

    def test_captured_delivery_token_must_match_attested_destination(self):
        poller = self.poller()
        self.assertFalse(poller.destination_verified("other-destination"))
        self.assertFalse(poller.destination_verified("fixture-destination"))
        self.assertEqual(self.poll()["reason"], "identity_unverified")

    def test_missing_or_conflicting_cache_identity_is_not_attestable(self):
        path = self.profile / ".claude.json"
        for document in ({}, {"cachedUsageUtilization": {"accountUuid": "a"},
                             "oauthAccount": {"accountUuid": "b"}}):
            path.write_text(json.dumps(document))
            self.assertFalse(self.poller().attest(NOW))
            self.assertFalse(self.poll()["attempted"])

    def test_switch_during_request_discards_result(self):
        outer = self
        class Switching(Opener):
            def open(self, request, timeout=None):
                outer.write_credentials(token="new-account-token")
                return super().open(request, timeout)
        result = self.poll(self.poller(Switching()))
        self.assertTrue(result["attempted"])
        self.assertIsNone(result["reading"])
        self.assertFalse(self.poll(now=NOW + 600, seconds_ago=1)["attempted"])

    def test_profiles_share_budget_even_for_distinct_attestations(self):
        self.poll()
        other = Path(self.temp.name) / "second"
        other.mkdir()
        (other / ".credentials.json").write_bytes(self.credentials.read_bytes())
        (other / ".claude.json").write_bytes((self.profile / ".claude.json").read_bytes())
        poller = oauth.OAuthPoller(str(other / "settings.json"), self.state_dir,
                                  opener=self.opener, environ={},
                                  destination=lambda: "fixture-destination")
        self.assertTrue(poller.attest(NOW))
        self.assertEqual(self.poll(poller, now=NOW + 60, seconds_ago=1)["reason"], "cooldown")
        self.assertEqual(self.count(), 1)

    def test_attestation_never_resets_provider_backoff(self):
        self.opener = Opener(http_error(429))
        self.poll()
        self.assertTrue(self.poller().attest(NOW + 60))
        self.assertEqual(self.poll(now=NOW + 60, seconds_ago=1)["reason"], "backoff")


class RecoveryTests(Base):
    def test_lost_rate_limit_outcome_blocks_every_restarted_collector(self):
        self.opener = Opener(http_error(429))
        real = oauth.StateFile.update
        def lose_outcome(file, change, repair=False):
            if "_request" in change.__qualname__:
                return None
            return real(file, change, repair)
        with patch.object(oauth.StateFile, "update", lose_outcome):
            self.poll()
        result = self.poll(now=NOW + 86400, seconds_ago=1)
        self.assertEqual(result["reason"], "request_incomplete")
        self.assertEqual(self.count(), 1)

    def test_damaged_counters_and_state_types_fail_closed(self):
        path = Path(oauth.state_path(self.state_dir, self.settings))
        for changes in ({"failures": []}, {"rate_limited": -1}, {"history": [None]},
                        {"in_flight": "no"}, {"export_age": "bad"}, {"stopped": []}):
            path.write_text(json.dumps(dict(oauth.new_state(), **changes)))
            self.assertEqual(self.poll()["reason"], "state_unreadable", changes)
        self.assertEqual(self.count(), 0)

    def test_nonfinite_expiry_and_hook_are_rejected(self):
        self.write_credentials(expires=float("inf"))
        self.assertEqual(self.poll()["reason"], "credentials_unavailable")
        path = Path(oauth.hook_path(self.state_dir, self.settings))
        path.write_text('{"at": NaN}')
        self.assertIsNone(oauth.read_hook_signal(self.state_dir, self.settings))

    def test_very_long_retry_after_cannot_raise_or_shorten_wait(self):
        self.assertEqual(oauth.retry_after_seconds("9" * 10000, NOW),
                         oauth.MAX_RETRY_AFTER_SECONDS)
