"""One device payload for the tests that only need *a* plausible screen.

It is the six-account fixture from ``device/test_device_payload.py``, cut to
the device view and given the display and alert settings a daemon sends, so
tests that used to load the deleted built-in sample keep exercising the
real payload shape without copying one.

The fixture modules are imported inside the function: ``test_device_payload``
imports ``device.test_ui``, which imports this module, so a module-level
import here would close a cycle.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def sample_payload():
    """The six-account device payload, with its screen and alert settings."""
    from presto_ai_dash.dashboard import device_view
    from device.test_device_payload import full_payload

    payload = device_view(full_payload())
    payload["display"] = {"overflow_rows": 1}
    payload["alerts"] = {"quiet": False, "sound": False, "pop_to_top": True,
                         "enabled": True, "mute_hours": 4}
    return payload
