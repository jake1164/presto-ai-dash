"""Codex sign-in sessions: they are forgotten in time, and what the Codex
program says about a failure is shown as one short line of plain text.

No Codex program is started: the process is a stand-in that says what a test
needs it to say.
"""

import queue
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash import codex_login
from presto_ai_dash.codex_login import (CodexLoginManager, ERROR_LIMIT, SESSION_KEEP_SECONDS,
                                        SESSION_LIMIT, plain_error)


class FakeProcess:
    """Stands in for the running Codex program of one sign-in."""

    def __init__(self, messages=()):
        self.closed = threading.Event()
        self.messages = list(messages)
        self.process = self
        self.mine = None

    def poll(self):
        return 0 if self.closed.is_set() else None

    def request(self, method, params, timeout=None):
        return {"loginId": "login-1", "verificationUrl": "https://example.invalid/device",
                "userCode": "ABCD-EFGH"}

    def drain(self, timeout=0.0):
        items, self.messages = self.messages, []
        if not items:
            time.sleep(0.01)
        return items

    def close(self):
        self.closed.set()


class SessionTestBase(unittest.TestCase):
    def manager(self, messages=()):
        manager = CodexLoginManager(manager=None)
        record = {"id": "src-1", "type": "codex", "config": {}}
        self.processes = []
        self.threads = []

        def start_process(*args, **kwargs):
            process = FakeProcess(messages)
            self.processes.append(process)
            return record, process

        self.addCleanup(self.stop_threads)
        patcher = patch.object(manager, "_start_process", side_effect=start_process)
        patcher.start()
        self.addCleanup(patcher.stop)
        return manager

    def stop_threads(self):
        # Every process, including those of sessions the manager has already
        # forgotten: a watcher thread runs until its process is closed, and a
        # thread left running would outlive the test.
        for process in self.processes:
            process.close()
        for thread in threading.enumerate():
            if thread is not threading.current_thread() and thread.daemon and thread.name.startswith("Thread-"):
                thread.join(timeout=5)

    def finish(self, manager, session_id, when):
        session = manager._sessions[session_id]
        session["status"] = "success"
        session["completed_at"] = when


class PlainErrorTests(unittest.TestCase):
    def test_a_message_is_one_short_printable_line(self):
        self.assertEqual(plain_error("denied"), "denied")
        self.assertEqual(plain_error("two\nlines\tand  spaces"), "two lines and spaces")
        escape = chr(27)
        self.assertEqual(plain_error("bad" + escape + "[2Jname"), "bad?[2Jname")
        self.assertEqual(plain_error("café"), "caf?")

    def test_a_long_message_is_cut(self):
        text = plain_error("x" * 5000)
        self.assertEqual(len(text), ERROR_LIMIT)
        self.assertTrue(text.endswith("..."))

    def test_nothing_says_login_failed(self):
        for value in (None, "", "   ", 0):
            self.assertEqual(plain_error(value), "login failed")

    def test_a_value_that_is_not_text_is_shown_as_text(self):
        self.assertEqual(plain_error({"code": 3}), "{'code': 3}")


class ErrorOnTheSessionTests(SessionTestBase):
    def wait_for(self, manager, session_id, status):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if manager.status(session_id)["status"] == status:
                return manager.status(session_id)
            time.sleep(0.02)
        self.fail("the login never reached " + status)

    def test_the_providers_error_is_cleaned_before_it_is_shown(self):
        escape = chr(27)
        message = {"method": "account/login/completed",
                   "params": {"loginId": "login-1", "success": False,
                              "error": "nope" + escape + "[31m" + "x" * 1000}}
        manager = self.manager([message])
        session = manager.start("src-1")
        shown = self.wait_for(manager, session["id"], "error")
        self.assertNotIn(escape, shown["error"])
        self.assertLessEqual(len(shown["error"]), ERROR_LIMIT)
        self.assertTrue(shown["error"].startswith("nope?[31m"))

    def test_a_failure_with_no_text_says_so_plainly(self):
        message = {"method": "account/login/completed",
                   "params": {"loginId": "login-1", "success": False}}
        manager = self.manager([message])
        session = manager.start("src-1")
        self.assertEqual(self.wait_for(manager, session["id"], "error")["error"], "login failed")


class PruningTests(SessionTestBase):
    def test_an_old_finished_login_is_forgotten_at_the_next_start(self):
        manager = self.manager()
        first = manager.start("src-1")["id"]
        self.finish(manager, first, time.time() - SESSION_KEEP_SECONDS - 5)
        second = manager.start("src-1")["id"]
        with self.assertRaises(KeyError):
            manager.status(first)
        self.assertEqual(manager.status(second)["id"], second)

    def test_a_recently_finished_login_is_kept_so_the_page_can_show_it(self):
        manager = self.manager()
        first = manager.start("src-1")["id"]
        self.finish(manager, first, time.time() - 5)
        manager.start("src-1")
        self.assertEqual(manager.status(first)["status"], "success")

    def test_a_pending_login_is_never_dropped(self):
        manager = self.manager()
        first = manager.start("src-1")["id"]
        manager._sessions[first]["started_at"] = time.time() - 10 * SESSION_KEEP_SECONDS
        manager.start("src-1")
        self.assertEqual(manager.status(first)["status"], "pending")

    def test_the_number_kept_is_bounded_and_the_oldest_finished_go_first(self):
        manager = self.manager()
        ids = []
        now = time.time()
        for index in range(SESSION_LIMIT + 5):
            ids.append(manager.start("src-1")["id"])
            self.finish(manager, ids[-1], now - 1000 + index)
        self.assertLessEqual(len(manager._sessions), SESSION_LIMIT)
        with self.assertRaises(KeyError):
            manager.status(ids[0])
        self.assertEqual(manager.status(ids[-2])["id"], ids[-2])


if __name__ == "__main__":
    unittest.main()
