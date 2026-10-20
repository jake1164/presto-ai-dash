"""Database-backed source instances, secret updates, and live reconfiguration."""

from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

from .codex_login import CodexLoginManager
from .collectors import claude_statusline
from .config import ingest_enabled, overflow_rows
from .credentials import load_cipher
from .dashboard import build_instance_payload, device_view
from .sources import source_catalog, source_type
from .models import SourceSnapshot
from .quotas import claude_provenance, mark_passed_resets, present_quota, present_snapshot
from .storage import ConfigurationConflict

# The snippet URL is deliberately a placeholder: the user edits the address.
INGEST_URL_PLACEHOLDER = "http://REPLACE-WITH-DASH-ADDRESS:8787/api/v1/ingest"


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
    return result


class SourceConfiguration:
    def __init__(self, storage, config):
        self.storage = storage
        self.config = config
        self.changed = threading.Event()
        self._edit_lock = threading.RLock()
        marker = storage.configuration_marker()
        if marker is None and storage.source_instances():
            raise ValueError("credential store verifier is missing; restore the database backup")
        self.cipher = load_cipher(config["daemon"]["database"],
                                  config["admin"]["key_file"], existing=marker is not None)
        if marker is None:
            records = []
            for description in source_catalog():
                type_id = description["id"]
                section = config["sources"].get(type_id)
                if section is None:
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
    def list_sources(self):
        records = self.storage.source_instances()
        snapshots = self.storage.latest_snapshots()
        sources = []
        for record in records:
            public = {key: record[key] for key in ("id", "type", "name", "enabled", "config", "revision")}
            public["has_key"] = record["credential"] is not None
            if record["type"] == "claude":
                public["export_installed"] = self.storage.claude_bridge(record["id"]) is not None
            self._attach_ingest(public, record)
            snapshot = snapshots.get(record["id"])
            if snapshot:
                snapshot = present_snapshot(snapshot)
            public["status"] = snapshot.status if snapshot else "waiting"
            public["updated_at"] = snapshot.to_dict()["updated_at"] if snapshot else None
            if snapshot and snapshot.metrics.get("quota"):
                public["quota"] = mark_passed_resets(present_quota(snapshot.metrics["quota"]))
            if record["type"] == "claude":
                # Every Claude number says where it came from: the terminal
                # export, the local session logs, or the freshness hint.
                public["provenance"] = claude_provenance(public.get("quota"),
                                                         record.get("config"))
            sources.append(public)
        return {"types": source_catalog(), "sources": sources}

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
            "last_progress_at": observation["last_progress_at"] if observation else None}

    def ingest_state(self):
        """The route switch for the admin page; the environment locks the toggle."""
        return {"enabled": ingest_enabled(self.config, self.storage.ingest_enabled_setting()),
                "locked_by_environment": os.environ.get("DASH_INGEST_ENABLED") is not None}

    def set_ingest_enabled(self, data):
        if not isinstance(data, dict) or set(data) != {"enabled"} or \
                type(data["enabled"]) is not bool:
            raise ValueError("ingest needs an enabled boolean")
        if self.ingest_state()["locked_by_environment"]:
            raise ConfigurationConflict(
                "ingest is set by DASH_INGEST_ENABLED; edit the environment to change it")
        self.storage.save_ingest_enabled(data["enabled"])
        return self.ingest_state()

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

    def create_ingest_token(self, instance_id):
        """Mint or rotate one write-only push token, shown exactly once."""
        token = self.storage.create_ingest_token(instance_id)
        token_file = "claude-%s.token" % instance_id
        return {"token": token,
                "posix_snippet": claude_statusline.posix_snippet(
                    "/path/to/claude_statusline.py", INGEST_URL_PLACEHOLDER,
                    "~/.config/presto-ai-dash/" + token_file, "laptop"),
                "powershell_snippet": claude_statusline.powershell_snippet(
                    "C:/path/to/claude_statusline.py", INGEST_URL_PLACEHOLDER,
                    "C:/Users/USERNAME/presto-ai-dash/" + token_file, "laptop")}

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

    def full_status_payload(self):
        """Every field of every report: the superset the Presto's view is cut from."""
        with self.storage._lock:
            records = self.storage.source_instances()
            snapshots = self.storage.latest_snapshots()
        return build_instance_payload(snapshots, records)

    def status_payload(self):
        # The Presto parses the whole response in a few hundred KB of RAM, so it
        # gets only the fields it reads (dashboard.device_view).
        payload = device_view(self.full_status_payload())
        # The Home display settings travel with the payload, so a change
        # applies on the next poll without restarting the daemon.
        payload["display"] = {"overflow_rows": self.display_state()["overflow_rows"]}
        return payload

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
