"""The alarm pop-to-top: the debounce state machine.

Home scrolls to a service that has just got worse, but never while the user
is at the screen, never for an alarm that does not persist, never twice for
the same service in a short while, and never for an alarm that is not worse
than the one already shown. The user's own position comes back afterwards.

Pure Python: no hardware, no daemon, no provider. The clock is injected.
"""

import os
import sys
import unittest

sys.path.insert(0, str(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "src")))

import presto_path  # noqa: F401  (puts the device modules on the path)
from home import HomeLayout
from popup import (COOLDOWN_SECONDS, PERSIST_SECONDS, RETURN_SECONDS,
                   TOUCH_GUARD_SECONDS, PopToTop, row_offset)


def rows(count=8):
    return [{"id": "s%d" % index, "name": "Service %d" % index, "status": "ok",
             "windows": [{"label": "5h", "percent": 10, "state": "available"}]}
            for index in range(count)]


def layout(count=8):
    """A Home list of quota rows, with a measuring function for the strip."""
    return HomeLayout(rows(count), 1, lambda text: len(text) * 6, 200)


class RowOffsetTests(unittest.TestCase):
    def test_a_row_is_brought_into_view(self):
        home = layout()
        offset = row_offset(home, "s5", 0)
        self.assertIsNotNone(offset)
        # The row is fully visible at that offset.
        top = 46 * 5
        self.assertLessEqual(offset, top)
        self.assertLessEqual(top + 46, offset + home.region)

    def test_a_row_that_is_already_visible_keeps_the_offset(self):
        home = layout()
        self.assertEqual(row_offset(home, "s0", 0), 0)

    def test_an_unknown_service_has_no_offset(self):
        self.assertIsNone(row_offset(layout(), "nope", 0))
        self.assertIsNone(row_offset(None, "s0", 0))


class PopToTopTests(unittest.TestCase):
    def setUp(self):
        self.popup = PopToTop()
        self.home = layout()

    def alarm(self, service="s5", status="warning", at=10):
        """A service that was fine and then got worse."""
        self.popup.observe(service, "ok", 0)
        self.popup.observe(service, status, at)

    def test_the_alarm_must_persist_before_the_list_moves(self):
        self.alarm()
        self.assertIsNone(self.popup.poll(20, self.home, 0))
        self.assertIsNone(self.popup.poll(39, self.home, 0))
        action = self.popup.poll(40, self.home, 0)
        self.assertIsNotNone(action)
        self.assertEqual(action["kind"], "scroll_to")

    def test_the_persist_time_is_thirty_seconds(self):
        self.assertEqual(PERSIST_SECONDS, 30)

    def test_a_recovered_alarm_never_moves_the_list(self):
        self.alarm()
        self.popup.observe("s5", "ok", 20)
        self.assertIsNone(self.popup.poll(60, self.home, 0))

    def test_the_list_does_not_move_while_the_user_is_touching_it(self):
        self.alarm()
        self.popup.touch(30)
        self.assertIsNone(self.popup.poll(40, self.home, 0))
        action = self.popup.poll(51, self.home, 0)
        self.assertIsNotNone(action)

    def test_the_touch_guard_is_twenty_seconds(self):
        self.assertEqual(TOUCH_GUARD_SECONDS, 20)

    def test_a_service_moves_the_list_at_most_once_in_ten_minutes(self):
        self.alarm(status="warning")
        self.assertIsNotNone(self.popup.poll(40, self.home, 0))
        # It recovers and gets worse again, worse than before.
        self.popup.observe("s5", "ok", 60)
        self.popup.observe("s5", "critical", 70)
        self.popup.poll(100, self.home, 0)  # the user's position comes back
        action = self.popup.poll(101, self.home, 0)
        self.assertNotEqual((action or {}).get("kind"), "scroll_to")
        # After the cool-down it may act again.
        action = self.popup.poll(40 + COOLDOWN_SECONDS + 31, self.home, 0)
        self.assertIsNotNone(action)
        self.assertEqual(action["kind"], "scroll_to")

    def test_the_cooldown_is_ten_minutes(self):
        self.assertEqual(COOLDOWN_SECONDS, 600)

    def test_a_new_alarm_must_be_worse_than_the_one_already_shown(self):
        self.alarm(service="s5", status="warning")
        self.assertIsNotNone(self.popup.poll(40, self.home, 0))
        # Another service turns critical: that is worse, so it may act.
        self.popup.observe("s6", "ok", 60)
        self.popup.observe("s6", "critical", 70)
        self.popup.poll(100, self.home, 0)  # the user's position comes back
        action = self.popup.poll(102, self.home, 0)
        self.assertIsNotNone(action)
        self.assertEqual(action["kind"], "scroll_to")
        self.assertEqual(action["service"], "s6")
        # A third service only warns: that is not worse than the critical
        # one already shown, so it does not move the list.
        self.popup.observe("s7", "ok", 110)
        self.popup.observe("s7", "warning", 120)
        action = self.popup.poll(151, self.home, 0)
        self.assertNotEqual((action or {}).get("kind"), "scroll_to")

    def test_once_everything_has_recovered_a_new_warning_may_act_again(self):
        # The "worse than the one already shown" rule is about alarms that are
        # still active, not about the worst one that ever was.
        self.alarm(service="s5", status="critical")
        self.assertIsNotNone(self.popup.poll(40, self.home, 0))
        self.popup.poll(100, self.home, 0)            # the user's position returns
        self.popup.observe("s5", "ok", 200)           # it recovers; nothing is alarmed
        self.popup.observe("s7", "ok", 1000)
        self.popup.observe("s7", "warning", 1010)
        action = self.popup.poll(1041, self.home, 0)
        self.assertIsNotNone(action)
        self.assertEqual(action["service"], "s7")

    def test_a_warning_still_does_not_act_while_the_critical_one_is_active(self):
        self.alarm(service="s5", status="critical")
        self.popup.poll(40, self.home, 0)
        self.popup.observe("s7", "ok", 50)
        self.popup.observe("s7", "warning", 60)
        action = self.popup.poll(100, self.home, 0)
        self.assertNotEqual((action or {}).get("kind"), "scroll_to")

    def test_the_users_position_comes_back_after_a_minute(self):
        self.alarm()
        action = self.popup.poll(40, self.home, 0)
        offset = action["offset"]
        self.assertNotEqual(offset, 0)
        self.assertIsNone(self.popup.poll(40 + RETURN_SECONDS - 1, self.home, offset))
        back = self.popup.poll(40 + RETURN_SECONDS, self.home, offset)
        self.assertIsNotNone(back)
        self.assertEqual(back["kind"], "restore")
        self.assertEqual(back["offset"], 0)

    def test_the_return_time_is_a_minute(self):
        self.assertEqual(RETURN_SECONDS, 60)

    def test_a_user_who_scrolled_away_keeps_their_position(self):
        self.alarm()
        action = self.popup.poll(40, self.home, 0)
        offset = action["offset"]
        self.popup.scrolled(50)
        self.assertIsNone(self.popup.poll(40 + RETURN_SECONDS, self.home, offset))

    def test_a_service_that_is_already_on_screen_does_not_move_the_list(self):
        self.popup.observe("s0", "ok", 0)
        self.popup.observe("s0", "warning", 10)
        self.assertIsNone(self.popup.poll(40, self.home, 0))

    def test_the_first_reading_of_a_service_is_a_baseline(self):
        self.popup.observe("s5", "critical", 10)
        self.assertIsNone(self.popup.poll(100, self.home, 0))

    def test_a_service_that_is_not_in_the_list_does_not_move_it(self):
        self.alarm(service="nope")
        self.assertIsNone(self.popup.poll(40, self.home, 0))

    def test_the_worst_alarm_is_the_one_that_acts(self):
        self.alarm(service="s5", status="warning", at=10)
        self.alarm(service="s6", status="critical", at=10)
        action = self.popup.poll(41, self.home, 0)
        self.assertIsNotNone(action)
        self.assertEqual(action["service"], "s6")


if __name__ == "__main__":
    unittest.main()
