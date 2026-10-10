"""Load and validate the M2 TOML config with environment overrides."""

from copy import deepcopy
import logging
import os
import secrets
import tomllib

log = logging.getLogger(__name__)
DEFAULTS = {
    "daemon": {"host": "0.0.0.0", "port": 8787, "shared_token": "",
               "poll_interval": 15, "database": "dash.db"},
    "logging": {"level": "INFO"},
    "sources": {"fake": {"enabled": True, "scenario_interval": 20}},
}
ENV_OVERRIDES = {
    "DASH_SHARED_TOKEN": ("daemon", "shared_token"),
    "DASH_HOST": ("daemon", "host"),
    "DASH_PORT": ("daemon", "port"),
    "DASH_DATABASE": ("daemon", "database"),
    "DASH_LOG_LEVEL": ("logging", "level"),
}


class ConfigError(ValueError):
    """Invalid or unreadable daemon configuration."""


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
    for section, key, minimum, maximum in (
        (config["daemon"], "port", 1, 65535),
        (config["daemon"], "poll_interval", 1, None),
        (config["sources"]["fake"], "scenario_interval", 1, None),
    ):
        value = section[key]
        if type(value) is not int or value < minimum or (maximum and value > maximum):
            raise ConfigError("%s must be an integer in range %s..%s" %
                              (key, minimum, maximum or "unbounded"))
    for key in ("host", "database", "shared_token"):
        value = config["daemon"][key]
        if not isinstance(value, str) or (key != "shared_token" and not value.strip()):
            raise ConfigError("daemon.%s must be a valid string" % key)
    token = config["daemon"]["shared_token"]
    if token and (not token.isascii() or any(char.isspace() for char in token)):
        raise ConfigError("daemon.shared_token must be ASCII without whitespace")
    if type(config["sources"]["fake"]["enabled"]) is not bool:
        raise ConfigError("sources.fake.enabled must be a boolean")
    for name, section in config["sources"].items():
        if not isinstance(section, dict):
            raise ConfigError("sources.%s must be a table" % name)
        if name != "fake" and section.get("enabled", False):
            raise ConfigError("sources.%s is unavailable in M2; use fake only" % name)
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


def configure_logging(config):
    logging.basicConfig(level=config["logging"]["level"],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
