"""Load and validate daemon TOML config with environment overrides."""

from copy import deepcopy
import logging
import os
import re
import secrets
import tomllib
from urllib.parse import urlsplit

from .alert_policy import (DEFAULT_CRITICAL_PERCENT, DEFAULT_QUIET_END,
                           DEFAULT_QUIET_START, DEFAULT_WARNING_PERCENT,
                           is_quiet, parse_clock, validate_thresholds)

log = logging.getLogger(__name__)
DEFAULTS = {
    "daemon": {"host": "0.0.0.0", "port": 8787, "shared_token": "",
               "poll_interval": 15, "database": "dash.db",
               "retention_hours": 72, "cleanup_interval": 300,
               "ingest_enabled": False},
    "logging": {"level": "INFO"},
    "admin": {"enabled": False, "port": 8788, "token": "", "key_file": ""},
    "display": {"overflow_rows": 1},
    "alerts": {"warning_percent": 80, "critical_percent": 95,
               "sound": True, "quiet_hours": False,
               "quiet_start": "22:00", "quiet_end": "07:00",
               "pop_to_top": True},
    "sources": {
        "fake": {"enabled": True, "scenario_interval": 20},
        "ollama": {"enabled": False, "base_url": "http://localhost:11434",
                   "timeout": 5, "samples_file": ""},
        "litellm": {"enabled": False, "base_url": "http://localhost:4000",
                    "timeout": 5, "api_key": "", "api_key_env": "LITELLM_API_KEY"},
    },
}
ENV_OVERRIDES = {
    "DASH_SHARED_TOKEN": ("daemon", "shared_token"),
    "DASH_HOST": ("daemon", "host"),
    "DASH_PORT": ("daemon", "port"),
    "DASH_DATABASE": ("daemon", "database"),
    "DASH_LOG_LEVEL": ("logging", "level"),
    "DASH_ADMIN_TOKEN": ("admin", "token"),
    "DASH_KEY_FILE": ("admin", "key_file"),
}


class ConfigError(ValueError):
    """Invalid or unreadable daemon configuration."""


INGEST_ENV = "DASH_INGEST_ENABLED"
TRUTHY_FLAGS = frozenset(("1", "true", "yes", "on"))
FALSY_FLAGS = frozenset(("0", "false", "no", "off"))

OVERFLOW_ENV = "DASH_OVERFLOW_ROWS"
OVERFLOW_MIN = 0
OVERFLOW_MAX = 3
DEFAULT_OVERFLOW_ROWS = 1
_warned_overflow = False


def parse_flag(value, name):
    """Parse a boolean environment flag, rejecting anything ambiguous."""
    lowered = value.strip().lower() if isinstance(value, str) else ""
    if lowered in TRUTHY_FLAGS:
        return True
    if lowered in FALSY_FLAGS:
        return False
    raise ConfigError("%s must be a boolean flag (1/true/yes/on or 0/false/no/off)" % name)


def parse_overflow_rows(value):
    """The strip-line maximum from a string, or None when it is unusable."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number < OVERFLOW_MIN or number > OVERFLOW_MAX:
        return None
    return number


def overflow_rows(config, stored=None, environ=None):
    """The effective strip-line maximum, resolved afresh on every payload.

    The environment beats the admin's stored setting, which beats TOML; one
    line by default. Reading the environment here - not once at startup - is
    what lets an admin change apply without a daemon restart. A garbled
    variable fails closed to the default and is logged once.
    """
    global _warned_overflow
    names = os.environ if environ is None else environ
    if OVERFLOW_ENV in names:
        value = parse_overflow_rows(names[OVERFLOW_ENV])
        if value is None:
            if not _warned_overflow:
                _warned_overflow = True
                log.error("%s is set to an unusable value; the strip keeps one line",
                          OVERFLOW_ENV)
            return DEFAULT_OVERFLOW_ROWS
        return value
    if stored is not None:
        return stored
    section = config.get("display") if isinstance(config, dict) else None
    if isinstance(section, dict):
        value = section.get("overflow_rows", DEFAULT_OVERFLOW_ROWS)
        if type(value) is int and OVERFLOW_MIN <= value <= OVERFLOW_MAX:
            return value
    return DEFAULT_OVERFLOW_ROWS


def ingest_enabled(config, stored=None, environ=None):
    """The effective ingest switch, resolved afresh on every request.

    The environment beats the admin's stored setting, which beats TOML; off by
    default. Reading the environment here — not once at startup — is what lets
    an admin toggle apply without a daemon restart. A garbled variable fails
    closed instead of taking the route down.
    """
    names = os.environ if environ is None else environ
    if INGEST_ENV in names:
        try:
            return parse_flag(names[INGEST_ENV], INGEST_ENV)
        except ConfigError:
            log.error("%s is set to a non-boolean value; ingest stays disabled", INGEST_ENV)
            return False
    if stored is not None:
        return stored
    return config["daemon"].get("ingest_enabled", False)


# The alert settings the admin page can change, and the file can set.
ALERT_KEYS = ("warning_percent", "critical_percent", "sound", "quiet_hours",
              "quiet_start", "quiet_end", "pop_to_top")


def alert_base_settings(config):
    """The [alerts] values from the file or the defaults, without the admin's.

    This is what the stored value is compared against: a stored value that
    equals the base is not stored at all, so the effective configuration
    still says where the value came from.
    """
    section = config.get("alerts") if isinstance(config, dict) else None
    section = section if isinstance(section, dict) else {}
    values = {}
    for key in ALERT_KEYS:
        value = section.get(key, DEFAULTS["alerts"][key])
        values[key] = value
    return values


def alert_settings(config, stored=None, now=None):
    """The effective alert settings, resolved afresh on every payload.

    The admin's stored value beats the file, which beats the built-in
    default. There is no environment variable for these: they are the
    user's own choices, and the file plus the admin page are enough.

    ``now`` is the moment the quiet flag is judged against; it defaults to
    the daemon's own local time, so the device needs no clock of its own.
    """
    section = alert_base_settings(config)
    stored = stored if isinstance(stored, dict) else {}
    values = {}
    for key in ALERT_KEYS:
        values[key] = stored[key] if key in stored else section[key]
    try:
        validate_thresholds(values["warning_percent"], values["critical_percent"])
    except ValueError:
        values["warning_percent"] = DEFAULT_WARNING_PERCENT
        values["critical_percent"] = DEFAULT_CRITICAL_PERCENT
    for key in ("sound", "quiet_hours", "pop_to_top"):
        if type(values[key]) is not bool:
            values[key] = DEFAULTS["alerts"][key]
    for key in ("quiet_start", "quiet_end"):
        if parse_clock(values[key]) is None:
            values[key] = DEFAULTS["alerts"][key]
    values["quiet"] = bool(values["quiet_hours"]) and is_quiet(
        now, values["quiet_start"], values["quiet_end"])
    return values


def read_config_file(path=None):
    """The raw TOML tables of the bootstrap file, or an empty dict.

    The admin page shows where each effective value came from, so it needs to
    know what the file said, not only what the merged configuration holds. An
    unreadable or invalid file yields an empty dict: the effective values come
    from the environment, the database or the built-in defaults.
    """
    if not path:
        return {}
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _merge(base, override, prefix=""):
    for key, value in override.items():
        if key in base and isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise ConfigError(prefix + key + " must be a table")
            _merge(base[key], value, prefix + key + ".")
        else:
            base[key] = value


def load_config(path=None):
    config = deepcopy(DEFAULTS)
    if path:
        try:
            with open(path, "rb") as handle:
                _merge(config, tomllib.load(handle))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError("cannot load config %s: %s" % (path, exc)) from exc
    for name, (section, key) in ENV_OVERRIDES.items():
        if os.environ.get(name):
            value = os.environ[name]
            if name == "DASH_PORT":
                try:
                    value = int(value)
                except ValueError as exc:
                    raise ConfigError("DASH_PORT must be an integer") from exc
            config[section][key] = value
    if INGEST_ENV in os.environ:
        config["daemon"]["ingest_enabled"] = parse_flag(os.environ[INGEST_ENV], INGEST_ENV)
    if OVERFLOW_ENV in os.environ:
        overflow = parse_overflow_rows(os.environ[OVERFLOW_ENV])
        if overflow is None:
            raise ConfigError("%s must be an integer in range %d..%d" %
                              (OVERFLOW_ENV, OVERFLOW_MIN, OVERFLOW_MAX))
        config["display"]["overflow_rows"] = overflow
    for section, key, minimum, maximum in (
        (config["daemon"], "port", 1, 65535),
        (config["daemon"], "poll_interval", 1, None),
        (config["daemon"], "retention_hours", 1, None),
        (config["daemon"], "cleanup_interval", 1, None),
        (config["sources"]["fake"], "scenario_interval", 1, None),
        (config["admin"], "port", 1, 65535),
    ):
        value = section[key]
        if type(value) is not int or value < minimum or (maximum and value > maximum):
            raise ConfigError("%s must be an integer in range %s..%s" %
                              (key, minimum, maximum or "unbounded"))
    for key in ("host", "database", "shared_token"):
        value = config["daemon"][key]
        if not isinstance(value, str) or (key != "shared_token" and not value.strip()):
            raise ConfigError("daemon.%s must be a valid string" % key)
    if type(config["daemon"]["ingest_enabled"]) is not bool:
        raise ConfigError("daemon.ingest_enabled must be a boolean")
    overflow = config["display"].get("overflow_rows")
    if type(overflow) is not int or overflow < OVERFLOW_MIN or overflow > OVERFLOW_MAX:
        raise ConfigError("display.overflow_rows must be an integer in range %d..%d" %
                          (OVERFLOW_MIN, OVERFLOW_MAX))
    alerts = config["alerts"]
    for key in ("warning_percent", "critical_percent"):
        value = alerts.get(key)
        if type(value) is not int or value < 1 or value > 100:
            raise ConfigError("alerts.%s must be an integer in range 1..100" % key)
    try:
        validate_thresholds(alerts["warning_percent"], alerts["critical_percent"])
    except ValueError as exc:
        raise ConfigError("alerts: %s" % exc) from exc
    for key in ("sound", "quiet_hours", "pop_to_top"):
        if type(alerts.get(key)) is not bool:
            raise ConfigError("alerts.%s must be a boolean" % key)
    for key in ("quiet_start", "quiet_end"):
        if parse_clock(alerts.get(key)) is None:
            raise ConfigError("alerts.%s must be a time of day as HH:MM" % key)
    token = config["daemon"]["shared_token"]
    if token and (not token.isascii() or any(char.isspace() for char in token)):
        raise ConfigError("daemon.shared_token must be ASCII without whitespace")
    if type(config["admin"]["enabled"]) is not bool:
        raise ConfigError("admin.enabled must be a boolean")
    for key in ("token", "key_file"):
        if not isinstance(config["admin"][key], str):
            raise ConfigError("admin.%s must be a string" % key)
    admin_token = config["admin"]["token"]
    if admin_token and (not admin_token.isascii() or any(char.isspace() for char in admin_token)):
        raise ConfigError("admin.token must be ASCII without whitespace")
    if token and admin_token == token:
        raise ConfigError("admin token must be different from the device shared token")
    if type(config["sources"]["fake"]["enabled"]) is not bool:
        raise ConfigError("sources.fake.enabled must be a boolean")
    from .sources import available_sources, source_type
    supported_sources = available_sources()
    for name, section in config["sources"].items():
        if not isinstance(section, dict):
            raise ConfigError("sources.%s must be a table" % name)
        if name not in supported_sources and section.get("enabled", False):
            raise ConfigError("sources.%s is unavailable" % name)
    for name in supported_sources:
        section = config["sources"].get(name)
        if section is None:
            continue
        if type(section.get("enabled", False)) is not bool:
            raise ConfigError("sources.%s.enabled must be a boolean" % name)
        if name == "fake":
            continue
        if name not in ("ollama", "litellm"):
            from .management import validate_settings
            cls = source_type(name)
            try:
                validate_settings(cls, {field["key"]: section.get(field["key"], field["default"])
                                        for field in cls.config_fields})
            except ValueError as exc:
                raise ConfigError("sources.%s: %s" % (name, exc)) from exc
            if cls.secret_field and not isinstance(section.get(cls.secret_field, ""), str):
                raise ConfigError("sources.%s key must be a string" % name)
            continue
        timeout = section.get("timeout")
        if type(timeout) is not int or timeout < 1:
            raise ConfigError("sources.%s.timeout must be a positive integer" % name)
        base_url = section.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ConfigError("sources.%s.base_url must be a non-empty string" % name)
        try:
            parsed_url = urlsplit(base_url)
        except ValueError as exc:
            raise ConfigError("sources.%s.base_url must be an http(s) URL" % name) from exc
        if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
            raise ConfigError("sources.%s.base_url must be an http(s) URL" % name)
        for key in (("samples_file",) if name == "ollama" else ("api_key", "api_key_env")):
            if not isinstance(section.get(key, ""), str):
                raise ConfigError("sources.%s.%s must be a string" % (name, key))
    key_env = config["sources"]["litellm"].get("api_key_env", "")
    if key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
        raise ConfigError("sources.litellm.api_key_env must be an environment variable name")
    if key_env and os.environ.get(key_env):
        config["sources"]["litellm"]["api_key"] = os.environ[key_env]
    if config["sources"]["fake"].get("enabled") and any(
        section.get("enabled") for name, section in config["sources"].items() if name != "fake"
    ):
        raise ConfigError("disable sources.fake when enabling a live source")
    level = config["logging"]["level"]
    if not isinstance(level, str) or level.upper() not in (
        "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
    ):
        raise ConfigError("invalid logging.level")
    config["logging"]["level"] = level.upper()
    return config


def resolve_shared_token(config):
    token = config["daemon"]["shared_token"]
    if not token:
        token = secrets.token_urlsafe(24)
        config["daemon"]["shared_token"] = token
        log.warning("no shared token configured; temporary token: %s", token)
    return token


def resolve_admin_token(config, shared_token):
    token = config["admin"]["token"]
    if not token:
        token = secrets.token_urlsafe(32)
        config["admin"]["token"] = token
        # A flag for the admin page, so the Settings tab can say the token is
        # temporary without ever showing it. INFO, not WARNING: this is a setup
        # notice that returns at every start, not a fault.
        config["admin"]["token_generated"] = True
        log.info("temporary admin token (new at every start; set admin.token in "
                 "dash.toml to keep one): %s", token)
    if token == shared_token:
        raise ValueError("admin token must be different from the device shared token")
    return token


def configure_logging(config):
    logging.basicConfig(level=config["logging"]["level"],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
