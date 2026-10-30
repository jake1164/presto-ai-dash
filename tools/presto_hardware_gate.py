"""Run bounded hardware experiments without deploying or reading private files.

    .venv/Scripts/python tools/presto_hardware_gate.py probe --port COM3

The board's normal app is interrupted and relaunched, as with USB check.
Only a freshly named scratch directory is written. Output contains structured
diagnostics, never raw board stdout, remote traceback text, or private files.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "daemon" / "src"))
from presto_ai_dash.device_install import LAUNCH, device_lock
from presto_bundle import RECEIPT, snapshot

MARKER = b"PRESTO_GATE:"


def parse_result(output):
    if len(output) > 32768:
        raise RuntimeError("probe_output_too_large")
    lines = [line[len(MARKER):] for line in output.splitlines() if line.startswith(MARKER)]
    if len(lines) != 1:
        raise RuntimeError("probe_result_missing_or_ambiguous")
    result = json.loads(lines[0])
    if not isinstance(result, dict):
        raise RuntimeError("probe_result_invalid")
    return result


def run_source(port, out, source, kind, scratch, timeout=90, metadata=None):
    if not port or port in ("auto", "list"):
        raise ValueError("explicit_port_required")
    bundle = snapshot()
    report = {"checked_at": datetime.now(timezone.utc).isoformat(),
              "port": port, "source_revision": bundle["revision"],
              "kind": kind, "scratch": scratch,
              "private_files_opened": False, "app_relaunch_requested": False}
    if metadata:
        report["fixture"] = metadata
    with device_lock(RECEIPT.with_suffix(".lock")):
        try:
            print("Probe: running isolated checks through the established mpremote CLI", flush=True)
            out.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="public-probe-", dir=out.parent) as temporary:
                script = Path(temporary) / "probe.py"
                script.write_text(source, encoding="utf-8")
                process = subprocess.run([sys.executable, "-u", "-m", "mpremote", "connect", port,
                                          "soft-reset", "run", str(script)], capture_output=True,
                                         timeout=timeout)
                if process.returncode:
                    raise RuntimeError("board_probe_failed")
                report["probe"] = parse_result(process.stdout)
        except Exception as exc:
            report["host_error_type"] = type(exc).__name__
        finally:
            print("Probe: relaunching AI Dash", flush=True)
            try:
                restarted = subprocess.run([sys.executable, "-m", "mpremote", "connect", port,
                                            "soft-reset", "exec", "--no-follow", LAUNCH],
                                           capture_output=True, timeout=30)
                report["app_relaunch_requested"] = restarted.returncode == 0
            except Exception as exc:
                report["relaunch_error_type"] = type(exc).__name__
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def run_probe(port, out):
    bundle = snapshot()
    paths = list(bundle["files"]) + ["/main.py"]
    scratch = "/ai_dash_gate_" + secrets.token_hex(8)
    source = (ROOT / "tools" / "hardware_gate" / "probe.py").read_text(encoding="utf-8")
    source += ("\ntry:\n    _gate_value = run_probe(%r, %r)\n"
               "except Exception as _gate_exc:\n"
               "    _gate_value = {'probe_error_type': type(_gate_exc).__name__, 'stage': _probe_stage}\n"
               "print('PRESTO_GATE:' + json.dumps(_gate_value))\n") % (scratch, paths)
    return run_source(port, out, source, "capability_probe", scratch)


def run_tls(port, out, host):
    from hardware_gate.tls_fixture import fixtures
    if not host:
        raise ValueError("explicit_lan_host_required")
    out.parent.mkdir(parents=True, exist_ok=True)
    with fixtures(host, out.parent) as settings:
        settings["scratch"] = "/ai_dash_gate_" + secrets.token_hex(8)
        source = (ROOT / "tools" / "hardware_gate" / "tls_probe.py").read_text(encoding="utf-8")
        source += ("\ntry:\n    _gate_value = run_tls_probe(%r)\n"
                   "except Exception as _gate_exc:\n"
                   "    _gate_value = {'probe_error_type': type(_gate_exc).__name__}\n"
                   "print('PRESTO_GATE:' + json.dumps(_gate_value))\n") % settings
        metadata = {"host": host, "certificate_keys": "ephemeral_synthetic_only",
                    "payload_bytes": 65536, "payload_sha256": settings["payload_sha256"]}
        return run_source(port, out, source, "tls_probe", settings["scratch"], 180, metadata)


def power_interrupted_staging(before_resume, disconnect_after_arming_s):
    """A sustained writer covers the whole armed window, even if size is full.

    A recovered LittleFS file can expose its previous complete length. File
    length alone cannot establish whether a repeated staging write was active.
    """
    return (before_resume.get("writing") is True
            and before_resume.get("writer_duration_ms") == 240000
            and type(disconnect_after_arming_s) in (int, float)
            and 0 <= disconnect_after_arming_s < 180)


def run_boot(port, out, power=False):
    from hardware_gate.boot_fixture import build_sources, launch_source, read_source, cleanup_source
    if not port or port.lower() in ("auto", "list"):
        raise ValueError("explicit_port_required")
    if power:
        baseline = json.loads((out.parent / "boot-final.json").read_text(encoding="utf-8"))
        if baseline.get("all_fixture_cases_pass") is not True or baseline.get("port") != port:
            raise ValueError("passing_boot_fixture_required_before_power_cut")
    fixture = build_sources(secrets.token_hex(8))
    out.parent.mkdir(parents=True, exist_ok=True)
    report = {"kind": "disposable_boot_fixture", "checked_at": datetime.now(timezone.utc).isoformat(),
              "port": port, "fixture_root": fixture["root"], "cases": {},
              "production_bootstrap_tested": False, "app_relaunch_requested": False}
    with device_lock(RECEIPT.with_suffix(".lock")), tempfile.TemporaryDirectory(prefix="public-boot-", dir=out.parent) as temporary:
        def command(source, follow=True):
            if follow:
                script = Path(temporary) / "command.py"
                wrapped = ("import json\ntry:\n    exec(%r)\nexcept Exception as exc:\n"
                    "    code = exc.args[0] if exc.args and exc.args[0] in "
                    "('wrong_board', 'scratch_exists', 'entry_exists', 'launch_already_pending', 'insufficient_safe_headroom') else None\n"
                    "    print('PRESTO_GATE:' + json.dumps({'_error': type(exc).__name__, '_code': code}))\n") % source
                script.write_text(wrapped, encoding="utf-8")
                args = ["run", str(script)]
            else:
                args = ["exec", "--no-follow", source]
            process = subprocess.run([sys.executable, "-u", "-m", "mpremote", "connect", port, *args],
                                     capture_output=True, timeout=35)
            if process.returncode:
                raise RuntimeError("boot_fixture_command_failed")
            value = parse_result(process.stdout) if follow else None
            if value and "_error" in value:
                report["board_error"] = value
                raise RuntimeError("boot_fixture_rejected")
            return value

        prepared = False
        board_identity_ok = True
        try:
            prepared = command(fixture["setup"])["prepared"] is True
            for mode in (("power",) if power else ("good", "malformed", "reset", "watchdog")):
                print("Boot fixture: " + mode, flush=True)
                command(launch_source(fixture, mode), follow=False)
                if power:
                    from serial.tools import list_ports
                    # Exclude the USB re-enumeration caused by the warm reset
                    # we just requested; it is not physical power-loss evidence.
                    time.sleep(4)
                    expected = next((p.serial_number for p in list_ports.comports() if p.device.upper() == port.upper()), None)
                    if expected is None:
                        raise RuntimeError("board_not_ready_to_arm_power_test")
                    report["user_confirmed_power_cycle"] = False
                    print("POWER TEST READY: unplug USB while the disposable scratch write is running, then reconnect.", flush=True)
                    began = time.monotonic()
                    disconnected = False
                    while time.monotonic() - began < 180:
                        board = next((p for p in list_ports.comports() if p.device.upper() == port.upper()), None)
                        if board is None:
                            if not disconnected:
                                report["disconnect_after_arming_s"] = round(time.monotonic() - began, 3)
                            disconnected = True
                        elif disconnected:
                            if expected and board.serial_number != expected:
                                board_identity_ok = False
                                raise RuntimeError("different_board_reconnected")
                            report["usb_disconnect_reconnect_observed_after_arming"] = True
                            break
                        time.sleep(0.25)
                    else:
                        raise RuntimeError("physical_power_cycle_not_observed")
                    time.sleep(2)
                    before_resume = command(read_source(fixture))
                    report["state_after_reconnection"] = before_resume
                    # Equivalent to the stock carousel's launch handoff; this
                    # is not a claim that a physical touchscreen tap was tested.
                    command(launch_source(fixture, "resume"), follow=False)
                    report["physical_touch_launch_tested"] = False
                time.sleep(4)
                state = command(read_source(fixture))
                expected_active = "b" if mode == "good" else "a"
                report["cases"][mode] = {"state": state,
                    "passed": state.get("active") == expected_active and state.get("confirmed") is True
                    and state.get("active_a_unchanged") is True and state.get("pending") is False}
                if power:
                    report["cases"][mode]["passed"] = (report["cases"][mode]["passed"]
                        and power_interrupted_staging(before_resume, report.get("disconnect_after_arming_s")))
                    report["staging_interruption_observed"] = power_interrupted_staging(
                        before_resume, report.get("disconnect_after_arming_s"))
        except Exception as exc:
            report["host_error_type"] = type(exc).__name__
        finally:
            if prepared and board_identity_ok:
                try:
                    command(cleanup_source(fixture) + "\nprint('PRESTO_GATE:' + json.dumps({'cleaned': True}))\n")
                    report["fixture_cleanup_requested"] = True
                    report["fixture_cleanup_completed"] = True
                except Exception as exc:
                    report["cleanup_error_type"] = type(exc).__name__
            if board_identity_ok:
                try:
                    restarted = subprocess.run([sys.executable, "-m", "mpremote", "connect", port,
                        "soft-reset", "exec", "--no-follow", LAUNCH], capture_output=True, timeout=30)
                    report["app_relaunch_requested"] = restarted.returncode == 0
                except Exception as exc:
                    report["relaunch_error_type"] = type(exc).__name__
    required = {"power"} if power else {"good", "malformed", "reset", "watchdog"}
    report["all_fixture_cases_pass"] = (set(report["cases"]) == required
        and all(v["passed"] for v in report["cases"].values()) and "host_error_type" not in report
        and report.get("fixture_cleanup_completed") is True and report["app_relaunch_requested"])
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("probe", "tls", "boot", "power"))
    parser.add_argument("--port", required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--host", help="explicit workstation LAN IPv4 for synthetic TLS fixture")
    args = parser.parse_args(argv)
    try:
        out = args.out or ROOT / ".preview" / "hardware-gate" / (args.action + ".json")
        if args.action == "probe":
            report = run_probe(args.port, out)
        elif args.action == "tls":
            report = run_tls(args.port, out, args.host)
        else:
            report = run_boot(args.port, out, power=args.action == "power")
    except Exception as exc:
        # Transport errors may contain device output. Never print their text.
        print("Hardware probe stopped (%s); no raw board output is displayed." % type(exc).__name__, file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    print("Evidence: " + str(out))
    passed = (report["app_relaunch_requested"] and "host_error_type" not in report
              and "probe_error_type" not in report.get("probe", {}))
    if args.action == "tls":
        passed = passed and report.get("probe", {}).get("all_security_vectors_pass") is True
    elif args.action in ("boot", "power"):
        passed = passed and report.get("all_fixture_cases_pass") is True
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
