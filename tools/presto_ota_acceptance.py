"""Controlled full-app OTA on an explicitly selected, already migrated Presto.

Starts an isolated daemon with synthetic data/token and a disposable test CA.
Private settings are never read or replaced. The temporary device profile is
removed on exit, returning polling to the device's normal daemon. No firmware
is flashed and no normal daemon's OTA setting is changed.
"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import ssl
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'daemon/src'))
from presto_ai_dash.api import ApiServer
from presto_ai_dash.device_app import DeviceApp
from presto_ai_dash.device_install import run_remote, BOOT_LAUNCH, device_lock, reset_to_launcher
from presto_ai_dash.device_lifecycle import DeviceLifecycle
from presto_ai_dash.storage import Storage
from presto_ai_dash import demo
from presto_bundle import snapshot, revision, RECEIPT
from hardware_gate.tls_fixture import _authority, _leaf, HOSTNAME


def public_result(port, source):
    # A running app's 8-second watchdog would cut this session short; a hard
    # reset to the stock launcher, which arms none, makes it reliable.
    reset_to_launcher(port)
    process = subprocess.run([sys.executable, '-m', 'mpremote', 'connect', port,
                              'soft-reset', 'exec', source], capture_output=True, text=True, timeout=30)
    if process.returncode:
        raise RuntimeError('Public acceptance probe failed; no raw board output displayed')
    output = process.stdout
    values = [line[len('OTA_RESULT:'):] for line in output.splitlines() if line.startswith('OTA_RESULT:')]
    if len(values) != 1:
        raise RuntimeError('Device did not return a unique public result')
    return json.loads(values[0])


CUT_POINTS = ('download', 'flash', 'confirm')
# How long the daemon withholds its confirmation acknowledgement in the
# 'confirm' cut, so there is a usable window instead of a few-second race.
CONFIRM_HOLD_SECONDS = 30

# A banner the person pulling power cannot miss.
CUT_NOW = '*** CUT POWER NOW ***'


def cut_prompt(cut, seen, line):
    """What to tell the person pulling power for one device serial line, or None.

    ``seen`` is a set the caller keeps between lines. The 'confirm' cut is
    prompted from the lifecycle report instead, not from serial.
    """
    if cut == 'download':
        if line.endswith('update transfer recorded'):
            seen.add('recorded')
        elif 'recorded' in seen and 'cut' not in seen and line.endswith('update request sent'):
            seen.add('cut')
            return CUT_NOW + ' The download is running; you have about a minute.'
    elif cut == 'flash':
        if line.endswith('update transfer recorded'):
            return ('GET READY: in about a minute the screen freezes for about 6 seconds. '
                    'Cut power while it is frozen.')
        if 'update writing slot' in line and 'cut' not in seen:
            seen.add('cut')
            return CUT_NOW + ' The screen is frozen; you have about 5 seconds.'
    return None


def cut_verdict(cut, after_cut, after_recovery, slot_before):
    """Problems found in the journal around a power cut; an empty list passes.

    ``after_cut`` is the public journal as the cut left it (read before the
    bootstrap ran again) and ``after_recovery`` is the same after the real
    bootstrap decided what to run.
    """
    if not isinstance(after_cut, dict) or not isinstance(after_recovery, dict):
        return ['journal could not be read']
    problems = []
    if cut in ('download', 'flash'):
        if not after_cut.get('transfer'):
            problems.append('the cut did not leave an interrupted transfer record')
        expected = ('failed', 'connection_lost')
    else:
        if not after_cut.get('pending'):
            problems.append('the cut did not leave a pending candidate')
        expected = ('rolled_back', 'boot_crash')
    failure = after_recovery.get('failure') or {}
    if (failure.get('phase'), failure.get('error')) != expected:
        problems.append('recovery recorded %s/%s, expected %s/%s'
                        % (failure.get('phase'), failure.get('error'), expected[0], expected[1]))
    for name in ('pending', 'transfer'):
        if after_recovery.get(name):
            problems.append('%s is still set after recovery' % name)
    if after_recovery.get('active') != slot_before or after_recovery.get('confirmed') != slot_before:
        problems.append('the confirmed slot changed')
    return problems


def announce(message):
    """Print an instruction for the person pulling power, and make it audible.

    The window can be a few seconds long and the terminal may not be in front
    of them, so a prompt also beeps: one low beep to get ready, three high
    beeps to cut now. The beep runs on its own thread so it never delays the
    serial reader or the daemon's answer to the device.
    """
    print(message, flush=True)
    urgent = message.startswith(CUT_NOW)

    def beep():
        try:
            import winsound
            for _ in range(3 if urgent else 1):
                winsound.Beep(1500 if urgent else 600, 350)
        except (ImportError, RuntimeError):
            print('', end='', flush=True)

    threading.Thread(target=beep, daemon=True).start()


def wait_for_port(port, seconds, present=True):
    """Wait until the serial port appears (or disappears); True when it does."""
    from serial.tools import list_ports
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if any(item.device == port for item in list_ports.comports()) == present:
            return True
        time.sleep(1)
    return False


JOURNAL_PROBE = '''
import sys, json
sys.path.insert(0, '/ai_dash_boot_files')
import activation
state, origin = activation.load(activation.FileStore())
print('OTA_RESULT:' + json.dumps({'journal': {
    'source': origin, 'active': state['active'], 'confirmed': state['confirmed'],
    'pending': bool(state['pending']), 'transfer': bool(state['transfer']),
    'failure': state['failure'], 'success': bool(state['success'])}}))
'''


def candidate(scenario):
    bundle = snapshot()
    slot = dict(bundle['slot'])
    files = dict(slot['files'])
    # A distinct, full-dashboard test release from this exact source tree.
    # Only a harmless build marker differs; crash/hang are deliberate bad
    # candidate entrypoints used to prove the production bootstrap recovery.
    if scenario == 'crash':
        files['ai_dash_app.py'] = b'raise RuntimeError("controlled candidate crash")\n' + files['ai_dash_app.py']
    elif scenario == 'hang':
        files['ai_dash_app.py'] = b'while True:\n    pass\n' + files['ai_dash_app.py']
    else:
        files['ui.py'] += b'\n# Controlled OTA acceptance build: ' + secrets.token_hex(8).encode() + b'\n'
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()
              if name not in ('revision.txt', 'revision.sha256')}
    target = revision(hashes)
    files['revision.sha256'] = (target + '\n').encode()
    files['revision.txt'] = (target[:12] + '\n').encode()
    slot.update(files=files, hashes=hashes, revision=target)
    return dict(bundle, slot=slot)


def run(port, host, out, scenario, timeout, cut=None):
    from cryptography.hazmat.primitives import serialization
    out.mkdir(parents=True, exist_ok=True)
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'port': port,
              'scenario': scenario, 'events': [], 'private_files_opened': False}
    if cut is not None:
        report['cut'] = cut
        report['cut_performed'] = False
    # Set when a person has been told to pull power, and when the device then
    # disappeared from USB without its own restart.
    prompted = threading.Event()
    gone = threading.Event()
    bundle = candidate(scenario)
    report['target'] = bundle['slot']['revision']
    report['download_bytes'] = sum(len(data) for name, data in bundle['slot']['files'].items()
                                   if name not in ('revision.txt', 'revision.sha256'))
    token = secrets.token_hex(24)
    storage = Storage(out / 'acceptance.db')
    class ObservedLifecycle(DeviceLifecycle):
        last = None
        last_logged = 0
        hold_until = None
        def observe(self, headers):
            value = super().observe(headers)
            try:
                report = json.loads(headers.get('X-Presto-Lifecycle', '{}'))
                current = (report.get('update', {}).get('phase'), value.get('report_status'))
                if cut == 'confirm' and current[0] == 'confirming':
                    # Withhold the acknowledgement so the candidate stays
                    # unconfirmed long enough for a person to cut power.
                    if self.hold_until is None:
                        self.hold_until = time.monotonic() + CONFIRM_HOLD_SECONDS
                        prompted.set()
                        announce(CUT_NOW + ' The candidate is running and unconfirmed; you have about %d seconds.'
                                 % CONFIRM_HOLD_SECONDS)
                    if time.monotonic() < self.hold_until:
                        value = dict(value, confirm_attempt_id=None)
                if current != self.last or time.monotonic() - self.last_logged > 10:
                    print(json.dumps({'device_phase': current[0], 'report_status': current[1],
                                      'permit': bool(value.get('command')), 'seq': report.get('seq')}), flush=True)
                    self.last = current
                    self.last_logged = time.monotonic()
            except (ValueError, AttributeError):
                pass
            return value
    lifecycle = ObservedLifecycle(storage, expected_provider=lambda: bundle, ota_enabled=True)
    def payload(lazy=False):
        value = demo.demo_payload(0)
        value.pop('demo', None)
        return value
    device_app = DeviceApp(provider=lambda: bundle)
    status = ApiServer(host, 0, storage, token, status_provider=payload,
                       device_lifecycle=lifecycle, device_app=device_app)
    download = ApiServer(host, 0, storage, token, status_provider=payload,
                         device_lifecycle=lifecycle, device_app=device_app)
    now = datetime.now(timezone.utc)
    ca_key, ca = _authority('Disposable dashboard OTA acceptance CA', now)
    key, cert = _leaf(ca_key, ca, now - timedelta(days=1), now + timedelta(days=1))
    provisioned = False
    serial_stop = threading.Event()
    serial_thread = None
    try:
        with tempfile.TemporaryDirectory(prefix='ota-tls-', dir=out) as folder:
            key_file = Path(folder) / 'synthetic-key.pem'
            cert_file = Path(folder) / 'synthetic-cert.pem'
            key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert_file, key_file)
            accept = download._server.get_request
            def tls_accept():
                raw, address = accept()
                raw.settimeout(3)
                try:
                    return context.wrap_socket(raw, server_side=True), address
                except BaseException:
                    raw.close()
                    raise
            download._server.get_request = tls_accept
            status.start()
            download.start()
            profile = {'enabled': True, 'host': host, 'port': download.actual_port,
                       'hostname': HOSTNAME, 'ca_file': '/ai_dash_state/ota-ca.der',
                       'acceptance': {'host': host, 'port': status.actual_port, 'token': token,
                                      'until': int(time.time()) + timeout + 300}}
            ca_der = ca.public_bytes(serialization.Encoding.DER)
            rtc = (now.year, now.month, now.day, now.weekday(), now.hour, now.minute, now.second, 0)
            source = '''
import os, sys, json, machine
if 'Presto' not in os.uname().machine:
    raise RuntimeError('wrong board')
if 'ai_dash_boot.py' not in os.listdir('/'):
    raise RuntimeError('migrate the device first')
for path in ('/ai_dash_state/ota.json', '/ai_dash_state/ota-ca.der'):
    try:
        os.stat(path)
    except OSError:
        continue
    raise RuntimeError('existing OTA provisioning must be preserved')
profile = json.loads(%r)
profile['firmware'] = os.uname().version
with open('/ai_dash_state/ota-ca.der', 'wb') as handle:
    handle.write(%r)
with open('/ai_dash_state/ota.json', 'w') as handle:
    handle.write(json.dumps(profile))
machine.RTC().datetime(%r)
if hasattr(os, 'sync'):
    os.sync()
sys.path.insert(0, '/ai_dash_boot_files')
import activation
slot_before = activation.load(activation.FileStore())[0]['confirmed']
print('OTA_RESULT:' + json.dumps({'provisioned': True, 'firmware': os.uname().version,
                                  'slot_before': slot_before}))
''' % (json.dumps(profile), ca_der, rtc)
            with device_lock(RECEIPT.with_suffix('.lock')):
                report.update(public_result(port, source))
                provisioned = True
                run_remote(port, ['soft-reset', 'exec', '--no-follow', BOOT_LAUNCH])
            def serial_trace():
                import serial
                seen = set()
                staged = False
                # The candidate's restart drops USB; reopen so its own lines
                # are seen. Opening the port does not interrupt the app.
                while not serial_stop.is_set():
                    try:
                        with serial.Serial(port, 115200, timeout=0.1) as connection:
                            buffered = b''
                            while not serial_stop.is_set():
                                buffered += connection.read(256)
                                while b'\n' in buffered:
                                    line, buffered = buffered.split(b'\n', 1)
                                    text = line.decode('ascii', 'replace').strip()
                                    if text.startswith(('AI Dash: update', 'AI Dash: slot',
                                                        'AI Dash: clock', 'AI Dash: display')):
                                        print(json.dumps({'serial': text[:140]}), flush=True)
                                        if text.endswith(('update staged in slot a', 'update staged in slot b')):
                                            staged = True
                                        message = cut_prompt(cut, seen, text)
                                        if message:
                                            announce(message)
                                            if message.startswith(CUT_NOW):
                                                prompted.set()
                                if len(buffered) > 4096:
                                    buffered = b''
                    except (OSError, serial.SerialException):
                        # The device's own restart after staging also drops
                        # USB, so in the download and flash cuts only a
                        # disappearance before that is a cut. The confirm cut
                        # is prompted after that restart, so any later loss is.
                        if cut is not None and prompted.is_set() and (cut == 'confirm' or not staged):
                            gone.set()
                        serial_stop.wait(1)
            serial_thread = threading.Thread(target=serial_trace, daemon=True)
            serial_thread.start()
            end, previous = time.monotonic() + timeout, None
            while time.monotonic() < end:
                if cut is not None and gone.is_set():
                    report['cut_performed'] = True
                    print('Power loss detected.', flush=True)
                    break
                rows = storage.devices()
                if rows:
                    row = rows[0]
                    state = (row['update_state'], row.get('reason'), row.get('revision'),
                             row.get('boot_confirmed'), row.get('last_phase'))
                    if state != previous:
                        event = {'elapsed_s': round(timeout - (end - time.monotonic()), 2),
                                 'state': state[0], 'reason': state[1], 'revision': state[2],
                                 'boot_confirmed': state[3], 'phase': state[4]}
                        report['events'].append(event)
                        print(json.dumps(event), flush=True)
                        previous = state
                    if scenario == 'success' and row['revision'] == report['target'] and row['update_state'] == 'current':
                        report['passed'] = True
                        break
                    if scenario in ('crash', 'hang') and row['update_state'] == 'failed' and row['revision'] != report['target']:
                        report['passed'] = True
                        break
                    if row['update_state'] in ('manual_action', 'unconfirmed'):
                        report['passed'] = False
                        break
                time.sleep(1)
            report.setdefault('passed', False)
            if cut is not None and not report['cut_performed']:
                report['passed'] = False
                report['failure'] = 'cut_not_performed'
            if not rows:
                report['failure'] = 'no_device_reports'
            rows = storage.devices()
            report['attempts'] = storage.device_attempts(rows[0]['device_id']) if rows else []
    finally:
        serial_stop.set()
        if serial_thread is not None:
            serial_thread.join(2)
        status.stop()
        download.stop()
        storage.close()
        if provisioned and report.get('cut_performed'):
            print('RECONNECT: plug the Presto back in now.', flush=True)
            if not wait_for_port(port, 900):
                report['failure'] = 'device_not_reconnected'
            time.sleep(5)
        if provisioned:
            with device_lock(RECEIPT.with_suffix('.lock')):
                source = '''
import os, sys, json, time
result = {'clock_usable': time.time() >= 1735689600}
try:
    sys.path.insert(0, '/ai_dash_boot_files')
    import activation
    state, origin = activation.load(activation.FileStore())
    result['journal'] = {'source': origin, 'active': state['active'],
                         'confirmed': state['confirmed'], 'pending': bool(state['pending']),
                         'transfer': bool(state['transfer']), 'failure': state['failure'],
                         'success': bool(state['success'])}
except Exception as exc:
    result['journal_error'] = type(exc).__name__
try:
    with open('/ai_dash_state/ota-metrics.json', 'r') as handle:
        result['metrics'] = json.loads(handle.read(4096))
except OSError:
    pass
for path in ('/ai_dash_state/ota.json', '/ai_dash_state/ota-ca.der'):
    try:
        os.remove(path)
    except OSError:
        pass
print('OTA_RESULT:' + json.dumps(result))
'''
                try:
                    report.update(public_result(port, source))
                    report['temporary_profile_removed'] = True
                except Exception as exc:
                    report['cleanup_error_type'] = type(exc).__name__
                    report['passed'] = False
                finally:
                    try:
                        run_remote(port, ['soft-reset', 'exec', '--no-follow', BOOT_LAUNCH])
                    except Exception as exc:
                        report['relaunch_error_type'] = type(exc).__name__
                        report['passed'] = False
                if report.get('cut_performed'):
                    # The journal above is as the cut left it; the relaunch just
                    # ran the real bootstrap's decision. Read it again.
                    report['journal_after_cut'] = report.pop('journal', None)
                    try:
                        time.sleep(30)
                        report['journal_after_recovery'] = public_result(port, JOURNAL_PROBE)['journal']
                        report['cut_problems'] = cut_verdict(cut, report['journal_after_cut'],
                                                             report['journal_after_recovery'],
                                                             report.get('slot_before'))
                        report['passed'] = (not report['cut_problems'] and 'cleanup_error_type' not in report
                                            and 'relaunch_error_type' not in report)
                    except Exception as exc:
                        report['cut_error_type'] = type(exc).__name__
                        report['passed'] = False
                    finally:
                        try:
                            run_remote(port, ['soft-reset', 'exec', '--no-follow', BOOT_LAUNCH])
                        except Exception as exc:
                            report['relaunch_error_type'] = type(exc).__name__
                            report['passed'] = False
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        (out / 'result.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--host', required=True, help='explicit LAN address reachable by the device')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--scenario', choices=('success', 'crash', 'hang'), default='success')
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--cut', choices=CUT_POINTS,
                        help='arm a power-cut window: the tool says when to pull power, then '
                             'waits for the device to be plugged back in and checks recovery')
    args = parser.parse_args()
    if args.port in ('auto', 'list'):
        parser.error('an explicit device port is required')
    if args.cut and args.scenario != 'success':
        parser.error('--cut needs the success scenario')
    result = run(args.port, args.host, args.out.resolve(), args.scenario, args.timeout, args.cut)
    print('OTA acceptance: ' + ('PASS' if result.get('passed') else 'FAIL'), flush=True)
    return 0 if result.get('passed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
