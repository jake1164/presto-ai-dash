"""The Ollama source.

Reports live local-model health. Endpoints used, all verified against the
official Ollama API documentation:

- ``GET /api/version``  - reachability and version
- ``GET /api/tags``     - models available locally
- ``GET /api/ps``       - models currently loaded in memory

Ollama exposes **no request history**, so "tokens used today against Ollama
directly" is not reconstructable from Ollama alone. See docs/limitations.md.

If ``samples_file`` points at a JSON file written by a collector or shim, the
last measured request is read from it and tokens/sec is computed. The
arithmetic is implemented and tested now so it is ready for the v0.2 collector.
"""

import json
import logging
import os

from ..http_client import HttpError, HttpUnreachable, get_json, join_url
from ..models import Alert, SourceSnapshot
from . import Source, register

log = logging.getLogger(__name__)

# Fields a samples file may carry. All optional; anything missing is simply
# not reported.
SAMPLE_FIELDS = (
    "model",
    "prompt_eval_count",
    "eval_count",
    "total_duration",
    "load_duration",
    "prompt_eval_duration",
    "eval_duration",
    "created_at",
)


def tokens_per_second(eval_count, eval_duration_ns):
    """Compute tokens/sec from an Ollama ``/api/generate`` response.

    From the Ollama documentation, verbatim:

        To calculate how fast the response is generated in tokens per second
        (token/s), divide ``eval_count`` / ``eval_duration`` * ``10^9``.

    All Ollama durations are in nanoseconds. Returns ``None`` when the inputs
    are missing or would divide by zero - never a fabricated number.
    """
    if eval_count is None or eval_duration_ns is None:
        return None
    try:
        count = float(eval_count)
        duration = float(eval_duration_ns)
    except (TypeError, ValueError):
        return None
    if duration <= 0 or count < 0:
        return None
    return count / duration * 1e9


def _first_model_name(models):
    """The name of the first entry in an Ollama model list, or None."""
    if not isinstance(models, list):
        return None
    for entry in models:
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("model")
            if name:
                return name
    return None


@register
class OllamaSource(Source):
    """Local Ollama health."""

    id = "ollama"
    name = "Ollama"

    def __init__(self, config=None):
        super().__init__(config)
        self.base_url = str(self.config.get("base_url",
                                            "http://localhost:11434")).rstrip("/")
        self.timeout = int(self.config.get("timeout", 5))
        self.samples_file = str(self.config.get("samples_file") or "")

    # -- individual probes ------------------------------------------------

    def _version(self):
        return get_json(join_url(self.base_url, "/api/version"),
                        timeout=self.timeout)

    def _tags(self):
        return get_json(join_url(self.base_url, "/api/tags"),
                        timeout=self.timeout)

    def _ps(self):
        return get_json(join_url(self.base_url, "/api/ps"),
                        timeout=self.timeout)

    # -- samples file ------------------------------------------------------

    def read_samples(self):
        """Read the last measured request from the samples file.

        Returns a dict of the recognised fields, or ``None`` if there is no
        file, it is unreadable, or it holds no usable sample. Never raises.
        """
        if not self.samples_file:
            return None

        try:
            with open(self.samples_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            log.debug("ollama samples file not found: %s", self.samples_file)
            return None
        except (OSError, ValueError) as exc:
            log.warning("could not read ollama samples file %s: %s",
                        self.samples_file, exc)
            return None

        # Accept either a single sample or a list, taking the last entry.
        if isinstance(data, list):
            data = data[-1] if data else None
        if not isinstance(data, dict):
            log.warning("ollama samples file %s held no object",
                        self.samples_file)
            return None

        sample = {}
        for field in SAMPLE_FIELDS:
            if field in data and data[field] is not None:
                sample[field] = data[field]
        return sample or None

    # -- collection --------------------------------------------------------

    def collect(self):
        metrics = {}
        alerts = []

        try:
            version = self._version()
        except (HttpError, HttpUnreachable) as exc:
            return self.offline("cannot reach Ollama at %s: %s"
                                % (self.base_url, exc))

        version_string = version.get("version") if isinstance(version, dict) else None
        if version_string:
            metrics["version"] = str(version_string)

        # Model inventory. A failure here is not fatal: health is already known.
        try:
            tags = self._tags()
            models = tags.get("models") if isinstance(tags, dict) else None
            if isinstance(models, list):
                metrics["available_models"] = len(models)
        except (HttpError, HttpUnreachable) as exc:
            log.debug("ollama /api/tags failed: %s", exc)

        # Currently loaded models. This is what makes the card read "active".
        loaded = None
        try:
            ps = self._ps()
            running = ps.get("models") if isinstance(ps, dict) else None
            if isinstance(running, list) and running:
                loaded = _first_model_name(running)
                metrics["loaded_models"] = len(running)
        except (HttpError, HttpUnreachable) as exc:
            log.debug("ollama /api/ps failed: %s", exc)

        if loaded:
            metrics["loaded_model"] = loaded

        # Last measured request, if a collector has written one.
        sample = self.read_samples()
        if sample:
            for field in SAMPLE_FIELDS:
                if field in sample:
                    metrics["last_measured_%s" % field] = sample[field]

            speed = tokens_per_second(sample.get("eval_count"),
                                      sample.get("eval_duration"))
            if speed is not None:
                metrics["last_measured_tokens_per_sec"] = round(speed, 1)
        else:
            # Be explicit rather than silently absent.
            metrics["last_measured"] = "unavailable"

        status = "active" if loaded else "ok"
        return SourceSnapshot(
            source_id=self.id,
            status=status,
            metrics=metrics,
            alerts=[a.to_dict() for a in alerts],
        )
