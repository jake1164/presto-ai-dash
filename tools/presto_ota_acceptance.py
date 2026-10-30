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
from presto_ai_dash.device_install import run_remote, BOOT_LAUNCH, device_lock
from presto_ai_dash.device_lifecycle import DeviceLifecycle
from presto_ai_dash.storage import Storage
from presto_ai_dash import demo
from presto_bundle import snapshot, revision, RECEIPT
from hardware_gate.tls_fixture import _authority, _leaf, HOSTNAME


def public_result(port, source):
    process = subprocess.run([sys.executable, '-m', 'mpremote', 'connect', port,
                              'soft-reset', 'exec', source], capture_output=True, text=True, timeout=30)
    if process.returncode:
        raise RuntimeError('Public acceptance probe failed; no raw board output displayed')
    output = process.stdout
    values = [line[len('OTA_RESULT:'):] for line in output.splitlines() if line.startswith('OTA_RESULT:')]
    if len(values) != 1:
        raise RuntimeError('Device did not return a unique public result')
    return json.loads(values[0])


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


def run(port, host, out, scenario, timeout):
    from cryptography.hazmat.primitives import serialization
    out.mkdir(parents=True, exist_ok=True)
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'port': port,
              'scenario': scenario, 'events': [], 'private_files_opened': False}
    bundle = candidate(scenario)
    report['target'] = bundle['slot']['revision']
    report['download_bytes'] = sum(len(data) for name, data in bundle['slot']['files'].items()
                                   if name not in ('revision.txt', 'revision.sha256'))
    token = secrets.token_hex(24)
    storage = Storage(out / 'acceptance.db')
    class ObservedLifecycle(DeviceLifecycle):
        last = None
        last_logged = 0
        def observe(self, headers):
            value = super().observe(headers)
            try:
                report = json.loads(headers.get('X-Presto-Lifecycle', '{}'))
                current = (report.get('update', {}).get('phase'), value.get('report_status'))
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
import os, json, machine
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
print('OTA_RESULT:' + json.dumps({'provisioned': True, 'firmware': os.uname().version}))
''' % (json.dumps(profile), ca_der, rtc)
            with device_lock(RECEIPT.with_suffix('.lock')):
                report.update(public_result(port, source))
                provisioned = True
                run_remote(port, ['soft-reset', 'exec', '--no-follow', BOOT_LAUNCH])
            def serial_trace():
                import serial
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
                                if len(buffered) > 4096:
                                    buffered = b''
                    except (OSError, serial.SerialException):
                        serial_stop.wait(1)
            serial_thread = threading.Thread(target=serial_trace, daemon=True)
            serial_thread.start()
            end, previous = time.monotonic() + timeout, None
            while time.monotonic() < end:
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
            if not rows:
                report['failure'] = 'no_device_reports'
            report['attempts'] = storage.device_attempts(rows[0]['device_id']) if rows else []
    finally:
        serial_stop.set()
        if serial_thread is not None:
            serial_thread.join(2)
        status.stop()
        download.stop()
        storage.close()
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
    args = parser.parse_args()
    if args.port in ('auto', 'list'):
        parser.error('an explicit device port is required')
    result = run(args.port, args.host, args.out.resolve(), args.scenario, args.timeout)
    print('OTA acceptance: ' + ('PASS' if result.get('passed') else 'FAIL'), flush=True)
    return 0 if result.get('passed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
