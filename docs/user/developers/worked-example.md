# The worked example

This page builds a source from the template and runs it. The example *is*
the template: `daemon/templates/source_template.py`, with its matching test
template in `daemon/templates/test_source_template.py`.

## The two files

```text
daemon/
  templates/
    source_template.py        the source you copy
    test_source_template.py   the test you copy
  tests/
    test_source_template.py   the test that keeps the template honest
```

The template lives **outside** the importable package, in `daemon/templates/`.
The daemon only discovers modules inside
`daemon/src/presto_ai_dash/sources/`, and `pyproject.toml` only packages
what is under `src/`. So the template can never be found by the collector
and can never appear in the admin page's connector list.

The class is not decorated with `@register` either. Add the decorator when
you move the file into `sources/`.

## Why the template is not a copy of a real source

A template that nobody runs rots. The one in this repository is executed by
`daemon/tests/providers/test_source_template.py`, which:

- imports it **by path**, so discovery never sees it,
- runs its `collect()` against a **fake response**, so no network is
  involved,
- asserts that its ID is **not** in `source_catalog()`,
- checks that a missing window is *unavailable* and not zero,
- checks that a bad response is *offline* and not an exception,
- checks that the secret never appears in an error message.

## Building it

### 1. Copy the template

```bash
cd daemon
cp templates/source_template.py src/presto_ai_dash/sources/your_source.py
cp templates/test_source_template.py tests/providers/test_your_source.py
```

### 2. Change four things

In `src/presto_ai_dash/sources/your_source.py`:

1. `id` and `name` - the connector type and the admin page's label.
2. `config_fields` - the settings the account needs.
3. `parse` - how the provider's response becomes a quota report.
4. `fetch` - the read-only request that gets the response.

Then add the decorator:

```python
from . import register


@register
class YourSource(Source):
    ...
```

### 3. Run it

Here is the template's own test, run in this repository:

```console
$ cd daemon
$ ../.venv/Scripts/python -m unittest discover -s tests/providers -t tests -p test_source_template.py -v
test_a_window_the_provider_did_not_report_is_unavailable ... ok
test_an_unexpected_response_is_offline_rather_than_an_exception ... ok
test_an_unreachable_service_is_offline ... ok
test_collect_turns_a_fake_response_into_a_quota_report ... ok
test_the_secret_never_appears_in_an_error ... ok
test_the_template_declares_a_secret_and_a_config_field_set ... ok
test_the_template_is_not_in_the_source_catalog ... ok
test_the_template_test_template_exists ... ok

Ran 8 tests in 0.008s

OK
```

The template's own test template runs too, so you can see the shape of the
test you are about to copy:

```console
$ cd daemon
$ ../.venv/Scripts/python -m unittest discover -s templates -p "test_*.py" -v
test_a_missing_window_is_unavailable_and_not_zero ... ok
test_an_unexpected_response_is_offline_and_does_not_raise ... ok
test_an_unreachable_service_is_offline ... ok
test_the_happy_path_produces_the_expected_windows ... ok
test_the_secret_never_appears_in_an_error ... ok

Ran 5 tests in 0.064s

OK
```

## What the template actually does

### The configuration

```python
config_fields = (
    {"key": "base_url", "label": "Service URL", "type": "url",
     "default": "http://localhost:9000"},
    {"key": "timeout", "label": "Request timeout (seconds)", "type": "int",
     "default": 10, "minimum": 1, "maximum": 60},
    {"key": "refresh_seconds", "label": "Refresh interval (seconds)", "type": "int",
     "default": 60, "minimum": 30, "maximum": 3600},
)
secret_field = "api_key"
```

The admin page renders a URL box, two number boxes and a key box, with the
bounds you declared. The key is encrypted before it is stored.

### The parse

```python
def parse(self, data, observed):
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        raise ValueError("unrecognized usage response")
    windows = []
    for key, label, minutes in WINDOWS:
        value = usage.get(key)
        if not isinstance(value, dict):
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
```

Two decisions worth copying:

- A window the provider did not report becomes
  `state="unavailable"` with a `None` percentage. **Never `0`.**
- A response that is not the shape you expect raises `ValueError`, which
  becomes an *unavailable* reading rather than a guess.

### The fetch

```python
def fetch(self):
    headers = {"Accept": "application/json", "User-Agent": "presto-ai-dash/0.1"}
    if self.api_key:
        headers["Authorization"] = "Bearer " + self.api_key
    return get_json(self.base_url, timeout=self.timeout, headers=headers,
                    allow_redirects=False)
```

One GET. Redirects refused, so the credential is never forwarded to another
host. No model request, ever.

### The collect

```python
def collect(self):
    try:
        data = self.fetch()
    except HttpError as exc:
        ...
        return self.offline(error)
    except HttpUnreachable:
        return self.offline("Usage server unreachable")
    try:
        report = self.parse(data, now_utc())
    except ValueError as exc:
        return self.offline("Usage unavailable: %s" % exc)
    return self.snapshot("ok", metrics={"quota": report})
```

`collect()` returns a snapshot and never raises. The reasons it reports -
"API key rejected", "Subscription access denied", "Usage server returned
HTTP 403", "Usage server unreachable" - are the plain words the admin page
shows.

## Turning it into a real source

1. Put the file in `daemon/src/presto_ai_dash/sources/` and add
   `@register`.
2. Point `fetch` at the provider's documented endpoint.
3. Replace the fixture in the test with a **recorded** response: a real
   shape from the official documentation, or from the live service. Do not
   mock an external API from assumption.
4. Add the cases your provider needs: a missing window, a malformed
   response, an unreachable service, a rejected key, and the secret never
   appearing in an error.
5. Run the suite:

   ```bash
   cd daemon && ../.venv/Scripts/python -m unittest discover -s tests -q
   ```

6. Add the source to the admin page by restarting the daemon. The connector
   appears in **Add source** with its own fields.
7. Add an account, and look at the device.

## The checklist

- [ ] The class is decorated with `@register` and has a unique `id`.
- [ ] `config_fields` declares every setting, with a type and a default.
- [ ] `secret_field` names the credential, or is `None`.
- [ ] `collect()` never raises, and `offline` means unreachable.
- [ ] A missing measurement is `None`, never `0`.
- [ ] Every window carries the provider's own label.
- [ ] There is no provider write anywhere in the code path.
- [ ] The tests cover the happy path, a missing field, a bad shape, an
      unreachable service and a rejected key.
- [ ] The payload stays inside the size budget.

## See also

- [How a source works](how-a-source-works.md)
- [Writing a collector](writing-a-collector.md)
- [Running the tests](running-the-tests.md)
