"""OpenCode Go allowance meters; read-only HTTPS, never an inference request."""

from ..quotas import quota_report, quota_window
from . import register
from ._quota_http import QuotaHttpSource, UsageReplyError

USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
WINDOWS = (("rolling", "5h", 300), ("weekly", "Weekly", 10080),
           ("monthly", "Monthly", None))


@register
class OpenCodeGoSource(QuotaHttpSource):
    id = "opencode_go"
    name = "OpenCode Go"
    usage_url = USAGE_URL
    windows = WINDOWS
    provider = "opencode"
    product = "Go"

    def parse(self, data, observed):
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict) or not any(key in usage for key, _, _ in WINDOWS):
            # A key that is valid but cannot see the subscription gets an answer
            # without the allowance windows: say so, rather than "changed schema".
            raise UsageReplyError("The usage reply has no Go allowance windows; "
                                  "this key may not see the Go subscription")
        windows = []
        limited = False
        for key, label, minutes in WINDOWS:
            value = usage.get(key)
            if value is None:
                windows.append(quota_window(key, label, None, None, observed, minutes,
                                            state="unavailable"))
                continue
            if not isinstance(value, dict) or value.get("status") not in ("ok", "rate-limited"):
                raise ValueError("unrecognized subscription window")
            if value.get("percent") is None or not value.get("resetsAt"):
                raise ValueError("incomplete subscription window")
            windows.append(quota_window(key, label, value["percent"], value["resetsAt"],
                                        observed, minutes))
            limited = limited or value["status"] == "rate-limited"
        return quota_report("opencode", "Go", windows,
                            max(180, self.config.get("refresh_seconds", 60) * 3)), limited

