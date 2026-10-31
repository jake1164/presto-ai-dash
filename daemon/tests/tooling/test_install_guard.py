"""The older single-folder installer must not run on a bootstrap device.

It would put a second, older copy of the app beside the slots and confuse the next
update. A Presto on that layout is changed with the admin page's setup script instead.
"""

import io
import unittest
from unittest.mock import patch

import presto_path  # noqa: F401
from presto_ai_dash import device_install


class SlotDeviceGuardTests(unittest.TestCase):
    """The older single-folder installer must not run on a bootstrap device."""

    def run_install(self, layout):
        launches = []

        def remote(port, arguments):
            launches.append(arguments)
            return ""

        out, err = io.StringIO(), io.StringIO()
        with patch("importlib.util.find_spec", return_value=object()), \
                patch.object(device_install, "device_layout", return_value=layout), \
                patch.object(device_install, "run_remote", side_effect=remote), \
                patch.object(device_install, "operate", return_value=[]) as operate:
            code = device_install.run("sync", "COM3", {"revision": "a" * 64}, out=out, err=err)
        return code, err.getvalue(), operate, launches

    def test_a_bootstrap_device_is_refused_and_relaunched(self):
        code, err, operate, launches = self.run_install({"boot": True, "legacy": False})
        self.assertEqual(code, 2)
        self.assertIn("A/B app slots", err)
        operate.assert_not_called()
        self.assertEqual(launches, [["soft-reset", "exec", "--no-follow", device_install.LAUNCH_AUTO]])

    def test_a_legacy_device_goes_ahead_as_before(self):
        for layout in ({"boot": False, "legacy": True}, {"boot": True, "legacy": True}):
            with self.subTest(layout=layout):
                code, _err, operate, _launches = self.run_install(layout)
                self.assertEqual(code, 0)
                operate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
