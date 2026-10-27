"""The one demo: sample readings for the preview, the daemon and the docs.

Every payload here is built by the daemon's own builders - the same
``dashboard.build_instance_payload``, ``dashboard.device_view`` and
``dashboard.build_card_details`` that serve a real daemon - so the demo can
differ from live only in where the readings come from. Nothing is
hand-written JSON.

The scenario is a fixed loop: ``LOOP_SECONDS`` seconds, sampled every
``STEP_SECONDS``. ``SCENES`` names the part of the loop, and the comment
beside it says what each scene exists to show. Six accounts climb from calm
to their limits, one reading goes stale, two accounts go offline and
everything recovers.

Every account id starts with ``demo-``, so a demo reading can never be
mistaken for, or collide with, a real saved account. The module imports only
the standard library and the payload builders: it must import in a plain
Python environment (the docs build uses it), so it never touches storage,
the API server or the admin page, and it never contacts a provider.

This is sample data. It is not a provider client and takes no action on any
account.
"""

from datetime import timedelta

from . import dashboard, quotas
from .alert_policy import resolve_alert_settings
from .models import SourceSnapshot, now_utc

# The loop: 90 seconds, sampled every 3 seconds.
LOOP_SECONDS = 90
STEP_SECONDS = 3

# (scene name, first second of the scene). Ordered; the last one runs to the
# end of the loop. Each scene is there for a reason:
#   calm       - everything healthy, one local source active
#   climbing   - readings progress without any status changing
#   warning    - a stale reading raises its service to warning
#   near_limit - a quota window crosses the warning percent (80)
#   critical   - a quota window crosses the critical percent (95)
#   rate_limited - a quota window reaches 100%
#   offline    - two services stop reporting
#   recovery   - every reading comes back to normal
SCENES = (
    ("calm", 0),
    ("climbing", 12),
    ("warning", 30),
    ("near_limit", 42),
    ("critical", 51),
    ("rate_limited", 60),
    ("offline", 69),
    ("recovery", 78),
)

# The statuses the loop reaches. Every status in models.STATUS_ORDER is
# produced by the real path, so none is exempt.
UNREACHABLE_STATUSES = ()

# The settings the payload is built with: the defaults a daemon uses, so the
# two threshold ticks and the status decisions are the real ones.
ALERT_SETTINGS = {"warning_percent": 80, "critical_percent": 95, "enabled": True}
# What the demo tells the device about alerts when the daemon gives it nothing
# better. A running daemon passes its real settings (see demo_payload), so the
# demo chirps, dims and mutes exactly as the product would; the sound stays off
# unless the person turned it on.
DEVICE_ALERTS = {"quiet": False, "sound": False, "pop_to_top": True,
                 "enabled": True, "mute_hours": 4}
DEVICE_DISPLAY = {"overflow_rows": 1}

# One account per provider type, in the order the demo shows them.
ACCOUNTS = (
    ("demo-opencode", "opencode_go", "OpenCode Go"),
    ("demo-codex", "codex", "Codex"),
    ("demo-claude", "claude", "Claude"),
    ("demo-ollama-cloud", "ollama_cloud", "Ollama Cloud"),
    ("demo-ollama", "ollama", "Ollama"),
    ("demo-litellm", "litellm", "LiteLLM"),
)

# The ids the demo really produces. A request that names any other id is not
# the demo's, so it is refused rather than remembered.
ACCOUNT_IDS = tuple(account_id for account_id, _type, _name in ACCOUNTS)


def is_demo_id(source_id):
    """Whether this is one of the demo's own account ids."""
    return source_id in ACCOUNT_IDS

# Scenes where the local Ollama has a model loaded, which is what makes its
# card read "active" in the real source. The model is unloaded for the
# recovery scene, so the loop also shows an all-quiet Home.
OLLAMA_LOADED_SCENES = ("calm", "climbing", "warning", "near_limit")

# Each table maps a scene to the percentage at its first second and at its
# last second; the value is interpolated in between, so numbers climb in
# small steps instead of jumping. A jump between scenes is deliberate.

# Claude drives the threshold ladder: below the warning, then across 80,
# across 95 and to 100. It recovers at the end of the loop.
CLAUDE_FIVE_HOUR = {
    "calm": (12, 14), "climbing": (14, 55), "warning": (55, 72),
    "near_limit": (80, 94), "critical": (95, 99), "rate_limited": (100, 100),
    "offline": (100, 100), "recovery": (18, 12),
}
CLAUDE_WEEKLY = {
    "calm": (28, 30), "climbing": (30, 44), "warning": (44, 52),
    "near_limit": (52, 62), "critical": (62, 66), "rate_limited": (66, 68),
    "offline": (68, 68), "recovery": (22, 28),
}

# OpenCode Go climbs alongside, but never leaves the comfortable range: the
# demo shows one quiet account next to a busy one.
OPENCODE_FIVE_HOUR = {
    "calm": (4, 6), "climbing": (6, 18), "warning": (18, 26),
    "near_limit": (26, 34), "critical": (34, 40), "rate_limited": (40, 44),
    "offline": (44, 46), "recovery": (8, 4),
}
OPENCODE_WEEKLY = {
    "calm": (3, 4), "climbing": (4, 12), "warning": (12, 17),
    "near_limit": (17, 22), "critical": (22, 25), "rate_limited": (25, 27),
    "offline": (27, 28), "recovery": (6, 3),
}
OPENCODE_MONTHLY = {
    "calm": (5, 6), "climbing": (6, 16), "warning": (16, 22),
    "near_limit": (22, 28), "critical": (28, 32), "rate_limited": (32, 34),
    "offline": (34, 35), "recovery": (7, 5),
}

# Codex stays low; what the demo shows for it is a stale reading in the
# warning scene, not usage.
CODEX_WEEKLY = {
    "calm": (1, 2), "climbing": (2, 3), "warning": (3, 3),
    "near_limit": (3, 4), "critical": (4, 4), "rate_limited": (4, 5),
    "offline": (5, 5), "recovery": (2, 1),
}

# Cloud has a real monthly credit meter and separate purchased funds. Claude
# drives the threshold ladder. In "critical" Cloud exhausts its included
# allowance but has purchased funds. Its month refills in "rate_limited",
# while Claude really is blocked: the accounts reset independently.
OLLAMA_MONTHLY = {
    "calm": (14, 16), "climbing": (16, 32), "warning": (32, 40),
    "near_limit": (40, 52), "critical": (100, 100), "rate_limited": (0, 6),
    "offline": (6, 6), "recovery": (14, 12),
}


def scene_at(t):
    """The scene name at loop second ``t``."""
    return _scene_span(_second(t))[0]


def scene_second(name):
    """The loop second a scene starts at. KeyError for an unknown name."""
    for candidate, start in SCENES:
        if candidate == name:
            return start
    raise KeyError(name)


def _second(t):
    return int(t) % LOOP_SECONDS


def _scene_span(second):
    """The scene holding a loop second: its name and its first/last seconds."""
    for index, (name, start) in enumerate(SCENES):
        end = SCENES[index + 1][1] if index + 1 < len(SCENES) else LOOP_SECONDS
        if start <= second < end:
            return name, start, end
    # Unreachable for a second inside the loop; the first scene is the fallback.
    return SCENES[0][0], SCENES[0][1], SCENES[1][1]


def _level(table, second):
    """The percentage at this loop second, interpolated inside its scene."""
    name, start, end = _scene_span(second)
    low, high = table[name]
    if end <= start:
        return high
    return int(round(low + (high - low) * (second - start) / float(end - start)))


def _observed(now):
    """When the sample readings were taken: a couple of minutes ago."""
    return now - timedelta(minutes=2)


def _opencode(second, now):
    observed = _observed(now)
    report = quotas.quota_report("opencode", "OpenCode Go", [
        quotas.quota_window("rolling", "5h", _level(OPENCODE_FIVE_HOUR, second),
                            now + timedelta(hours=3), observed, 300),
        quotas.quota_window("weekly", "Weekly", _level(OPENCODE_WEEKLY, second),
                            now + timedelta(days=5), observed, 10080),
        quotas.quota_window("monthly", "Monthly", _level(OPENCODE_MONTHLY, second),
                            now + timedelta(days=4), observed, None),
    ], 180)
    return SourceSnapshot("demo-opencode", "ok", now, {"quota": report})


def _codex(second, now):
    # In the warning scene the last reading is an hour old: the report ages it
    # at read time and the service goes to warning, exactly as it would live.
    stale = _scene_span(second)[0] == "warning"
    observed = now - timedelta(hours=1) if stale else now - timedelta(minutes=1)
    report = quotas.quota_report("openai", "Codex", [
        quotas.quota_window("codex:primary", "Weekly", _level(CODEX_WEEKLY, second),
                            now + timedelta(days=6), observed, 10080)], 180)
    report["windows"].append(quotas.quota_window(
        # A known bucket with no reading stays visible as "--"; the unknown
        # optional bucket in other scenes stays out of Home, as it does live.
        "codex:secondary", "5h" if stale else "Secondary", None, None, observed,
        300 if stale else None,
        state="unavailable"))
    report["reset_credits"] = {"available": 3, "expires_at": [
        (now + timedelta(days=2)).isoformat(),
        (now + timedelta(days=9)).isoformat(),
        (now + timedelta(days=17)).isoformat()]}
    report["credits"] = {"balance": "0", "unlimited": False}
    return SourceSnapshot("demo-codex", "ok", now, {"quota": report})


def _claude(second, now):
    observed = _observed(now)
    report = quotas.quota_report("anthropic", "Claude", [
        quotas.quota_window("five_hour", "5h", _level(CLAUDE_FIVE_HOUR, second),
                            now + timedelta(hours=3), observed, 300,
                            source="usage cache"),
        quotas.quota_window("seven_day", "Weekly", _level(CLAUDE_WEEKLY, second),
                            now + timedelta(days=2), observed, 10080,
                            source="usage cache"),
    ], 900)
    report.update(
        note="Sample data for the demo: the quota line and the activity line "
             "name where each number comes from.",
        usage_cache={"available": True, "reason": None, "message": "ok",
                     "path": "~/.claude.json",
                     "fetched_at": (now - timedelta(minutes=2)).isoformat(),
                     "credit": {"limit": 100, "used": 0, "currency": "USD",
                                "resets_at": (now + timedelta(days=16)).isoformat()},
                     "account_changed": False,
                     "refreshes": [], "since": now.isoformat()},
        local_activity={"available": True, "complete": True,
                        "last_request_at": (now - timedelta(minutes=3)).isoformat(),
                        "last_model": "claude-sonnet",
                        "last_entrypoint": "VS Code",
                        "requests_5h": 60 + second // 3,
                        "tokens_5h": 1200000 + second * 12000,
                        "tokens_7d": 19000000 + second * 40000},
        collector_seen_at=None, collector_host=None, collector_version=None,
        last_progress_at=None, active_seconds=300, warn_on_stale=False)
    return SourceSnapshot("demo-claude", "ok", now, {"quota": report})


def _ollama_cloud(second, now, offline):
    # A failed poll retains the last successful reading, just like the client.
    reading_second = scene_second("offline") - STEP_SECONDS if offline else second
    reading_time = now - timedelta(seconds=second - reading_second)
    observed = reading_time
    percent = _level(OLLAMA_MONTHLY, reading_second)
    allowance = 60
    used = round(allowance * percent / 100, 2)
    refilled = _scene_span(reading_second)[0] in ("rate_limited", "recovery")
    start = reading_time - timedelta(days=0 if refilled else 16)
    reset = reading_time + timedelta(days=30 if refilled else 14)
    error = "Sample provider did not reply" if offline else None
    report = quotas.quota_report("ollama", "Ollama Cloud", [
        quotas.quota_window("monthly_credits", "Monthly credits", percent, reset,
                            observed, used_amount=used, limit_amount=allowance,
                            currency="USD")], 180)
    report["windows"][0]["exhaustion_blocks"] = False
    report["cloud_balance"] = {
        "included_remaining_usd": round(allowance - used, 2),
        "purchased_remaining_usd": 12.5, "observed_at": observed.isoformat(),
        "period_from": start.isoformat(), "period_until": reset.isoformat()}
    report["activity"] = {
        "range": "7d",
        "from": (reading_time.replace(hour=0, minute=0, second=0, microsecond=0)
                 - timedelta(days=7)).isoformat(),
        "until": reading_time.isoformat(), "observed_at": observed.isoformat(),
        "request_count": 297 + reading_second // 3,
        "usage_usd": round(2 + reading_second * 0.025, 2),
        "input_tokens": 22000000 + reading_second * 12000,
        "cached_input_tokens": 1000000 + reading_second * 6000,
        "output_tokens": 231608 + reading_second * 200, "error": error}
    report["note"] = "Rolling 7-day activity is separate from monthly included credits."
    report["error"] = error
    return SourceSnapshot("demo-ollama-cloud", "offline" if offline else "ok",
                          now, {"quota": report}, error=error)


def _ollama(second, now, loaded):
    metrics = {"version": "0.12.1", "available_models": 5,
               "loaded_model": "llama3" if loaded else None,
               "last_measured": None,
               "last_measured_tokens_per_sec": 38.4 if loaded else None}
    return SourceSnapshot("demo-ollama", "active" if loaded else "ok", now, metrics)


def _litellm(second, now, offline):
    metrics = {"online": not offline, "database": True,
               "requests_today": 12 + second // 3,
               "tokens_today": 340 + second * 11,
               "errors_today": 0, "top_model": "gpt-4o",
               "spend_today": round(0.5 + second * 0.01, 2)}
    error = "Sample provider did not reply" if offline else None
    return SourceSnapshot("demo-litellm", "offline" if offline else "ok",
                          now, metrics, error=error)


def demo_pairs(t, now=None):
    """The six demo accounts at loop second ``t``: ``(row, snapshot)`` pairs."""
    now = now or now_utc()
    second = _second(t)
    scene = _scene_span(second)[0]
    offline = scene == "offline"
    loaded = scene in OLLAMA_LOADED_SCENES
    snapshots = {
        "demo-opencode": _opencode(second, now),
        "demo-codex": _codex(second, now),
        "demo-claude": _claude(second, now),
        "demo-ollama-cloud": _ollama_cloud(second, now, offline),
        "demo-ollama": _ollama(second, now, loaded),
        "demo-litellm": _litellm(second, now, offline),
    }
    return [({"id": account_id, "type": type_id, "name": name, "enabled": True},
             snapshots[account_id]) for account_id, type_id, name in ACCOUNTS]


def _builders(pairs):
    return ({row["id"]: snapshot for row, snapshot in pairs},
            [row for row, _ in pairs])


def demo_payload(t, now=None, alert_overrides=None, device_alerts=None):
    """The status payload a daemon in demo mode serves, at loop second ``t``.

    Built by the real builders and cut to the device view, so it is exactly
    the shape (and size) a real daemon sends; ``demo`` marks it as sample data.
    ``device_alerts`` is the daemon's own alert settings for the device (sound,
    quiet hours, pop to top, the master switch, the mute length): the demo shows
    the product as this person has set it up, so it can demo the sound too.
    """
    now = now or now_utc()
    snapshots, instances = _builders(demo_pairs(t, now))
    overrides = alert_overrides or {}
    resolutions = {row["id"]: resolve_alert_settings(
        ALERT_SETTINGS, {"enabled": overrides.get(row["id"], True)})
        for row in instances}
    payload = dashboard.build_instance_payload(snapshots, instances,
                                               ALERT_SETTINGS, now, resolutions)
    payload = dashboard.device_view(payload)
    payload["display"] = dict(DEVICE_DISPLAY)
    payload["alerts"] = dict(DEVICE_ALERTS)
    for key, value in (device_alerts or {}).items():
        if key in DEVICE_ALERTS and type(value) is type(DEVICE_ALERTS[key]):
            payload["alerts"][key] = value
    payload["demo"] = True
    return payload


def demo_card_detail(card_id, t, now=None):
    """One card's lazy detail at loop second ``t``, or None for an unknown id."""
    now = now or now_utc()
    snapshots, instances = _builders(demo_pairs(t, now))
    details = dashboard.build_card_details(snapshots, instances,
                                           ALERT_SETTINGS, now)
    return details.get(card_id)
