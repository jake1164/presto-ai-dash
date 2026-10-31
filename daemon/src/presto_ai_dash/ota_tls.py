"""The daemon's own TLS identity for Presto app downloads.

A Presto installs app code only from a TLS connection it can verify against a
certificate authority it was given over USB (see ``presto/updater.py``). This
module is that authority: the daemon makes it once, keeps it in one private
file beside the database, and signs a fresh server certificate from it every
time it starts.

The certificate names a fixed host, ``HOSTNAME``, not the daemon computer's
address. The device connects to whatever address its settings give and checks
the certificate against this name, so a new IP address, a DNS name, Docker or
NAT never break the update path. The name is under ``.invalid``, which never
resolves, so it cannot be confused with a real host.

The authority's private key never leaves the file: the setup handoff carries
only the public certificate.
"""

import logging
import os
import ssl
import tempfile
from datetime import datetime, timedelta, timezone

from . import private_files

log = logging.getLogger(__name__)

HOSTNAME = "presto-ai-dash-ota.invalid"
# RSA-2048 with SHA-256 is what the Presto's TLS stack was measured with.
KEY_BITS = 2048
AUTHORITY_YEARS = 20
# The server certificate is signed again at every start, so it never needs
# renewing; this only has to outlast the longest run.
SERVER_YEARS = 5
# A device clock a little behind the daemon's must still accept a new server
# certificate.
CLOCK_SLACK = timedelta(days=7)
FILE_SUFFIX = ".ota.pem"


def identity_path(database):
    """Where the authority is kept for a database path."""
    return str(database) + FILE_SUFFIX


class OtaTls:
    """The authority, its public certificate, and a server context."""

    def __init__(self, ca_key, ca_cert, now=None):
        from cryptography.hazmat.primitives import serialization
        self.hostname = HOSTNAME
        self.ca_der = ca_cert.public_bytes(serialization.Encoding.DER)
        self.context = _server_context(ca_key, ca_cert, now or _now())

    @classmethod
    def load_or_create(cls, path, now=None):
        """The authority from ``path``, made first if the file does not exist."""
        moment = now or _now()
        if os.path.exists(path):
            ca_key, ca_cert = _read(path)
        else:
            ca_key, ca_cert = _authority(moment)
            _write(path, ca_key, ca_cert)
            log.info("made the Presto update certificate authority (%s)",
                     os.path.basename(path))
        return cls(ca_key, ca_cert, moment)


def _now():
    return datetime.now(timezone.utc)


def _authority(now):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_BITS)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                            "Presto AI Dash update authority")])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - CLOCK_SLACK)
            .not_valid_after(now + timedelta(days=365 * AUTHORITY_YEARS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                           critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=True,
                                         crl_sign=True, encipher_only=False,
                                         decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def _server(ca_key, ca_cert, now):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_BITS)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOSTNAME)]))
            .issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - CLOCK_SLACK)
            .not_valid_after(min(now + timedelta(days=365 * SERVER_YEARS),
                                 ca_cert.not_valid_after_utc))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                           critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca_key.public_key()), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                         key_encipherment=True, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False,
                                         decipher_only=False), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOSTNAME)]),
                           critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
    return key, cert


def _server_context(ca_key, ca_cert, now):
    """A TLS 1.2+ server context holding a newly signed server certificate."""
    from cryptography.hazmat.primitives import serialization
    key, cert = _server(ca_key, ca_cert, now)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # ``load_cert_chain`` reads only files. The key exists on disk for the
    # moment it takes to load, in a folder only this account can open.
    with tempfile.TemporaryDirectory(prefix="presto-ota-") as folder:
        chain = os.path.join(folder, "server.pem")
        private_files.create_private(chain)
        with open(chain, "wb") as handle:
            handle.write(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
            handle.write(cert.public_bytes(serialization.Encoding.PEM))
        context.load_cert_chain(chain)
    return context


def _write(path, key, cert):
    from cryptography.hazmat.primitives import serialization
    private_files.create_private(path)
    with open(path, "wb") as handle:
        handle.write(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
        handle.write(cert.public_bytes(serialization.Encoding.PEM))


def _read(path):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    with open(path, "rb") as handle:
        data = handle.read(65536)
    private_files.protect(path)
    key = serialization.load_pem_private_key(data, password=None)
    cert = x509.load_pem_x509_certificate(data)
    return key, cert
