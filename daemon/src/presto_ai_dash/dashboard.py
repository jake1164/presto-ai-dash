"""Render Home and per-account cards, preserving the original fake demo."""

from datetime import datetime

from . import __version__
from .models import Card, iso, overall_status, severity
from .quotas import QUOTA_SCHEMA_VERSION, present_quota, present_snapshot


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


def _account_card(row, snapshot):
    """Each configured account owns a card, including new connector types."""
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
            if not window["resets_at"]:
                return window["label"] + " reset: unavailable"
            # A passed reset says nothing about current use: flag it, don't assume 0%.
            when = window["resets_at"][5:16].replace("T", " ") + "Z"
            if datetime.fromisoformat(window["resets_at"]) <= snapshot.updated_at:
                return window["label"] + " reset passed " + when
            return window["label"] + " reset: " + when
        details = [reset(w) for w in windows]
        activity = report.get("activity")
        if activity:
            primary = "%s requests" % activity["request_count"]
            secondary = "$%.6f / %s tokens" % (activity["usage_usd"],
                          activity["input_tokens"] + activity["output_tokens"])
            details += ["Activity: " + activity["state"],
                        "Period: " + activity["from"][:10] + " - " + activity["until"][:10]]
        if report.get("error"):
            details.append(report["error"])
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
    payload["sources"] = [
        {"id": row["id"], "type": row["type"], "name": row["name"],
         "status": reporting[row["id"]].status if row["id"] in reporting else "offline",
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
