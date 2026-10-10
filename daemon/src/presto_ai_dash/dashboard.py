"""Render the three M2 demo cards from the fake source only."""

from . import __version__
from .models import Card, iso, now_utc, overall_status, severity


def build_status_payload(snapshots):
    snapshot = snapshots.get("fake")
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
        "updated_at": iso(snapshot.updated_at if snapshot else now_utc()),
        "overall_status": overall_status(cards),
        "cards": [card.to_dict() for card in cards],
    }
