"""Ollama Cloud activity, explicitly separate from local model health and quotas.

The observed API reports dated activity totals, not remaining monthly credits.
Do not infer account allowances from the public plan or an arbitrary date range.
"""

import math

from ..quotas import quota_report, quota_window, timestamp
from ..models import iso
from . import register
from ._quota_http import QuotaHttpSource


@register
class OllamaCloudSource(QuotaHttpSource):
    id = "ollama_cloud"
    name = "Ollama Cloud"
    usage_url = "https://ollama.com/api/usage"
    provider = "ollama"
    product = "Cloud"
    windows = (("monthly_credits", "Monthly credits", None),)

    def parse(self, data, observed):
        if not isinstance(data, dict) or not isinstance(data.get("totals"), dict):
            raise ValueError("unrecognized cloud activity response")
        start, end = timestamp(data.get("from")), timestamp(data.get("until"))
        if end <= start:
            raise ValueError("invalid cloud activity range")
        totals = data["totals"]
        activity = {"from": iso(start), "until": iso(end), "observed_at": iso(observed)}
        for key in ("request_count", "usage_usd", "input_tokens", "cached_input_tokens", "output_tokens"):
            value = totals.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("invalid cloud activity measurement")
            if key != "usage_usd" and type(value) is not int:
                raise ValueError("invalid cloud activity count")
            activity[key] = value
        report = quota_report(self.provider, self.product,
                              [quota_window("monthly_credits", "Monthly credits", None, None,
                                            observed, state="unavailable")],
                              max(180, self.config.get("refresh_seconds", 60) * 3))
        report["activity"] = activity
        report["note"] = "The reporting API supplies activity totals, not monthly allowance or remaining credits."
        return report, False
