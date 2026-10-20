"""Phase 5: the local Claude Code session-log activity reader (claude_activity.py).

Everything here uses synthetic JSONL fixtures under a temp directory. No test
ever reads ~/.claude. SENTINEL strings stand in for prompt text, session ids
and paths; the reader's result must never contain them.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import builtins
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash.collectors import claude_activity
from presto_ai_dash.collectors.claude_activity import ActivityReader
from presto_ai_dash.management import ManagedCollector
from test_management import ManagementBase

SENTINEL_PROMPT = "SENTINEL-PRIVATE-PROMPT-TEXT"
SENTINEL_SESSION = "SENTINEL-SESSION-IDENTITY"
SENTINEL_CWD = "SENTINEL-C:/private/cwd"
NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
RESULT_KEYS = {"available", "complete", "last_request_at", "last_model", "last_entrypoint",
               "requests_5h", "tokens_5h", "tokens_7d"}


def moment(seconds_ago):
    return NOW - timedelta(seconds=seconds_ago)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _stamp(value):
    """Seconds for an ISO-8601 timestamp (``Z`` allowed); None if unusable."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def line(request_id="req-1", message_id="msg-1", model="claude-test", entrypoint="cli",
         seconds_ago=60, inp=10, out=20, cc=5, cr=7, raw_timestamp=None, with_usage=True):
    """One assistant session-log line with the privacy sentinels embedded."""
    message = {"id": message_id, "model": model,
               "content": [{"type": "text", "text": SENTINEL_PROMPT}]}
    if with_usage:
        message["usage"] = {"input_tokens": inp, "output_tokens": out,
                            "cache_creation_input_tokens": cc,
                            "cache_read_input_tokens": cr}
    entry = {"type": "assistant", "timestamp": raw_timestamp or iso(moment(seconds_ago)),
             "entrypoint": entrypoint, "sessionId": SENTINEL_SESSION, "cwd": SENTINEL_CWD,
             "isSidechain": False, "message": message}
    if request_id is not None:
        entry["requestId"] = request_id
    return json.dumps(entry) + "\n"


class ReaderBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.projects = Path(self.temp.name) / "projects"
        self.projects.mkdir()

    def session(self, name, *lines, project="proj-a", mtime=None):
        directory = self.projects / project
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes("".join(lines).encode("utf-8"))  # exact bytes, no \r\n conversion
        if mtime is not None:
            os.utime(path, (mtime.timestamp(), mtime.timestamp()))
        return path

    def refresh(self, reader, now=NOW):
        result = reader.refresh(now)
        self.assertIsInstance(result, dict)
        return result


class ReaderOutputTests(ReaderBase):
    def test_reads_only_the_allowed_fields_and_excludes_cache_reads(self):
        self.session("a.jsonl", line(inp=100, out=50, cc=25, cr=9999))
        result = self.refresh(ActivityReader(self.projects))
        self.assertIs(result["available"], True)
        self.assertNotIn("reason", result)
        self.assertEqual(set(result), RESULT_KEYS)
        self.assertEqual(result["requests_5h"], 1)
        self.assertEqual(result["tokens_5h"], 175)  # 100+50+25; the 9999 cache read is out
        self.assertEqual(result["tokens_7d"], 175)
        self.assertEqual(result["last_model"], "claude-test")
        self.assertEqual(result["last_entrypoint"], "CLI")
        self.assertEqual(datetime.fromisoformat(result["last_request_at"]), moment(60))

    def test_result_never_contains_the_sentinel_strings(self):
        reader = ActivityReader(self.projects)
        self.session("a.jsonl", line(), line(request_id="req-2", seconds_ago=30))
        self.session("b.jsonl", '{"usage": "' + SENTINEL_PROMPT + '" unclosed}\n',
                     line(request_id="req-3", entrypoint="claude-vscode"))
        for _ in range(3):
            result = self.refresh(reader)
        for sentinel in (SENTINEL_PROMPT, SENTINEL_SESSION, SENTINEL_CWD):
            self.assertNotIn(sentinel, json.dumps(result))
            self.assertNotIn(sentinel, str(result))
            self.assertNotIn(sentinel, str(reader.__dict__))

    def test_empty_directory_is_available_with_no_activity(self):
        result = self.refresh(ActivityReader(self.projects))
        self.assertIs(result["available"], True)
        self.assertIsNone(result["last_request_at"])
        self.assertEqual(result["last_entrypoint"], "?")
        self.assertEqual((result["requests_5h"], result["tokens_5h"], result["tokens_7d"]),
                         (0, 0, 0))


class DedupeTests(ReaderBase):
    def test_identical_request_id_lines_count_once(self):
        # The measured real-world shape: 588 usage lines were only 216 requests.
        self.session("a.jsonl", *[line()] * 3, line(request_id="req-2"))
        result = self.refresh(ActivityReader(self.projects))
        self.assertEqual(result["requests_5h"], 2)
        self.assertEqual(result["tokens_7d"], 35 * 2)

    def test_differing_totals_for_one_request_keep_the_larger(self):
        orders = []
        for index, request in enumerate(("req-9", "req-10")):
            small = line(request_id=request, inp=1, out=1, cc=1, cr=0)
            big = line(request_id=request, inp=100, out=50, cc=25, cr=0)
            orders.append((small, big) if index == 0 else (big, small))
        for index, order in enumerate(orders):
            project = self.projects / ("order-%d" % index)
            project.mkdir()
            (project / "a.jsonl").write_text("".join(order), encoding="utf-8")
        result = self.refresh(ActivityReader(self.projects))
        self.assertEqual(result["requests_5h"], 2)  # one per project directory
        self.assertEqual(result["tokens_7d"], 175 * 2)  # the larger total won, either order

    def test_falls_back_to_message_id_when_request_id_is_missing(self):
        self.session("a.jsonl", *[line(request_id=None, message_id="msg-7")] * 2,
                     line(request_id=None, message_id="msg-8"))
        result = self.refresh(ActivityReader(self.projects))
        self.assertEqual(result["requests_5h"], 2)

    def test_without_either_id_only_exact_repeats_collapse(self):
        self.session("a.jsonl",
                     line(request_id=None, message_id=None, seconds_ago=100),
                     line(request_id=None, message_id=None, seconds_ago=100),
                     line(request_id=None, message_id=None, seconds_ago=100, inp=99))
        result = self.refresh(ActivityReader(self.projects))
        self.assertEqual(result["requests_5h"], 2)


class LineFilterTests(ReaderBase):
    def test_malformed_and_incomplete_entries_are_skipped(self):
        junk = [
            "plain text, not json\n",
            '{"usage": broken}\n',
            "[1, 2, 3]\n",
            json.dumps({"timestamp": iso(NOW), "usage": "misplaced"}) + "\n",
            json.dumps({"timestamp": iso(NOW), "message": {}}) + "\n",
            line(with_usage=False),
            line(raw_timestamp="not-a-time"),
            line(raw_timestamp="2026-13-45T99:99:99Z"),
        ]
        self.session("a.jsonl", *junk, line(request_id="kept", inp=1, out=0, cc=0, cr=0))
        result = self.refresh(ActivityReader(self.projects))
        self.assertEqual(result["requests_5h"], 1)
        self.assertEqual(result["tokens_7d"], 1)

    def test_oversized_line_is_skipped_and_later_lines_still_count(self):
        huge = json.dumps({"usage": "x" * (1024 * 1024 + 10), "text": SENTINEL_PROMPT})
        self.session("a.jsonl", huge + "\n", line(request_id="after-huge", inp=1, out=0, cc=0, cr=0))
        result = self.refresh(ActivityReader(self.projects))
        self.assertEqual(result["requests_5h"], 1)
        self.assertEqual(result["tokens_7d"], 1)

    def test_partial_last_line_is_deferred_then_counted_once(self):
        path = self.session("a.jsonl", line(request_id="full"),
                            line(request_id="partial").rstrip("\n"))
        reader = ActivityReader(self.projects)
        result = self.refresh(reader)
        self.assertEqual(result["requests_5h"], 1)
        with open(path, "ab") as handle:
            handle.write(("\n" + line(request_id="next") + "\n").encode("utf-8"))
        result = self.refresh(reader)
        self.assertEqual(result["requests_5h"], 3)
        self.assertEqual(result["tokens_7d"], 105)

    def test_non_jsonl_files_are_ignored(self):
        self.session("notes.txt", line(request_id="in-txt"))
        result = self.refresh(ActivityReader(self.projects))
        self.assertIs(result["available"], True)
        self.assertEqual(result["requests_5h"], 0)


class WindowTests(ReaderBase):
    def test_windows_are_five_hours_and_seven_days_before_now(self):
        one = dict(inp=1, out=0, cc=0, cr=0)
        self.session("a.jsonl",
                     line(request_id="just-in", seconds_ago=5 * 3600 - 1, **one),
                     line(request_id="edge", seconds_ago=5 * 3600, **one),
                     line(request_id="old-but-kept", seconds_ago=7 * 86400 - 10, **one),
                     line(request_id="too-old", seconds_ago=7 * 86400 + 60, **one),
                     line(request_id="fresh", **one))
        result = self.refresh(ActivityReader(self.projects))
        self.assertEqual(result["requests_5h"], 2)  # just-in + fresh; the exact edge is out
        self.assertEqual(result["tokens_7d"], 4)    # too-old is outside the week
        self.assertEqual(result["last_model"], "claude-test")

    def test_clock_defaults_to_wall_time_when_no_now_is_passed(self):
        recent = datetime.now(timezone.utc) - timedelta(seconds=30)
        self.session("a.jsonl", line(raw_timestamp=iso(recent)))
        result = ActivityReader(self.projects).refresh()
        self.assertEqual(result["requests_5h"], 1)


class SurfaceTests(ReaderBase):
    def surface_for(self, entrypoint):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name) / "projects"
        (root / "p").mkdir(parents=True)
        (root / "p" / "a.jsonl").write_text(line(entrypoint=entrypoint), encoding="utf-8")
        return ActivityReader(root).refresh(NOW)["last_entrypoint"]

    def test_entrypoint_is_mapped_or_sanitised_to_a_short_surface(self):
        cases = [("claude-vscode", "VS Code"), ("cli", "CLI"), ("aws-saml", "aws-saml"),
                 ("JetBrains Plugin", "JetBrain"), ("", "?"), ("h\u00e9llo w\u00f6rld", "hllowrld"),
                 ("***", "?"), ("a/b c.d", "abcd")]
        for entrypoint, expected in cases:
            with self.subTest(entrypoint=entrypoint):
                self.assertEqual(self.surface_for(entrypoint), expected)

    def test_non_string_entrypoint_and_model_degrade_cleanly(self):
        entry = json.loads(line().rstrip("\n"))
        entry["entrypoint"] = {"nested": SENTINEL_PROMPT}
        entry["message"]["model"] = 12345
        self.session("a.jsonl", json.dumps(entry) + "\n")
        result = self.refresh(ActivityReader(self.projects))
        self.assertEqual(result["last_entrypoint"], "?")
        self.assertIsNone(result["last_model"])

    def test_a_format_change_degrades_instead_of_leaking(self):
        # Covers assumption E: junk that merely mentions usage never surfaces.
        self.session("a.jsonl", "{'usage': '" + SENTINEL_PROMPT + "'}\n")
        result = self.refresh(ActivityReader(self.projects))
        self.assertEqual(result["tokens_7d"], 0)
        self.assertNotIn(SENTINEL_PROMPT, json.dumps(result))


class IncrementalTests(ReaderBase):
    class CountingHandle:
        """Wraps a real file object and counts bytes read through it."""

        def __init__(self, handle, counter):
            self._handle, self._counter = handle, counter

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *exc):
            return self._handle.__exit__(*exc)

        def read(self, *args):
            data = self._handle.read(*args)
            self._counter[0] += len(data)
            return data

        def __getattr__(self, name):
            return getattr(self._handle, name)

    def count_open_reads(self):
        counter = [0]
        real_open = builtins.open

        def opener(file, *args, **kwargs):
            return IncrementalTests.CountingHandle(real_open(file, *args, **kwargs), counter)

        patcher = patch("builtins.open", side_effect=opener)
        patcher.start()
        self.addCleanup(patcher.stop)
        return counter

    def test_append_reads_only_the_new_tail_bytes(self):
        lines = [line(request_id="r%d" % index, seconds_ago=300 + index)
                 for index in range(60)]
        path = self.session("a.jsonl", *lines)
        reader = ActivityReader(self.projects)
        counter = self.count_open_reads()
        self.assertEqual(self.refresh(reader)["requests_5h"], 60)
        self.assertEqual(counter[0], path.stat().st_size)  # cold scan: the whole file
        counter[0] = 0
        appended = line(request_id="late", seconds_ago=5)
        with open(path, "ab") as handle:
            handle.write(appended.encode("utf-8"))
        result = self.refresh(reader)
        self.assertEqual(counter[0], len(appended.encode("utf-8")))  # only the tail
        self.assertEqual(result["requests_5h"], 61)

    def test_shrunk_or_replaced_file_is_rescanned_not_missed(self):
        path = self.session("a.jsonl", line(request_id="a1"), line(request_id="a2"),
                            line(request_id="a3"))
        reader = ActivityReader(self.projects)
        self.assertEqual(self.refresh(reader)["requests_5h"], 3)
        replacement = line(request_id="a1")
        path.write_bytes(replacement.encode("utf-8"))
        counter = self.count_open_reads()
        self.refresh(reader)
        self.assertEqual(counter[0], len(replacement.encode("utf-8")),
                         "a shrunk file must be rescanned from byte 0")
        with open(path, "ab") as handle:
            handle.write((line(request_id="a4") + "\n").encode("utf-8"))
        # a2/a3 were read once and stay in the 7-day memory; a4 arrives with the tail.
        self.assertEqual(self.refresh(reader)["requests_5h"], 4)


class FirstScanTests(ReaderBase):
    def test_files_not_modified_in_eight_days_are_skipped_on_the_first_scan(self):
        self.session("recent.jsonl", line(request_id="recent"), mtime=NOW - timedelta(days=1))
        self.session("ancient.jsonl", line(request_id="ancient", model="old-model"),
                     mtime=NOW - timedelta(days=9))
        result = self.refresh(ActivityReader(self.projects))
        self.assertEqual(result["requests_5h"], 1)
        self.assertEqual(result["last_model"], "claude-test")

    def test_first_scan_stops_at_the_byte_cap_newest_first(self):
        newest = self.session("new.jsonl", line(request_id="new-1"), line(request_id="new-2"),
                              mtime=NOW - timedelta(minutes=1))
        self.session("older.jsonl", line(request_id="older"), mtime=NOW - timedelta(minutes=5))
        cap = newest.stat().st_size
        result = self.refresh(ActivityReader(self.projects, byte_cap=cap))
        self.assertEqual(result["requests_5h"], 2)  # newest fits; the next file stays closed
        # A fresh instance (later poll, no cap pressure) finishes the remaining file.
        self.assertEqual(self.refresh(ActivityReader(self.projects))["requests_5h"], 3)


class FailureTests(ReaderBase):
    def test_missing_directory_is_unavailable_with_a_short_reason(self):
        result = self.refresh(ActivityReader(self.projects / "absent"))
        self.assertIs(result["available"], False)
        self.assertEqual(result["reason"], "no session directory")
        self.assertTrue(result["reason"].isascii())
        self.assertIsNone(result["last_request_at"])
        self.assertEqual(result["requests_5h"], 0)

    def test_projects_path_that_is_a_regular_file_is_unavailable(self):
        target = Path(self.temp.name) / "settings.json"
        target.write_text("{}")
        result = self.refresh(ActivityReader(target))
        self.assertIs(result["available"], False)
        self.assertEqual(result["reason"], "no session directory")

    def test_unreadable_directory_is_reported_not_raised(self):
        real_scandir = os.scandir

        def fake_scandir(path, *args, **kwargs):
            if str(path) == str(self.projects):
                raise PermissionError("fixture: no access")
            return real_scandir(path, *args, **kwargs)
        with patch("os.scandir", side_effect=fake_scandir):
            result = self.refresh(ActivityReader(self.projects))
        self.assertIs(result["available"], False)
        self.assertEqual(result["reason"], "session directory unreadable")

    def test_unreadable_file_is_reported_not_raised(self):
        self.session("a.jsonl", line())
        real_open = builtins.open

        def fake_open(file, *args, **kwargs):
            if str(file).endswith(".jsonl"):
                raise PermissionError("fixture: no access")
            return real_open(file, *args, **kwargs)
        with patch("builtins.open", side_effect=fake_open):
            result = self.refresh(ActivityReader(self.projects))
        self.assertIs(result["available"], False)
        self.assertEqual(result["reason"], "session directory unreadable")

    def test_any_unexpected_error_degrades_instead_of_raising(self):
        self.session("a.jsonl", line())
        with patch("json.loads", side_effect=RuntimeError("format changed underneath us")):
            result = self.refresh(ActivityReader(self.projects))
        self.assertIs(result["available"], False)
        self.assertEqual(result["reason"], "activity scan failed")
        self.assertNotIn("RuntimeError", json.dumps(result))

    def test_refresh_never_raises_even_when_the_scan_breaks(self):
        reader = ActivityReader(self.projects)
        with patch.object(reader, "_scan", side_effect=OSError("broken")):
            result = self.refresh(reader)
        self.assertIs(result["available"], False)


class SymlinkTests(ReaderBase):
    def setUp(self):
        super().setUp()
        self.outside = Path(self.temp.name) / "outside.jsonl"
        self.outside.write_text(line(request_id="outside-1"), encoding="utf-8")
        self.other_dir = Path(self.temp.name) / "outside-dir"
        self.other_dir.mkdir()
        (self.other_dir / "b.jsonl").write_text(line(request_id="outside-2"), encoding="utf-8")

    def _try_symlink(self, target, link):
        try:
            os.symlink(str(target), str(link), target_is_directory=target.is_dir())
        except (OSError, NotImplementedError):
            self.skipTest("this platform or permission set cannot create symlinks")

    def test_symlinked_file_outside_the_tree_is_ignored(self):
        directory = self.projects / "proj-a"
        directory.mkdir(exist_ok=True)
        self._try_symlink(self.outside, directory / "link.jsonl")
        result = self.refresh(ActivityReader(self.projects))
        self.assertIs(result["available"], True)
        self.assertEqual(result["requests_5h"], 0)

    def test_symlinked_directory_is_not_followed(self):
        self._try_symlink(self.other_dir, self.projects / "linked")
        inside = self.projects / "real"
        inside.mkdir()
        (inside / "a.jsonl").write_bytes(line(request_id="real-1").encode("utf-8"))
        result = self.refresh(ActivityReader(self.projects))
        self.assertEqual(result["requests_5h"], 1)

    def test_files_resolving_outside_the_tree_are_ignored(self):
        # The same refusal the symlink rules give, testable where creating a
        # symlink needs privileges: a file whose real path escapes projects
        # is refused without ever being opened.
        self.session("a.jsonl", line(request_id="inside"))
        sneaky = self.projects / "proj-a" / "sneaky.jsonl"
        sneaky.write_bytes(line(request_id="sneaky", model="escaped-model").encode("utf-8"))
        real = os.path.realpath

        def fake_realpath(path):
            if str(path).endswith("sneaky.jsonl"):
                return str(Path(self.temp.name) / "escaped.jsonl")
            return real(path)
        with patch("os.path.realpath", side_effect=fake_realpath):
            result = self.refresh(ActivityReader(self.projects))
        self.assertEqual(result["requests_5h"], 1)
        self.assertEqual(result["last_model"], "claude-test")


class ChunkedScanTests(ReaderBase):
    """The cold start reads a week of history: bounded chunks, a tail probe.

    A real machine holds about 70 MB of session logs, more than one poll
    should read at once. The reader therefore reads in bounded chunks from
    the saved offset, spends a byte budget per refresh, probes the tail of
    the newest files first, and says with its "complete" flag whether the
    totals are still lower bounds.
    """

    CHUNK = 4096
    PROBE = 1024

    def build_fixture(self):
        """Many files, one bigger than a chunk, ids repeated across chunks and
        across the probed tail, and one partial last line."""
        for index in range(12):
            lines = [line(request_id="req-%d-%d" % (index, entry),
                          seconds_ago=60 + index * 60 + entry) for entry in range(6)]
            if index == 0:
                lines += [line(request_id="big-%d" % entry, seconds_ago=900 + entry)
                          for entry in range(40)]
                lines.append(line(request_id="big-0", seconds_ago=900))
                lines.append(line(request_id="newest", seconds_ago=30))
            text = "".join(lines)
            if index == 1:
                text += line(request_id="partial").rstrip("\n")
            path = self.session("session-%d.jsonl" % index, *text.splitlines(True),
                                project="proj-%d" % (index % 3))
            stamp = NOW - timedelta(minutes=index + 1)  # file 0 is the newest
            os.utime(path, (stamp.timestamp(), stamp.timestamp()))
        return self.projects

    def reference(self, now=NOW):
        """A straightforward one-shot calculation of the same three numbers."""
        records = {}
        for path in sorted(self.projects.rglob("*.jsonl")):
            data = path.read_bytes()
            cut = data.rfind(b"\n")
            if cut < 0:
                continue
            for raw in data[:cut].split(b"\n"):
                if not raw:
                    continue
                try:
                    entry = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                message = entry.get("message") if isinstance(entry, dict) else None
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                stamp = _stamp(entry.get("timestamp"))
                if stamp is None:
                    continue
                tokens = 0
                for name in ("input_tokens", "output_tokens", "cache_creation_input_tokens"):
                    value = usage.get(name)
                    if type(value) is int and value > 0:
                        tokens += value
                key = entry.get("requestId") or message.get("id") or (stamp, tokens)
                old = records.get(key)
                if old is None or tokens > old[1]:
                    records[key] = (stamp, tokens)
        cutoff = now.timestamp() - 7 * 86400
        kept = [value for value in records.values() if value[0] >= cutoff]
        five = [value for value in kept if now.timestamp() - value[0] < 5 * 3600]
        return {"requests_5h": len(five),
                "tokens_5h": sum(value[1] for value in five),
                "tokens_7d": sum(value[1] for value in kept)}

    def scan(self, reader, limit=60):
        """Poll until the reader says the scan is complete."""
        for polls in range(limit):
            result = self.refresh(reader)
            if result["complete"]:
                return result, polls + 1
        self.fail("the scan never completed")

    def test_totals_match_a_one_shot_reference_once_the_scan_completes(self):
        self.build_fixture()
        expected = self.reference()
        reader = ActivityReader(self.projects, byte_cap=8192, chunk_bytes=self.CHUNK,
                                probe_bytes=self.PROBE, probe_files=3)
        result, polls = self.scan(reader)
        self.assertGreater(polls, 1, "the small budget must spread the scan over polls")
        self.assertEqual(result["requests_5h"], expected["requests_5h"])
        self.assertEqual(result["tokens_5h"], expected["tokens_5h"])
        self.assertEqual(result["tokens_7d"], expected["tokens_7d"])

    def test_totals_never_exceed_the_final_answer_and_never_decrease(self):
        self.build_fixture()
        expected = self.reference()
        reader = ActivityReader(self.projects, byte_cap=4096, chunk_bytes=self.CHUNK,
                                probe_bytes=self.PROBE, probe_files=3)
        seen = []
        for _ in range(60):
            result = self.refresh(reader)
            seen.append((result["requests_5h"], result["tokens_5h"], result["tokens_7d"]))
            if result["complete"]:
                break
        for earlier in seen:
            self.assertLessEqual(earlier[0], expected["requests_5h"])
            self.assertLessEqual(earlier[1], expected["tokens_5h"])
            self.assertLessEqual(earlier[2], expected["tokens_7d"])
        for before, after in zip(seen, seen[1:]):
            self.assertLessEqual(before[0], after[0])
            self.assertLessEqual(before[1], after[1])
            self.assertLessEqual(before[2], after[2])
        self.assertEqual(seen[-1][2], expected["tokens_7d"])

    def test_complete_flips_only_on_the_poll_that_finishes_the_last_file(self):
        self.build_fixture()
        reader = ActivityReader(self.projects, byte_cap=4096, chunk_bytes=self.CHUNK,
                                probe_bytes=self.PROBE, probe_files=3)
        results = []
        for _ in range(60):
            result = self.refresh(reader)
            results.append(result)
            if result["complete"]:
                break
        self.assertTrue(results[-1]["complete"])
        self.assertFalse(any(item["complete"] for item in results[:-1]))
        self.assertGreater(len(results), 1)
        self.assertEqual(results[-1]["tokens_7d"], self.reference()["tokens_7d"])

    def test_a_budget_smaller_than_one_line_still_makes_progress(self):
        """Every poll must finish at least one line, however small the budget.

        A real line is far smaller than the real budget, but a reader that
        forgot its partial line between polls would re-read the same bytes
        forever once the budget was below one line.
        """
        self.build_fixture()
        expected = self.reference()
        longest = max(len(raw) for path in self.projects.rglob("*.jsonl")
                      for raw in path.read_bytes().splitlines(True))
        reader = ActivityReader(self.projects, byte_cap=50, chunk_bytes=64,
                               probe_bytes=64, probe_files=0)
        self.assertGreater(longest, 64 * 3, "the fixture lines must dwarf the chunk")
        result, polls = self.scan(reader, limit=900)
        self.assertGreater(polls, 1)
        self.assertEqual(result["requests_5h"], expected["requests_5h"])
        self.assertEqual(result["tokens_5h"], expected["tokens_5h"])
        self.assertEqual(result["tokens_7d"], expected["tokens_7d"])

    def test_no_single_read_exceeds_the_chunk_size(self):
        self.build_fixture()
        sizes = []
        real_open = builtins.open

        class Counting:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                self.handle.__enter__()
                return self

            def __exit__(self, *exc):
                return self.handle.__exit__(*exc)

            def read(self, *args):
                data = self.handle.read(*args)
                sizes.append(len(data))
                return data

            def seek(self, position):
                return self.handle.seek(position)

            def __getattr__(self, name):
                return getattr(self.handle, name)

        def opener(file, *args, **kwargs):
            return Counting(real_open(file, *args, **kwargs))

        with patch("builtins.open", side_effect=opener):
            reader = ActivityReader(self.projects, byte_cap=64 * 1024, chunk_bytes=self.CHUNK,
                                    probe_bytes=self.PROBE, probe_files=3)
            self.scan(reader)
        self.assertTrue(sizes)
        self.assertLessEqual(max(sizes), self.CHUNK)

    def test_the_first_refresh_knows_the_newest_use_even_with_a_huge_file(self):
        self.build_fixture()
        reader = ActivityReader(self.projects, byte_cap=64, chunk_bytes=self.CHUNK,
                                probe_bytes=self.PROBE, probe_files=3)
        result = self.refresh(reader)
        self.assertIs(result["complete"], False)
        self.assertEqual(_stamp(result["last_request_at"]), moment(30).timestamp())
        self.assertEqual(result["last_entrypoint"], "CLI")
        self.assertEqual(result["last_model"], "claude-test")

    def test_a_request_seen_by_the_probe_is_not_counted_twice(self):
        directory = self.projects / "probe"
        directory.mkdir()
        # The same request twice with different usage: once early in the file
        # and once inside the probed tail. Only the request id says they are
        # one request, so the probe and the full read must agree.
        filler = [line(request_id="filler-%d" % index, seconds_ago=300)
                  for index in range(20)]
        early = line(request_id="dup", seconds_ago=120, inp=1, out=1, cc=1, cr=0)
        late = line(request_id="dup", seconds_ago=60, inp=100, out=50, cc=25, cr=0)
        (directory / "a.jsonl").write_bytes(("".join(filler) + early + late).encode("utf-8"))
        reader = ActivityReader(self.projects, byte_cap=1 << 20, chunk_bytes=self.CHUNK,
                                probe_bytes=self.PROBE, probe_files=2)
        result, _ = self.scan(reader)
        self.assertEqual(result["requests_5h"], 21)  # 20 fillers and one request
        self.assertEqual(result["tokens_7d"], 20 * 35 + 175)  # the larger total won


class ModelNameTests(unittest.TestCase):
    """The model name is shown, so only a short printable ASCII name is kept."""

    def last_model(self, model):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name) / "projects"
        (root / "p").mkdir(parents=True)
        entry = json.loads(line().rstrip("\n"))
        entry["message"]["model"] = model
        (root / "p" / "a.jsonl").write_bytes((json.dumps(entry) + "\n").encode("utf-8"))
        return ActivityReader(root).refresh(NOW)["last_model"]

    def test_only_short_printable_ascii_model_names_are_kept(self):
        cases = (("claude-sonnet-4-5", "claude-sonnet-4-5"), ("x" * 40, "x" * 40),
                 ("", None), ("x" * 41, None), ("claude-\u00e9", None),
                 ("bad\x07model", None), ("bad\nmodel", None), (12345, None), (None, None))
        for model, expected in cases:
            with self.subTest(model=model):
                self.assertEqual(self.last_model(model), expected)


class ModuleContractTests(unittest.TestCase):
    def test_reader_module_imports_nothing_from_the_package(self):
        # Phase 5c reuses this file on remote machines: stdlib only, no package.
        source = Path(claude_activity.__file__).read_text(encoding="utf-8")
        self.assertNotIn("presto_ai_dash", source)
        self.assertNotIn("from .", source)


def observation_for(report, seen=None, host=None):
    """The storage.observation row shape ClaudeSource.collect consumes."""
    return {"report": report, "evidence": {}, "collector_seen_at": seen,
            "collector_host": host, "collector_version": None,
            "last_progress_at": seen, "progress": None, "last_activity_at": None}


def quota_observation(observed, percent=12.0, reset_hours=5.0):
    from presto_ai_dash.quotas import quota_report, quota_window
    report = quota_report("anthropic", "Claude", [
        quota_window("five_hour", "5h", percent, NOW + timedelta(hours=reset_hours),
                     observed, 300),
        quota_window("seven_day", "Weekly", 34.0, NOW + timedelta(days=4),
                     observed, 10080)])
    return observation_for(report, seen=iso(observed), host="laptop")


class ClaudeSourceActivityTests(unittest.TestCase):
    """Phase 5 step 3: ClaudeSource.collect consumes the reader and sets the flag."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.projects = self.root / "projects"

    def source(self, observation=None, activity_logs=None, active_seconds=300):
        from presto_ai_dash.sources.claude import ClaudeSource
        config = {"settings_path": str(self.root / "settings.json"),
                  "stale_after_seconds": 900, "active_seconds": active_seconds}
        if activity_logs is not None:
            config["activity_logs"] = activity_logs
        source = ClaudeSource(config)
        source.id = "claude-1"

        class Storage:
            def observation(inner, source_id):
                return observation
        source.attach_storage(Storage())
        return source

    def session(self, *lines):
        directory = self.projects / "proj"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "s.jsonl"
        with open(path, "ab") as handle:
            handle.write("".join(lines).encode("utf-8"))
        return path

    def collect(self, source):
        with patch("presto_ai_dash.sources.claude.now_utc", return_value=NOW):
            return source.collect()

    def test_disabled_source_is_phase_4_byte_for_byte(self):
        self.session(line(request_id="x"))
        snapshot = self.collect(self.source(quota_observation(NOW - timedelta(days=6))))
        self.assertIsNone(snapshot.metrics["quota"].get("local_activity"))
        self.assertNotIn("local_activity", snapshot.metrics["quota"])
        self.assertNotIn("warn_on_stale", snapshot.metrics["quota"])
        self.assertEqual(snapshot.status, "ok")  # today's stale-quota collect status; present_snapshot warns

    def test_enabled_but_unavailable_keeps_phase_4_status_with_a_reason(self):
        snapshot = self.collect(self.source(None, activity_logs=True))
        report = snapshot.metrics["quota"]
        self.assertIs(report["local_activity"]["available"], False)
        self.assertTrue(report["local_activity"]["reason"].isascii())
        self.assertNotIn("warn_on_stale", report)
        self.assertEqual(snapshot.status, "warning")  # awaiting-export warning survives

    def test_in_use_gives_active_even_when_quota_never_arrived(self):
        self.session(line(request_id="r", seconds_ago=60))
        snapshot = self.collect(self.source(None, activity_logs=True))
        report = snapshot.metrics["quota"]
        self.assertEqual(snapshot.status, "active")
        self.assertIs(report["warn_on_stale"], False)
        self.assertIs(report["local_activity"]["available"], True)
        self.assertEqual(report["active_seconds"], 300)

    def test_outside_the_active_window_the_status_is_plain_ok(self):
        self.session(line(request_id="r", seconds_ago=3 * 3600))
        for observation in (None, quota_observation(NOW - timedelta(days=6))):
            with self.subTest(quota="never" if observation is None else "stale"):
                snapshot = self.collect(self.source(observation, activity_logs=True))
                self.assertEqual(snapshot.status, "ok")
                self.assertIs(snapshot.metrics["quota"]["warn_on_stale"], False)

    def test_rate_limited_still_wins_over_in_use(self):
        self.session(line(request_id="r", seconds_ago=60))
        snapshot = self.collect(self.source(quota_observation(NOW - timedelta(minutes=1),
                                                             percent=100.0),
                                            activity_logs=True))
        self.assertEqual(snapshot.status, "rate_limited")

    def test_the_source_owns_one_reader_and_polls_incrementally(self):
        path = self.session(line(request_id="one"))
        source = self.source(None, activity_logs=True)
        first = self.collect(source).metrics["quota"]["local_activity"]
        self.assertEqual(first["requests_5h"], 1)
        with open(path, "ab") as handle:
            handle.write(line(request_id="two", seconds_ago=30).encode("utf-8"))
        second = self.collect(source).metrics["quota"]["local_activity"]
        self.assertEqual(second["requests_5h"], 2)
        self.assertEqual(second["tokens_7d"], 70)

    def test_config_fields_declare_the_new_settings(self):
        from presto_ai_dash.sources.claude import ClaudeSource
        fields = {field["key"]: field for field in ClaudeSource.config_fields}
        self.assertEqual(fields["activity_logs"]["type"], "bool")
        self.assertIs(fields["activity_logs"]["default"], False)
        self.assertEqual(fields["activity_logs"]["label"],
                         "Read local Claude Code activity (timestamps and token counts only)")
        self.assertEqual((fields["active_seconds"]["type"], fields["active_seconds"]["default"],
                          fields["active_seconds"]["minimum"], fields["active_seconds"]["maximum"]),
                         ("int", 300, 30, 3600))


class WarnOnStaleFlagTests(unittest.TestCase):
    """Assumption D is carried by an explicit report flag, not provider branches."""

    def report(self, warn=None):
        from test_claude_hint import trail_report, measured  # same shapes as Phase 4
        report = trail_report(measured(6 * 24))
        if warn is not None:
            report["warn_on_stale"] = warn
        return report

    def stale_snapshot(self, warn=None, status="ok"):
        from presto_ai_dash.models import SourceSnapshot
        return SourceSnapshot("claude-1", status, NOW, {"quota": self.report(warn)})

    def test_present_snapshot_raises_stale_windows_without_the_flag(self):
        from presto_ai_dash.quotas import present_snapshot
        self.assertEqual(present_snapshot(self.stale_snapshot(), NOW).status, "warning")

    def test_present_snapshot_respects_the_flag(self):
        from presto_ai_dash.quotas import present_snapshot
        snapshot = self.stale_snapshot(warn=False)
        self.assertEqual(present_snapshot(snapshot, NOW).status, "ok")
        active = self.stale_snapshot(warn=False, status="active")
        self.assertEqual(present_snapshot(active, NOW).status, "active")

    def test_present_snapshot_never_downgrades_worse_statuses(self):
        from presto_ai_dash.quotas import present_snapshot
        for status in ("rate_limited", "offline", "critical"):
            with self.subTest(status=status):
                snapshot = self.stale_snapshot(warn=False, status=status)
                self.assertEqual(present_snapshot(snapshot, NOW).status, status)

    def card_for(self, hint, warn=None, status="active"):
        from presto_ai_dash.dashboard import _account_card
        from presto_ai_dash.models import SourceSnapshot
        report = self.report(warn)
        report["hint"] = hint
        snapshot = SourceSnapshot("claude-1", status, NOW, {"quota": report})
        return _account_card({"id": "claude-1", "name": "Claude", "type": "claude"},
                             snapshot, NOW)

    def test_account_card_raises_for_a_hint_without_the_flag(self):
        self.assertEqual(self.card_for("In use - quota 6d old").status, "warning")

    def test_account_card_respects_the_flag(self):
        self.assertEqual(self.card_for("In use - quota 6d old", warn=False).status, "active")


class ActivityPrivacyEndToEndTests(ManagementBase):
    """Assumption C end to end: sentinels in session files reach nothing.

    The real pipeline (temp database, ManagedCollector, payload builder) with
    activity enabled; prompt text, session ids and paths from the fixtures
    must never appear in the quota report, the status payload, the stored
    rows, the database file or DEBUG logs.
    """

    def setUp(self):
        super().setUp()
        self.disable_fake()
        self.root = Path(self.temp.name)
        self.projects = self.root / "projects"
        now = datetime.now(timezone.utc)
        directory = self.projects / "privacy"
        directory.mkdir(parents=True)
        (directory / "s.jsonl").write_bytes("".join((
            line(request_id="p1", raw_timestamp=iso(now - timedelta(seconds=30))),
            line(request_id="p2", raw_timestamp=iso(now - timedelta(minutes=2))),
        )).encode("utf-8"))
        self.row = self.manager.save({"type": "claude", "name": "Claude personal",
                                      "enabled": True,
                                      "config": {"settings_path": str(self.root / "settings.json"),
                                                 "activity_logs": True}})

    def _exercise(self):
        from presto_ai_dash.claude_export import build_envelope
        from presto_ai_dash.ingest import ingest
        envelope = build_envelope(json.loads(json.dumps({
            "version": "2.1.296", "cost": {"total_api_duration_ms": 300},
            "context_window": {"total_input_tokens": 600, "total_output_tokens": 100},
            "rate_limits": {"five_hour": {"used_percentage": 23.5,
                                          "resets_at": int(time.time()) + 18000},
                            "seven_day": {"used_percentage": 41.2,
                                          "resets_at": int(time.time()) + 8 * 86400}}})),
            "laptop", client_time=datetime.now(timezone.utc))
        self.assertTrue(ingest(self.storage, self.row["id"], envelope))
        ManagedCollector(self.manager, self.config, threading.Event()).collect_once()
        # The privacy sweep covers everything the daemon holds: the full payload
        # (a superset of what the Presto is sent), the device view, the admin list.
        full = self.manager.full_status_payload()
        self.assertTrue(full["sources"][0]["quota"]["local_activity"]["available"])
        return (json.dumps(full) + json.dumps(self.manager.status_payload()) +
                json.dumps(self.manager.list_sources()))

    def test_sentinels_never_reach_the_payload_or_reports(self):
        text = self._exercise()
        for sentinel in (SENTINEL_PROMPT, SENTINEL_SESSION, SENTINEL_CWD):
            self.assertNotIn(sentinel, text)

    def test_sentinels_never_reach_the_database(self):
        self._exercise()
        # The stored snapshots and the stored observation are read back
        # through the storage API: the rows on disk hold no session data.
        stored = json.dumps([snapshot.to_dict()
                             for snapshot in self.storage.latest_snapshots().values()])
        stored += json.dumps(self.storage.observation(self.row["id"]))
        for sentinel in (SENTINEL_PROMPT, SENTINEL_SESSION, SENTINEL_CWD):
            self.assertNotIn(sentinel, stored)
        raw = self.database.read_bytes()
        for sentinel in (SENTINEL_PROMPT, SENTINEL_SESSION, SENTINEL_CWD):
            self.assertNotIn(sentinel.encode(), raw)

    def test_sentinels_never_reach_debug_logs(self):
        import logging
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append("%s %s" % (record.levelname, record.getMessage()))

        root = logging.getLogger()
        handler = Capture()
        root.addHandler(handler)
        saved_level = root.level
        root.setLevel(logging.DEBUG)
        try:
            self._exercise()
        finally:
            root.removeHandler(handler)
            root.setLevel(saved_level)
        joined = "\n".join(records)
        for sentinel in (SENTINEL_PROMPT, SENTINEL_SESSION, SENTINEL_CWD):
            self.assertNotIn(sentinel, joined)


if __name__ == "__main__":
    unittest.main()
