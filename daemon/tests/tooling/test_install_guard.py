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

    def test_install_points_to_the_setup_on_every_layout(self):
        # Only the setup download carries the update certificate, so it is the
        # one install path; nothing is copied and the device is not touched.
        for layout in ({"boot": True, "legacy": False}, {"boot": False, "legacy": True},
                       {"boot": True, "legacy": True}):
            with self.subTest(layout=layout):
                code, err, operate, launches = self.run_install(layout)
                self.assertEqual(code, 2)
                self.assertIn("Set up a Presto", err)
                operate.assert_not_called()
                self.assertEqual(launches, [])


if __name__ == "__main__":
    unittest.main()
