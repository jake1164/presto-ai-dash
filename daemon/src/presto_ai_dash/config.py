"""Load and validate daemon TOML config with environment overrides."""

from copy import deepcopy
import logging
import os
import re
import secrets
import tomllib
from urllib.parse import urlsplit

log = logging.getLogger(__name__)
DEFAULTS = {
    "daemon": {"host": "0.0.0.0", "port": 8787, "shared_token": "",
               "poll_interval": 15, "database": "dash.db",
               "retention_hours": 72, "cleanup_interval": 300},
    "logging": {"level": "INFO"},
    "admin": {"enabled": False, "port": 8788, "token": "", "key_file": ""},
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
        log.warning("temporary admin token: %s", token)
    if token == shared_token:
        raise ValueError("admin token must be different from the device shared token")
    return token


def configure_logging(config):
    logging.basicConfig(level=config["logging"]["level"],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
