# Running the tests

## The daemon suite

Pure CPython, no external services, localhost HTTP only.

```bash
cd daemon
../.venv/Scripts/python -m unittest discover -s tests -q
```

On macOS or Linux use `../.venv/bin/python`.

Use the repository's `.venv`, not a global Python: it has the project and its
test dependencies installed.

To run one file, or one test:

```bash
../.venv/Scripts/python -m unittest tests.test_sources -v
../.venv/Scripts/python -m unittest tests.test_sources.OllamaTests -v
```

## What the suite covers

| Area | Where |
| --- | --- |
| The status vocabulary, snapshots and cards | `tests/test_m2.py`, `tests/test_cards.py` |
| Storage and migration | `tests/test_m3.py`, `tests/test_observations.py` |
| Accounts, settings and the admin API | `tests/test_management.py` |
| The quota model and the hints | `tests/test_quotas.py`, `tests/test_claude_hint.py` |
| The alert thresholds and the policy | `tests/test_alert_thresholds.py`, `tests/test_alert_policy.py` |
| The device payload and its whitelist | `tests/test_device_payload.py` |
| The device's one write | `tests/test_alert_write_api.py`, `tests/test_device_alerts.py` |
| The ingest protocol | `tests/test_ingest.py`, `tests/test_ingest_api.py`, `tests/test_ingest_roundtrip.py` |
| The Claude readers | `tests/test_claude.py`, `tests/test_claude_activity.py`, `tests/test_claude_usage_cache.py` |
| The Presto app | `tests/test_presto_cards.py`, `tests/test_presto_bars.py`, `tests/test_home.py`, `tests/test_ui.py` |
| The source template | `tests/test_source_template.py` |

## The device payload guard

`tests/test_device_payload.py` is the test that keeps the device and the
daemon honest about the payload. It proves:

- the device draws exactly the same screens from the slim payload as from
  the full one,
- the payload stays inside the size budget,
- a quota report is sent once,
- the admin-only fields never reach the device,
- every key the device code reads is on the whitelist.

The whitelist scan parses the device's own source with `ast` and looks for
the keys it reads. If you add a key to the device, add it to the
`DEVICE_*_KEYS` constants in `dashboard.py`, or this test fails.

The size budget is 5000 bytes for the whole payload and 1000 bytes per
account. It is a hard limit: the test fails the build when it is exceeded.

## The Presto tests

The Presto logic runs on the host too. Only `presto/main.py` needs real
hardware. `tests/test_presto_structure.py` enforces the layering: only
`main.py` and `ui.py` may import `presto`, `machine`, `picographics` or
`picovector`.

The drawing tests use a fake display and check that text fits and does not
overlap, in the firmware's own bitmap fonts.

## The documentation site

The site is built with MkDocs and the Material theme, in its own virtual
environment so the project environment is untouched:

```bash
python -m venv .venv-docs
.venv-docs/Scripts/python -m pip install -r docs/requirements.txt
.venv-docs/Scripts/mkdocs build --strict
```

On macOS or Linux use `.venv-docs/bin/` in place of `.venv-docs/Scripts/`.

The strict build fails on a broken internal link, a page that is not in the
navigation, or any warning at all. The site is written from
`docs/user/`; the rest of `docs/` is internal and is never published.

The external links are checked by [lychee](https://github.com/lycheeverse/lychee)
in CI. Locally, if lychee is installed:

```bash
lychee --no-progress --timeout 20 --max-retries 1 --exclude-loopback "docs/user/**/*.md"
```

After a build, `tools/check_doc_anchors.py` checks that every help link of
the admin page points at an anchor that exists in the built site:

```bash
.venv/Scripts/python tools/check_doc_anchors.py
```

## The browser checks

`tools/check_admin_help_browser.py` opens the admin page in a real browser,
against a throwaway daemon on a temporary database, and checks the help
links, the keyboard, the phone-width layout and the alert settings. The
browser checks need Playwright, which is NOT needed for the test suite and
is not a dependency of the project. Install it into a throwaway environment
rather than into `.venv`:

```bash
python -m venv .venv-browser
.venv-browser/Scripts/python -m pip install playwright
.venv-browser/Scripts/python -m playwright install chromium
.venv-browser/Scripts/python tools/check_admin_help_browser.py
```

The throwaway daemon itself still runs with the repository's `.venv`; only
the checker needs Playwright. Delete `.venv-browser` when you are done.

## Before you call the work done

```bash
cd daemon && ../.venv/Scripts/python -m unittest discover -s tests -q
```

And, if you touched the Presto app, the device sync in
[the preview and the device sync](preview-and-device.md).
