"""Render Home and per-account cards, preserving the original fake demo."""

from . import __version__
from .models import Card, iso, now_utc, overall_status, severity
from .quotas import (QUOTA_SCHEMA_VERSION, age_label, compact_age, compact_tokens,
                     estimated_weekly_reset, present_quota, present_snapshot, timestamp)

# The device draws at most six detail lines and truncates a line that is
# longer than this many characters (presto/ui.py DETAIL_MAX). The daemon keeps
# its own lines inside the same budget so nothing is cut on the screen.
DETAIL_LINE_COUNT = 6
DETAIL_LINE_CHARS = 34


def _short_label(value, limit=10):
    """Bound provider labels so a reset detail keeps its complete timestamp."""
    value = str(value)
    if len(value) <= limit:
        return value
    first = value.split(" ", 1)[0]
    if len(first) <= limit:
        return first
    if limit <= 3:
        return value[:limit]
    return value[:limit - 3] + "..."


def _source_short(source):
    """A short label for where a reading came from, for the narrow card."""
    return "cache" if source == "usage cache" else "export"


def _money(value):
    """A dollar amount without noise: 100, 12.50."""
    if float(value) == int(value):
        return "%d" % int(value)
    return "%.2f" % value


def _credit_line(report):
    """The included credit the usage cache reports, when it reports one."""
    cache = report.get("usage_cache")
    if not isinstance(cache, dict):
        return None
    credit = cache.get("credit")
    if not isinstance(credit, dict):
        return None
    limit = credit.get("limit")
    used = credit.get("used")
    if type(limit) not in (int, float) or type(used) not in (int, float):
        return None
    expires = ""
    resets_at = credit.get("resets_at")
    if resets_at:
        try:
            expires = " (exp %s)" % timestamp(resets_at).strftime("%m-%d")
        except (TypeError, ValueError):
            expires = ""
    return "Credit: $%s of $%s%s" % (_money(limit - used), _money(limit), expires)


def _trail_lines(report, now):
    """Detail lines about the observation trail: estimates, use, quota age, collector.

    They follow the per-window reset lines and the six-line cap drops them
    bottom-first, so a full reset list always keeps every reset line. The
    quota line comes first among them and names the source of the reading, so
    a number is never shown as if another source had produced it. With the
    Phase 5 local activity reader working, its estimated weekly reset,
    last-used line and token totals follow; while the reader's cold scan is
    unfinished the totals are lower bounds, so the line says "scanning"
    instead of numbers. "Quota via ..." needs a measured window: a
    placeholder window has no age to show. Every line says where its number
    comes from.
    """
    def ago(moment):
        return "%s ago" % age_label((now - timestamp(moment)).total_seconds())

    lines = []
    activity = report.get("local_activity")
    reading = isinstance(activity, dict) and activity.get("available")
    measured = [window for window in report["windows"]
                if window.get("used_percent") is not None]
    if measured:
        # The age of the quota reading, not of the account, and the source of
        # the newest reading: the two belong together.
        newest = max(measured, key=lambda window: timestamp(window["observed_at"]))
        lines.append("Quota via %s, %s old" % (
            _source_short(newest.get("source")),
            age_label((now - timestamp(newest["observed_at"])).total_seconds())))
    if reading:
        estimate = estimated_weekly_reset(report["windows"], now)
        if estimate:
            lines.append("Wk reset ~%s (est)"
                         % age_label((estimate - now).total_seconds()))
        last = activity.get("last_request_at")
        if last:
            lines.append("Last used: %s (%s)" % (
                "%s ago" % compact_age((now - timestamp(last)).total_seconds()),
                activity["last_entrypoint"]))
        else:
            lines.append("Last used: unavailable")
        if activity.get("complete", True):
            five = compact_tokens(activity.get("tokens_5h"))
            seven = compact_tokens(activity.get("tokens_7d"))
            if five is None or seven is None:
                lines.append("Log tokens 5h/7d: unknown")
            else:
                # The label says where the numbers come from: the local logs.
                lines.append("Log tokens 5h/7d: %s/%s" % (five, seven))
        else:
            # The cold scan is still reading the week: the totals are lower
            # bounds, and lower bounds are not shown as measurements.
            lines.append("Log tokens 5h/7d: scanning")
    if not reading:
        replied = report.get("last_progress_at")
        lines.append("Last reply: %s" % ("unavailable" if replied is None else ago(replied)))
    credit = _credit_line(report)
    if credit:
        lines.append(credit)
    seen = report.get("collector_seen_at")
    if seen is None:
        lines.append("Reports: never")
    else:
        age = ago(seen)
        host = _short_label(report.get("collector_host") or "unknown",
                            max(1, 30 - len("Reports: ") - 1 - len(age)))
        lines.append("Reports: %s %s" % (host, age))
    return lines


def _expiry_age(moment, now):
    """The time left until an expiry, spelled out unless the line would not fit."""
    seconds = (moment - now).total_seconds()
    spelled = age_label(seconds)
    when = moment.strftime("%m-%d %H:%MZ")
    if len("Next exp: %s (%s)" % (when, spelled)) <= DETAIL_LINE_CHARS:
        return spelled
    return compact_age(seconds)


def _reset_credit_lines(report, now):
    """Banked reset credits, the credit balance, and where they came from.

    A provider that reports no count says nothing: an unknown count is never
    shown as a count of zero. The window lines always come first; these lines
    are the ones that go when the six-line card is full.
    """
    lines = []
    credits = report.get("reset_credits")
    if isinstance(credits, dict):
        available = credits.get("available")
        if type(available) is int:
            lines.append("Resets avail: %d" % available)
            expiries = credits.get("expires_at")
            moments = []
            if isinstance(expiries, list):
                for value in expiries:
                    try:
                        moments.append(timestamp(value))
                    except (TypeError, ValueError):
                        continue
            if moments:
                earliest = min(moments)
                lines.append("Next exp: %s (%s)" % (earliest.strftime("%m-%d %H:%MZ"),
                                                    _expiry_age(earliest, now)))
    balance = report.get("credits")
    if isinstance(balance, dict):
        if balance.get("unlimited") is True:
            lines.append("Credits: unlimited")
        elif isinstance(balance.get("balance"), str):
            lines.append("Credits: %s" % balance["balance"])
    if lines:
        lines.append("Source: local %s app-server" % (report.get("product") or "Codex"))
    return lines


def build_status_payload(snapshots):
    snapshot = snapshots.get("fake")
    if snapshot:
        return _fake_payload(snapshot)

    if any(key not in ("ollama", "litellm") for key in snapshots):
        from .sources import source_type
        instances = []
        for key in snapshots:
            try:
                name = source_type(key).name
            except KeyError:
                name = key
            instances.append({"id": key, "type": key, "name": name, "enabled": True})
        return build_instance_payload(snapshots, instances)

    live = {key: snapshots[key] for key in ("ollama", "litellm")
            if key in snapshots}
    if not live:
        return _fake_payload(None)

    def value(metrics, key):
        result = metrics.get(key)
        return "unavailable" if result is None else str(result)

    def alert_for(source):
        if not source or not source.alerts:
            return None
        worst = max(source.alerts,
                    key=lambda item: severity(item.get("severity")))
        return {"severity": worst["severity"], "message": worst["message"]}

    ollama = live.get("ollama")
    ollama_metrics = ollama.metrics if ollama else {}
    if ollama:
        loaded = ollama_metrics.get("loaded_model") or "No model loaded"
        speed = value(ollama_metrics, "last_measured_tokens_per_sec")
        measured = ("Last measured: %s tok/s" % speed
                    if speed != "unavailable" else "Last measured: unavailable")
        details = [measured]
        if ollama.error:
            details.append("Error: " + ollama.error)
        ollama_card = Card(
            "ollama", "Ollama", ollama.status, primary=loaded,
            secondary="%s available · version %s" % (
                value(ollama_metrics, "available_models"),
                value(ollama_metrics, "version")),
            detail=details,
            alert=alert_for(ollama))
    else:
        ollama_card = Card("ollama", "Ollama", "offline",
                           primary="Not configured", secondary="Source disabled")

    litellm = live.get("litellm")
    litellm_metrics = litellm.metrics if litellm else {}
    if litellm:
        request_count = value(litellm_metrics, "requests_today")
        primary = (request_count + " requests today"
                   if request_count != "unavailable" else "Usage unavailable")
        details = ["Top model: %s" % value(litellm_metrics, "top_model"),
                   "Errors today: %s" % value(litellm_metrics, "errors_today"),
                   "Database: %s" % value(litellm_metrics, "database")]
        if litellm.error:
            details.append("Error: " + litellm.error)
        litellm_card = Card(
            "litellm", "LiteLLM", litellm.status, primary=primary,
            secondary="%s tokens · spend %s" % (
                value(litellm_metrics, "tokens_today"),
                ("$%.4f" % litellm_metrics["spend_today"]
                 if isinstance(litellm_metrics.get("spend_today"), (int, float))
                 else value(litellm_metrics, "spend_today"))),
            detail=details,
            alert=alert_for(litellm))
    else:
        litellm_card = Card("litellm", "LiteLLM", "offline",
                            primary="Not configured", secondary="Source disabled")

    statuses = [Card(key, key, source.status) for key, source in live.items()]
    summary_status = overall_status(statuses)
    alerts = [alert_for(source) for source in live.values() if source.alerts]
    summary_alert = (max(alerts, key=lambda item: severity(item["severity"]))
                     if alerts else None)
    provider_names = " · ".join(
        ("Ollama" if key == "ollama" else "LiteLLM") for key in live)
    summary = Card(
        "dash", "AI Dash", summary_status,
        primary="%d live source%s" % (len(live), "" if len(live) == 1 else "s"),
        secondary=provider_names,
        detail=["%s: %s" % ("Ollama" if key == "ollama" else "LiteLLM",
                             source.status) for key, source in live.items()],
        alert=summary_alert)
    cards = [summary, ollama_card, litellm_card]
    updated = max((source.updated_at for source in live.values()), default=None)
    return {
        "app": "presto-ai-dash", "version": __version__,
        "updated_at": iso(updated),
        "overall_status": summary_status,
        "cards": [card.to_dict() for card in cards],
    }


def _fake_payload(snapshot):
    status = snapshot.status if snapshot else "offline"
    metrics = snapshot.metrics if snapshot else {}
    alert = None
    if snapshot and snapshot.alerts:
        worst = max(snapshot.alerts, key=lambda item: severity(item.get("severity")))
        alert = {"severity": worst["severity"], "message": worst["message"]}

    def metric(key):
        value = metrics.get(key)
        return "unavailable" if value is None else str(value)

    secondary = "Demo data: fake source" if snapshot else "No fake source reporting"
    cards = [
        Card("dash", "AI Dash", status,
             primary=metric("tokens_today") + " tokens today", secondary=secondary,
             detail=["Requests: " + metric("requests_today")], alert=alert),
        Card("ollama", "Ollama", status,
             primary=metric("tokens_per_sec") + " tok/s", secondary=secondary,
             detail=["Model: " + metric("top_model")], alert=alert),
        Card("litellm", "LiteLLM", status,
             primary=metric("requests_today") + " requests", secondary=secondary,
             detail=["Spend today: " + metric("spend_today"),
                     "Errors: " + metric("errors_today")], alert=alert),
    ]
    return {
        "app": "presto-ai-dash", "version": __version__,
        "updated_at": iso(snapshot.updated_at) if snapshot else None,
        "overall_status": overall_status(cards),
        "cards": [card.to_dict() for card in cards],
    }


def _account_card(row, snapshot, now=None):
    """Each configured account owns a card, including new connector types.

    ``now`` is the moment resets are judged against; a stale snapshot's own
    ``updated_at`` is the time of its last reading, not the present.
    """
    now = timestamp(now or now_utc())
    if snapshot is None:
        return Card(row["id"], row["name"], "offline", primary="Awaiting first report")
    report = snapshot.metrics.get("quota")
    if report:
        def reading(window):
            percent = window.get("used_percent")
            value = "%g%%" % percent if percent is not None else window["state"]
            if window["state"] == "stale":
                value += " (stale)"
            return window["label"] + ": " + value
        windows = report["windows"]
        primary = reading(windows[0]) if windows else "Usage unavailable"
        secondary = " / ".join(reading(w) for w in windows[1:])
        def reset(window):
            label = _short_label(window["label"])
            if not window["resets_at"]:
                return label + " reset: unavailable"
            # A passed reset says nothing about current use: flag it, don't assume 0%.
            when = window["resets_at"][5:16].replace("T", " ") + "Z"
            if timestamp(window["resets_at"]) <= now:
                return label + " passed " + when
            return label + " reset: " + when
        details = [reset(w) for w in windows]
        if "collector_seen_at" in report:
            # Claude accounts carry the observation trail: an old reading is
            # never presented as current, and a hint never reads as offline.
            # warn_on_stale (Phase 5) means the local activity reader is
            # working, so stale quota alone stays quiet and the hint speaks.
            status = snapshot.status
            if (report.get("hint") and report.get("warn_on_stale", True) and
                    severity(status) < severity("warning")):
                status = "warning"
            return Card(row["id"], row["name"], status, primary, secondary,
                        (details + _trail_lines(report, now) +
                         ([report["error"]] if report.get("error") else []))[:6])
        activity = report.get("activity")
        if activity:
            primary = "%s requests" % activity["request_count"]
            secondary = "$%.6f / %s tokens" % (activity["usage_usd"],
                          activity["input_tokens"] + activity["output_tokens"])
            details += ["Activity: " + activity["state"],
                        "Period: " + activity["from"][:10] + " - " + activity["until"][:10]]
        extra = _reset_credit_lines(report, now)
        errors = [report["error"]] if report.get("error") else []
        # The window lines are never dropped; the new lines go first.
        room = max(0, DETAIL_LINE_COUNT - len(details) - len(errors))
        details = details + extra[:room] + errors
        return Card(row["id"], row["name"], snapshot.status, primary, secondary, details)
    if row["type"] in ("ollama", "litellm"):
        template = build_status_payload({row["type"]: snapshot})["cards"][
            1 if row["type"] == "ollama" else 2]
        template.update(id=row["id"], title=row["name"])
        return Card(**template)
    return Card(row["id"], row["name"], snapshot.status, primary="Usage unavailable",
                secondary=row["type"], detail=[snapshot.error] if snapshot.error else [])


def build_instance_payload(snapshots, instances):
    """Home plus one card per enabled account, without overlapping totals."""
    enabled = [row for row in instances if row["enabled"]]
    reporting = {row["id"]: present_snapshot(snapshots[row["id"]])
                 for row in enabled if row["id"] in snapshots}
    fake = next((row for row in enabled if row["type"] == "fake"), None)
    if fake:
        payload = _fake_payload(reporting.get(fake["id"]))
    else:
        states = [_account_card(row, reporting.get(row["id"])) for row in enabled]
        status = overall_status(states)
        all_alerts = [alert for snapshot in reporting.values() for alert in snapshot.alerts]
        worst = max(all_alerts, key=lambda alert: severity(alert.get("severity"))) if all_alerts else None
        home = Card(
            "dash", "AI Dash", status, primary="%d live sources" % len(enabled),
            secondary="Configured source status",
            detail=["%s: %s" % (card.title, card.status) for card in states],
            alert={"severity": worst["severity"], "message": worst["message"]} if worst else None
        )
        payload = {"app": "presto-ai-dash", "version": __version__,
                   "overall_status": status,
                   "updated_at": iso(max((s.updated_at for s in reporting.values()), default=None)),
                   "card_order": "accounts",
                   "cards": [home.to_dict()] + [card.to_dict() for card in states]}
    card_status = {card.id: card.status for card in states} if not fake else {}
    payload["sources"] = [
        {"id": row["id"], "type": row["type"], "name": row["name"],
         "status": card_status.get(
             row["id"], reporting[row["id"]].status if row["id"] in reporting else "offline"),
         "updated_at": iso(reporting[row["id"]].updated_at) if row["id"] in reporting else None,
         "metrics": reporting[row["id"]].metrics if row["id"] in reporting else {},
         "alerts": reporting[row["id"]].alerts if row["id"] in reporting else []}
        for row in enabled
    ]
    payload["quota_schema_version"] = QUOTA_SCHEMA_VERSION
    for source in payload["sources"]:
        report = source["metrics"].get("quota")
        if report:
            source["quota"] = present_quota(report)
    return payload
