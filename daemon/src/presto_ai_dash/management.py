"""Database-backed source instances, secret updates, and live reconfiguration."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import logging
import os
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

from . import __version__
from .alert_policy import (apply_threshold, parse_clock, resolve_alert_settings,
                           threshold_status, validate_mute_hours,
                           validate_thresholds)
from .card_tiles import local_notices, local_readings
from .clients import ClientRegistry
from .codex_login import CodexLoginManager
from .collectors import (collector_config_text, collector_info, presto_collector,
                          served_version, update_state)
from .config import (ALERT_KEYS, DEFAULTS, alert_base_settings, alert_settings,
                     demo_enabled, ingest_enabled, overflow_rows)
from .credentials import load_cipher
from .dashboard import build_card_details, build_instance_payload, device_view, lazy_cards
from .device_app import DeviceApp
from .device_lifecycle import DeviceLifecycle, release_instructions
from .effective import effective_settings, uptime_label, uptime_seconds
from .http_client import origin_of
from .logbuffer import LogBuffer
from .problems import ProblemBuffer
from .sources import in_container, source_catalog, source_type
from .sources._codex_rpc import CodexReportingError, find_executable
from .models import SourceSnapshot, iso, now_utc
from .quotas import claude_provenance, mark_passed_resets, present_quota, present_snapshot
from .storage import ConfigurationConflict

# The snippet URL is deliberately a placeholder: the user edits the address.
INGEST_URL_PLACEHOLDER = "http://REPLACE-WITH-DASH-ADDRESS:8787/api/v1/ingest"

log = logging.getLogger(__name__)


def codex_found(executable=""):
    """Whether this machine has a Codex program for the given executable setting."""
    try:
        find_executable(str(executable or ""))
    except CodexReportingError:
        return False
    return True


def _stamp(value):
    """A UTC timestamp for an epoch moment, or None."""
    if value is None:
        return None
    try:
        return iso(datetime.fromtimestamp(float(value), timezone.utc))
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _activity_state(activity):
    """ok, scanning, or unavailable: what the local activity reader says."""
    if not activity.get("available"):
        return "unavailable"
    return "ok" if activity.get("complete", True) else "scanning"


def _from_collector(reading):
    """A note saying a reading was pushed by a collector, and from which computer."""
    host = reading.get("reported_by")
    return " (reported by the collector on %s)" % str(host)[:32] if host else ""


def _activity_detail(activity):
    """Why the activity reader says what it says, in plain words."""
    source = _from_collector(activity)
    if not activity.get("available"):
        return (str(activity.get("reason") or activity.get("message") or
                    "unavailable")[:160] + source)
    if not activity.get("complete", True):
        return "still scanning the session logs" + source
    last = activity.get("last_request_at")
    if not last:
        return "no request recorded yet" + source
    return "last request at %s%s" % (str(last), source)


def _cache_state(cache):
    """ok or unavailable: what the usage cache reader says."""
    return "ok" if cache.get("available") else "unavailable"


def _cache_detail(cache):
    """When the usage cache was read, or why it could not be read."""
    source = _from_collector(cache)
    if not cache.get("available"):
        return (str(cache.get("message") or cache.get("reason") or
                    "unavailable")[:160] + source)
    fetched = cache.get("fetched_at")
    if not fetched:
        return "no reading yet" + source
    return "fetched at %s%s" % (str(fetched), source)


def validate_settings(cls, settings):
    if not isinstance(settings, dict):
        raise ValueError("source settings must be an object")
    fields = {field["key"]: field for field in cls.config_fields}
    if settings.keys() - fields.keys():
        raise ValueError("unknown source setting")
    result = {}
    for key, field in fields.items():
        value = settings.get(key, field["default"])
        if field["type"] == "int":
            if (type(value) is not int or value < field.get("minimum", 1) or
                    value > field.get("maximum", 3600)):
                raise ValueError(field["label"] + " is out of range")
        elif field["type"] == "bool":
            # Only a real boolean: "true" or 1 must never silently enable a
            # privacy-relevant setting like local activity reading.
            if type(value) is not bool:
                raise ValueError(field["label"] + " must be true or false")
        else:
            if not isinstance(value, str) or len(value) > 2048 or "\x00" in value:
                raise ValueError(field["label"] + " must be a valid string")
            if field["type"] == "url":
                try:
                    parsed = urlsplit(value)
                    valid = (parsed.scheme in ("http", "https") and parsed.hostname
                             and not parsed.username and not parsed.password
                             and not parsed.query and not parsed.fragment
                             and not any(char.isspace() for char in value))
                    if parsed.port is not None and not 1 <= parsed.port <= 65535:
                        valid = False
                except ValueError:
                    valid = False
                if not valid:
                    raise ValueError(field["label"] + " must be an HTTP(S) URL without credentials")
                value = value.rstrip("/")
        result[key] = value
    # A source may refuse a combination of settings that is valid field by field.
    check = getattr(cls, "check_settings", None)
    if check is not None:
        check(result)
    return result


class SourceConfiguration:
    def __init__(self, storage, config, config_path=None, started_at=None):
        self.storage = storage
        self.config = config
        # The port of the Presto preview page when this daemon serves one.
        self.preview_port = None
        # The bootstrap file and the start time are only used to explain the
        # effective configuration and the uptime on the Status tab.
        self.config_path = config_path
        self.started_at = time.time() if started_at is None else float(started_at)
        # In memory only: the clients that fetch the status endpoint, and the
        # recent problems. Both are lost when the daemon restarts.
        self.clients = ClientRegistry()
        self.problems = ProblemBuffer()
        self.logs = LogBuffer()
        self.changed = threading.Event()
        self._edit_lock = threading.RLock()
        # The services whose own alert thresholds are being ignored, so the
        # daemon says it once and not once per payload.
        self._ignored_overrides = set()
        marker = storage.configuration_marker()
        if marker is None and storage.source_instances():
            raise ValueError("credential store verifier is missing; restore the database backup")
        self.cipher = load_cipher(config["daemon"]["database"],
                                  config["admin"]["key_file"], existing=marker is not None)
        if marker is None:
            records = []
            for description in source_catalog(container=False):
                type_id = description["id"]
                section = config["sources"].get(type_id)
                if section is None:
                    continue
                # A section that is only the built-in default means the user
                # configured nothing: a fresh install starts with no accounts
                # instead of two disabled ones nobody asked for.
                if section == DEFAULTS["sources"].get(type_id):
                    continue
                cls = source_type(type_id)
                settings = {field["key"]: section.get(field["key"], field["default"])
                            for field in cls.config_fields}
                secret = section.get(cls.secret_field, "") if cls.secret_field else ""
                records.append({"id": type_id, "type": type_id, "name": cls.name,
                                "enabled": section.get("enabled", False),
                                "config": validate_settings(cls, settings),
                                "credential": self.cipher.encrypt(type_id, secret) if secret else None})
            storage.initialize_configuration(
                records, self.cipher.encrypt("__vault__", "presto-ai-dash-vault-v1"))
        marker = storage.configuration_marker()
        if self.cipher.decrypt("__vault__", marker) != "presto-ai-dash-vault-v1":
            raise ValueError("invalid credential store")
        # Fail closed on lost/wrong keys, even when affected sources are disabled.
        for record in storage.source_instances():
            if record["credential"] is not None:
                self.cipher.decrypt(record["id"], record["credential"])
        self.codex_logins = CodexLoginManager(self)
        # Durable device identity and update state. Updates over the air are
        # always on: a device set up for them is offered this daemon's app.
        self.lifecycle = DeviceLifecycle(storage, ota_enabled=True)
        # The exact slot bundle this running daemon serves to a device.
        self.device_app = DeviceApp()
        # The background daemon-release checker, attached by the daemon. The
        # admin page reads its cached result and asks it for a check; it never
        # contacts a registry itself.
        self.release_checker = None
    def list_sources(self):
        records = self.storage.source_instances()
        snapshots = self.storage.latest_snapshots()
        settings = self.alerts_state()
        stored = self.storage.service_alert_settings_all()
        sources = []
        for record in records:
            public = {key: record[key] for key in ("id", "type", "name", "enabled", "config", "revision")}
            public["has_key"] = record["credential"] is not None
            if record["type"] == "claude":
                public["export_installed"] = self.storage.claude_bridge(record["id"]) is not None
                # A container has no Claude Code profile to edit, so the setup
                # button would only fail there: Claude reports through a collector.
                public["export_available"] = public["export_installed"] or not in_container()
            if record["type"] == "codex":
                public["codex_found"] = codex_found((record.get("config") or {}).get("executable", ""))
            self._attach_ingest(public, record)
            resolution = resolve_alert_settings(settings, stored.get(record["id"]))
            # What the admin page needs to say: whether this service
            # alerts, and why not when it does not.
            public["alerts"] = {
                "enabled": resolution["enabled"],
                "service_enabled": resolution["service_enabled"],
                "master_off": not resolution["master_enabled"],
                "override_ignored": resolution["override_ignored"],
                "warning_percent": resolution["warning_percent"],
                "critical_percent": resolution["critical_percent"],
            }
            snapshot = snapshots.get(record["id"])
            if snapshot:
                snapshot = present_snapshot(snapshot)
            status = snapshot.status if snapshot else "waiting"
            if snapshot:
                # The daemon decides the user-facing status, so the page
                # shows the same warning a nearly used window raises on the
                # device.
                level = threshold_status(snapshot.metrics.get("quota"),
                                         resolution["warning_percent"],
                                         resolution["critical_percent"])
                if level:
                    status = apply_threshold(status, level)
            public["status"] = status
            public["updated_at"] = snapshot.to_dict()["updated_at"] if snapshot else None
            if snapshot and snapshot.metrics.get("quota"):
                public["quota"] = mark_passed_resets(present_quota(snapshot.metrics["quota"]))
            if snapshot and record["type"] in ("ollama", "litellm"):
                public["readings"] = local_readings(record["type"], snapshot.metrics, now_utc())
                public["notices"] = local_notices(snapshot.alerts, snapshot.error, snapshot.metrics)
            if record["type"] == "claude":
                # Every Claude number says where it came from: the terminal
                # export, the local session logs, or the freshness hint.
                public["provenance"] = claude_provenance(public.get("quota"),
                                                         record.get("config"))
            sources.append(public)
        # Whether this machine has Codex, so the page can send a user without it
        # (a container, a server) to remote reports instead of a sign-in that fails.
        return {"types": source_catalog(), "sources": sources, "codex_found": codex_found("")}

    def _attach_ingest(self, public, record):
        """Push-capable accounts gain token state and the collector trail."""
        try:
            pushes = source_type(public["type"]).accepts_push
        except KeyError:
            pushes = False  # an unavailable source module is never push-capable
        if not pushes:
            return
        info = self.storage.ingest_token_info(record["id"])
        observation = self.storage.observation(record["id"])
        public["ingest"] = {
            "token_exists": info["exists"], "created_at": info["created_at"],
            "collector_seen_at": observation["collector_seen_at"] if observation else None,
            "collector_host": observation["collector_host"] if observation else None,
            "collector_version": observation["collector_version"] if observation else None,
            # Against the collector this daemon serves: only a remote copy can lag.
            "collector_update": update_state(
                observation["collector_version"] if observation else None),
            "collector_latest": served_version(),
            "last_progress_at": observation["last_progress_at"] if observation else None}

    def ingest_state(self):
        """The route switch for the admin page; the environment locks the toggle."""
        return {"enabled": ingest_enabled(self.config, self.storage.ingest_enabled_setting()),
                "locked_by_environment": os.environ.get("DASH_INGEST_ENABLED") is not None,
                # The file the page offers for download, and its hash.
                "collector": collector_info()}

    def set_ingest_enabled(self, data):
        if not isinstance(data, dict) or set(data) != {"enabled"} or \
                type(data["enabled"]) is not bool:
            raise ValueError("ingest needs an enabled boolean")
        if self.ingest_state()["locked_by_environment"]:
            raise ConfigurationConflict(
                "ingest is set by DASH_INGEST_ENABLED; edit the environment to change it")
        self.storage.save_ingest_enabled(data["enabled"])
        return self.ingest_state()

    def demo_state(self):
        """The demo switch; the environment locks the control.

        While it is on, the daemon serves sample data to the device and to
        the status API instead of the real accounts. It writes nothing.
        """
        return {"enabled": demo_enabled(self.config, self.storage.demo_enabled_setting()),
                "locked_by_environment": os.environ.get("DASH_DEMO") is not None}

    def set_demo_enabled(self, data):
        if not isinstance(data, dict) or set(data) != {"enabled"} or \
                type(data["enabled"]) is not bool:
            raise ValueError("demo needs an enabled boolean")
        if self.demo_state()["locked_by_environment"]:
            raise ConfigurationConflict(
                "demo mode is set by DASH_DEMO; edit the environment to change it")
        self.storage.save_demo_enabled(data["enabled"])
        return self.demo_state()

    def display_state(self):
        """The Home display settings; the environment locks the control."""
        return {"overflow_rows": overflow_rows(self.config,
                                               self.storage.overflow_rows_setting()),
                "locked_by_environment": os.environ.get("DASH_OVERFLOW_ROWS") is not None}

    def set_display(self, data):
        if not isinstance(data, dict) or set(data) != {"overflow_rows"} or \
                type(data["overflow_rows"]) is not int:
            raise ValueError("display needs an overflow_rows integer")
        value = data["overflow_rows"]
        if value < 0 or value > 3:
            raise ValueError("overflow_rows must be between 0 and 3")
        if self.display_state()["locked_by_environment"]:
            raise ConfigurationConflict(
                "overflow_rows is set by DASH_OVERFLOW_ROWS; edit the environment to change it")
        self.storage.save_overflow_rows(value)
        return self.display_state()

    def alerts_state(self):
        """The alert settings, and whether it is quiet right now.

        The quiet flag is judged against the daemon's own local time, so the
        device needs no clock and no time zone. ``enabled`` is the master
        switch: with it off, no service alerts at all.
        """
        return alert_settings(self.config, self.storage.alerts_settings())

    def set_alerts(self, data):
        """Save the alert settings the admin page changed.

        A setting that is put back to the value in dash.toml stops
        overriding the file, so the effective configuration still says
        where each value came from.
        """
        if not isinstance(data, dict) or set(data) != set(ALERT_KEYS):
            raise ValueError("alerts needs every alert setting")
        validate_thresholds(data["warning_percent"], data["critical_percent"])
        for key in ("enabled", "sound", "quiet_hours", "pop_to_top"):
            if type(data[key]) is not bool:
                raise ValueError("alerts.%s must be true or false" % key)
        for key in ("quiet_start", "quiet_end"):
            if parse_clock(data[key]) is None:
                raise ValueError("alerts.%s must be a time of day as HH:MM" % key)
        try:
            validate_mute_hours(data["mute_hours"])
        except ValueError as exc:
            raise ValueError("alerts.mute_hours must be an integer in range 0..48") from exc
        base = alert_base_settings(self.config)
        changed = {}
        for key in ALERT_KEYS:
            if data[key] != base[key]:
                changed[key] = data[key]
        self.storage.save_alerts(changed)
        return self.alerts_state()

    # -- per-service alert settings ------------------------------------------

    def _service_record(self, instance_id):
        """One source instance, or None."""
        return next((row for row in self.storage.source_instances()
                     if row["id"] == instance_id), None)

    def service_alert_state(self, instance_id):
        """One service's alert settings, resolved against the global ones.

        The stored values are the user's own: a threshold that is null is
        inherited from the global setting. The resolved values say what the
        service actually uses, and the flags say why they differ.
        """
        record = self._service_record(instance_id)
        if record is None:
            raise KeyError(instance_id)
        settings = self.alerts_state()
        stored = self.storage.service_alert_settings(instance_id) or {}
        resolution = resolve_alert_settings(settings, stored)
        self._note_ignored_override(instance_id, resolution)
        return {
            "id": instance_id,
            "name": record["name"],
            "enabled": stored.get("enabled", True),
            "warning_percent": stored.get("warning_percent"),
            "critical_percent": stored.get("critical_percent"),
            "effective": {
                "enabled": resolution["enabled"],
                "warning_percent": resolution["warning_percent"],
                "critical_percent": resolution["critical_percent"],
            },
            "override_ignored": resolution["override_ignored"],
            "master_off": not resolution["master_enabled"],
            "master": resolution["master_enabled"],
        }

    def set_service_alerts(self, instance_id, data):
        """Save one service's alert settings.

        Only the keys in the request are changed: a write that switches the
        service's alerts on or off never touches its thresholds, and the
        device's write and the admin page's write go through here, so the
        last one wins.
        """
        if self._service_record(instance_id) is None:
            raise KeyError(instance_id)
        if not isinstance(data, dict) or not data:
            raise ValueError("service alerts need at least one setting")
        unknown = set(data) - {"enabled", "warning_percent", "critical_percent"}
        if unknown:
            raise ValueError("unknown service alert setting: %s" % sorted(unknown)[0])
        if "enabled" in data and type(data["enabled"]) is not bool:
            raise ValueError("enabled must be true or false")
        thresholds = {}
        for key in ("warning_percent", "critical_percent"):
            if key not in data:
                continue
            value = data[key]
            if value is None:
                thresholds[key] = None
                continue
            if type(value) is not int or value < 1 or value > 100:
                raise ValueError("%s must be an integer in range 1..100" % key)
            thresholds[key] = value
        if thresholds:
            # The pair that would result must be usable: the warning must
            # sit below the critical one, whether the value is the
            # service's own or the global one it inherits.
            settings = self.alerts_state()
            stored = self.storage.service_alert_settings(instance_id) or {}
            warning = (thresholds["warning_percent"]
                       if "warning_percent" in thresholds
                       else stored.get("warning_percent"))
            critical = (thresholds["critical_percent"]
                        if "critical_percent" in thresholds
                        else stored.get("critical_percent"))
            if warning is None:
                warning = settings["warning_percent"]
            if critical is None:
                critical = settings["critical_percent"]
            validate_thresholds(warning, critical)
        values = {}
        if "enabled" in data:
            values["enabled"] = data["enabled"]
        values.update(thresholds)
        self.storage.save_service_alert_settings(instance_id, values)
        return self.service_alert_state(instance_id)

    def set_service_alert_enabled(self, instance_id, enabled):
        """Switch one service's alerts on or off. The device's only write.

        It changes that one setting and nothing else: never the thresholds,
        never the master switch, never a provider.
        """
        if type(enabled) is not bool:
            raise ValueError("enabled must be true or false")
        return self.set_service_alerts(instance_id, {"enabled": enabled})

    def alert_resolutions(self):
        """Every service's effective alert behaviour, by instance id."""
        settings = self.alerts_state()
        stored = self.storage.service_alert_settings_all()
        result = {}
        for record in self.storage.source_instances():
            resolution = resolve_alert_settings(settings, stored.get(record["id"]))
            self._note_ignored_override(record["id"], resolution)
            result[record["id"]] = resolution
        return result

    def _note_ignored_override(self, instance_id, resolution):
        """Say once that a service's own thresholds are not being used.

        The service falls back to the global pair when its own pair cannot
        be used - for example after the global value changed under it. The
        line is logged once per service, not once per payload.
        """
        if not resolution.get("override_ignored"):
            return
        if instance_id in self._ignored_overrides:
            return
        self._ignored_overrides.add(instance_id)
        log.info("the alert thresholds of %s are ignored: they would not be usable, "
                 "so the global values are used", instance_id)

    def create_ingest_token(self, instance_id):
        """Mint or rotate one write-only push token, shown exactly once."""
        token = self.storage.create_ingest_token(instance_id)
        record = next((row for row in self.storage.source_instances()
                       if row["id"] == instance_id), None)
        service = "codex" if record and record["type"] == "codex" else "claude"
        token_file = "%s-%s.token" % (service, instance_id)
        result = {"token": token, "service": service, "token_file": token_file,
                  "name": (record or {}).get("name") or service,
                  "ingest_url": INGEST_URL_PLACEHOLDER,
                  "device_port": self.config["daemon"]["port"],
                  "admin_port": self.config["admin"]["port"],
                  "collector_folder": "~/.presto-ai-dash",
                  "collector_config": collector_config_text(
                      service, (record or {}).get("name") or service, token_file,
                      INGEST_URL_PLACEHOLDER)}
        if service == "claude":
            # The terminal's status line, for Claude Code in a terminal. Codex has
            # no status line: it is read on a timer by `watch`.
            result["posix_snippet"] = presto_collector.posix_snippet(
                "/path/to/presto_collector.py", INGEST_URL_PLACEHOLDER,
                "~/.config/presto-ai-dash/" + token_file, "laptop")
            result["powershell_snippet"] = presto_collector.powershell_snippet(
                "C:/path/to/presto_collector.py", INGEST_URL_PLACEHOLDER,
                "C:/Users/USERNAME/presto-ai-dash/" + token_file, "laptop")
        return result

    def revoke_ingest_token(self, instance_id):
        self.storage.revoke_ingest_token(instance_id)
        return {"revoked": True}

    def save(self, data, instance_id=None):
        if not isinstance(data, dict) or data.keys() - {
            "type", "name", "enabled", "config", "api_key", "clear_key", "revision"
        }:
            raise ValueError("invalid source configuration")
        with self._edit_lock:
            current = next((row for row in self.storage.source_instances()
                            if row["id"] == instance_id), None)
            if instance_id is not None and current is None:
                raise KeyError(instance_id)
            type_id = data.get("type", current["type"] if current else None)
            try:
                cls = source_type(type_id)
            except (KeyError, TypeError):
                raise ValueError("unknown source type") from None
            if current and current["type"] != type_id:
                raise ValueError("create a new source to change its type")
            name = data.get("name", current["name"] if current else cls.name)
            if not isinstance(name, str) or not name.strip() or len(name) > 80:
                raise ValueError("source name must contain 1–80 characters")
            enabled = data.get("enabled", current["enabled"] if current else False)
            if type(enabled) is not bool:
                raise ValueError("enabled must be a boolean")
            settings = data.get("config", current["config"] if current else {})
            settings = validate_settings(cls, settings)
            if (current and type_id == "claude" and self.storage.claude_bridge(instance_id) and
                    settings["settings_path"] != current["config"]["settings_path"]):
                raise ValueError("Remove the Claude export setup before changing its profile settings path")
            revision = data.get("revision", 0)
            if type(revision) is not int or (current and revision < 1) or (not current and revision != 0):
                raise ValueError("reload the source before editing it")
            clear_key = data.get("clear_key", False)
            secret = data.get("api_key", "")
            if type(clear_key) is not bool or not isinstance(secret, str):
                raise ValueError("invalid key update")
            if secret and (not secret.isascii() or len(secret) > 4096 or
                           any(char.isspace() for char in secret)):
                raise ValueError("API key must be ASCII without whitespace")
            if (secret or clear_key) and not cls.secret_field:
                raise ValueError("this source type does not use an API key")
            if secret and clear_key:
                raise ValueError("choose replace key or remove key")
            record_id = instance_id or uuid4().hex
            credential = current["credential"] if current else None
            if credential is not None and not secret and not clear_key:
                # The saved key is sent to the URL. Keeping it across a change
                # of address would let an edit that never shows the key move it
                # to another server, so a new address needs the key again.
                for field in cls.config_fields:
                    if (field["type"] == "url" and
                            origin_of(settings[field["key"]]) !=
                            origin_of(current["config"].get(field["key"], field["default"]))):
                        raise ValueError(
                            "Enter the API key again, or remove it, when you change the "
                            "URL, so a saved key is never sent to a new address")
            if clear_key:
                credential = None
            elif secret:
                credential = self.cipher.encrypt(record_id, secret)
            record = {"id": record_id, "type": type_id, "name": name.strip(),
                      "enabled": enabled, "config": settings, "credential": credential}
            self.storage.save_source_instance(record, revision)
            self.changed.set()
            return next(row for row in self.list_sources()["sources"] if row["id"] == record_id)

    def reorder(self, data):
        if not isinstance(data, dict) or set(data) != {"order", "expected_order"}:
            raise ValueError("reorder requires order and expected_order")
        for order in (data["order"], data["expected_order"]):
            if (not isinstance(order, list) or
                    any(not isinstance(value, str) for value in order) or
                    len(order) != len(set(order))):
                raise ValueError("account order must be a list of unique IDs")
        with self._edit_lock:
            self.storage.reorder_source_instances(data["order"], data["expected_order"])
            return self.list_sources()

    def delete(self, instance_id, revision):
        if type(revision) is not int or revision < 1:
            raise ValueError("reload the source before deleting it")
        with self._edit_lock:
            bridge = self.storage.claude_bridge(instance_id)
            if bridge:
                self.configure_claude_export(instance_id, {"action": "remove", "revision": revision})
            self.storage.delete_source_instance(instance_id, revision)
            self.changed.set()

    def configure_claude_export(self, instance_id, data):
        from .claude_export import configure_bridge
        if (not isinstance(data, dict) or set(data) != {"action", "revision"} or
                type(data["revision"]) is not int):
            raise ValueError("Export setup requires an action and the current revision")
        with self._edit_lock:
            record = next((r for r in self.storage.source_instances() if r["id"] == instance_id), None)
            if not record:
                raise KeyError(instance_id)
            if record["type"] != "claude":
                raise ValueError("Export setup is only available for Claude")
            if record["revision"] != data["revision"]:
                raise ConfigurationConflict("source changed; reload before configuring export")
            return configure_bridge(self, record, data["action"])

    def start_codex_login(self, instance_id, data):
        from .codex_login import configure_codex_login
        record = next((r for r in self.storage.source_instances() if r["id"] == instance_id), None)
        if not record:
            raise KeyError(instance_id)
        if record["type"] != "codex":
            raise ValueError("Codex login is only available for Codex sources")
        if not isinstance(data, dict):
            raise ValueError("invalid login request")
        return configure_codex_login(self, instance_id, data)

    def codex_login_status(self, session_id):
        return self.codex_logins.status(session_id)

    def cancel_codex_login(self, session_id):
        return self.codex_logins.cancel(session_id)

    def logout_codex(self, instance_id):
        record = next((r for r in self.storage.source_instances() if r["id"] == instance_id), None)
        if not record:
            raise KeyError(instance_id)
        if record["type"] != "codex":
            raise ValueError("Codex logout is only available for Codex sources")
        return self.codex_logins.logout(instance_id)

    def full_status_payload(self, alerts=None, resolutions=None):
        """Every field of every report: the superset the Presto's view is cut from."""
        with self.storage._lock:
            records = self.storage.source_instances()
            snapshots = self.storage.latest_snapshots()
        return build_instance_payload(snapshots, records, alerts, resolutions=resolutions)

    def status_payload(self, lazy=False):
        settings = self.alerts_state()
        resolutions = self.alert_resolutions()
        thresholds = {"warning_percent": settings["warning_percent"],
                      "critical_percent": settings["critical_percent"],
                      "enabled": settings["enabled"]}
        # The Presto parses the whole response in a few hundred KB of RAM, so it
        # gets only the fields it reads (dashboard.device_view).
        payload = device_view(self.full_status_payload(thresholds, resolutions))
        if lazy:
            # The card's lines and tiles are fetched when the card is opened,
            # so the poll stays small.
            lazy_cards(payload)
        # The Home display settings travel with the payload, so a change
        # applies on the next poll without restarting the daemon.
        payload["display"] = {"overflow_rows": self.display_state()["overflow_rows"]}
        # The alert settings travel too: the device has no clock of its own,
        # so the daemon says whether it is quiet right now, and whether
        # alerts are on at all.
        payload["alerts"] = self.device_alerts(settings)
        return payload

    def device_alerts(self, settings=None):
        """The alert settings the Presto is told: sound, quiet now, pop to top, the
        master switch and the mute length. Demo mode sends the same ones."""
        settings = settings or self.alerts_state()
        return {"quiet": settings["quiet"], "sound": settings["sound"],
                "pop_to_top": settings["pop_to_top"],
                "enabled": settings["enabled"],
                "mute_hours": settings["mute_hours"]}

    def card_detail(self, card_id):
        """One card's lazy detail: its tiles and its lines.

        None when no card has that id. The call is read-only: it reads the
        last snapshots and returns what the card would show.
        """
        settings = self.alerts_state()
        resolutions = self.alert_resolutions()
        thresholds = {"warning_percent": settings["warning_percent"],
                      "critical_percent": settings["critical_percent"],
                      "enabled": settings["enabled"]}
        with self.storage._lock:
            records = self.storage.source_instances()
            snapshots = self.storage.latest_snapshots()
        details = build_card_details(snapshots, records, thresholds,
                                      resolutions=resolutions)
        return details.get(card_id)

    def effective_config(self):
        """The effective daemon configuration and where each value came from."""
        return {"settings": effective_settings(self.config, self.storage, self.config_path)}

    def status_view(self):
        """Everything the Status tab shows: read-only, and never a secret."""
        listing = self.list_sources()
        sources = listing["sources"]
        snapshots = self.storage.latest_snapshots()
        poll_interval = self.config["daemon"]["poll_interval"]
        uptime = uptime_seconds(self.started_at)
        devices = self.devices_view()
        attention = devices["needs_attention_count"]
        return {            "daemon": {
                "version": __version__,
                "schema_version": self.storage.schema_version(),
                "uptime_seconds": uptime,
                "uptime": uptime_label(uptime),
                "poll_interval": poll_interval,
                "started_at": _stamp(self.started_at),
                "now": iso(now_utc()),
                "log_level": self.config["logging"]["level"],
                "host": self.config["daemon"]["host"],
                "port": self.config["daemon"]["port"],
            },
            "clients": [dict(record, last_seen=_stamp(record["last_seen"]),
                            first_seen=_stamp(record["first_seen"]),
                            diag=dict(record.get("diag") or {}),
                            events=[dict(event, at=_stamp(event["at"]))
                                    for event in record.get("events") or []])
                        for record in self.clients.clients(poll_interval=poll_interval)],
            "accounts": [self._account_health(source, snapshots.get(source["id"]))
                         for source in sources],
            "alerts": self._alert_state_view(sources),
            "readers": self._readers(sources),
            "collectors": self._collectors(sources),
            "problems": [dict(record, time=_stamp(record["time"]))
                         for record in self.problems.problems()],
            # The durable devices, and how many need attention. These are the
            # same authoritative projections the device routes return, so the
            # page has one source of device truth. "clients" above stays the
            # in-memory view of who polled, and is never mixed with them.
            "devices": devices["devices"],
            "device_attention_count": attention,
            "release": self.release_view(),
            "preview": {"port": self.preview_port} if self.preview_port else None,
        }

    def devices_view(self):
        """Every durable device, plus the count that needs attention."""
        return self.lifecycle.devices_view(self.config["daemon"]["poll_interval"])

    def device_view(self, device_id):
        """One device's detail, or None for an ID the daemon does not know."""
        return self.lifecycle.device_view(device_id, self.config["daemon"]["poll_interval"])

    def forget_device(self, device_id):
        """Remove the record of a device that has gone quiet."""
        self.lifecycle.forget(device_id, self.config["daemon"]["poll_interval"])

    def device_attention_count(self):
        return self.lifecycle.attention_count()

    def refuse_device_mutation(self, device_id, kind):
        """Refuse a retry or recovery request: there is no updater yet.

        The route exists so a client can tell "not available" from "no such
        device". It never authorizes a transfer, resets a budget or clears a
        warning; a warning clears only on a supported recovery.
        """
        self.lifecycle.refuse_mutation(device_id, kind)

    def device_mutation(self, device_id, kind, body, idempotency_key):
        """Authorize a retry, or queue a recovery check, when OTA is enabled.

        With the updater absent this raises the same ``409 ota_not_enabled``
        the route has always answered. Neither branch contacts the device.
        """
        if self.lifecycle.ota_enabled:
            if kind == "retry":
                return self.lifecycle.authorize_retry(device_id, body,
                                                      idempotency_key)
            return self.lifecycle.queue_recovery(device_id, body, idempotency_key)
        self.lifecycle.refuse_mutation(device_id, kind)
        raise AssertionError("refuse_mutation always raises")

    def release_view(self):
        """The cached daemon-release result, with this install's own commands.

        No registry is contacted: this only reads what the background checker
        stored. A development build or a switched-off check is ``disabled``,
        which is honest about not having looked, never "current".
        """
        result = dict(self.lifecycle.release())
        checker = self.release_checker
        installation = checker.installation if checker is not None else None
        result["instructions"] = release_instructions(
            result["channel"], result["latest"], result["state"],
            ambiguous=(installation.running
                       if installation is not None and installation.ambiguous else None))
        result["checking"] = bool(checker is not None and checker.is_checking())
        result["can_check"] = bool(checker is not None and checker.enabled)
        result["next_check_at"] = checker.next_check_at() if checker is not None else None
        result["running_note"] = (
            "version_ambiguous" if installation is not None and installation.ambiguous
            else None)
        return result

    def request_release_check(self):
        """Queue a background registry check. Returns ``(outcome, retry_after)``."""
        checker = self.release_checker
        if checker is None:
            return "disabled", None
        return checker.request_check()

    def log_view(self, limit=None, level="DEBUG", text=""):
        """The recent log lines for the admin page, newest first, with their times."""
        view = self.logs.view(limit if limit is not None else 100, level, text)
        view["lines"] = [dict(line, time=_stamp(line["time"])) for line in view["lines"]]
        view["level"] = self.config["logging"]["level"]
        return view

    def _alert_state_view(self, sources):
        """Who is not alerting, and why: the Status tab's alert list."""
        settings = self.alerts_state()
        master = settings["enabled"]
        rows = []
        for source in sources:
            alerts = source.get("alerts") or {}
            if alerts.get("master_off") or not alerts.get("enabled"):
                rows.append({"id": source["id"], "name": source["name"],
                             "enabled": alerts.get("enabled", True),
                             "service_enabled": alerts.get("service_enabled", True),
                             "master_off": alerts.get("master_off", False),
                             "override_ignored": alerts.get("override_ignored", False)})
        return {"master": master, "master_off": not master, "services": rows}

    @staticmethod
    def _account_health(source, snapshot):
        """One account's health: when it was last read, and its last problem."""
        quota = source.get("quota") or {}
        problem = "none"
        if snapshot is None:
            problem = "no reading yet"
        elif snapshot.error:
            problem = str(snapshot.error)
        elif quota.get("error"):
            problem = str(quota["error"])
        return {"id": source["id"], "name": source["name"], "type": source["type"],
                "enabled": source["enabled"], "status": source["status"],
                "last_collection": iso(snapshot.updated_at) if snapshot else None,
                "last_problem": problem}

    @staticmethod
    def _readers(sources):
        """The local readers: the activity reader and the usage cache reader."""
        rows = []
        for source in sources:
            quota = source.get("quota") or {}
            config = source.get("config") or {}
            activity = quota.get("local_activity")
            if isinstance(activity, dict):
                rows.append({"account": source["name"], "reader": "local activity",
                             "state": _activity_state(activity),
                             "detail": _activity_detail(activity)})
            elif config.get("activity_logs"):
                rows.append({"account": source["name"], "reader": "local activity",
                             "state": "unavailable", "detail": "no reading yet"})
            cache = quota.get("usage_cache")
            if isinstance(cache, dict):
                rows.append({"account": source["name"], "reader": "usage cache",
                             "state": _cache_state(cache),
                             "detail": _cache_detail(cache)})
            elif config.get("usage_cache"):
                rows.append({"account": source["name"], "reader": "usage cache",
                             "state": "unavailable", "detail": "no reading yet"})
        return rows

    @staticmethod
    def _collectors(sources):
        """The remote collectors: when each account last received a report."""
        rows = []
        for source in sources:
            ingest = source.get("ingest")
            if not isinstance(ingest, dict):
                continue
            seen = ingest.get("collector_seen_at")
            update = ingest.get("collector_update")
            state = "reporting" if seen else "no reports yet"
            if seen and update == "available":
                state = "update available"
            elif seen and update == "newer":
                state = "newer than this daemon"
            rows.append({"account": source["name"],
                         "host": ingest.get("collector_host") or "unknown",
                         "last_seen": seen,
                         "version": ingest.get("collector_version"),
                         "latest": ingest.get("collector_latest"),
                         "state": state})
        return rows

    def save_observation(self, record, snapshot):
        # An in-flight probe cannot restore stale data after an edit/disable/delete.
        with self.storage._lock:
            current = next((row for row in self.storage.source_instances()
                            if row["id"] == record["id"]), None)
            if current and current["enabled"] and current["revision"] == record["revision"]:
                self.storage.save_snapshot(snapshot)


class ManagedCollector:
    def __init__(self, manager, config, stop_event):
        self.manager = manager
        self.interval = config["daemon"]["poll_interval"]
        self.stop_event = stop_event
        self.thread = None
        self._cache = {}

    def collect_once(self):
        entries = [row for row in self.manager.storage.source_instances() if row["enabled"]]
        active_ids = {row["id"] for row in entries}
        self._cache = {key: value for key, value in self._cache.items() if key in active_ids}
        work = []
        for record in entries:
            cache = self._cache.get(record["id"])
            if cache is None or cache[0] != record["revision"]:
                try:
                    cls = source_type(record["type"])
                    settings = dict(record["config"])
                    if cls.secret_field and record["credential"] is not None:
                        settings[cls.secret_field] = self.manager.cipher.decrypt(
                            record["id"], record["credential"])
                    source = cls(settings)
                except Exception:
                    self.manager.save_observation(record, SourceSnapshot(
                        record["id"], "offline",
                        error="source type unavailable or configuration invalid"))
                    continue
                source.id, source.name = record["id"], record["name"]
                source.attach_storage(self.manager.storage)
                cache = (record["revision"], source)
                self._cache[record["id"]] = cache
            work.append((record, cache[1]))

        def collect(item):
            record, source = item
            if not self.stop_event.is_set():
                snapshot = source.collect_safely()
                self.manager.save_observation(record, snapshot)

        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="source-probe") as pool:
            list(pool.map(collect, work))

    def _run(self):
        import logging
        next_poll = time.monotonic() + self.interval
        while not self.stop_event.is_set():
            changed = self.manager.changed.wait(0.25)
            if self.stop_event.is_set():
                break
            if changed or time.monotonic() >= next_poll:
                self.manager.changed.clear()
                try:
                    self.collect_once()
                except Exception:
                    # Avoid logging a provider or decryption error containing secret material.
                    logging.getLogger(__name__).error("managed source collection failed")
                next_poll = time.monotonic() + self.interval

    def start(self):
        self.thread = threading.Thread(target=self._run, name="managed-sources", daemon=True)
        self.thread.start()

    def join(self):
        if self.thread is not None:
            self.thread.join()
