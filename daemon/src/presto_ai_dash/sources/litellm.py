"""The LiteLLM source.

Reports routed AI usage through the gateway. Endpoints used, all verified
against the current LiteLLM documentation:

- ``GET /health/liveliness``  - process liveness (no auth)
- ``GET /health/readiness``   - readiness and database state (no auth)
- ``GET /user/daily/activity`` - daily spend, tokens, requests, errors, top model

Notes that shaped this module:

- ``GET /daily_metrics`` was **removed** from the proxy. Daily spend and usage
  is now served by ``GET /user/daily/activity``.
- ``GET /health`` is **not used**: it runs a real test request against every
  configured model and therefore costs tokens.
- LiteLLM's API stability policy says only endpoints in the OpenAPI spec are a
  public contract. Everything else may change without notice, so this source is
  fail-soft: an unexpected status, a missing field or a 404 degrades that one
  metric to "unavailable" and never raises.
- Spend tracking requires a database. Without one, spend is *unavailable*, not
  zero, and the card must say so.
"""

import logging
from datetime import datetime, timedelta, timezone

from ..http_client import HttpError, HttpUnreachable, get_json, join_url
from ..models import Alert, SourceSnapshot
from . import Source, register

log = logging.getLogger(__name__)

UNAVAILABLE = "unavailable"


def _metric(metrics, key):
    """Read a value out of a LiteLLM metrics dict, or None."""
    if not isinstance(metrics, dict):
        return None
    value = metrics.get(key)
    return value


def _top_model(breakdown):
    """The model with the highest spend in a LiteLLM breakdown, or None."""
    if not isinstance(breakdown, dict):
        return None
    models = breakdown.get("models")
    if not isinstance(models, dict) or not models:
        return None

    best_name = None
    best_spend = None
    for name, entry in models.items():
        spend = None
        if isinstance(entry, dict):
            entry_metrics = entry.get("metrics")
            if isinstance(entry_metrics, dict):
                spend = entry_metrics.get("spend")
        try:
            spend = float(spend) if spend is not None else 0.0
        except (TypeError, ValueError):
            spend = 0.0
        if best_spend is None or spend > best_spend:
            best_name = name
            best_spend = spend
    return best_name


@register
class LiteLLMSource(Source):
    """Routed AI usage through a LiteLLM proxy."""

    id = "litellm"
    name = "LiteLLM"

    def __init__(self, config=None):
        super().__init__(config)
        self.base_url = str(self.config.get("base_url",
                                            "http://localhost:4000")).rstrip("/")
        self.api_key = str(self.config.get("api_key") or "")
        self.timeout = int(self.config.get("timeout", 5))

    # -- probes ------------------------------------------------------------

    def _headers(self):
        if not self.api_key:
            return {}
        return {"Authorization": "Bearer %s" % self.api_key}

    def _liveliness(self):
        return get_json(join_url(self.base_url, "/health/liveliness"),
                        timeout=self.timeout)

    def _readiness(self):
        return get_json(join_url(self.base_url, "/health/readiness"),
                        timeout=self.timeout)

    def _daily_activity(self, start_date, end_date):
        url = join_url(
            self.base_url,
            "/user/daily/activity?start_date=%s&end_date=%s"
            % (start_date, end_date))
        return get_json(url, timeout=self.timeout, headers=self._headers())

    # -- collection --------------------------------------------------------

    def collect(self):
        metrics = {}
        alerts = []

        # 1. Is the process up? This is the only thing that decides online vs
        #    offline, so it is checked first and on its own.
        try:
            self._liveliness()
        except HttpUnreachable as exc:
            return self.offline("cannot reach LiteLLM at %s: %s"
                                % (self.base_url, exc))
        except HttpError as exc:
            # A 503 during graceful shutdown is still "the proxy answered".
            if exc.status >= 500:
                log.warning("litellm liveness returned %s", exc.status)
                return SourceSnapshot(
                    source_id=self.id,
                    status="critical",
                    metrics={"online": False},
                    alerts=[Alert("critical",
                                  "LiteLLM returned HTTP %s" % exc.status)
                            .to_dict()],
                    error="liveness returned HTTP %s" % exc.status,
                )
            return self.offline("LiteLLM liveness returned HTTP %s" % exc.status)

        metrics["online"] = True

        # 2. Readiness, including database state. Not fatal if it fails.
        try:
            readiness = self._readiness()
            if isinstance(readiness, dict):
                db_state = readiness.get("db")
                if db_state is not None:
                    metrics["database"] = str(db_state)
        except (HttpError, HttpUnreachable) as exc:
            log.debug("litellm readiness failed: %s", exc)

        # 3. Daily usage. Every field is optional and degrades independently.
        today = datetime.now(timezone.utc).date()
        start = (today - timedelta(days=1)).isoformat()
        end = today.isoformat()

        try:
            activity = self._daily_activity(start, end)
        except HttpError as exc:
            # 401 means the key is wrong; 404 means the endpoint moved. Neither
            # is fatal to the card, but both are worth surfacing.
            log.warning("litellm daily activity returned HTTP %s", exc.status)
            metrics["usage"] = UNAVAILABLE
            if exc.status in (401, 403):
                alerts.append(Alert("warning",
                                    "LiteLLM rejected the API key").to_dict())
            elif exc.status == 404:
                alerts.append(Alert(
                    "warning",
                    "LiteLLM daily activity endpoint not found").to_dict())
        except HttpUnreachable as exc:
            log.warning("litellm daily activity unreachable: %s", exc)
            metrics["usage"] = UNAVAILABLE
        else:
            self._merge_activity(activity, metrics, alerts)

        status = "ok"
        for alert in alerts:
            from ..models import severity
            if severity(alert.get("severity")) > severity(status):
                status = alert.get("severity")

        return SourceSnapshot(
            source_id=self.id,
            status=status,
            metrics=metrics,
            alerts=alerts,
        )

    def _merge_activity(self, activity, metrics, alerts):
        """Pull the fields we report out of a daily-activity response."""
        if not isinstance(activity, dict):
            metrics["usage"] = UNAVAILABLE
            return

        results = activity.get("results")
        if not isinstance(results, list) or not results:
            metrics["usage"] = UNAVAILABLE
            return

        # Sum across the days we asked for. Usually that is one or two rows.
        totals = {
            "spend": 0.0,
            "total_tokens": 0,
            "api_requests": 0,
            "failed_requests": 0,
        }
        saw_spend = False

        for row in results:
            if not isinstance(row, dict):
                continue
            row_metrics = row.get("metrics")
            if not isinstance(row_metrics, dict):
                continue

            spend = _metric(row_metrics, "spend")
            if spend is not None:
                try:
                    totals["spend"] += float(spend)
                    saw_spend = True
                except (TypeError, ValueError):
                    pass

            for key in ("total_tokens", "api_requests", "failed_requests"):
                value = _metric(row_metrics, key)
                if value is not None:
                    try:
                        totals[key] += int(value)
                    except (TypeError, ValueError):
                        pass

            # Top model comes from the breakdown of the most recent row.
            top = _top_model(row.get("breakdown"))
            if top:
                metrics["top_model"] = top

        metrics["requests_today"] = totals["api_requests"]
        metrics["tokens_today"] = totals["total_tokens"]
        metrics["errors_today"] = totals["failed_requests"]

        if saw_spend:
            metrics["spend_today"] = round(totals["spend"], 4)
        else:
            # No database configured, or no priced requests. Either way this is
            # unknown, not zero.
            metrics["spend_today"] = UNAVAILABLE

        if totals["failed_requests"]:
            alerts.append(Alert(
                "warning",
                "%d failed requests today" % totals["failed_requests"]).to_dict())
