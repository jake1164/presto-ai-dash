"""The fake source.

Cycles deterministically through every status the UI can show, so the device
can be demonstrated and the LED/sound paths exercised without any real
integration. There is no randomness: tests and demos are reproducible.
"""

import logging
import time

from ..models import Alert, SourceSnapshot
from . import Source, register

log = logging.getLogger(__name__)

# The plan's section 6.1 list, in the order they are shown.
SCENARIOS = (
    "ok",
    "active",
    "warning",
    "critical",
    "offline",
    "rate_limited",
    "near_limit",
)

# Synthetic metrics per scenario, shaped like the real ones so the Presto
# rendering path is identical whether the data came from here or from LiteLLM.
SCENARIO_METRICS = {
    "ok": {
        "requests_today": 42,
        "tokens_today": 128000,
        "spend_today": 0.42,
        "top_model": "qwen2.5-coder",
        "errors_today": 0,
        "tokens_per_sec": 38.0,
    },
    "active": {
        "requests_today": 57,
        "tokens_today": 164000,
        "spend_today": 0.61,
        "top_model": "qwen2.5-coder",
        "errors_today": 0,
        "tokens_per_sec": 44.5,
    },
    "warning": {
        "requests_today": 210,
        "tokens_today": 640000,
        "spend_today": 2.85,
        "top_model": "qwen2.5-coder",
        "errors_today": 3,
        "tokens_per_sec": 21.0,
    },
    "critical": {
        "requests_today": 12,
        "tokens_today": 31000,
        "spend_today": 0.09,
        "top_model": None,
        "errors_today": 47,
        "tokens_per_sec": None,
    },
    "offline": {
        "requests_today": None,
        "tokens_today": None,
        "spend_today": None,
        "top_model": None,
        "errors_today": None,
        "tokens_per_sec": None,
    },
    "rate_limited": {
        "requests_today": 980,
        "tokens_today": 2100000,
        "spend_today": 9.40,
        "top_model": "qwen2.5-coder",
        "errors_today": 128,
        "tokens_per_sec": 12.5,
    },
    "near_limit": {
        "requests_today": 890,
        "tokens_today": 1900000,
        "spend_today": 8.75,
        "top_model": "qwen2.5-coder",
        "errors_today": 6,
        "tokens_per_sec": 18.0,
    },
}

SCENARIO_ALERTS = {
    "warning": [Alert("warning", "Usage is elevated today")],
    "critical": [Alert("critical", "Source is reporting failures")],
    "rate_limited": [Alert("rate_limited", "Rate limit reached")],
    "near_limit": [Alert("near_limit", "Approaching the daily limit")],
    "offline": [Alert("offline", "Source is unreachable")],
}


@register
class FakeSource(Source):
    """A deterministic demo source."""

    id = "fake"
    name = "Fake"

    def __init__(self, config=None):
        super().__init__(config)
        self.scenario_interval = int(self.config.get("scenario_interval", 20))
        self._index = 0
        self._started_at = time.monotonic()

    # -- scenario selection ------------------------------------------------

    def _elapsed(self):
        return time.monotonic() - self._started_at

    def current_scenario(self):
        """The scenario for the current moment.

        Deterministic: it depends only on elapsed time and the configured
        interval, so a test can advance a fake clock and get a known answer.
        """
        if self.scenario_interval <= 0:
            return SCENARIOS[0]
        steps = int(self._elapsed() // self.scenario_interval)
        return SCENARIOS[steps % len(SCENARIOS)]

    def scenario_at(self, elapsed_seconds):
        """The scenario that would be showing after ``elapsed_seconds``.

        Exposed separately so tests do not have to sleep.
        """
        if self.scenario_interval <= 0:
            return SCENARIOS[0]
        steps = int(elapsed_seconds // self.scenario_interval)
        return SCENARIOS[steps % len(SCENARIOS)]

    def advance(self):
        """Force the next scenario. Used by tests and by a manual refresh."""
        self._index = (self._index + 1) % len(SCENARIOS)
        self._started_at = time.monotonic() - (self._index * self.scenario_interval)

    # -- collection --------------------------------------------------------

    def collect(self):
        scenario = self.current_scenario()
        metrics = dict(SCENARIO_METRICS.get(scenario, {}))
        alerts = list(SCENARIO_ALERTS.get(scenario, []))

        snapshot = SourceSnapshot(
            source_id=self.id,
            status=scenario,
            metrics=metrics,
            alerts=[a.to_dict() for a in alerts],
        )
        log.debug("fake source reporting %s", scenario)
        return snapshot
