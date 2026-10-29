"""Durable device identity, revision comparison, and the per-request status.

The coordinator is the only place that decides what the daemon believes about
one device. It is deliberately conservative:

* A report is **self-reported telemetry**, not proof. Nothing here claims a USB
  hash check, and ``usb_observation`` is always null: this work adds no remote
  receipt API.
* A known revision mismatch is ``manual_action`` with USB instructions, because
  there is no operational updater yet. It is never left ``pending``.
* Operational OTA is off. No command is issued, no confirmation is
  acknowledged, and the retry and recovery routes answer ``ota_not_enabled``.
  An injected updater lets the tests exercise the same code paths without
  turning anything on.
* Connection and update state are separate. A device that stops polling keeps
  its last-known update state, labelled as last-known rather than live.

State lives in the existing SQLite database so a container restart does not
erase an unresolved problem, and it holds only what the protocol needs: no
diagnostics, no addresses, no credentials.
"""

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from . import device_protocol as protocol
from .clients import sanitize_header
from .device_install import app_snapshot
from .device_updater import OtaUpdater, stamp as _updater_stamp, parse_stamp as _updater_parse
from .release import release_channel, release_version
from .sources import in_container
from .storage import ConfigurationConflict

log = logging.getLogger(__name__)

# The two headers a device may add to a status GET.
DEVICE_ID_HEADER = protocol.DEVICE_ID_HEADER
LIFECYCLE_HEADER = protocol.LIFECYCLE_HEADER
# The compatibility label the current app already sends.
REVISION_HEADER = "X-Presto-Revision"
NAME_HEADER = "X-Presto-Client"

# A cached daemon-release result older than this is stale, never "current".
RELEASE_MAX_AGE_S = 172800

# The device error codes that mean "a person must do something", and the ones
# that mean "an attempt failed but the previous app is intact".
GATE_ERRORS = ("storage_insufficient", "firmware_unsupported",
               "bootstrap_incompatible", "transport_untrusted",
               "signature_invalid", "manifest_invalid", "hash_mismatch")
FAILED_ERRORS = ("transfer_timeout", "connection_lost", "http_error",
                 "boot_crash", "rollback_failed", "local_state_invalid",
                 "unknown_error")

# The device phases that are progress, not a fault.
PROGRESS_PHASES = ("downloading", "verifying", "activating", "confirming")

# The most attempt rows the device detail returns, and the most retained.
ATTEMPT_DETAIL_LIMIT = 32
# An attempt in one of these states is still outstanding for its device.
OUTSTANDING_ATTEMPT = ("queued", "started", "confirming", "unconfirmed")


class AdminRequestError(Exception):
    """An admin mutation the daemon will not honour, with its HTTP status."""

    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


class DisabledUpdater(object):
    """No operational OTA.

    It never issues a permit, never acknowledges a confirmation, and never
    spends a retry. Part 2 replaces it, behind the hardware and deployment
    gates; until then every update path stays on USB.
    """

    enabled = False

    def permit(self, device, expected):
        """A command for this device, or None. There is none in this stage."""
        return None

    def confirm(self, device, report, attempt):
        """The attempt ID to acknowledge, or None. Nothing is acknowledged."""
        return None


def _stamp(moment):
    """A UTC RFC 3339 timestamp at second precision, ending in Z."""
    return datetime.fromtimestamp(float(moment), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_stamp(text):
    """Seconds since the epoch for a stored timestamp, or None."""
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _header_values(headers, name):
    """Every value a request carried for one header name.

    A repeated header is how a request says two things at once, so the caller
    treats more than one value as invalid rather than picking one.
    """
    getter = getattr(headers, "get_all", None)
    if getter is not None:
        try:
            values = getter(name)
        except Exception:  # pragma: no cover - defensive
            values = None
        if values is not None:
            return [value for value in values if value is not None]
    if not hasattr(headers, "get"):
        return []
    value = headers.get(name)
    return [] if value is None else [value]


def release_channel_name(environ=None):
    """The registry channel this running installation belongs to.

    A container image comes from GHCR; a wheel or checkout comes from PyPI. A
    locally built image is ``development`` and is never offered a release.
    """
    if in_container():
        return "ghcr" if release_channel(environ) == "published" else "development"
    return "pypi" if release_channel(environ) == "published" else "development"


class DeviceLifecycle(object):
    """The per-request lifecycle extension and the durable device records."""

    def __init__(self, storage, expected_provider=None, clock=None,
                 ota_enabled=False, updater=None, running_version=None,
                 channel=None):
        self.storage = storage
        self._expected_provider = expected_provider or app_snapshot
        self._clock = clock or time.time
        self.ota_enabled = bool(ota_enabled)
        if updater is not None:
            self.updater = updater
        elif self.ota_enabled:
            # Operational OTA exists, but only a configuration that passed the
            # deployment gate turns it on. The shipped default is off.
            self.updater = OtaUpdater(self._clock)
        else:
            self.updater = DisabledUpdater()
        self.running_version = running_version or release_version()
        self.channel = channel or release_channel_name()
        self._lock = threading.Lock()
        # Keep tuple validation, registry admission and the resulting commit
        # together across the status server's concurrent request threads.
        self._observation_lock = threading.Lock()
        # The highest (boot, seq) this process has seen, per device, with the
        # report that carried it. The table holds the checkpointed tuple.
        self._tuples = {}
        # Live last-seen times are separate from the throttled DB checkpoints.
        self._seen = {}
        self._identities_cache = None

    # -- the expected bundle ----------------------------------------------

    def _identities(self):
        """The identities of this daemon's own bundle: legacy and slot.

        Both are read from the running build and cached for the process. A
        bundle without a slot layout yields no slot identity, and then a
        slot-reporting device is ``incomparable`` rather than guessed at.
        """
        with self._lock:
            if self._identities_cache is not None:
                return self._identities_cache
        values = {"legacy": None, "slot": None, "bootstrap": None}
        try:
            bundle = self._expected_provider()
            values["legacy"] = protocol.bundle_identity(bundle)
            values["slot"] = protocol.slot_identity(bundle)
            values["bootstrap"] = protocol.bootstrap_version(bundle)
        except Exception:
            log.debug("the Presto app bundle identity could not be read", exc_info=True)
        with self._lock:
            self._identities_cache = values
        return values

    def expected(self):
        """The identity a legacy report is compared with, or None.

        Read from the running build, never from device input, a stored previous
        value, or the newest registry release. A bundle that cannot be read is
        reported as unavailable, and no update command follows from it.
        """
        values = self._identities()
        return values["legacy"] or values["slot"]

    def slot_expected(self):
        """The ``slot-v1`` identity this daemon can install, or None."""
        return self._identities()["slot"]

    def expected_for(self, scheme):
        """The expectation that belongs to a reported revision scheme.

        A legacy report is only ever compared with a legacy aggregate, and a
        slot report only with a slot hash. Passing the other scheme yields
        ``incomparable`` in ``protocol.compare``, which is the honest answer.
        """
        values = self._identities()
        if scheme == protocol.SCHEME_SLOT:
            return values["slot"] or values["legacy"]
        return values["legacy"] or values["slot"]

    # -- one status request ------------------------------------------------

    def observe(self, headers):
        """The ``device_lifecycle`` extension for this request, or None.

        ``headers`` is the request's header collection. Nothing is written for
        a legacy client, a rejected report, or a full registry, and a write
        failure degrades to ``unavailable`` rather than failing quota polling.
        """
        try:
            with self._observation_lock:
                return self._observe(headers)
        except Exception:
            log.exception("the device lifecycle report could not be processed")
            return self._unavailable_extension(None)

    def _observe(self, headers):
        identity = self.expected()
        device_values = _header_values(headers, DEVICE_ID_HEADER)
        lifecycle_values = _header_values(headers, LIFECYCLE_HEADER)
        if len(device_values) > 1 or len(lifecycle_values) > 1:
            return self._rejected(protocol.REPORT_INVALID, None, identity)
        device_id = (device_values[0] or "").strip() if device_values else ""
        raw = lifecycle_values[0] if lifecycle_values else None
        if not device_id:
            return self._legacy(headers, identity)
        if not protocol.valid_id(device_id):
            return self._rejected(protocol.REPORT_INVALID, None, identity)
        if raw is None:
            # A valid ID without a lifecycle report is not a durable device.
            return self._rejected(protocol.REPORT_INVALID, device_id, identity)
        try:
            report = protocol.parse_lifecycle(raw)
        except protocol.LifecycleError as exc:
            return self._rejected(exc.code, device_id, identity)
        revisions = _header_values(headers, REVISION_HEADER)
        if len(report.get("revision") or "") == protocol.SLOT_REVISION_CHARS and revisions:
            # A compatibility label cannot contradict the full report. The
            # old header also supports a full legacy hash, compared in full.
            label = revisions[0].strip().lower()
            if (len(revisions) != 1 or not protocol.valid_revision(
                    protocol.SCHEME_LEGACY, label) or
                    label != report["revision"][:len(label)]):
                return self._rejected(protocol.REPORT_INVALID, device_id, identity)
        return self._accepted(device_id, headers, report, identity)

    def _legacy(self, headers, identity):
        """An old client: the revision header only, remembered in memory.

        The header is read raw rather than through the 32-character label
        sanitizer: a full legacy hash is 64 characters, and dropping it would
        turn a comparable device into an ``unknown`` one.
        """
        raw = headers.get(REVISION_HEADER) if hasattr(headers, "get") else None
        scheme, revision = self._legacy_revision(raw)
        reported = protocol.reported_shape(scheme, revision, None)
        comparison = protocol.COMPARISON_UNKNOWN
        if scheme is not None and identity is not None:
            comparison = protocol.compare({"scheme": scheme, "revision": revision},
                                          identity)
        state, reason, attention = self._state_for(comparison, None, identity)
        return self._extension(None, protocol.REPORT_LEGACY, reported, identity,
                               comparison, state, reason, attention, None)

    @staticmethod
    def _legacy_revision(text):
        """A revision header as (scheme, value), or (None, None) when unusable."""
        if not isinstance(text, str):
            return None, None
        lowered = text.strip().lower()
        if not protocol.HASH_PATTERN.match(lowered):
            return None, None
        if len(lowered) not in (protocol.LEGACY_REVISION_CHARS,
                                protocol.SLOT_REVISION_CHARS):
            return None, None
        # A bare 12-character label is legacy telemetry, never slot-v1.
        return protocol.SCHEME_LEGACY, lowered

    def _rejected(self, code, device_id, identity):
        """A report the daemon will not use. Nothing is stored."""
        status = protocol.REPORT_UNSUPPORTED if code == protocol.REPORT_UNSUPPORTED \
            else protocol.REPORT_INVALID
        stored = self._read_device(device_id) if device_id is not None else None
        reason = "revision_uncomparable" if identity else "bundle_unavailable"
        if stored is not None and stored["attention_latched"]:
            reason = stored["reason"] or reason
        return self._extension(device_id, status, protocol.reported_shape(None, None, None),
                               identity, protocol.COMPARISON_UNKNOWN, "unknown",
                               reason, False, stored)

    def _unavailable_extension(self, device_id):
        return self._extension(device_id, protocol.REPORT_UNAVAILABLE,
                               protocol.reported_shape(None, None, None), None,
                               protocol.COMPARISON_UNKNOWN, "unknown",
                               "state_unavailable", False, None)

    def _accepted(self, device_id, headers, report, identity):
        """A well-formed report: compare it, settle the tuple, persist."""
        identity = self.expected_for(report["scheme"]) or identity
        stored = self._read_device(device_id)
        if stored is None and self._device_count() >= protocol.MAX_DEVICES:
            return self._extension(
                device_id, protocol.REPORT_REGISTRY_FULL,
                protocol.reported_shape(report["scheme"], report["revision"],
                                        report["bootstrap"]),
                identity, protocol.COMPARISON_UNKNOWN, "unknown",
                "registry_full", False, None)
        text = protocol.lifecycle_text(report)
        verdict = self._settle_tuple(device_id, report, text, stored)
        if verdict == protocol.REPORT_STALE:
            return self._extension(
                device_id, protocol.REPORT_STALE,
                protocol.reported_shape(report["scheme"], report["revision"],
                                        report["bootstrap"]),
                identity, protocol.COMPARISON_UNKNOWN, "unknown",
                stored["reason"] if stored else None, False, stored)
        if verdict == protocol.REPORT_INVALID:
            return self._rejected(protocol.REPORT_INVALID, device_id, identity)

        comparison = protocol.compare(
            {"scheme": report["scheme"], "revision": report["revision"]}, identity)
        budget = None
        attempt = None
        command = None
        confirmation = None
        force = False
        extra_budgets = []
        extra_attempts = []
        name = sanitize_header(headers.get(NAME_HEADER))
        overrides = None
        expired = None
        cancelled = None
        if self.ota_enabled and stored is not None:
            # A queued admin reconciliation is settled before any new permit,
            # so a fresh healthy report cannot be overtaken by another update.
            overrides, cancelled = self.settle_recovery(stored, report, comparison)
            if overrides is not None and "update_state" not in overrides:
                # An expired request clears the check, not the warning.
                expired, overrides = overrides, None
        if self.ota_enabled and overrides is None:
            budget = self._current_budget(device_id, identity)
            attempt = self._outstanding_attempt(stored)
            if attempt is not None:
                # The attempt's own generation is the row that must be
                # superseded if the daemon's target has moved on.
                budget = self._budget_for(device_id, attempt["generation_id"]) or budget
            # A first report still gets a permit: the device row is written in
            # the same transaction as the command, so nothing is issued before
            # it can be persisted.
            device = stored if stored is not None else {"device_id": device_id}
            outcome = self.updater.advance(device, report, identity, budget, attempt)
            state, reason, attention = outcome.state, outcome.reason, outcome.attention
            budget, attempt = outcome.budget, outcome.attempt
            command, confirmation = outcome.command, outcome.confirm_attempt_id
            extra_budgets = outcome.extra_budgets
            extra_attempts = outcome.extra_attempts
            force = outcome.changed
        elif overrides is None:
            state, reason, attention = self._state_for(comparison, report["update"],
                                                       identity)
        else:
            state, reason, attention = "current", None, False
        generation = budget["generation_id"] if budget else None
        active_attempt = (attempt["attempt_id"]
                          if attempt is not None and attempt["status"] in OUTSTANDING_ATTEMPT
                          else None)
        try:
            row = self._merge(stored, device_id, name, report, text, state,
                              reason, attention, generation, active_attempt,
                              force=force or overrides is not None
                              or expired is not None)
            if row is not None and overrides is not None:
                row = dict(row, **overrides)
                row["state_revision"] = int(row["state_revision"]) + 1
            if row is not None and expired is not None:
                row = dict(row, **expired)
                row["state_revision"] = int(row["state_revision"]) + 1
            if row is not None:
                budgets = list(extra_budgets) + ([budget] if budget is not None else [])
                attempts = (list(extra_attempts)
                            + ([attempt] if attempt is not None else []))
                if cancelled is not None:
                    attempts.append(cancelled)
                if budgets or attempts:
                    # The permit and the rows it names move together, so a
                    # restart never finds a command with no attempt.
                    self.storage.save_device_report(row, budgets or None,
                                                    attempts or None)
                else:
                    self.storage.save_device(row)
        except Exception:
            log.exception("the device record could not be written")
            return self._unavailable_extension(device_id)
        with self._lock:
            self._seen[device_id] = self._clock()
        settled = row if row is not None else stored
        return self._extension(
            device_id, protocol.REPORT_ACCEPTED,
            protocol.reported_shape(report["scheme"], report["revision"],
                                    report["bootstrap"]),
            identity, comparison, state, reason, attention, settled,
            command=command, confirm_attempt_id=confirmation)

    def _outstanding_attempt(self, stored):
        """The device's one attempt, when it is still open."""
        if stored is None or not stored.get("active_attempt_id"):
            return None
        try:
            attempt = self.storage.device_attempt(stored["active_attempt_id"])
        except Exception:
            log.debug("the device attempt could not be read", exc_info=True)
            return None
        if attempt is None or attempt["status"] not in OUTSTANDING_ATTEMPT:
            return None
        return attempt

    def _read_device(self, device_id):
        try:
            return self.storage.device(device_id)
        except Exception:
            log.exception("the device record could not be read")
            return None

    def _device_count(self):
        try:
            return self.storage.device_count()
        except Exception:
            log.exception("the device count could not be read")
            return 0

    def _settle_tuple(self, device_id, report, text, stored):
        """Compare this report's (boot, seq) with the highest seen.

        A lower tuple is ``stale``: it cannot regress a terminal state, spend a
        retry or clear a warning. An equal tuple is idempotent only when the
        report is identical, and ``invalid`` otherwise. This is ordering within
        one daemon's view, not adversarial replay protection.
        """
        with self._lock:
            known = self._tuples.get(device_id)
        if known is None and stored is not None:
            known = (int(stored["boot"]), int(stored["seq"]), stored["report_json"])
        if known is not None:
            known_boot, known_seq, known_text = known
            if (report["boot"], report["seq"]) < (known_boot, known_seq):
                return protocol.REPORT_STALE
            if (report["boot"], report["seq"]) == (known_boot, known_seq):
                return (protocol.REPORT_ACCEPTED if text == known_text
                        else protocol.REPORT_INVALID)
        with self._lock:
            self._tuples[device_id] = (report["boot"], report["seq"], text)
        return protocol.REPORT_ACCEPTED

    def _state_for(self, comparison, update, identity):
        """The daemon's derived update state, reason and attention for a report.

        The device's own reported phase wins where it says something a report
        can know; otherwise the comparison against the daemon's bundle decides.
        There is no operational updater, so a known mismatch asks for USB.
        """
        if update is not None and update.get("phase") not in (None, "idle"):
            phase = update["phase"]
            if phase in PROGRESS_PHASES:
                return "updating", None, False
            if phase == "blocked":
                return "manual_action", update.get("error") or "unknown_error", True
            if phase in ("failed", "rolled_back"):
                return "failed", update.get("error") or "unknown_error", True
            # A device that says it succeeded still has to match the daemon.
        if identity is None:
            return "unknown", "bundle_unavailable", False
        if comparison == protocol.COMPARISON_EQUAL:
            return "current", None, False
        if comparison == protocol.COMPARISON_DIFFERENT:
            return "manual_action", "usb_update_required", True
        return "unknown", "revision_uncomparable", False

    def _merge(self, stored, device_id, name, report, text, state, reason, attention,
               generation_id=None, attempt_id=None, force=False):
        """The device row for this report, keeping what a bad report cannot say."""
        now = self._clock()
        stamp = _stamp(now)
        previous_latched = bool(stored["attention_latched"]) if stored else False
        latched = bool(attention or previous_latched)
        if state == "current":
            # A supported recovery: the device reports the expected app.
            latched = False
        capabilities = report["caps"]
        if stored is not None and not capabilities and stored["capabilities_json"]:
            capabilities = json.loads(stored["capabilities_json"])
        previous_name = stored["name"] if stored else None
        if name == "unknown" and previous_name:
            name = previous_name
        if generation_id is None and stored is not None:
            generation_id = stored["active_generation_id"]
        if attempt_id is None and stored is not None and force is False:
            attempt_id = stored["active_attempt_id"]
        meaningful = (
            stored is None or
            stored["scheme"] != report["scheme"] or
            stored["revision"] != report["revision"] or
            stored["bootstrap_version"] != report["bootstrap"] or
            stored["slot"] != report["slot"] or
            bool(stored["boot_confirmed"]) != report["boot_confirmed"] or
            json.loads(stored["capabilities_json"]) != capabilities or
            stored["update_state"] != state or
            stored["reason"] != reason or
            bool(stored["attention_latched"]) != latched or
            stored["name"] != name or
            stored["active_generation_id"] != generation_id or
            stored["active_attempt_id"] != attempt_id)
        checkpoint_due = (
            stored is None or
            now - (_parse_stamp(stored["last_seen_at"]) or 0) >= protocol.CHECKPOINT_SECONDS)
        revision = int(stored["state_revision"]) if stored else 1
        if meaningful:
            revision += 1
        row = {
            "device_id": device_id,
            "name": name,
            "first_seen_at": stored["first_seen_at"] if stored else stamp,
            "last_seen_at": stamp if (meaningful or checkpoint_due) else stored["last_seen_at"],
            "scheme": report["scheme"],
            "revision": report["revision"],
            "bootstrap_version": report["bootstrap"],
            "capabilities_json": json.dumps(capabilities),
            "slot": report["slot"],
            "boot_confirmed": 1 if report["boot_confirmed"] else 0,
            "boot": report["boot"],
            "seq": report["seq"],
            "report_json": text,
            "update_state": state,
            "reason": reason,
            "attention_latched": 1 if latched else 0,
            "active_generation_id": generation_id,
            "active_attempt_id": attempt_id,
            "state_revision": revision,
            "recovery_request_id": stored["recovery_request_id"] if stored else None,
            "recovery_requested_at": stored["recovery_requested_at"] if stored else None,
            "recovery_after_boot": stored["recovery_after_boot"] if stored else None,
            "recovery_after_seq": stored["recovery_after_seq"] if stored else None,
            "recovery_target_scheme": stored["recovery_target_scheme"] if stored else None,
            "recovery_target_revision": stored["recovery_target_revision"] if stored else None,
            "last_recovery_at": stored["last_recovery_at"] if stored else None,
            "last_recovery_kind": stored["last_recovery_kind"] if stored else None,
        }
        if stored is not None and not meaningful and not checkpoint_due and not force:
            # Nothing changed and the checkpoint is not due: write nothing.
            return None
        return row

    # -- the extension -----------------------------------------------------

    def _extension(self, device_id, report_status, reported, identity, comparison,
                   state, reason, attention, row, command=None,
                   confirm_attempt_id=None):
        """The ``device_lifecycle`` object for one request.

        It describes only the requesting device. A command is present only when
        an enabled updater issued one for this device and persisted it first;
        with OTA off it is always null.
        """
        attempts_started = 0
        if row is not None:
            budget = self._current_budget(device_id, identity)
            attempts_started = int(budget["attempts_started"]) if budget else 0
        needs_attention = bool(attention)
        if row is not None and row.get("attention_latched"):
            needs_attention = True
        return {
            "v": protocol.PROTOCOL_VERSION,
            "device_id": device_id,
            "report_status": report_status,
            "reported": reported,
            "expected": protocol.expected_shape(identity),
            "comparison": comparison,
            "update": {
                "state": state,
                "reason": reason,
                "needs_attention": needs_attention,
                "ota_enabled": self.ota_enabled,
                "generation": None,
                "attempt": None,
                "attempts_started": attempts_started,
                "attempt_limit": protocol.ATTEMPT_LIMIT,
                "next_attempt_at": None,
                "attempt_deadline": None,
                "confirmation_deadline": None,
            },
            "command": command,
            "confirm_attempt_id": confirm_attempt_id,
        }

    def _active_budget(self, device_id):
        try:
            return self.storage.active_budget(device_id)
        except Exception:
            log.debug("the device budget could not be read", exc_info=True)
            return None

    def _budget_for(self, device_id, generation_id):
        """The retained budget with this generation, or None."""
        try:
            rows = self.storage.device_budgets(device_id)
        except Exception:
            log.debug("the device budgets could not be read", exc_info=True)
            return None
        for row in rows:
            if row["generation_id"] == generation_id:
                return row
        return None

    def _current_budget(self, device_id, identity):
        """The newest budget for the current target, whatever its status.

        An exhausted or superseded budget must be remembered for its target:
        forgetting it would recreate the same automatic budget on the next
        poll, which is exactly the retry loop the policy forbids. A budget for
        a different target is history, not a reason to refuse the new one.
        """
        if identity is None:
            return None
        try:
            rows = self.storage.device_budgets(device_id)
        except Exception:
            log.debug("the device budgets could not be read", exc_info=True)
            return None
        if not rows:
            return None
        newest = rows[0]
        if (newest["target_scheme"] == identity["scheme"]
                and newest["target_revision"] == identity["revision"]):
            return newest
        return None

    # -- the release notice ------------------------------------------------

    def release(self):
        """The cached Part 4 result, projected into the status response.

        No registry is contacted here. Until the checker exists there is no
        cache row for this running version, and the honest answer is
        ``disabled``: not "up to date", not "unknown release".
        """
        base = {"v": protocol.PROTOCOL_VERSION, "running": self.running_version,
                "latest": None, "channel": self.channel, "state": "disabled",
                "last_attempt_at": None, "checked_at": None,
                "max_age_s": RELEASE_MAX_AGE_S, "error": None, "source_url": None}
        try:
            row = self.storage.release_cache(self.channel)
        except Exception:
            log.debug("the release cache could not be read", exc_info=True)
            return dict(base, state="error", error="state_unavailable")
        if row is None or row["running"] != self.running_version:
            # A row from another running version is history, not evidence.
            return base
        checked = _parse_stamp(row["checked_at"])
        state = row["state"] if row["state"] in (
            "disabled", "unknown", "checking", "current", "available", "error",
            "stale") else "unknown"
        if checked is not None and self._clock() - checked > RELEASE_MAX_AGE_S:
            state = "stale"
        return {"v": protocol.PROTOCOL_VERSION, "running": self.running_version,
                "latest": row["latest"], "channel": self.channel, "state": state,
                "last_attempt_at": row["last_attempt_at"],
                "checked_at": row["checked_at"], "max_age_s": RELEASE_MAX_AGE_S,
                "error": row["error_code"], "source_url": row["source_url"]}

    # -- admin projections -------------------------------------------------

    def devices_view(self, poll_interval=None):
        """Every durable device, sorted by ID, with the attention count."""
        rows = self._device_rows()
        devices = [self._projection(row, poll_interval) for row in rows]
        return {"v": protocol.PROTOCOL_VERSION, "devices": devices,
                "needs_attention_count": sum(1 for device in devices
                                             if device["update"]["needs_attention"])}

    def device_view(self, device_id, poll_interval=None):
        """One device's projection plus its newest attempts, or None."""
        if not protocol.valid_id(device_id):
            return None
        row = self._read_device(device_id)
        if row is None:
            return None
        view = self._projection(row, poll_interval)
        try:
            attempts = self.storage.device_attempts(device_id, ATTEMPT_DETAIL_LIMIT)
        except Exception:
            log.exception("the device attempts could not be read")
            attempts = []
        view["attempts"] = [self._attempt_row(attempt) for attempt in attempts]
        return view

    def attention_count(self):
        try:
            return sum(1 for row in self._device_rows()
                       if row["attention_latched"] or
                       row["update_state"] in protocol.ATTENTION_STATES)
        except Exception:
            log.debug("the attention count could not be read", exc_info=True)
            return 0

    def _device_rows(self):
        try:
            return self.storage.devices()
        except Exception:
            log.exception("the device records could not be read")
            return []

    def _projection(self, row, poll_interval=None):
        """The admin shape of one device row.

        ``connected`` is derived at read time from three poll intervals. After
        a restart, a checkpointed last-seen is labelled as approximate until
        this process sees a fresh accepted report.
        """
        limit = 3 * (poll_interval if poll_interval else 15)
        with self._lock:
            live_seen = self._seen.get(row["device_id"])
        last_seen = live_seen if live_seen is not None else _parse_stamp(row["last_seen_at"])
        now = self._clock()
        checkpoint = live_seen is None
        capabilities = json.loads(row["capabilities_json"] or "[]")
        identity = self.expected_for(row["scheme"])
        comparison = protocol.compare(
            {"scheme": row["scheme"], "revision": row["revision"]}, identity)
        needs_attention = bool(row["attention_latched"]) or \
            row["update_state"] in protocol.ATTENTION_STATES
        budget = self._current_budget(row["device_id"], identity)
        attempt = self._latest_attempt(row["device_id"])
        outstanding = self._outstanding_attempt(row)
        return {
            "device_id": row["device_id"],
            "name": row["name"],
            "connected": bool(last_seen is not None and now - last_seen <= limit),
            "last_seen_at": _stamp(last_seen) if last_seen is not None else row["last_seen_at"],
            "last_seen_checkpoint_at": row["last_seen_at"],
            "last_seen_is_checkpoint": checkpoint,
            "reported": protocol.reported_shape(row["scheme"], row["revision"],
                                                row["bootstrap_version"]),
            "expected": protocol.expected_shape(identity),
            "comparison": comparison,
            "bootstrap_version": row["bootstrap_version"],
            "capabilities": capabilities,
            "update": {
                "state": row["update_state"],
                "reason": row["reason"],
                "needs_attention": needs_attention,
                "ota_enabled": self.ota_enabled,
                "generation": row["active_generation_id"],
                "attempt": row["active_attempt_id"],
                "attempts_started": int(budget["attempts_started"]) if budget else 0,
                "attempt_limit": protocol.ATTEMPT_LIMIT,
                "next_attempt_at": budget["next_attempt_at"] if budget else None,
                "attempt_deadline": attempt["attempt_deadline"] if attempt else None,
                "confirmation_deadline": attempt["confirmation_deadline"] if attempt else None,
                "last_error": (attempt["error_code"] if attempt and attempt["error_code"]
                               else row["reason"]),
                "issued_at": attempt["issued_at"] if attempt else None,
                "started_at": attempt["started_at"] if attempt else None,
                "finished_at": attempt["finished_at"] if attempt else None,
                "command_status": _command_status(attempt),
                # Never a device-supplied value, and never true while the
                # updater is absent.
                "can_retry": bool(self.ota_enabled
                                  and (outstanding is None
                                       or outstanding["status"] == "unconfirmed")
                                  and row["update_state"] in ("failed", "unconfirmed")
                                  and self.slot_expected() is not None),
                "recovery_pending": bool(row["recovery_request_id"]),
                "recovery_expires_at": self._recovery_expiry(row),
                "last_recovery_at": row["last_recovery_at"],
                "last_recovery_kind": row["last_recovery_kind"],
            },
            "state_revision": int(row["state_revision"]),
            # A dated host USB hash check, when one is genuinely available.
            # Nothing here produces one, and device headers never fill it.
            "usb_observation": None,
        }

    def _latest_attempt(self, device_id):
        try:
            rows = self.storage.device_attempts(device_id, 1)
        except Exception:
            log.debug("the newest device attempt could not be read", exc_info=True)
            return None
        return rows[0] if rows else None

    def _attempts_started(self, device_id):
        budget = self._current_budget(device_id, self.expected())
        return int(budget["attempts_started"]) if budget else 0

    def _recovery_expiry(self, row):
        requested = _parse_stamp(row["recovery_requested_at"])
        if not row["recovery_request_id"] or requested is None:
            return None
        return _stamp(requested + protocol.RECOVERY_SECONDS)

    @staticmethod
    def _attempt_row(attempt):
        """One attempt, with no raw report JSON and no device-supplied verdict."""
        return {
            "attempt_id": attempt["attempt_id"],
            "generation_id": attempt["generation_id"],
            "command_id": attempt["command_id"],
            "attempt_no": int(attempt["attempt_no"]),
            "status": attempt["status"],
            "issued_at": attempt["issued_at"],
            "started_at": attempt["started_at"],
            "attempt_deadline": attempt["attempt_deadline"],
            "confirmation_deadline": attempt["confirmation_deadline"],
            "confirmation_acked_at": attempt["confirmation_acked_at"],
            "finished_at": attempt["finished_at"],
            "last_phase": attempt["last_phase"],
            "error_code": attempt["error_code"],
            "retryable": bool(attempt["retryable"]),
            "recovery": attempt["recovery"],
        }

    # -- authenticated admin mutations -------------------------------------

    def refuse_mutation(self, device_id, kind):
        """Answer a retry or recovery request while the updater is absent.

        Both routes exist in the contract so a client can tell "not available
        yet" from "no such device". They never authorize a transfer, reset a
        budget, or clear a warning: with no updater there is nothing to
        authorize, and a warning clears only on a supported recovery.
        """
        if not protocol.valid_id(device_id):
            raise AdminRequestError(404, "not_found")
        if self._read_device(device_id) is None:
            raise AdminRequestError(404, "not_found")
        log.info("device %s %s refused: the updater is not enabled",
                 device_id, kind)
        raise AdminRequestError(409, "ota_not_enabled")

    def authorize_retry(self, device_id, body, idempotency_key):
        """Authorize a fresh bounded budget for the current target.

        Nothing is sent to the device: the permit travels in its next status
        response, and the device still has to pass every gate itself.
        """
        if not self.ota_enabled:
            self.refuse_mutation(device_id, "retry")
        return self._mutate(device_id, "retry", body, idempotency_key)

    def queue_recovery(self, device_id, body, idempotency_key):
        """Queue a reconciliation check; it clears nothing by itself."""
        if not self.ota_enabled:
            self.refuse_mutation(device_id, "confirm-recovery")
        return self._mutate(device_id, "confirm-recovery", body, idempotency_key)

    def _mutate(self, device_id, kind, body, idempotency_key):
        with self._observation_lock:
            return self._mutate_locked(device_id, kind, body, idempotency_key)

    def _mutate_locked(self, device_id, kind, body, idempotency_key):
        if not protocol.valid_id(device_id):
            raise AdminRequestError(404, "not_found")
        row = self._read_device(device_id)
        if row is None:
            raise AdminRequestError(404, "not_found")
        if not protocol.valid_id(idempotency_key):
            raise AdminRequestError(400, "invalid_idempotency_key")
        wanted = _validated_mutation_body(body)
        if wanted is None:
            raise AdminRequestError(400, "invalid_request")
        path = "/api/admin/devices/%s/%s" % (
            device_id, "retry" if kind == "retry" else "confirm-recovery")
        digest = request_hash("POST", path, wanted)
        saved = self._saved_request(device_id, idempotency_key)
        if saved is not None:
            if saved["body_sha256"] != digest:
                raise AdminRequestError(409, "idempotency_conflict")
            return json.loads(saved["response_json"])
        if int(row["state_revision"]) != wanted["state_revision"]:
            raise AdminRequestError(409, "state_changed")
        identity = self.slot_expected()
        if identity is None:
            raise AdminRequestError(409, "ota_not_enabled")
        if (wanted["target_scheme"] != identity["scheme"]
                or wanted["target_revision"] != identity["revision"]):
            raise AdminRequestError(409, "target_changed")
        attempt = self._outstanding_attempt(row)
        if attempt is not None and attempt["status"] != "unconfirmed":
            raise AdminRequestError(409, "attempt_unresolved")
        if kind == "retry":
            if row["update_state"] == "manual_action" and \
                    row["reason"] != "usb_update_required":
                # A gate is not something a retry can override.
                raise AdminRequestError(409, "manual_action_required")
            updated, budgets, result = self._grant_budget(row, identity, wanted)
        else:
            if row["update_state"] not in ("failed", "manual_action", "unconfirmed"):
                raise AdminRequestError(409, "state_changed")
            updated, budgets, result = self._queue_recovery(row, identity)
        record = {"device_id": device_id, "request_key": idempotency_key,
                  "body_sha256": digest, "response_json": json.dumps(result),
                  "created_at": _updater_stamp(self._clock())}
        try:
            self.storage.save_device_report(updated, budgets, None,
                admin_request=record, expected_revision=wanted["state_revision"])
        except ConfigurationConflict:
            raise AdminRequestError(409, "state_changed") from None
        except Exception:
            log.exception("the device admin transition could not be committed")
            raise AdminRequestError(500, "state_unavailable") from None
        return result

    def _grant_budget(self, row, identity, wanted):
        """Replace any budget with a fresh bounded one, atomically."""
        device_id = row["device_id"]
        now = self._clock()
        stamp = _updater_stamp(now)
        generation = _new_generation(device_id, identity["revision"], stamp)
        old = self._active_budget(device_id)
        budgets = [dict(old, status="superseded", updated_at=stamp)] if old else []
        budget = {"generation_id": generation, "device_id": device_id,
                  "target_scheme": identity["scheme"],
                  "target_revision": identity["revision"], "attempts_started": 0,
                  "status": "active", "next_attempt_at": None,
                  "created_at": stamp, "updated_at": stamp}
        updated = dict(row, active_generation_id=generation,
                       active_attempt_id=None,
                       state_revision=int(row["state_revision"]) + 1,
                       recovery_request_id=None, recovery_requested_at=None,
                       recovery_after_boot=None, recovery_after_seq=None,
                       recovery_target_scheme=None, recovery_target_revision=None)
        return updated, budgets + [budget], {"queued": True, "generation": generation,
                                             "state_revision": updated["state_revision"]}

    def _queue_recovery(self, row, identity):
        """Persist a reconciliation request; it grants no transfer and no reset.

        The target stored is the identity the daemon *expects*, not the
        revision the broken app last reported: a fresh report must prove the
        expected app is running, not the same old hash again.
        """
        device_id = row["device_id"]
        now = self._clock()
        recovery_id = _new_generation(device_id, "recovery", _updater_stamp(now))
        updated = dict(row, recovery_request_id=recovery_id,
                       recovery_requested_at=_updater_stamp(now),
                       recovery_after_boot=int(row["boot"]),
                       recovery_after_seq=int(row["seq"]),
                       recovery_target_scheme=identity["scheme"],
                       recovery_target_revision=identity["revision"],
                       state_revision=int(row["state_revision"]) + 1)
        return updated, None, {"queued": True, "recovery_id": recovery_id,
                               "state_revision": updated["state_revision"]}

    def _saved_request(self, device_id, key):
        try:
            return self.storage.device_admin_request(device_id, key)
        except Exception:
            log.debug("a saved admin request could not be read", exc_info=True)
            return None

    # -- recovery reconciliation -------------------------------------------

    def _recovery_state(self, stored, report, comparison):
        """Whether a queued reconciliation now qualifies, expired, or neither."""
        if stored is None or not stored.get("recovery_request_id"):
            return None
        requested = _parse_stamp(stored["recovery_requested_at"])
        if requested is None or self._clock() - requested > protocol.RECOVERY_SECONDS:
            return "expired"
        after = (stored["recovery_after_boot"], stored["recovery_after_seq"])
        if after[0] is None or after[1] is None:
            return "expired"
        if (report["boot"], report["seq"]) <= after:
            return None
        if comparison != protocol.COMPARISON_EQUAL:
            return None
        if stored["recovery_target_revision"] != report["revision"]:
            return None
        if not report["boot_confirmed"]:
            return None
        if report["update"]["phase"] not in ("idle", "succeeded"):
            return None
        return "matched"

    def settle_recovery(self, stored, report, comparison):
        """Apply a queued reconciliation to one accepted report.

        Returns ``(overrides, attempt)``: the row fields that change, and an
        attempt that must be cancelled if one is still open. A qualified report
        cancels a superseded stuck attempt with the ``admin_app_reconciled``
        label and clears the active warning, keeping its history.
        """
        verdict = self._recovery_state(stored, report, comparison)
        if verdict is None:
            return None, None
        stamp = _updater_stamp(self._clock())
        cleared = {"recovery_request_id": None, "recovery_requested_at": None,
                   "recovery_after_boot": None, "recovery_after_seq": None,
                   "recovery_target_scheme": None, "recovery_target_revision": None}
        if verdict == "expired":
            # An expired request cancels the check, not the warning.
            return cleared, None
        attempt = None
        row = self._outstanding_attempt(stored)
        if row is not None:
            attempt = dict(row, status="cancelled", finished_at=stamp,
                           recovery="admin_app_reconciled")
        cleared.update({"update_state": "current", "reason": None,
                        "attention_latched": 0, "active_attempt_id": None,
                        "last_recovery_at": stamp,
                        "last_recovery_kind": "admin_app_reconciled"})
        return cleared, attempt


def release_instructions(channel):
    """The complete update commands for this installation's channel.

    The daemon cannot inspect the user's Compose reference or their Python
    environment, so it explains the applicable cases instead of pretending it
    knows which one applies. It never runs any of them.
    """
    if channel == "ghcr":
        return {
            "update": ["docker compose pull", "docker compose up -d"],
            "note": ("A pull on its own only downloads the image: the running "
                     "container keeps its current version until it is recreated. "
                     "If your Compose file pins DASH_IMAGE to a version tag, "
                     "change that reference first, then pull and recreate."),
            "verify": "docker compose ps",
        }
    if channel == "pypi":
        return {
            "update": ["pipx upgrade presto-ai-dash",
                       'pipx install --force "presto-ai-dash==<version>"'],
            "note": ("pipx and pip skip pre-releases unless asked. To move to a "
                     "release candidate, name the version or pass --pre. For a "
                     "plain pip install, use python -m pip install --upgrade "
                     "presto-ai-dash in the same Python environment."),
            "verify": "presto-ai-dash --version",
        }
    return {
        "update": [],
        "note": ("This is a development build, not a published release, so no "
                 "release update commands apply."),
        "verify": None,
    }


def request_hash(method, path, body):
    """The deduplication key of an admin mutation: method, route and body.

    Computed over all three so one idempotency key reused for a different
    operation is a conflict rather than a replay of the first result.
    """
    canonical = json.dumps({"method": method, "path": path, "body": body},
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validated_mutation_body(body):
    """A retry or recovery body with exactly the agreed keys, or None.

    ``state_revision`` is the optimistic-concurrency value the device detail
    returned; the target must be a full slot hash, because a legacy prefix or
    a display label is not a target this daemon can install.
    """
    if not isinstance(body, dict):
        return None
    if set(body) != {"state_revision", "target_scheme", "target_revision"}:
        return None
    revision_number = body["state_revision"]
    if type(revision_number) is not int or revision_number < 1:
        return None
    scheme = body["target_scheme"]
    revision = body["target_revision"]
    if scheme not in protocol.SCHEMES:
        return None
    if not protocol.valid_revision(scheme, revision):
        return None
    if len(revision) != protocol.SLOT_REVISION_CHARS:
        return None
    return {"state_revision": revision_number, "target_scheme": scheme,
            "target_revision": revision}


def _new_generation(device_id, target, stamp):
    """A fresh 32-character identifier for a budget or a recovery request."""
    seed = "%s|%s|%s|%s" % (device_id, target, stamp, os.urandom(8).hex())
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _command_status(attempt):
    """The persisted command's state, derived from its attempt row."""
    if attempt is None:
        return "none"
    status = attempt["status"]
    if status == "queued":
        return "queued"
    if status == "cancelled":
        return "cancelled"
    return "acknowledged"
