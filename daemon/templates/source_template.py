"""A template for a new quota source.

Copy this file to ``daemon/src/presto_ai_dash/sources/<your_source>.py`` and
work through it. The template deliberately lives *outside* the importable
package, in ``daemon/templates/``, so it can never be discovered by the
daemon and can never appear in the admin page's source catalog. A test
(``daemon/tests/providers/test_source_template.py``) keeps it working.

The four things to change when you copy it:

1. ``id`` and ``name`` - the connector type and what the admin page calls it.
2. ``config_fields`` - the settings the admin page offers.
3. ``parse`` - how the provider's response becomes a quota report.
4. ``fetch`` - the read-only request that gets the response.

After the source works, add its demo data: an account of your type in
``daemon/src/presto_ai_dash/demo.py``, with realistic windows that move through
the scenes. A test (``daemon/tests/core/test_demo.py``) fails until a registered
source type has one, because the demo is how users see a source before they own
an account.

Two rules that are not negotiable:

* **Read only.** A source may read documented usage and limit data. It may
  not send a prompt, spend a credit, redeem a reset, change a setting, log
  in or sign out. If the provider's API offers such an action, do not wire
  it up.
* **Never raise.** ``collect()`` returns a snapshot; an unreachable service
  is ``offline`` with an error message, not an exception. The daemon catches
  exceptions anyway, but a clean source reports what it knows.

The class is not decorated with ``@register`` on purpose: that decorator
adds the connector to the registry the admin page reads. Add it when the
file moves into ``sources/``::

    from . import register

    @register
    class YourSource(Source):
        ...
"""

from presto_ai_dash.http_client import HttpError, HttpUnreachable, get_json
from presto_ai_dash.models import now_utc
from presto_ai_dash.quotas import quota_report, quota_window
from presto_ai_dash.sources import Source

# The windows this provider reports. Each entry is
# (window id, the provider's own label, the period in minutes or None).
# The label is what the user sees: "5h", "Weekly", "Monthly". Never call a
# five-hour allowance "daily".
WINDOWS = (
    ("five_hour", "5h", 300),
    ("seven_day", "Weekly", 10080),
)


class TemplateSource(Source):
    """A read-only quota source that reads one documented usage endpoint."""

    # The connector type. It is the key in the admin page's source list and
    # it must be unique.
    id = "template_example"
    # The name the admin page shows.
    name = "Template Example"

    # The settings the admin page offers for this type. Each one has a key,
    # a label, a type and a default. The types are "string", "url", "int"
    # and "bool".
    config_fields = (
        {"key": "base_url", "label": "Service URL", "type": "url",
         "default": "http://localhost:9000"},
        {"key": "timeout", "label": "Request timeout (seconds)", "type": "int",
         "default": 10, "minimum": 1, "maximum": 60},
        {"key": "refresh_seconds", "label": "Refresh interval (seconds)", "type": "int",
         "default": 60, "minimum": 30, "maximum": 3600},
    )

    # The one setting that is a secret. It is encrypted before it is stored,
    # it is never returned by the admin API, and it is redacted from the
    # error messages this source produces. Set it to None when the provider
    # needs no key.
    secret_field = "api_key"

    # The provider and the product, as they appear in the quota report.
    provider = "example"
    product = "Example"

    # How long a reading stays fresh, in seconds. The effective value is
    # three times the refresh interval, or three minutes, whichever is
    # longer, unless you override it here.
    stale_after_seconds = 180

    def __init__(self, config=None):
        super().__init__(config)
        self.base_url = self.config.get("base_url", "")
        self.api_key = self.config.get("api_key", "")
        self.timeout = int(self.config.get("timeout", 10))
        self.refresh_seconds = int(self.config.get("refresh_seconds", 60))

    # -- the two methods you write ---------------------------------------

    def parse(self, data, observed):
        """Turn the provider's response into a quota report.

        ``observed`` is the moment the reading was taken. Every window you
        report carries that same moment, so a cached read keeps its original
        timestamp.

        Raise ``ValueError`` when the response is not the shape you expect.
        The caller turns that into an "unavailable" report; it never invents
        a number. A window the provider did not report becomes a window with
        ``state="unavailable"``, never a window with 0%.
        """
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            raise ValueError("unrecognized usage response")
        windows = []
        for key, label, minutes in WINDOWS:
            value = usage.get(key)
            if not isinstance(value, dict):
                # Not reported is not zero. Say "unavailable".
                windows.append(quota_window(key, label, None, None, observed,
                                            minutes, state="unavailable"))
                continue
            percent = value.get("percent")
            if type(percent) not in (int, float):
                raise ValueError("incomplete usage window")
            windows.append(quota_window(key, label, percent,
                                        value.get("resets_at"), observed, minutes))
        return quota_report(self.provider, self.product, windows,
                            self.stale_after_seconds)

    def fetch(self):
        """The read-only request that gets the response.

        One authenticated GET against a fixed endpoint. Redirects are
        refused, so a credential is never forwarded to another host. No
        model request is ever made.
        """
        headers = {"Accept": "application/json", "User-Agent": "presto-ai-dash/0.1"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return get_json(self.base_url, timeout=self.timeout, headers=headers,
                        allow_redirects=False)

    # -- the method the daemon calls -------------------------------------

    def collect(self):
        """Read the service and report what it said.

        Returns a ``SourceSnapshot``. It never raises: a failure is an
        ``offline`` snapshot carrying the reason, and the last known report
        is kept so the user sees a stale reading rather than nothing.
        """
        try:
            data = self.fetch()
        except HttpError as exc:
            if exc.status == 401:
                error = "API key rejected"
            elif exc.status == 403:
                error = "Subscription access denied"
            else:
                error = "Usage server returned HTTP %d" % exc.status
            return self.offline(error)
        except HttpUnreachable:
            return self.offline("Usage server unreachable")
        try:
            report = self.parse(data, now_utc())
        except ValueError as exc:
            return self.offline("Usage unavailable: %s" % exc)
        return self.snapshot("ok", metrics={"quota": report})
