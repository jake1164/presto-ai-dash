"""An authenticated wall-clock time from the daemon, for a board with no RTC battery.

Every hard reset restarts the Presto's clock at 2021, and the updater refuses
to fetch code until the date is real, because the date is what rejects an
expired certificate. The daemon's own clock is the one this app already
trusts, so it supplies the time, but plain HTTP replies can be forged on the
LAN, and a forged *past* time would revive an expired certificate.

So the time is challenge-response, using the shared token the device already
holds. The device sends a fresh random nonce with a status request; the
daemon answers ``{"t": epoch, "mac": hex}`` where ``mac`` is
HMAC-SHA256(token, "presto-time-v1|" + nonce + "|" + t). A reply made for
another nonce, an old reply, or one made without the token does not verify.

Pure Python with no hardware import, so CPython tests run it unchanged.
MicroPython has no ``hmac`` module here, so HMAC is built from ``hashlib``.
"""

import binascii
import hashlib
import os

NONCE_HEADER = "X-Presto-Time-Nonce"
# What the daemon puts in the status reply under this key.
PAYLOAD_KEY = "clock"
PREFIX = b"presto-time-v1|"
NONCE_BYTES = 16
# No later than 2100-01-01: far beyond any real reply, well inside what the
# board's RTC and its calendar can hold.
CEILING = 4102444800

_BLOCK = 64


def hmac_sha256(key, message):
    """HMAC-SHA256 from ``hashlib`` alone (RFC 2104)."""
    if len(key) > _BLOCK:
        key = hashlib.sha256(key).digest()
    key = key + bytes(_BLOCK - len(key))
    inner = hashlib.sha256(bytes(byte ^ 0x36 for byte in key))
    inner.update(message)
    outer = hashlib.sha256(bytes(byte ^ 0x5C for byte in key))
    outer.update(inner.digest())
    return outer.digest()


def new_nonce():
    """A fresh 32-character lowercase hex nonce."""
    return binascii.hexlify(os.urandom(NONCE_BYTES)).decode()


def message(nonce, epoch):
    return PREFIX + nonce.encode() + b"|" + str(epoch).encode()


def _same(a, b):
    """Constant-time equality for two equal-purpose strings."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0


def verify(token, nonce, proof, floor):
    """The proven epoch, or None when the reply must not set the clock.

    ``floor`` is the earliest time the app treats as real. Nothing here
    echoes the input; a malformed or wrong reply is simply None.
    """
    if not isinstance(token, str) or not token or not isinstance(nonce, str) or not nonce:
        return None
    if not isinstance(proof, dict):
        return None
    epoch, mac = proof.get("t"), proof.get("mac")
    if type(epoch) is not int or not isinstance(mac, str) or len(mac) != 64:
        return None
    if not floor <= epoch < CEILING:
        return None
    expected = binascii.hexlify(hmac_sha256(token.encode(), message(nonce, epoch))).decode()
    if not _same(expected, mac.lower()):
        return None
    return epoch
