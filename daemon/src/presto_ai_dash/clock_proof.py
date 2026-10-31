"""The daemon's half of the Presto's authenticated clock.

A Presto with no RTC battery restarts its calendar at 2021, which disables
over-the-air updates until it knows the real date. A device with that problem
sends a fresh random nonce in ``X-Presto-Time-Nonce``; the status reply then
carries ``"clock": {"t": <unix seconds>, "mac": <hex>}`` where ``mac`` is
HMAC-SHA256(shared token, ``presto-time-v1|<nonce>|<t>``).

The nonce makes the proof single-use: a reply captured earlier, or forged by
someone on the LAN without the token, does not verify on the device, so the
clock cannot be moved back to revive an expired certificate. Nothing is
stored, and a request with no valid nonce gets nothing.

The device's side is ``presto/clockproof.py``; the tests pin the two together.
"""

import hashlib
import hmac
import re
import time

NONCE_HEADER = "X-Presto-Time-Nonce"
PAYLOAD_KEY = "clock"
PREFIX = b"presto-time-v1|"
_NONCE = re.compile(r"[0-9a-f]{32}")


def valid_nonce(value):
    return isinstance(value, str) and _NONCE.fullmatch(value) is not None


def proof(token, nonce, now=None):
    """The ``clock`` object for one valid nonce."""
    epoch = int(time.time() if now is None else now)
    mac = hmac.new(token.encode("utf-8"),
                   PREFIX + nonce.encode("ascii") + b"|" + str(epoch).encode("ascii"),
                   hashlib.sha256).hexdigest()
    return {"t": epoch, "mac": mac}


def attach(payload, headers, token, limit, now=None):
    """A request-local copy of ``payload`` with the proof, or ``payload`` itself.

    ``headers`` is the request's header collection. Exactly one valid nonce is
    answered; anything else, and a payload with no room left under ``limit``
    bytes, is left alone rather than risking the quota data.
    """
    try:
        values = headers.get_all(NONCE_HEADER) or []
    except AttributeError:
        return payload
    if len(values) != 1 or not valid_nonce(values[0].strip()):
        return payload
    merged = dict(payload, **{PAYLOAD_KEY: proof(token, values[0].strip(), now)})
    try:
        import json
        if len(json.dumps(merged).encode("utf-8")) > limit:
            return payload
    except (TypeError, ValueError):
        return payload
    return merged
