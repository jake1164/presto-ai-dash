"""Self-contained collectors that run on machines other than the daemon host."""

import hashlib
from importlib.resources import files

# The one file users copy to a remote machine. The admin page serves these
# bytes, and shows the hash of the same bytes beside the link, so a copy can be
# checked against what the daemon actually serves.
COLLECTOR_FILE = "presto_collector.py"


def collector_bytes():
    """The collector exactly as it is served."""
    return files("presto_ai_dash").joinpath("collectors", COLLECTOR_FILE).read_bytes()


def served_version():
    """The version of the collector this daemon serves: the newest there is."""
    from . import presto_collector
    return presto_collector.COLLECTOR_VERSION


def collector_config_text(service, name, token_file, url="http://REPLACE-WITH-DASH-ADDRESS:8787/api/v1/ingest"):
    """The text of a ``collector.json`` for one account, ready to save.

    ``watch`` reads it from ``~/.presto-ai-dash/``. The token file is named
    relative to that folder, so the file can be copied between computers.
    """
    import json
    account = {"service": service, "name": name, "token_file": token_file}
    return json.dumps({"url": url, "accounts": [account]}, indent=2) + "\n"


def collector_info():
    """What the admin page shows about the collector it serves."""
    raw = collector_bytes()
    return {"file": COLLECTOR_FILE, "version": served_version(),
            "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}


def update_state(reported):
    """Where a collector's reported version stands against the one served.

    ``current``, ``available`` (the copy is older than the file the daemon
    serves), ``newer`` (the copy is newer than this daemon, for example after
    a daemon downgrade) or ``unknown`` (no version, or one that is not a whole
    number: no claim is made either way).
    """
    if not (isinstance(reported, str) and reported.isascii() and reported.isdigit()
            and len(reported) <= 9):
        return "unknown"
    seen, latest = int(reported), int(served_version())
    if seen < latest:
        return "available"
    return "newer" if seen > latest else "current"
