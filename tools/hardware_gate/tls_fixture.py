"""Ephemeral local CA/HTTPS fixtures containing only generated test bytes."""

from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import ssl
import tempfile
import threading

PAYLOAD = bytes(range(256)) * 256
HOSTNAME = "presto-gate.invalid"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://wrong-gate.invalid/")
            self.end_headers()
            return
        body = b"OK" if self.path == "/probe" else PAYLOAD
        length = len(body)
        if self.path == "/corrupt":
            body = b"x" + body[1:]
        elif self.path == "/truncated":
            body = body[:4096]
        elif self.path == "/oversized":
            length = 1048576
            body = b""
        elif self.path not in ("/probe", "/payload"):
            self.send_error(404)
            return
        try:
            self.send_response(200)
            self.send_header("Content-Length", str(length))
            self.end_headers()
            self.wfile.write(body)
        except (OSError, ssl.SSLError):
            pass


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, *_):
        pass


def _authority(name, now):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=3650)).not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False, key_agreement=False,
                key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def _leaf(ca_key, ca_cert, before, after):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOSTNAME)]))
            .issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(before).not_valid_after(after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                key_encipherment=True, data_encipherment=False, key_agreement=False,
                key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOSTNAME)]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return key, cert


@contextmanager
def fixtures(host, directory):
    from cryptography.hazmat.primitives import serialization
    now = datetime.now(timezone.utc)
    ca_key, ca_cert = _authority("Disposable Presto gate CA", now)
    _, other = _authority("Different disposable CA", now)
    settings = {"host": host, "ca_der_hex": ca_cert.public_bytes(serialization.Encoding.DER).hex(),
                "other_ca_der_hex": other.public_bytes(serialization.Encoding.DER).hex(),
                "payload_sha256": hashlib.sha256(PAYLOAD).hexdigest(),
                "rtc": [now.year, now.month, now.day, now.weekday(), now.hour, now.minute, now.second, 0]}
    with tempfile.TemporaryDirectory(prefix="ephemeral-tls-", dir=directory) as temporary, ExitStack() as stack:
        for kind, before, after in (
            ("valid", now - timedelta(days=1), now + timedelta(days=1)),
            ("expired", now - timedelta(days=10), now - timedelta(days=1)),
            ("future", now + timedelta(days=1), now + timedelta(days=10)),
        ):
            key, cert = _leaf(ca_key, ca_cert, before, after)
            folder = Path(temporary)
            key_path = folder / (kind + "-synthetic-private.pem")
            cert_path = folder / (kind + "-synthetic-cert.pem")
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(cert_path, key_path)
            server = Server((host, 0), Handler)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
            thread.start()
            stack.callback(server.server_close)
            stack.callback(server.shutdown)
            settings[kind + "_port"] = server.server_port
        yield settings
