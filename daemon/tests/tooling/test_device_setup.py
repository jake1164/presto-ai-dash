"""The one-time setup handoff: encryption, payload rules, service and packaging.

The handoff is a temporary encrypted file, not an enrollment token: these tests
pin the versioned envelope, the entropy and format of the unlock code, the
refusal of tampered or oversized input, and the fact that the code and the
admin token are in neither download. The standalone installer is rendered from
the same template the admin page serves, so its decryptor is checked against
the daemon's envelope here rather than assumed to match.
"""

from datetime import datetime, timedelta, timezone
import base64
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
import unittest.mock

import presto_path

TOOLS = Path(presto_path.REPO_ROOT) / "tools"
SRC = Path(presto_path.REPO_ROOT) / "daemon" / "src"
for folder in (TOOLS, SRC):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import presto_bundle  # noqa: E402
from presto_ai_dash import device_setup  # noqa: E402
from presto_ai_dash import device_install  # noqa: E402

BUNDLE = presto_bundle.snapshot()
TOKEN = "device-token-0123456789-abcdefghij"
ADMIN = "admin-token-0123456789-abcdefghij"


def payload(**changes):
    base = device_setup.build_payload(BUNDLE, "192.168.1.50", 8787, TOKEN, "presto", "0.50.0")
    base.update(changes)
    return base


def rendered_module():
    """The standalone installer, executed as a module (it is not imported)."""
    source = device_setup.render_setup_script(BUNDLE, "0.50.0").decode("utf-8")
    module = types.ModuleType("rendered_presto_setup")
    exec(compile(source, "presto_setup.py", "exec"), module.__dict__)
    return module


class UnlockCodeTests(unittest.TestCase):
    def test_a_code_is_eight_groups_of_four_from_the_base32_alphabet(self):
        for _ in range(20):
            code = device_setup.new_unlock_code()
            groups = code.split("-")
            self.assertEqual(len(groups), 8)
            self.assertTrue(all(len(group) == 4 for group in groups))
            text = "".join(groups)
            self.assertEqual(len(text), 32)
            self.assertTrue(all(char in device_setup.CODE_ALPHABET for char in text))

    def test_every_code_is_fresh(self):
        codes = {device_setup.new_unlock_code() for _ in range(200)}
        self.assertEqual(len(codes), 200)

    def test_people_may_retype_a_code_in_lower_case_and_without_dashes(self):
        code = device_setup.new_unlock_code()
        self.assertEqual(device_setup.code_bytes(code.lower().replace("-", " ")),
                         device_setup.code_bytes(code))

    def test_an_unusable_code_is_refused_without_echoing_it(self):
        for candidate in ("", "short", "A" * 31, "A" * 33, "0000-0000-0000-0000-0000-0000-0000-0001",
                          None, 1234, "$BAD-$BAD-$BAD-$BAD-$BAD-$BAD-$BAD-$BAD"):
            with self.subTest(candidate=candidate):
                with self.assertRaises(device_setup.SetupError) as caught:
                    device_setup.code_bytes(candidate)
                self.assertNotIn("$BAD", str(caught.exception))


class EnvelopeTests(unittest.TestCase):
    def test_a_handoff_round_trips_and_carries_the_device_token(self):
        code = device_setup.new_unlock_code()
        envelope = device_setup.build_handoff(payload(), code)
        opened = device_setup.open_handoff(envelope, code)
        self.assertEqual(opened, payload())
        self.assertEqual(opened["daemon"]["token"], TOKEN)

    def test_the_envelope_is_json_with_exactly_the_documented_fields(self):
        envelope = json.loads(device_setup.build_handoff(payload(), device_setup.new_unlock_code()))
        self.assertEqual(set(envelope), {"v", "kdf", "cipher", "salt", "nonce", "ct"})
        self.assertEqual(envelope["v"], 1)
        self.assertEqual(envelope["kdf"], "HKDF-SHA256")
        self.assertEqual(envelope["cipher"], "AES-256-GCM")

    def test_the_salt_and_nonce_are_fresh_for_every_handoff(self):
        code = device_setup.new_unlock_code()
        first = json.loads(device_setup.build_handoff(payload(), code))
        second = json.loads(device_setup.build_handoff(payload(), code))
        self.assertNotEqual(first["salt"], second["salt"])
        self.assertNotEqual(first["nonce"], second["nonce"])
        self.assertNotEqual(first["ct"], second["ct"])

    def test_a_wrong_code_fails_without_revealing_which_part_was_wrong(self):
        code = device_setup.new_unlock_code()
        envelope = device_setup.build_handoff(payload(), code)
        with self.assertRaises(device_setup.SetupError) as caught:
            device_setup.open_handoff(envelope, "A" * 32)
        self.assertIn("unlock code did not open", str(caught.exception))
        self.assertNotIn(code, str(caught.exception))
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_tampering_any_sealed_part_is_refused(self):
        code = device_setup.new_unlock_code()
        envelope = json.loads(device_setup.build_handoff(payload(), code))
        sealed = bytearray(base64.b64decode(envelope["ct"]))
        sealed[5] ^= 0x01
        cases = {
            "ct": {**envelope, "ct": base64.b64encode(bytes(sealed)).decode()},
            "nonce": {**envelope, "nonce": base64.b64encode(b"\x00" * 12).decode()},
            "salt": {**envelope, "salt": base64.b64encode(b"\x00" * 16).decode()},
            "version": {**envelope, "v": 2},
            "cipher": {**envelope, "cipher": "AES-128-CBC"},
            "extra": {**envelope, "notes": "hello"},
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(device_setup.SetupError):
                    device_setup.open_handoff(json.dumps(value).encode(), code)

    def test_oversized_and_malformed_inputs_are_refused_before_decryption(self):
        code = device_setup.new_unlock_code()
        for data in (b"", b"not json", b"[]", b"{}",
                     b"x" * (device_setup.MAX_ENVELOPE_BYTES + 1),
                     json.dumps({"v": 1, "kdf": "HKDF-SHA256", "cipher": "AES-256-GCM",
                                 "salt": "!", "nonce": "", "ct": ""}).encode()):
            with self.subTest(data=data[:16]):
                with self.assertRaises(device_setup.SetupError):
                    device_setup.open_handoff(data, code)


class PayloadTests(unittest.TestCase):
    def assert_refused(self, change):
        value = payload()
        change(value)
        with self.assertRaises(device_setup.SetupError):
            device_setup.validate_payload(value)

    def test_a_built_payload_is_accepted_and_has_no_admin_field(self):
        value = device_setup.validate_payload(payload())
        self.assertEqual(set(value), {"v", "daemon", "device", "app", "release", "created_at"})
        self.assertNotIn("admin", json.dumps(value))
        self.assertEqual(value["app"]["label"], value["app"]["revision"][:12])
        self.assertEqual(value["device"]["settings"], device_setup.DEFAULT_SETTINGS)

    def test_hosts_that_a_device_cannot_use_are_refused(self):
        for host in ("", "localhost", "http://192.168.1.50", "192.168.1.50/",
                     "192.168.1.50:8787", "192.168.1.999", " 192.168.1.5", "a b",
                     "192.168.1", "-bad.example", "example..lan"):
            with self.subTest(host=host):
                self.assertFalse(device_setup.valid_host(host))
        for host in ("192.168.1.50", "10.0.0.7", "dash.lan", "dash.internal.example"):
            with self.subTest(host=host):
                self.assertTrue(device_setup.valid_host(host))

    def test_ports_tokens_names_defaults_and_revisions_are_bounded(self):
        self.assert_refused(lambda value: value["daemon"].update(port=0))
        self.assert_refused(lambda value: value["daemon"].update(port=70000))
        self.assert_refused(lambda value: value["daemon"].update(port=True))
        self.assert_refused(lambda value: value["daemon"].update(token="short"))
        self.assert_refused(lambda value: value["daemon"].update(token="a" * 600))
        self.assert_refused(lambda value: value["daemon"].update(token="has space in it 123"))
        self.assert_refused(lambda value: value["device"].update(name=""))
        self.assert_refused(lambda value: value["device"].update(name="x" * 33))
        self.assert_refused(lambda value: value["device"]["settings"].update(poll_interval=1))
        self.assert_refused(lambda value: value["device"]["settings"].pop("http_timeout"))
        self.assert_refused(lambda value: value["app"].update(revision="abc"))
        self.assert_refused(lambda value: value["app"].update(label="0" * 12))
        self.assert_refused(lambda value: value.update(release={"version": "has space"}))
        self.assert_refused(lambda value: value.update(extra=True))
        self.assert_refused(lambda value: value.update(v=2))


class StandaloneScriptTests(unittest.TestCase):
    def test_the_script_compiles_and_carries_the_exact_bundle(self):
        source = device_setup.render_setup_script(BUNDLE, "0.50.0")
        compile(source, "presto_setup.py", "exec")
        module = rendered_module()
        self.assertEqual(module.BUNDLE["revision"], BUNDLE["revision"])
        self.assertEqual(module.BUNDLE["hashes"], BUNDLE["hashes"])
        self.assertEqual(module.BUNDLE["files"], BUNDLE["files"])
        self.assertEqual(module.RELEASE, "0.50.0")

    def test_the_script_imports_nothing_from_the_daemon_package(self):
        source = device_setup.render_setup_script(BUNDLE, "0.50.0").decode("utf-8")
        self.assertNotIn("import presto_ai_dash", source)
        self.assertNotIn("from presto_ai_dash", source)

    def test_the_render_is_deterministic_for_one_bundle(self):
        first = device_setup.render_setup_script(BUNDLE, "0.50.0")
        second = device_setup.render_setup_script(BUNDLE, "0.50.0")
        self.assertEqual(first, second)

    def test_neither_download_carries_the_unlock_code(self):
        code = device_setup.new_unlock_code()
        script = device_setup.render_setup_script(BUNDLE, "0.50.0")
        envelope = device_setup.build_handoff(payload(), code)
        self.assertNotIn(code.encode(), script)
        self.assertNotIn(code.replace("-", "").encode(), envelope)
        self.assertNotIn(TOKEN.encode(), script)
        self.assertNotIn(ADMIN.encode(), script)
        self.assertNotIn(ADMIN.encode(), envelope)

    def test_the_standalone_decryptor_opens_the_daemons_handoff(self):
        module = rendered_module()
        code = device_setup.new_unlock_code()
        envelope = device_setup.build_handoff(payload(), code)
        self.assertEqual(module.open_handoff(envelope, code), payload())

    def test_the_standalone_decryptor_refuses_a_tampered_file(self):
        module = rendered_module()
        code = device_setup.new_unlock_code()
        envelope = device_setup.build_handoff(payload(), code)
        broken = envelope.replace(b'"ct":"', b'"ct":"A')
        with self.assertRaises(module.SetupStopped):
            module.open_handoff(broken, code)


class SetupServiceTests(unittest.TestCase):
    def service(self, bundle=BUNDLE, token=TOKEN, temporary=False, clock=None, container=False):
        return device_setup.SetupService(
            bundle_provider=(lambda: bundle) if not isinstance(bundle, Exception) else
            (lambda: (_ for _ in ()).throw(bundle)),
            shared_token=token, host="0.0.0.0", port=8787, release="0.50.0",
            clock=clock or (lambda: datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)),
            token_temporary=temporary, container=container)

    def test_state_describes_the_running_bundle_and_suggests_an_address(self):
        state = self.service(temporary=True).state("dash.lan:8788")
        self.assertTrue(state["available"])
        self.assertEqual(state["revision"], BUNDLE["revision"])
        self.assertEqual(state["label"], BUNDLE["revision"][:12])
        self.assertEqual(state["release"], "0.50.0")
        self.assertTrue(state["token_temporary"])
        self.assertEqual(state["daemon"]["host"], "dash.lan")
        self.assertEqual(state["daemon"]["port"], 8787)
        self.assertEqual(state["downloads"], {"script": "presto_setup.py",
                                              "handoff": "presto-setup.enc"})

    def test_a_short_shared_token_is_refused_with_what_to_change(self):
        with self.assertRaises(device_setup.SetupUnavailable) as caught:
            self.service(token="x" * 15).create("192.168.1.50", 8787)
        message = str(caught.exception)
        self.assertIn("shorter than 16 characters", message)
        self.assertIn("daemon.shared_token", message)
        self.assertNotIn("x" * 15, message)
        self.assertEqual(self.service(token="x" * 16).create("192.168.1.50", 8787)["v"], 1)

    def test_a_container_bind_address_is_never_suggested(self):
        state = self.service().state("127.0.0.1:8788")
        self.assertNotIn("0.0.0.0", state["daemon"]["suggested"])
        self.assertNotIn("127.0.0.1", state["daemon"]["suggested"])

    def test_a_container_never_offers_its_own_address_as_the_daemon(self):
        # Inside a container the discovered address is the container's own, and
        # no Presto on the LAN can reach it. The browser's Host header, which in
        # Docker is the published host address, is still the best hint.
        with unittest.mock.patch.object(device_setup, "_lan_address",
                                        return_value="172.17.0.2"):
            inside = self.service(container=True).state("dash.lan:8788")
            outside = self.service().state("127.0.0.1:8788")
        self.assertEqual(inside["daemon"]["suggested"], ["dash.lan"])
        self.assertEqual(outside["daemon"]["suggested"], ["172.17.0.2"])

    def test_a_build_without_app_files_says_so_instead_of_offering_a_download(self):
        service = self.service(bundle=device_install.NoBundledApp("none"))
        state = service.state()
        self.assertFalse(state["available"])
        self.assertEqual(state["reason"], "bundle_unavailable")
        with self.assertRaises(device_setup.SetupError):
            service.create("192.168.1.50", 8787)

    def test_create_returns_a_usable_pair_and_a_one_time_code(self):
        service = self.service()
        first = service.create("192.168.1.50", 8787)
        second = service.create("192.168.1.50", 8787)
        self.assertNotEqual(first["handoff_id"], second["handoff_id"])
        self.assertNotEqual(first["unlock_code"], second["unlock_code"])
        self.assertEqual(len(first["handoff_id"]), 32)
        opened = device_setup.open_handoff(service.handoff(first["handoff_id"]),
                                          first["unlock_code"])
        self.assertEqual(opened["daemon"]["host"], "192.168.1.50")
        self.assertEqual(opened["daemon"]["token"], TOKEN)
        script = service.script(first["handoff_id"])
        self.assertEqual(script, device_setup.render_setup_script(BUNDLE, "0.50.0"))

    def test_an_unknown_or_discarded_handoff_is_not_served(self):
        service = self.service()
        prepared = service.create("192.168.1.50", 8787)
        self.assertTrue(service.discard(prepared["handoff_id"]))
        self.assertFalse(service.discard(prepared["handoff_id"]))
        for call in (service.handoff, service.script):
            with self.assertRaises(KeyError):
                call(prepared["handoff_id"])
        with self.assertRaises(KeyError):
            service.handoff("../etc/passwd")

    def test_handoffs_are_bounded_in_number(self):
        service = self.service()
        for _ in range(device_setup.MAX_HANDOFFS + 3):
            service.create("192.168.1.50", 8787)
        self.assertEqual(service.state()["handoffs"], device_setup.MAX_HANDOFFS)

    def test_a_handoff_expires(self):
        now = [datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)]
        service = self.service(clock=lambda: now[0])
        prepared = service.create("192.168.1.50", 8787)
        now[0] = now[0] + timedelta(seconds=device_setup.HANDOFF_TTL_SECONDS + 1)
        with self.assertRaises(KeyError):
            service.handoff(prepared["handoff_id"])

    def test_bad_requests_are_refused_without_a_handoff(self):
        service = self.service()
        for host, port in (("", 8787), ("localhost", 8787), ("http://x", 8787),
                           ("192.168.1.50", 0), ("192.168.1.50", "8787")):
            with self.subTest(host=host, port=port), self.assertRaises(device_setup.SetupError):
                service.create(host, port)

    def test_no_shared_token_means_no_handoff(self):
        with self.assertRaises(device_setup.SetupError):
            self.service(token="").create("192.168.1.50", 8787)

    def test_the_wifi_choice_is_carried_inside_the_sealed_handoff(self):
        service = self.service()
        for ticked, expected in ((True, "set"), (False, "keep")):
            with self.subTest(ticked=ticked):
                prepared = service.create("192.168.1.50", 8787, wifi=ticked)
                self.assertEqual(prepared["wifi"], ticked)
                opened = device_setup.open_handoff(service.handoff(prepared["handoff_id"]),
                                                   prepared["unlock_code"])
                self.assertEqual(opened["device"]["wifi"], expected)

    def test_the_wifi_is_set_unless_the_admin_says_otherwise(self):
        service = self.service()
        prepared = service.create("192.168.1.50", 8787)
        self.assertTrue(prepared["wifi"])
        opened = device_setup.open_handoff(service.handoff(prepared["handoff_id"]),
                                           prepared["unlock_code"])
        self.assertEqual(opened["device"]["wifi"], "set")

    def test_the_wifi_choice_must_be_a_real_boolean(self):
        service = self.service()
        for bad in ("no", "false", 0, 1, None, [], "keep"):
            with self.subTest(value=bad), self.assertRaises(device_setup.SetupError):
                service.create("192.168.1.50", 8787, wifi=bad)

    def test_a_payload_with_an_unknown_wifi_choice_is_refused(self):
        for bad in ("maybe", "", None, True, "SET"):
            with self.subTest(value=bad):
                value = payload()
                value["device"] = dict(value["device"], wifi=bad)
                with self.assertRaises(device_setup.SetupError):
                    device_setup.validate_payload(value)

class PackagingParityTests(unittest.TestCase):
    def test_editable_checkout_ignores_stale_staged_bundle(self):
        with unittest.mock.patch.object(device_install, 'bundled_snapshot', return_value={'revision': 'stale'}) as staged:
            self.assertEqual(device_install.app_snapshot()['revision'], presto_bundle.snapshot()['revision'])
            staged.assert_not_called()

    def test_explicit_staged_folder_is_still_respected(self):
        with unittest.mock.patch.object(device_install, 'bundled_snapshot', return_value={'revision': 'explicit'}) as staged:
            self.assertEqual(device_install.app_snapshot(folder='explicit-folder')['revision'], 'explicit')
            staged.assert_called_once_with('explicit-folder')

    def test_the_container_stages_the_same_bundle_as_the_wheel(self):
        script = Path(presto_path.REPO_ROOT) / "docker" / "stage_app.py"
        namespace = {"__name__": "stage_app"}
        exec(compile(script.read_text(encoding="utf-8"), str(script), "exec"), namespace)
        with tempfile.TemporaryDirectory() as temporary:
            bundle = namespace["stage"](presto_path.REPO_ROOT, temporary)
            self.assertEqual(bundle["revision"], BUNDLE["revision"])
            staged = {}
            root = Path(temporary) / "daemon" / "src" / "presto_ai_dash" / "presto_app"
            for path in root.rglob("*"):
                if path.is_file():
                    staged["/" + path.relative_to(root).as_posix()] = path.read_bytes()
            self.assertEqual(staged, BUNDLE["files"])
            # The slot and bootstrap bundles come too, so the image serves what a
            # wheel install does: loaded the way the daemon loads them, they match.
            from presto_ai_dash.device_app import manifest_bytes, slot_inventory
            loaded = device_install.bundled_snapshot(root)
            self.assertIsNotNone(loaded.get("slot"), "the image can serve updates")
            self.assertIsNotNone(loaded.get("bootstrap"))
            served = slot_inventory(loaded)
            self.assertIsNotNone(served)
            # The same update manifest a wheel install serves (compared as bytes:
            # a dict diff of whole files would take minutes to print).
            self.assertTrue(manifest_bytes(served) == manifest_bytes(slot_inventory(BUNDLE)))
            self.assertTrue(loaded["bootstrap"]["hashes"] == BUNDLE["bootstrap"]["hashes"])

    def test_the_daemon_reads_the_checkout_bundle_when_it_has_no_staged_copy(self):
        self.assertEqual(device_install.app_snapshot()["revision"], BUNDLE["revision"])

    def test_the_wheel_declares_the_bundle_and_the_installer_template(self):
        pyproject = (Path(presto_path.REPO_ROOT) / "daemon" / "pyproject.toml").read_text(
            encoding="utf-8")
        for pattern in ('"presto_app/*.py"', '"presto_app/ai_dash_files/*.py"', '"setup/*.py"'):
            self.assertIn(pattern, pyproject)
        self.assertTrue((Path(presto_path.REPO_ROOT) / "daemon" / "src" / "presto_ai_dash"
                         / "setup" / "presto_setup_template.py").is_file())

    def test_the_release_checks_the_setup_template_is_in_the_wheel(self):
        source = (Path(presto_path.REPO_ROOT) / "tools" / "build_release.py").read_text(
            encoding="utf-8")
        self.assertIn("presto_ai_dash/setup/presto_setup_template.py", source)

    def test_the_release_workflow_records_the_exact_tag_in_the_image(self):
        workflow = (Path(presto_path.REPO_ROOT) / ".github" / "workflows"
                    / "release.yml").read_text(encoding="utf-8")
        self.assertIn('--build-arg DASH_RELEASE_VERSION="$VERSION"', workflow)
        dockerfile = (Path(presto_path.REPO_ROOT) / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("DASH_RELEASE_VERSION", dockerfile)
        self.assertIn("python docker/stage_app.py /build", dockerfile)


class ReleaseIdentityTests(unittest.TestCase):
    def test_a_tagged_build_reports_its_exact_identity_including_a_candidate(self):
        from presto_ai_dash import release
        self.assertEqual(release.release_version({"DASH_RELEASE_VERSION": "0.50.0-rc3"}),
                         "0.50.0-rc3")
        self.assertEqual(release.release_channel({"DASH_RELEASE_VERSION": "0.50.0-rc3"}),
                         "published")

    def test_an_unusable_override_is_ignored(self):
        from presto_ai_dash import __version__, release
        for value in ("", "  ", "has space", "x" * 65, "\x00"):
            with self.subTest(value=value):
                self.assertEqual(release.release_version({"DASH_RELEASE_VERSION": value}),
                                 __version__)
                self.assertEqual(release.release_channel({"DASH_RELEASE_VERSION": value}),
                                 "development")


if __name__ == "__main__":
    unittest.main()
