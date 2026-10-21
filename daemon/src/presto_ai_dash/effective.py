"""The effective daemon configuration, and where each value came from.

The admin page shows this read-only, because these values need a daemon
restart to change and are therefore edited in ``dash.toml`` or in the
environment rather than in the page.

Every value carries its origin:

``environment``
    An environment variable decided it.
``dash.toml``
    The bootstrap file decided it.
``database``
    The admin page stored it in SQLite.
``default``
    Nothing decided it; the built-in default is in force.
"""

import os
import time

from .config import (alert_settings, ingest_enabled, overflow_rows,
                     read_config_file)
from .localzone import zone_label

ENVIRONMENT = "environment"
FILE = "dash.toml"
DATABASE = "database"
DEFAULT = "default"
GENERATED = "generated at start"

# The environment variables that decide a value, per setting.
ENVIRONMENT_VARIABLES = {
    "host": "DASH_HOST",
    "port": "DASH_PORT",
    "log_level": "DASH_LOG_LEVEL",
    "overflow_rows": "DASH_OVERFLOW_ROWS",
    "ingest_enabled": "DASH_INGEST_ENABLED",
    "admin_host": "DASH_ADMIN_HOST",
    "admin_allowed_hosts": "DASH_ADMIN_ALLOWED_HOSTS",
}


def _file_value(file_values, section, key):
    """The value the bootstrap file holds for one key, or None."""
    table = file_values.get(section)
    if not isinstance(table, dict):
        return None
    return table.get(key)


def _timezone_label(now=None, environ=None):
    """The timezone the process runs in, as a short readable label."""
    return zone_label(now, environ)


def effective_settings(config, storage=None, config_path=None, environ=None, now=None):
    """The effective settings, each with the place it came from.

    ``config`` is the loaded configuration, ``storage`` the database (optional)
    and ``config_path`` the bootstrap file. The result is a list of
    ``{"name", "value", "origin"}`` entries, in the order the page shows them.
    """
    names = os.environ if environ is None else environ
    file_values = read_config_file(config_path)
    entries = []

    def add(name, value, origin):
        entries.append({"name": name, "value": value, "origin": origin})

    def origin_for(key, section, file_key=None, stored=None):
        """Where a value came from: environment, file, database or default."""
        variable = ENVIRONMENT_VARIABLES.get(key)
        if variable and variable in names:
            return ENVIRONMENT
        if stored is not None:
            return DATABASE
        if _file_value(file_values, section, file_key or key) is not None:
            return FILE
        return DEFAULT

    daemon = config.get("daemon", {})
    admin = config.get("admin", {})
    logging_section = config.get("logging", {})

    add("Poll interval", daemon.get("poll_interval"),
        origin_for("poll_interval", "daemon"))
    add("Retention", daemon.get("retention_hours"),
        origin_for("retention_hours", "daemon"))
    add("Cleanup interval", daemon.get("cleanup_interval"),
        origin_for("cleanup_interval", "daemon"))
    add("Daemon host", daemon.get("host"), origin_for("host", "daemon"))
    add("Daemon port", daemon.get("port"), origin_for("port", "daemon"))
    add("Admin port", admin.get("port"),
        FILE if _file_value(file_values, "admin", "port") is not None else DEFAULT)
    add("Admin bind address", admin.get("host"),
        origin_for("admin_host", "admin", "host"))
    allowed_hosts = admin.get("allowed_hosts") or []
    add("Admin allowed hosts", ", ".join(allowed_hosts) if allowed_hosts else "none",
        origin_for("admin_allowed_hosts", "admin", "allowed_hosts"))
    add("Log level", logging_section.get("level"), origin_for("log_level", "logging"))
    # Never the token itself: only whether it lasts across restarts.
    if admin.get("token_generated"):
        add("Admin token", "temporary: new at every start; set admin.token in dash.toml "
            "to keep one", GENERATED)
    elif "DASH_ADMIN_TOKEN" in names:
        add("Admin token", "set", ENVIRONMENT)
    elif admin.get("enabled") or admin.get("token"):
        add("Admin token", "set", FILE)
    add("Timezone", _timezone_label(now, names), ENVIRONMENT)

    stored_overflow = storage.overflow_rows_setting() if storage is not None else None
    add("Home overflow strip lines",
        overflow_rows(config, stored_overflow, names),
        origin_for("overflow_rows", "display", stored=stored_overflow))

    stored_ingest = storage.ingest_enabled_setting() if storage is not None else None
    add("Remote reports (ingest)",
        "on" if ingest_enabled(config, stored_ingest, names) else "off",
        origin_for("ingest_enabled", "daemon", stored=stored_ingest))

    # The alert settings have no environment variable: they are the user's
    # own choices, taken from the admin page or from dash.toml.
    stored_alerts = storage.alerts_settings() if storage is not None else {}
    settings = alert_settings(config, stored_alerts)

    def alert_origin(key):
        if key in stored_alerts:
            return DATABASE
        if _file_value(file_values, "alerts", key) is not None:
            return FILE
        return DEFAULT

    add("Alerts master", "on" if settings["enabled"] else "off",
        alert_origin("enabled"))
    add("Alert warning percent", settings["warning_percent"],
        alert_origin("warning_percent"))
    add("Alert critical percent", settings["critical_percent"],
        alert_origin("critical_percent"))
    add("Alert sound", "on" if settings["sound"] else "off", alert_origin("sound"))
    add("Mute length (hours)",
        "%d (until pressed again)" % settings["mute_hours"]
        if settings["mute_hours"] == 0 else settings["mute_hours"],
        alert_origin("mute_hours"))
    add("Quiet hours",
        ("on %s-%s" % (settings["quiet_start"], settings["quiet_end"])
         if settings["quiet_hours"] else "off"),
        alert_origin("quiet_hours"))
    add("Pop to top on alarm", "on" if settings["pop_to_top"] else "off",
        alert_origin("pop_to_top"))
    return entries


def uptime_seconds(started_at, now=None):
    """How long the daemon has been running, in seconds."""
    if started_at is None:
        return 0
    moment = time.time() if now is None else float(now)
    return max(0, int(moment - float(started_at)))


def uptime_label(seconds):
    """A readable uptime: 3 d 4 h, 2 h 5 min, 40 s."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%d s" % seconds
    minutes = seconds // 60
    if minutes < 60:
        return "%d min" % minutes
    hours = minutes // 60
    if hours < 48:
        return "%d h %d min" % (hours, minutes % 60)
    return "%d d %d h" % (hours // 24, hours % 24)
