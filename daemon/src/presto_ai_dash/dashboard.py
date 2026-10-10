"""Render the stable three-card API response from configured sources."""

from . import __version__
from .models import Card, iso, overall_status, severity


def build_status_payload(snapshots):
    snapshot = snapshots.get("fake")
    if snapshot:
        return _fake_payload(snapshot)

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


def build_instance_payload(snapshots, instances):
    """Keep device navigation stable and expose each instance without overlapping totals."""
    enabled = [row for row in instances if row["enabled"]]
    reporting = {row["id"]: snapshots[row["id"]] for row in enabled if row["id"] in snapshots}
    fake = next((row for row in enabled if row["type"] == "fake"), None)
    if fake:
        payload = _fake_payload(reporting.get(fake["id"]))
    else:
        aliases = {}
        for type_id in ("ollama", "litellm"):
            matching = [row for row in enabled if row["type"] == type_id]
            if len(matching) == 1 and matching[0]["id"] in reporting:
                aliases[type_id] = reporting[matching[0]["id"]]
        payload = build_status_payload(aliases)
        for index, type_id in enumerate(("ollama", "litellm"), start=1):
            matching = [row for row in enabled if row["type"] == type_id]
            if not matching:
                payload["cards"][index] = Card(
                    type_id, payload["cards"][index]["title"], "offline",
                    primary="Not configured", secondary="Source disabled").to_dict()
            elif len(matching) > 1:
                states = [Card(row["id"], row["name"],
                               reporting[row["id"]].status if row["id"] in reporting else "offline")
                          for row in matching]
                payload["cards"][index] = Card(
                    type_id, payload["cards"][index]["title"], overall_status(states),
                    primary="%d configured sources" % len(matching),
                    secondary="Separate accounts; usage is not summed",
                    detail=["%s: %s" % (card.title, card.status) for card in states]).to_dict()
            elif len(matching) == 1:
                row = matching[0]
                if row["id"] in reporting:
                    payload["cards"][index]["secondary"] = (
                        row["name"] + " · " + payload["cards"][index]["secondary"])
                else:
                    payload["cards"][index] = Card(
                        type_id, payload["cards"][index]["title"], "offline",
                        primary="Awaiting first report", secondary=row["name"]).to_dict()
        states = [Card(row["id"], row["name"],
                       reporting[row["id"]].status if row["id"] in reporting else "offline")
                  for row in enabled]
        status = overall_status(states)
        all_alerts = [alert for snapshot in reporting.values() for alert in snapshot.alerts]
        worst = max(all_alerts, key=lambda alert: severity(alert.get("severity"))) if all_alerts else None
        payload["overall_status"] = status
        payload["updated_at"] = iso(max((snapshot.updated_at for snapshot in reporting.values()),
                                       default=None))
        payload["cards"][0] = Card(
            "dash", "AI Dash", status, primary="%d live sources" % len(enabled),
            secondary="Configured source status",
            detail=["%s: %s" % (card.title, card.status) for card in states],
            alert={"severity": worst["severity"], "message": worst["message"]} if worst else None
        ).to_dict()
    payload["sources"] = [
        {"id": row["id"], "type": row["type"], "name": row["name"],
         "status": reporting[row["id"]].status if row["id"] in reporting else "offline",
         "updated_at": iso(reporting[row["id"]].updated_at) if row["id"] in reporting else None,
         "metrics": reporting[row["id"]].metrics if row["id"] in reporting else {},
         "alerts": reporting[row["id"]].alerts if row["id"] in reporting else []}
        for row in enabled
    ]
    return payload
