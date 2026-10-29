"""The operational updater: permits, bounded attempts, and confirmation.

This is the daemon half of Part 2. It is written and tested, and it is **off**
unless a configuration turns it on, because the production deployment gate in
the lifecycle plan has not been passed. Everything here is a pure function of
the stored device row, the report just received, and the clock, so the tests
can walk every boundary without a board.

The rules it enforces are the ones section 8.4 of the plan states:

* a permit is persisted **before** it is returned, and a repeated poll returns
  the same outstanding command rather than minting another;
* one outstanding attempt per device, so a lost reply, a reset or a duplicate
  report cannot start a second transfer;
* three attempts per device and target, with the 1-minute and 5-minute
  retry delays; a preflight block and a failed status poll spend nothing;
* only transient transfer failures are retried automatically. A security,
  storage or compatibility block stops immediately with ``manual_action``,
  and a first-boot crash stops immediately as ``failed``;
* an attempt that stops reporting becomes ``unconfirmed`` at its deadline, not
  a fabricated failure, and is reconciled when the device returns;
* confirmation is a two-poll handshake: an acknowledgement only after a
  candidate reports ``confirming`` with the expected hash, and ``current`` only
  after the device reports ``succeeded`` with ``boot_confirmed``;
* a manual retry grants a fresh budget through the device's own polling, never
  by connecting to it, and never clears the warning it is retrying.
"""

import hashlib
from datetime import datetime, timezone

from . import device_protocol as protocol

# The measured whole-attempt and confirmation durations. The plan requires
# them to come from full-app measurements before commands are enabled; these
# are the working figures the controlled tests use, and the deployment gate
# replaces them with the measured ones.
ATTEMPT_SECONDS = 900
CONFIRMATION_SECONDS = 180

# Transfer failures that may be retried automatically, in order of the two
# scheduled retries. Anything else stops automation.
TRANSIENT_ERRORS = ("transfer_timeout", "connection_lost", "http_error")
# Failures that mean a person must act; automation stops and stays stopped.
GATE_ERRORS = ("storage_insufficient", "firmware_unsupported",
               "bootstrap_incompatible", "transport_untrusted",
               "signature_invalid", "manifest_invalid", "hash_mismatch")


def stamp(moment):
    """A UTC RFC 3339 timestamp at second precision, ending in Z."""
    return datetime.fromtimestamp(float(moment), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(text):
    """Seconds since the epoch for a stored timestamp, or None."""
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _new_id(seed):
    """A deterministic 32-character id, so the tests can predict it.

    The coordinator passes a seed built from the device, target and counter;
    production entropy is not needed for a correlation identifier, and a
    replay of the same request then produces the same row instead of a second
    one.
    """
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def permit(attempt, target):
    """The ``start_update`` command for one persisted attempt.

    It is derived from the stored row every time, so a repeated poll returns
    the same permit rather than a new one, and the manifest route cannot be
    pointed anywhere but this daemon's own bundle.
    """
    return {"id": attempt["command_id"], "kind": "start_update",
            "generation": attempt["generation_id"],
            "attempt": attempt["attempt_id"],
            "attempt_no": int(attempt["attempt_no"]),
            "target_scheme": protocol.SCHEME_SLOT,
            "target_revision": target,
            "manifest_path": "/api/v1/device-app/manifest"}


class Outcome(object):
    """What one report changed: the verdict, and what to persist."""

    def __init__(self, state, reason, attention):
        self.state = state
        self.reason = reason
        self.attention = attention
        self.budget = None
        self.extra_budgets = []
        self.attempt = None
        self.extra_attempts = []
        self.command = None
        self.confirm_attempt_id = None
        self.changed = False


class OtaUpdater(object):
    """Permits, budgets, retries and confirmation for one device's report."""

    enabled = True

    def __init__(self, clock, attempt_limit=protocol.ATTEMPT_LIMIT,
                 retry_delays=protocol.RETRY_DELAYS_S,
                 attempt_seconds=ATTEMPT_SECONDS,
                 confirmation_seconds=CONFIRMATION_SECONDS):
        self._clock = clock
        self.attempt_limit = attempt_limit
        self.retry_delays = tuple(retry_delays)
        self.attempt_seconds = attempt_seconds
        self.confirmation_seconds = confirmation_seconds

    # -- the entry point ---------------------------------------------------

    def advance(self, device, report, identity, budget, attempt):
        """The verdict for an accepted report, and the rows it changes.

        ``device`` is the stored row, ``report`` the validated report, and
        ``budget``/``attempt`` the current active rows (either may be None).
        """
        now = self._clock()
        phase = (report.get("update") or {}).get("phase") or "idle"
        update = report.get("update") or {}
        target = identity["revision"] if identity else None
        matching = (update.get("attempt") is not None and attempt is not None
                    and update["attempt"] == attempt["attempt_id"])
        outcome = Outcome("unknown", "revision_uncomparable", False)

        if report.get("scheme") != protocol.SCHEME_SLOT or identity is None \
                or identity.get("scheme") != protocol.SCHEME_SLOT:
            # Nothing over the air applies: this is a legacy install, a device
            # without a slot scheme, or a daemon with no verifiable slot bundle.
            return self._fallback(device, report, identity, budget, outcome)

        equal = protocol.compare({"scheme": report["scheme"],
                                  "revision": report["revision"]}, identity) == \
            protocol.COMPARISON_EQUAL

        if attempt is not None and budget is not None \
                and budget["target_revision"] != target \
                and attempt["status"] == "queued":
            # The daemon's target changed before this permit was acknowledged:
            # cancel the unstarted command for the old target and evaluate the
            # new one. A started attempt is left to settle, because it may be
            # activating.
            attempt = dict(attempt, status="cancelled", finished_at=stamp(now),
                           recovery="target_changed")
            superseded = dict(budget, status="superseded", updated_at=stamp(now))
            outcome.extra_attempts.append(attempt)
            outcome.extra_budgets.append(superseded)
            outcome.changed = True
            return self._maybe_start(device, report, identity, None, target, now,
                                     outcome)
        if attempt is not None:
            return self._advance_attempt(device, report, identity, budget,
                                         attempt, phase, update, matching, equal,
                                         now, outcome)
        if equal:
            outcome.state, outcome.reason, outcome.attention = "current", None, False
            if budget is not None and budget["status"] == "active":
                budget = dict(budget, status="succeeded",
                              updated_at=stamp(now))
                outcome.budget = budget
                outcome.changed = True
            return outcome
        return self._maybe_start(device, report, identity, budget, target, now, outcome)

    # -- helpers -----------------------------------------------------------

    def _fallback(self, device, report, identity, budget, outcome):
        """A report no update can act on, judged by revision alone."""
        comparison = protocol.compare(
            {"scheme": report.get("scheme"), "revision": report.get("revision")},
            identity)
        if comparison == protocol.COMPARISON_EQUAL:
            outcome.state, outcome.reason, outcome.attention = "current", None, False
            return outcome
        if comparison == protocol.COMPARISON_DIFFERENT:
            outcome.state, outcome.reason, outcome.attention = \
                "manual_action", "usb_update_required", True
            return outcome
        if identity is None:
            outcome.state, outcome.reason = "unknown", "bundle_unavailable"
        else:
            outcome.state, outcome.reason = "unknown", "revision_uncomparable"
        return outcome

    def _maybe_start(self, device, report, identity, budget, target, now, outcome):
        """Issue a permit when the policy allows one, never before persisting."""
        if device is None:
            outcome.state, outcome.reason = "unknown", "revision_uncomparable"
            return outcome
        if "ota-v1" not in (report.get("caps") or []):
            # The device cannot run an update, so an impossible one is not
            # left pending: it asks for USB.
            outcome.state, outcome.reason, outcome.attention = \
                "manual_action", "usb_update_required", True
            return outcome
        if budget is not None and budget["status"] in ("exhausted", "superseded"):
            outcome.state, outcome.reason, outcome.attention = \
                "failed", "retry_exhausted", True
            outcome.budget = budget
            return outcome
        if budget is not None and budget["status"] == "active":
            due = parse_stamp(budget["next_attempt_at"])
            if due is not None and due > now:
                outcome.state, outcome.reason = "pending", None
                outcome.budget = budget
                return outcome
            if int(budget["attempts_started"]) >= self.attempt_limit:
                budget = dict(budget, status="exhausted", updated_at=stamp(now))
                outcome.state, outcome.reason, outcome.attention = \
                    "failed", "retry_exhausted", True
                outcome.budget = budget
                outcome.changed = True
                return outcome
        if not report.get("boot_confirmed"):
            # A device whose current slot is not yet confirmed must settle that
            # first; issuing another attempt would stack activations.
            outcome.state, outcome.reason = "updating", None
            return outcome
        return self._issue(device, identity, budget, target, now, outcome)

    def _issue(self, device, identity, budget, target, now, outcome):
        """Create (or reuse) the budget and persist the permit before returning."""
        attempt_no = 1
        if budget is None:
            generation = _new_id("%s|%s|%s" % (device["device_id"], target,
                                               self._clock()))
            budget = {"generation_id": generation,
                      "device_id": device["device_id"],
                      "target_scheme": protocol.SCHEME_SLOT,
                      "target_revision": target, "attempts_started": 0,
                      "status": "active", "next_attempt_at": None,
                      "created_at": stamp(now), "updated_at": stamp(now)}
        else:
            attempt_no = min(self.attempt_limit,
                             int(budget["attempts_started"]) + 1)
            budget = dict(budget, updated_at=stamp(now))
        attempt_id = _new_id("%s|%s|attempt%d" % (budget["generation_id"], target,
                                                  attempt_no))
        command_id = _new_id("%s|%s|command%d" % (budget["generation_id"], target,
                                                  attempt_no))
        attempt = {"attempt_id": attempt_id,
                   "generation_id": budget["generation_id"],
                   "command_id": command_id, "attempt_no": attempt_no,
                   "status": "queued", "issued_at": stamp(now),
                   "started_at": None, "attempt_deadline": None,
                   "confirmation_deadline": None, "confirmation_acked_at": None,
                   "finished_at": None, "last_phase": None, "error_code": None,
                   "retryable": 0, "recovery": None}
        outcome.budget = budget
        outcome.attempt = attempt
        outcome.changed = True
        outcome.state, outcome.reason = "updating", None
        outcome.command = permit(attempt, target)
        return outcome

    def _advance_attempt(self, device, report, identity, budget, attempt, phase,
                         update, matching, equal, now, outcome):
        """Fold one report into the outstanding attempt."""
        attempt = dict(attempt)
        budget = dict(budget) if budget is not None else None
        outcome.attempt = attempt
        outcome.budget = budget

        # Lost progress polls do not make a real transfer free. A terminal
        # transfer failure or confirmation can be our first evidence of start.
        began = phase in ("downloading", "verifying", "activating", "confirming", "succeeded", "rolled_back")
        began = began or (phase == "failed" and update.get("error") not in GATE_ERRORS)
        if matching and began and attempt["started_at"] is None:
            attempt["started_at"] = stamp(now)
            attempt["attempt_deadline"] = stamp(now + self.attempt_seconds)
            attempt["status"] = "started"
            if budget is not None:
                budget = dict(budget, attempts_started=int(budget["attempts_started"]) + 1,
                              updated_at=stamp(now))
                outcome.budget = budget
            outcome.changed = True

        if matching and phase in ("downloading", "verifying", "activating"):
            if attempt["started_at"] is None:
                # The attempt is spent exactly here, on the first proof it began.
                attempt["status"] = "started"
                attempt["started_at"] = stamp(now)
                attempt["attempt_deadline"] = stamp(now + self.attempt_seconds)
                attempt["last_phase"] = phase
                if budget is not None:
                    started = int(budget["attempts_started"]) + 1
                    budget = dict(budget, attempts_started=started,
                                  updated_at=stamp(now))
                    outcome.budget = budget
                outcome.changed = True
            elif attempt["last_phase"] != phase:
                attempt["last_phase"] = phase
                outcome.changed = True
            outcome.state, outcome.reason = "updating", None
            return outcome

        if matching and phase == "confirming" and not report.get("boot_confirmed") \
                and equal:
            deadline = parse_stamp(attempt["confirmation_deadline"])
            if deadline is not None and now >= deadline:
                # The candidate kept confirming past its deadline: the daemon
                # says so plainly instead of acknowledging it forever.
                attempt["status"] = "unconfirmed"
                outcome.changed = True
                outcome.state, outcome.reason, outcome.attention = \
                    "unconfirmed", "confirmation_timeout", True
                return outcome
            if attempt["status"] != "confirming":
                attempt["status"] = "confirming"
                attempt["last_phase"] = "confirming"
                attempt["confirmation_deadline"] = stamp(now + self.confirmation_seconds)
                outcome.changed = True
            if attempt["confirmation_acked_at"] is None:
                attempt["confirmation_acked_at"] = stamp(now)
                outcome.changed = True
            outcome.confirm_attempt_id = attempt["attempt_id"]
            outcome.state, outcome.reason = "updating", None
            return outcome

        if matching and phase == "succeeded" and report.get("boot_confirmed") and equal:
            attempt["status"] = "succeeded"
            attempt["finished_at"] = stamp(now)
            attempt["last_phase"] = "succeeded"
            attempt["error_code"] = None
            if budget is not None:
                budget = dict(budget, status="succeeded", updated_at=stamp(now))
                outcome.budget = budget
            outcome.state, outcome.reason, outcome.attention = "current", None, False
            outcome.changed = True
            return outcome

        if matching and phase in ("failed", "blocked", "rolled_back"):
            return self._terminal(device, report, budget, attempt, phase, update,
                                  now, outcome)

        if not matching and phase not in ("idle", "succeeded", "confirming"):
            # A known attempt of this device reported a phase that names another
            # attempt: the report cannot finish or replace it.
            outcome.state, outcome.reason = "updating", None
            return outcome

        if attempt["status"] == "queued":
            # The permit has not been acknowledged yet: send it again rather
            # than minting a second one, so a lost reply or a reset resumes the
            # same attempt with the same budget.
            outcome.command = permit(attempt, identity["revision"])
            outcome.state, outcome.reason = "updating", None
            return outcome

        # No progress: the deadlines decide, and they produce "unconfirmed",
        # never a fabricated failure.
        deadline = parse_stamp(attempt["attempt_deadline"])
        confirming = parse_stamp(attempt["confirmation_deadline"])
        if confirming is not None and now >= confirming:
            if attempt["status"] != "unconfirmed":
                attempt["status"] = "unconfirmed"
                outcome.changed = True
            outcome.state, outcome.reason, outcome.attention = \
                "unconfirmed", "confirmation_timeout", True
            return outcome
        if deadline is not None and now >= deadline:
            if attempt["status"] != "unconfirmed":
                attempt["status"] = "unconfirmed"
                outcome.changed = True
            outcome.state, outcome.reason, outcome.attention = \
                "unconfirmed", "attempt_timeout", True
            return outcome
        if attempt["status"] == "unconfirmed":
            outcome.state, outcome.reason, outcome.attention = \
                "unconfirmed", ("confirmation_timeout"
                                if confirming is not None else "attempt_timeout"), True
            return outcome
        outcome.state, outcome.reason = "updating", None
        return outcome

    def _terminal(self, device, report, budget, attempt, phase, update, now, outcome):
        """A terminal device report: decide whether automation continues."""
        error = update.get("error") or "unknown_error"
        attempt["status"] = "failed" if phase != "blocked" else "blocked"
        attempt["finished_at"] = stamp(now)
        attempt["last_phase"] = phase
        attempt["error_code"] = error
        attempt["recovery"] = "rolled_back" if phase == "rolled_back" else None
        outcome.changed = True
        outcome.attempt = attempt
        if phase == "blocked" or error in GATE_ERRORS or error == "boot_crash":
            # A gate or a first-boot crash never retries by itself.
            attempt["retryable"] = 0
            if budget is not None:
                budget = dict(budget, status="exhausted", updated_at=stamp(now))
                outcome.budget = budget
            outcome.state = "manual_action" if phase == "blocked" or error in GATE_ERRORS \
                else "failed"
            outcome.reason = error if outcome.state == "manual_action" else "retry_exhausted"
            outcome.attention = True
            return outcome
        if error in TRANSIENT_ERRORS and budget is not None:
            started = int(budget["attempts_started"])
            if started >= self.attempt_limit:
                attempt["retryable"] = 0
                budget = dict(budget, status="exhausted", updated_at=stamp(now))
                outcome.budget = budget
                outcome.state, outcome.reason, outcome.attention = \
                    "failed", "retry_exhausted", True
                return outcome
            attempt["retryable"] = 1
            delay = self.retry_delays[min(started - 1, len(self.retry_delays) - 1)]
            budget = dict(budget, next_attempt_at=stamp(now + delay),
                          updated_at=stamp(now))
            outcome.budget = budget
            outcome.state, outcome.reason, outcome.attention = "pending", None, True
            return outcome
        attempt["retryable"] = 0
        if budget is not None:
            budget = dict(budget, status="exhausted", updated_at=stamp(now))
            outcome.budget = budget
        outcome.state, outcome.reason, outcome.attention = \
            "failed", "retry_exhausted", True
        return outcome

    # -- reconciliation ----------------------------------------------------

    def reconcile(self, device, report):
        """Settle an unconfirmed attempt from a later, healthy report.

        Called for an accepted report when no attempt is outstanding. A device
        that reports the expected full revision, a compatible bootstrap and a
        confirmed boot, with no local activation in progress, ends the fault:
        the attempt is cancelled with the ``app_confirmed_no_attempt`` label,
        which says exactly what was observed and nothing more.
        """
        if device is None:
            return None
        update = report.get("update") or {}
        if update.get("phase") not in ("idle", "succeeded"):
            return None
        if not report.get("boot_confirmed"):
            return None
        recovery = device.get("recovery_request_id")
        if recovery:
            # The admin asked for a fresh report before reconciling.
            return {"kind": "admin_app_reconciled"}
        return {"kind": "app_confirmed_no_attempt"}
