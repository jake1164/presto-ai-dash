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
from datetime import datetime, timezone

from ..http_client import HttpError, HttpUnreachable, get_json, get_text, join_url
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
    secret_field = "api_key"
    config_fields = (
        {"key": "base_url", "label": "LiteLLM URL", "type": "url",
         "default": "http://localhost:4000"},
        {"key": "timeout", "label": "Request timeout (seconds)", "type": "int",
         "default": 5, "minimum": 1, "maximum": 60},
    )

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
        return get_text(join_url(self.base_url, "/health/liveliness"),
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
        start = today.isoformat()
        end = today.isoformat()

        try:
            activity = self._daily_activity(start, end)
        except HttpError as exc:
            # Authentication and route permissions are separate problems.
            # Neither is fatal to the card, but both are worth surfacing.
            log.warning("litellm daily activity returned HTTP %s", exc.status)
            metrics["usage"] = UNAVAILABLE
            if exc.status == 401:
                alerts.append(Alert("warning",
                                    "LiteLLM rejected the API key").to_dict())
            elif exc.status == 403:
                alerts.append(Alert(
                    "warning",
                    "LiteLLM key lacks permission to read usage").to_dict())
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
        if not isinstance(results, list):
            metrics["usage"] = UNAVAILABLE
            return

        # The query is scoped to today; never mix yesterday into *_today fields.
        keys = ("spend", "total_tokens", "api_requests", "failed_requests")
        totals = {key: 0 for key in keys}
        seen = {key: False for key in keys}
        top = None

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
                    seen["spend"] = True
                except (TypeError, ValueError):
                    pass

            for key in keys[1:]:
                value = _metric(row_metrics, key)
                if value is not None:
                    try:
                        totals[key] += int(value)
                        seen[key] = True
                    except (TypeError, ValueError):
                        pass
            if _metric(row_metrics, "total_tokens") is None:
                prompt = _metric(row_metrics, "prompt_tokens")
                completion = _metric(row_metrics, "completion_tokens")
                if prompt is not None and completion is not None:
                    try:
                        totals["total_tokens"] += int(prompt) + int(completion)
                        seen["total_tokens"] = True
                    except (TypeError, ValueError):
                        pass

            top = _top_model(row.get("breakdown")) or top

        metadata = activity.get("metadata")
        metadata_fields = {
            "spend": "total_spend",
            "api_requests": "total_api_requests",
        }
        for key, metadata_key in metadata_fields.items():
            value = _metric(metadata, metadata_key)
            if value is not None:
                try:
                    totals[key] = float(value) if key == "spend" else int(value)
                    seen[key] = True
                except (TypeError, ValueError):
                    pass
        prompt_total = _metric(metadata, "total_prompt_tokens")
        completion_total = _metric(metadata, "total_completion_tokens")
        if prompt_total is not None and completion_total is not None:
            try:
                totals["total_tokens"] = int(prompt_total) + int(completion_total)
                seen["total_tokens"] = True
            except (TypeError, ValueError):
                pass

        metrics["requests_today"] = totals["api_requests"] if seen["api_requests"] else UNAVAILABLE
        metrics["tokens_today"] = totals["total_tokens"] if seen["total_tokens"] else UNAVAILABLE
        # A daily total cannot establish whether failures are happening now.
        # Keep it visible as history without making a healthy proxy yellow all day.
        metrics["errors_today"] = totals["failed_requests"] if seen["failed_requests"] else UNAVAILABLE
        if top:
            metrics["top_model"] = top
        else:
            metrics["top_model"] = UNAVAILABLE

        if seen["spend"]:
            metrics["spend_today"] = round(totals["spend"], 4)
        else:
            # No database configured, or no priced requests. Either way this is
            # unknown, not zero.
            metrics["spend_today"] = UNAVAILABLE
