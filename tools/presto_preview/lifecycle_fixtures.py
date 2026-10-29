"""Fixture lifecycle extensions for the preview.

These are **display fixtures**, not device records. They never touch a daemon, a
database or a USB port, and nothing here is evidence that a real device is in
any particular state. They exist so the shared bottom-right indicator can be
inspected in every agreed state through the real ``presto/main.py`` and
``presto/ui.py``, including the states a real daemon cannot produce yet because
operational OTA is disabled.

The objects use the same shapes the daemon sends (section 8.3 of the device
lifecycle plan) so the device's own parser is exercised, not a simplified copy.
"""

# Synthetic identifiers. They are obviously fake on purpose: nothing may read
# them as a real device or a real revision.
DEVICE = "11111111111111111111111111111111"
REVISION = "a" * 64
GENERATION = "c" * 32
ATTEMPT = "d" * 32

# The states the preview can show, in the order they are most useful to review.
STATES = ("none", "current", "usb-required", "retry-pending", "retry-exhausted",
          "unconfirmed", "release", "both")


def device_lifecycle(state="current", reason=None, attention=False, comparison=None,
                     attempts=0, next_attempt_at=None):
    """One ``device_lifecycle`` object in the daemon's own shape."""
    if comparison is None:
        comparison = "equal" if state == "current" else "different"
    return {
        "v": 1,
        "device_id": DEVICE,
        "report_status": "accepted",
        "reported": {"scheme": "legacy-v1", "revision": REVISION[:12],
                     "bootstrap": None},
        "expected": {"scheme": "legacy-v1", "revision": REVISION,
                     "min_bootstrap": None},
        "comparison": comparison,
        "update": {
            "state": state,
            "reason": reason,
            "needs_attention": attention,
            "ota_enabled": False,
            "generation": None,
            "attempt": None,
            "attempts_started": attempts,
            "attempt_limit": 3,
            "next_attempt_at": next_attempt_at,
            "attempt_deadline": None,
            "confirmation_deadline": None,
        },
        "command": None,
        "confirm_attempt_id": None,
    }


def daemon_release(state="available", running="0.50.0", latest="0.51.0",
                   channel="pypi"):
    """One ``daemon_release`` object in the daemon's own shape."""
    checked = "2026-10-29T09:00:00Z" if state in ("current", "available") else None
    return {
        "v": 1,
        "running": running,
        "latest": latest if state in ("current", "available") else None,
        "channel": channel,
        "state": state,
        "last_attempt_at": "2026-10-29T09:00:00Z",
        "checked_at": checked,
        "max_age_s": 172800,
        "error": None,
        "source_url": None,
    }


def extension(name):
    """The ``(device_lifecycle, daemon_release)`` pair for a named state.

    ``none`` is the quiet case: no extension at all, so no icon and no reserved
    space. ``current`` is also quiet, which is what makes "identical revisions
    remain quiet" visible.
    """
    if name == "current":
        return device_lifecycle("current"), None
    if name == "usb-required":
        return device_lifecycle("manual_action", "usb_update_required", True), None
    if name == "retry-pending":
        return device_lifecycle("pending", None, False, attempts=1,
                                next_attempt_at="2026-10-29T09:01:00Z"), None
    if name == "retry-exhausted":
        return device_lifecycle("failed", "retry_exhausted", True, attempts=3), None
    if name == "unconfirmed":
        return device_lifecycle("unconfirmed", "confirmation_timeout", True,
                                attempts=1), None
    if name == "release":
        return None, daemon_release()
    if name == "both":
        return (device_lifecycle("manual_action", "usb_update_required", True),
                daemon_release())
    return None, None


def apply(payload, name):
    """A copy of a status payload carrying the named fixture state."""
    device, release = extension(name)
    result = dict(payload)
    if device is not None:
        result["device_lifecycle"] = device
    if release is not None:
        result["daemon_release"] = release
    return result
