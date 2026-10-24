"""Load and validate daemon TOML config with environment overrides."""

from copy import deepcopy
import logging
import os
import re
import secrets
import tomllib
from urllib.parse import urlsplit

from . import child_env
from .secret_env import SecretFileError, secret_from_environment
from .alert_policy import (DEFAULT_CRITICAL_PERCENT, DEFAULT_MUTE_HOURS,
                           DEFAULT_QUIET_END, DEFAULT_QUIET_START,
                           DEFAULT_WARNING_PERCENT, is_quiet, parse_clock,
                           usable_mute_hours, validate_mute_hours,
                           validate_thresholds)

log = logging.getLogger(__name__)
DEFAULTS = {
    "daemon": {"host": "0.0.0.0", "port": 8787, "shared_token": "",
               "poll_interval": 15, "database": "dash.db",
               "retention_hours": 72, "cleanup_interval": 300,
               "ingest_enabled": False},
    "logging": {"level": "INFO"},
    "admin": {"enabled": False, "port": 8788, "token": "", "key_file": "",
              "host": "127.0.0.1", "allowed_hosts": [], "secure_cookie": False},
    "display": {"overflow_rows": 1},
    # Demo mode: sample data served by the daemon itself, replacing the real
    # readings while it is on. Off until the user asks for it.
    "demo": {"enabled": False},
    # The sound is off until the user turns it on: a desk device must never
    # beep unannounced. A value the user already stored (in the database or
    # in dash.toml) is respected; only the unset default is off.
    "alerts": {"enabled": True,
               "warning_percent": 80, "critical_percent": 95,
               "sound": False, "quiet_hours": False,
               "quiet_start": "22:00", "quiet_end": "07:00",
               "pop_to_top": True, "mute_hours": DEFAULT_MUTE_HOURS},
    "sources": {
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
    "DASH_ADMIN_HOST": ("admin", "host"),
}

# The variables that may instead name a file holding the value (NAME_FILE), so
# a container secret stays out of the environment and `docker inspect`.
FILE_SECRETS = ("DASH_SHARED_TOKEN", "DASH_ADMIN_TOKEN")

# The names the admin page answers to, when it does not answer to the
# loopback names alone. A comma-separated list in the environment.
ADMIN_ALLOWED_HOSTS_ENV = "DASH_ADMIN_ALLOWED_HOSTS"
# The bind addresses that keep the admin page on this machine.
LOOPBACK_HOSTS = frozenset(("127.0.0.1", "::1", "localhost"))
MAX_ALLOWED_HOSTS = 32


class ConfigError(ValueError):
    """Invalid or unreadable daemon configuration."""


INGEST_ENV = "DASH_INGEST_ENABLED"
# Demo mode: the device shows sample data instead of the real accounts.
DEMO_ENV = "DASH_DEMO"
# Mark the admin session cookie Secure: set it when the page is served over HTTPS.
ADMIN_SECURE_COOKIE_ENV = "DASH_ADMIN_SECURE_COOKIE"
TRUTHY_FLAGS = frozenset(("1", "true", "yes", "on"))
FALSY_FLAGS = frozenset(("0", "false", "no", "off"))

OVERFLOW_ENV = "DASH_OVERFLOW_ROWS"
OVERFLOW_MIN = 0
OVERFLOW_MAX = 3
DEFAULT_OVERFLOW_ROWS = 1
_warned_overflow = False


# A token somebody chose, rather than one the daemon generated, is the only
# kind that can be guessed. The generated ones are 192 and 256 bits.
MIN_TOKEN_LENGTH = 16
MIN_TOKEN_DISTINCT_CHARACTERS = 6
# Values that appear in the documentation and the example files. Anyone can
# look them up, so they are as good as no token.
PLACEHOLDER_TOKENS = frozenset({"change-me", "changeme", "replace-me",
                                "a-long-random-string-here"})
TOKEN_ADVICE = ('generate one with: python -c "import secrets; '
                'print(secrets.token_urlsafe(24))"')


def check_token_strength(name, token):
    """Refuse a chosen token that is short, repetitive or a published example.

    An empty token is fine: the daemon then generates a strong one. The error
    never repeats the token.
    """
    if not token:
        return
    if token.lower() in PLACEHOLDER_TOKENS:
        raise ConfigError("%s is a placeholder from the documentation, which "
                          "anyone can look up; %s" % (name, TOKEN_ADVICE))
    if len(token) < MIN_TOKEN_LENGTH:
        raise ConfigError("%s is too short: use at least %d characters; %s" %
                          (name, MIN_TOKEN_LENGTH, TOKEN_ADVICE))
    if len(set(token)) < MIN_TOKEN_DISTINCT_CHARACTERS:
        raise ConfigError("%s is too repetitive: it needs at least %d different "
                          "characters; %s" % (name, MIN_TOKEN_DISTINCT_CHARACTERS,
                                              TOKEN_ADVICE))


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


def parse_allowed_hosts(value):
    """The names the admin page answers to, as a list of lowercase names.

    A list comes from ``dash.toml``; a comma-separated string comes from the
    environment. Every entry is one name, optionally with a port, such as
    ``localhost:8788`` or ``dash.example.com``. An empty entry is ignored.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        parts = []
        for part in value:
            if not isinstance(part, str):
                raise ConfigError("admin.allowed_hosts must be a list of names")
            parts.append(part.strip())
    else:
        raise ConfigError("admin.allowed_hosts must be a list of names")
    names = []
    for part in parts:
        if not part:
            continue
        if len(part) > 255 or not part.isascii() or any(char.isspace() for char in part):
            raise ConfigError("admin.allowed_hosts entries must be short ASCII names "
                              "without spaces")
        if "://" in part or "/" in part:
            raise ConfigError("admin.allowed_hosts entries are names, not addresses "
                              "with a path")
        names.append(part.lower())
    if len(names) > MAX_ALLOWED_HOSTS:
        raise ConfigError("admin.allowed_hosts may hold at most %d names" %
                          MAX_ALLOWED_HOSTS)
    unique = []
    for name in names:
        if name not in unique:
            unique.append(name)
    return unique


def is_loopback_host(host):
    """Whether a bind address keeps the listener on this machine."""
    return (host or "").strip().lower() in LOOPBACK_HOSTS


def admin_bind_error(config):
    """Why the admin page may not listen on this bind address, or None.

    A bind address that is not loopback makes the admin page reachable from
    other machines, so it takes two deliberate choices: an admin token the
    operator set (a token generated at every start would be printed in the
    log of every start), and the list of names the page is reached by.
    Without both, the daemon refuses to start the admin page and says why.
    """
    admin = config.get("admin") if isinstance(config, dict) else None
    admin = admin if isinstance(admin, dict) else {}
    host = (admin.get("host") or "").strip()
    if not host or is_loopback_host(host):
        return None
    if not (admin.get("token") or "").strip():
        return ("the admin page would listen on %s, which is not a loopback "
                "address; set an explicit admin token (admin.token in dash.toml "
                "or DASH_ADMIN_TOKEN) before listening on a non-loopback address"
                % host)
    if not admin.get("allowed_hosts"):
        return ("the admin page would listen on %s, which is not a loopback "
                "address; list the names it is reached by (admin.allowed_hosts "
                "in dash.toml or DASH_ADMIN_ALLOWED_HOSTS) before listening on "
                "a non-loopback address" % host)
    return None


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


def demo_enabled(config, stored=None, environ=None):
    """The effective demo switch, resolved afresh on every request.

    The environment beats the admin's stored setting, which beats TOML; off by
    default. Reading the environment here - not once at startup - is what lets
    an admin toggle apply without a daemon restart. A garbled variable fails
    closed, so a broken value can never put sample data in front of a reader
    who did not ask for it.
    """
    names = os.environ if environ is None else environ
    if DEMO_ENV in names:
        try:
            return parse_flag(names[DEMO_ENV], DEMO_ENV)
        except ConfigError:
            log.error("%s is set to a non-boolean value; demo mode stays off", DEMO_ENV)
            return False
    if stored is not None:
        return stored
    section = config.get("demo") if isinstance(config, dict) else None
    if isinstance(section, dict):
        return section.get("enabled", False) is True
    return False


# The alert settings the admin page can change, and the file can set.
ALERT_KEYS = ("enabled", "warning_percent", "critical_percent", "sound",
              "quiet_hours", "quiet_start", "quiet_end", "pop_to_top",
              "mute_hours")


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
    for key in ("enabled", "sound", "quiet_hours", "pop_to_top"):
        if type(values[key]) is not bool:
            values[key] = DEFAULTS["alerts"][key]
    values["mute_hours"] = usable_mute_hours(values["mute_hours"])
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
        try:
            # The two tokens may also come from a file (DASH_*_TOKEN_FILE).
            value = (secret_from_environment(name) if name in FILE_SECRETS
                     else os.environ.get(name))
        except SecretFileError as exc:
            raise ConfigError(str(exc)) from None
        if value:
            if name == "DASH_PORT":
                try:
                    value = int(value)
                except ValueError as exc:
                    raise ConfigError("DASH_PORT must be an integer") from exc
            config[section][key] = value
    if INGEST_ENV in os.environ:
        config["daemon"]["ingest_enabled"] = parse_flag(os.environ[INGEST_ENV], INGEST_ENV)
    if DEMO_ENV in os.environ:
        config["demo"]["enabled"] = parse_flag(os.environ[DEMO_ENV], DEMO_ENV)
    if ADMIN_SECURE_COOKIE_ENV in os.environ:
        config["admin"]["secure_cookie"] = parse_flag(os.environ[ADMIN_SECURE_COOKIE_ENV],
                                                      ADMIN_SECURE_COOKIE_ENV)
    if OVERFLOW_ENV in os.environ:
        overflow = parse_overflow_rows(os.environ[OVERFLOW_ENV])
        if overflow is None:
            raise ConfigError("%s must be an integer in range %d..%d" %
                              (OVERFLOW_ENV, OVERFLOW_MIN, OVERFLOW_MAX))
        config["display"]["overflow_rows"] = overflow
    if ADMIN_ALLOWED_HOSTS_ENV in os.environ:
        config["admin"]["allowed_hosts"] = parse_allowed_hosts(
            os.environ[ADMIN_ALLOWED_HOSTS_ENV])
    else:
        config["admin"]["allowed_hosts"] = parse_allowed_hosts(
            config["admin"].get("allowed_hosts"))
    for section, key, minimum, maximum in (
        (config["daemon"], "port", 1, 65535),
        (config["daemon"], "poll_interval", 1, None),
        (config["daemon"], "retention_hours", 1, None),
        (config["daemon"], "cleanup_interval", 1, None),
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
    if type(config["demo"].get("enabled", False)) is not bool:
        raise ConfigError("demo.enabled must be a boolean")
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
    for key in ("enabled", "sound", "quiet_hours", "pop_to_top"):
        if type(alerts.get(key)) is not bool:
            raise ConfigError("alerts.%s must be a boolean" % key)
    for key in ("quiet_start", "quiet_end"):
        if parse_clock(alerts.get(key)) is None:
            raise ConfigError("alerts.%s must be a time of day as HH:MM" % key)
    try:
        validate_mute_hours(alerts.get("mute_hours"))
    except ValueError as exc:
        raise ConfigError("alerts: %s" % exc) from exc
    token = config["daemon"]["shared_token"]
    if token and (not token.isascii() or any(char.isspace() for char in token)):
        raise ConfigError("daemon.shared_token must be ASCII without whitespace")
    check_token_strength("daemon.shared_token", token)
    if type(config["admin"]["enabled"]) is not bool:
        raise ConfigError("admin.enabled must be a boolean")
    if type(config["admin"]["secure_cookie"]) is not bool:
        raise ConfigError("admin.secure_cookie must be a boolean")
    for key in ("token", "key_file"):
        if not isinstance(config["admin"][key], str):
            raise ConfigError("admin.%s must be a string" % key)
    admin_host = config["admin"].get("host")
    if not isinstance(admin_host, str) or not admin_host.strip():
        raise ConfigError("admin.host must be a bind address")
    if len(admin_host) > 255 or not admin_host.isascii() or \
            any(char.isspace() for char in admin_host):
        raise ConfigError("admin.host must be a short ASCII bind address without spaces")
    admin_token = config["admin"]["token"]
    if admin_token and (not admin_token.isascii() or any(char.isspace() for char in admin_token)):
        raise ConfigError("admin.token must be ASCII without whitespace")
    check_token_strength("admin.token", admin_token)
    if token and admin_token == token:
        raise ConfigError("admin token must be different from the device shared token")
    # The demo source is gone. An install that still carries its section in
    # dash.toml keeps working: the section is dropped, with one warning.
    if "fake" in config["sources"]:
        del config["sources"]["fake"]
        log.warning("[sources.fake] is no longer used; demo mode replaces it "
                    "(DASH_DEMO or the admin page). The section was ignored.")
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
    child_env.hide(key_env)
    if key_env and os.environ.get(key_env):
        config["sources"]["litellm"]["api_key"] = os.environ[key_env]
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
        # Announced by the daemon once it is ready, as the last lines of the
        # startup log, so it is not buried under later messages.
        config["daemon"]["shared_token_generated"] = True
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
    if token == shared_token:
        raise ValueError("admin token must be different from the device shared token")
    return token


def configure_logging(config):
    logging.basicConfig(level=config["logging"]["level"],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
