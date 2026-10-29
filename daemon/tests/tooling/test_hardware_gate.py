"""Hardware experiment boundaries, safe output, cleanup, and fixture validity."""

from contextlib import contextmanager, redirect_stdout
import gc
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import presto_path

ROOT = Path(presto_path.REPO_ROOT)
if str(ROOT / "tools") not in sys.path:
    sys.path.append(str(ROOT / "tools"))
import presto_hardware_gate as gate
from hardware_gate.tls_fixture import fixtures, HOSTNAME
from hardware_gate.boot_fixture import build_sources, launch_source, read_source, cleanup_source


class BootSourceTests(unittest.TestCase):
    def test_full_file_length_does_not_disprove_an_active_rewrite(self):
        before = {"writing": True, "writer_duration_ms": 240000, "partial_bytes": 1048576}
        self.assertTrue(gate.power_interrupted_staging(before, 170.0))

    def test_completed_legacy_or_late_writer_cannot_pass_mid_write_gate(self):
        for state, elapsed in (({"writing": False, "writer_duration_ms": 240000}, 10),
                               ({"writing": True}, 10),
                               ({"writing": True, "writer_duration_ms": 240000}, 180),
                               ({"writing": True, "writer_duration_ms": 240000}, True)):
            with self.subTest(state=state, elapsed=elapsed):
                self.assertFalse(gate.power_interrupted_staging(state, elapsed))

    def test_power_cut_cannot_start_without_passing_boot_evidence(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(gate.subprocess, "run") as remote:
            folder = Path(temporary)
            (folder / "boot-final.json").write_text(json.dumps({"port": "COM3", "all_fixture_cases_pass": False}))
            with self.assertRaises(ValueError):
                gate.run_boot("COM3", folder / "power.json", power=True)
            remote.assert_not_called()

    def test_entry_module_cannot_collide_with_micropython_namespace_folder(self):
        fixture = build_sources("a" * 16)
        self.assertNotEqual(Path(fixture["root"]).name, Path(fixture["entry"]).stem)

    def test_generated_fixture_sources_compile_for_every_mode(self):
        fixture = build_sources("a" * 16)
        for name, source in (("setup", fixture["setup"]), ("loader", fixture["loader"]),
                             ("read", read_source(fixture)), ("cleanup", cleanup_source(fixture))):
            with self.subTest(name=name):
                compile(source, name, "exec")
        for mode in ("good", "malformed", "reset", "watchdog", "power", "resume"):
            compile(launch_source(fixture, mode), mode, "exec")

    def test_untrusted_fixture_path_or_operation_is_rejected(self):
        for nonce in ("../main", "a" * 15, "A" * 16, "a" * 16 + "/../"):
            with self.subTest(nonce=nonce), self.assertRaises(ValueError):
                build_sources(nonce)
        with self.assertRaises(ValueError):
            launch_source(build_sources("a" * 16), "arbitrary_code")


class ResultBoundaryTests(unittest.TestCase):
    def test_only_marked_result_is_returned(self):
        self.assertEqual(gate.parse_result(b"untrusted boot text\nPRESTO_GATE:{\"ok\":true}\n"), {"ok": True})

    def test_ambiguous_missing_and_oversized_results_are_rejected(self):
        for output in (b"boot only", b"PRESTO_GATE:{}\nPRESTO_GATE:{}\n", b"x" * 32769):
            with self.subTest(size=len(output)), self.assertRaises(RuntimeError):
                gate.parse_result(output)

    def test_non_object_result_is_rejected(self):
        with self.assertRaises(RuntimeError):
            gate.parse_result(b"PRESTO_GATE:[]")

    def test_automatic_port_is_rejected_before_board_access(self):
        for port in (None, "", "auto", "list"):
            with self.subTest(port=port), patch.object(gate.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    gate.run_source(port, Path("unused"), "", "test", "/unused")
                run.assert_not_called()

    def test_failed_probe_still_relaunches_and_never_echoes_output(self):
        secret_sentinel = b"untrusted output that must not be logged"
        calls = []

        def remote(args, **kwargs):
            calls.append(args)
            if "run" in args:
                return subprocess.CompletedProcess(args, 1, secret_sentinel, secret_sentinel)
            return subprocess.CompletedProcess(args, 0, b"", b"")

        @contextmanager
        def lock(_):
            yield

        with tempfile.TemporaryDirectory() as temporary, patch.object(gate, "device_lock", lock), \
                patch.object(gate, "snapshot", return_value={"revision": "a" * 64}), \
                patch.object(gate.subprocess, "run", side_effect=remote), redirect_stdout(io.StringIO()) as log:
            out = Path(temporary) / "report.json"
            result = gate.run_source("COM3", out, "print('public fixture')", "test", "/ai_dash_gate_" + "a" * 16)
            self.assertTrue(result["app_relaunch_requested"])
            self.assertEqual(result["host_error_type"], "RuntimeError")
            self.assertIn("--no-follow", calls[-1])
            self.assertNotIn(secret_sentinel.decode(), log.getvalue())
            self.assertNotIn(secret_sentinel.decode(), out.read_text())
            self.assertEqual(list(Path(temporary).iterdir()), [out])

    def test_timeout_does_not_skip_relaunch(self):
        @contextmanager
        def lock(_):
            yield

        with tempfile.TemporaryDirectory() as temporary, patch.object(gate, "device_lock", lock), \
                patch.object(gate, "snapshot", return_value={"revision": "a" * 64}), \
                patch.object(gate.subprocess, "run", side_effect=[
                    subprocess.TimeoutExpired("public probe", 90, output=b"unsafe boot output"),
                    subprocess.CompletedProcess([], 0, b"", b"")]), redirect_stdout(io.StringIO()):
            report = gate.run_source("COM3", Path(temporary) / "report.json", "", "test", "/unused")
            self.assertEqual(report["host_error_type"], "TimeoutExpired")
            self.assertTrue(report["app_relaunch_requested"])
            self.assertNotIn("unsafe", json.dumps(report))


class ScratchBoundaryTests(unittest.TestCase):
    def _load(self, folder):
        namespace = {}
        source = (ROOT / "tools" / "hardware_gate" / "probe.py").read_text(encoding="utf-8")
        exec(compile(source, "hardware-probe", "exec"), namespace)
        opened = []

        def local(path):
            return folder / str(path).lstrip("/")

        def safe_open(path, *args, **kwargs):
            opened.append(path)
            return open(local(path), *args, **kwargs)

        class SingleDigest:
            def __init__(self, data=b""):
                self.inner = hashlib.sha256(data)
                self.used = False

            def update(self, data):
                if self.used:
                    raise ValueError("finalized")
                self.inner.update(data)

            def digest(self):
                if self.used:
                    raise ValueError("finalized")
                self.used = True
                return self.inner.digest()

        namespace.update({
            "sys": SimpleNamespace(implementation=SimpleNamespace(name="micropython", version=(1, 29, 0, "")), version="synthetic"),
            "os": SimpleNamespace(uname=lambda: SimpleNamespace(machine="Presto with RP2350"),
                stat=lambda p: local(p).stat(), statvfs=lambda _: (4096, 4096, 3584, 3432, 3432, 0, 0, 0, 0, 255),
                mkdir=lambda p: local(p).mkdir(), remove=lambda p: local(p).unlink(),
                rmdir=lambda p: local(p).rmdir(), rename=lambda a, b: os.replace(local(a), local(b)), sync=lambda: None),
            "gc": SimpleNamespace(mem_free=lambda: 8000000, collect=gc.collect),
            "time": SimpleNamespace(ticks_us=lambda: time.monotonic_ns() // 1000, ticks_diff=lambda a, b: a - b),
            "hashlib": SimpleNamespace(sha256=SingleDigest), "open": safe_open})
        return namespace, opened

    def test_private_path_rejected_before_any_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            namespace, opened = self._load(Path(temporary))
            for path in ("/ai_dash_files/config.py", "/secrets.py", "/ai_dash_files/my_secrets.py", "/dash.db", "/outside.py"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    namespace["run_probe"]("/ai_dash_gate_" + "a" * 16, [path])
            self.assertEqual(opened, [])

    def test_existing_scratch_is_never_overwritten_or_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            scratch = "/ai_dash_gate_" + "a" * 16
            (folder / scratch[1:]).mkdir()
            sentinel = folder / scratch[1:] / "unrelated.txt"
            sentinel.write_text("preserve")
            namespace, opened = self._load(folder)
            with self.assertRaises(RuntimeError):
                namespace["run_probe"](scratch, [])
            self.assertEqual(sentinel.read_text(), "preserve")
            self.assertEqual(opened, [])

    def test_stream_hash_rename_and_cleanup_with_single_use_digest(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            folder = Path(temporary)
            namespace, _ = self._load(folder)
            report = namespace["run_probe"]("/ai_dash_gate_" + "a" * 16, [])
            self.assertNotIn("probe_error_type", report)
            self.assertTrue(report["sha256_vector_ok"])
            self.assertTrue(report["stream_write"]["hash_matches"])
            self.assertEqual(report["stream_write"]["bytes"], 262144)
            self.assertTrue(report["rename_preserved_hash"])
            self.assertEqual(list(folder.iterdir()), [])


class TlsFixtureTests(unittest.TestCase):
    def test_fixture_positive_and_negative_certificates_and_key_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with fixtures("127.0.0.1", folder) as settings:
                context = ssl.create_default_context(cadata=bytes.fromhex(settings["ca_der_hex"]))
                with socket.create_connection(("127.0.0.1", settings["valid_port"]), timeout=3) as raw:
                    with context.wrap_socket(raw, server_hostname=HOSTNAME) as stream:
                        stream.sendall(b"GET /probe HTTP/1.0\r\nHost: presto-gate.invalid\r\n\r\n")
                        self.assertIn(b"200", stream.recv(256))
                for kind, hostname in (("valid", "wrong-gate.invalid"), ("expired", HOSTNAME), ("future", HOSTNAME)):
                    with self.subTest(kind=kind), socket.create_connection(("127.0.0.1", settings[kind + "_port"]), timeout=3) as raw:
                        with self.assertRaises(ssl.SSLCertVerificationError):
                            context.wrap_socket(raw, server_hostname=hostname)
            self.assertEqual(list(folder.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
