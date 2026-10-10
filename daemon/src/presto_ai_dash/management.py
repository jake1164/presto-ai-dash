"""Database-backed source instances, secret updates, and live reconfiguration."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

from .credentials import load_cipher
from .dashboard import build_instance_payload
from .sources import source_catalog, source_type
from .models import SourceSnapshot


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

    def list_sources(self):
        records = self.storage.source_instances()
        snapshots = self.storage.latest_snapshots()
        sources = []
        for record in records:
            public = {key: record[key] for key in ("id", "type", "name", "enabled", "config", "revision")}
            public["has_key"] = record["credential"] is not None
            snapshot = snapshots.get(record["id"])
            public["status"] = snapshot.status if snapshot else "waiting"
            public["updated_at"] = snapshot.to_dict()["updated_at"] if snapshot else None
            sources.append(public)
        return {"types": source_catalog(), "sources": sources}

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

    def delete(self, instance_id, revision):
        if type(revision) is not int or revision < 1:
            raise ValueError("reload the source before deleting it")
        with self._edit_lock:
            self.storage.delete_source_instance(instance_id, revision)
            self.changed.set()

    def status_payload(self):
        with self.storage._lock:
            records = self.storage.source_instances()
            snapshots = self.storage.latest_snapshots()
        return build_instance_payload(snapshots, records)

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
