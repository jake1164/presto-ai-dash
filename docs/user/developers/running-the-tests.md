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

To run one group, one file, or one class (from `daemon/`):

```bash
../.venv/Scripts/python -m unittest discover -s tests/providers -t tests -q
../.venv/Scripts/python -m unittest discover -s tests/providers -t tests -p test_sources.py -v
../.venv/Scripts/python -m unittest discover -s tests/providers -t tests -p test_sources.py -k OllamaTests -v
```

Keep `-t tests` when selecting a group so shared fixtures and cross-group
imports resolve the same way as the full suite. Add `-k test_method_name` to
select a single method instead of a class.

In VS Code, the Testing view groups the files under the folders below. Use
**Test: Refresh Tests** after pulling the reorganization, then expand or run
the area you need. **Tasks: Run Task** also offers **Run all tests** and
**Run test group**, which prompts for a group.

## What the suite covers

| Area | Where |
| --- | --- |
| Models, storage, quotas, configuration and alert thresholds | `tests/core/` |
| Accounts, settings, sessions and admin routes | `tests/admin/` |
| Provider clients, Claude readers, Codex and the source template | `tests/providers/` |
| Remote ingest, pushed observations and the collector | `tests/ingest/` |
| Presto UI, gestures, polling, alerts and the device API contract | `tests/device/` |
| Input hardening, authentication, private files and HTTP boundaries | `tests/security/` |
| Preview, USB sync, containers, health checks, documentation and licences | `tests/tooling/` |

Put new test files in the relevant group. Each group has an `__init__.py`
so standard `unittest` discovery visits it. Shared helpers `envutil.py` and
`presto_path.py`, and shared `fixtures/`, stay at the `tests/` root. Import
fixtures from other test modules by their group, for example
`from admin.test_management import ManagementBase`.

## The device payload guard

`tests/device/test_device_payload.py` is the test that keeps the device and the
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
hardware. `tests/device/test_presto_structure.py` enforces the layering: only
`main.py` and `ui.py` may import `presto`, `machine`, `picographics` or
`picovector`.

The drawing tests use a stand-in display and check that text fits and does not
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
