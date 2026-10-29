# Newer-release notifications (Part 4) — 2026-10-29

The daemon tells the user when a newer published Presto AI Dash exists. It is
informational only: it never upgrades, pulls an image, restarts a container,
publishes a release or sends an external message. Operational OTA stays off and
the Part 2 review fixes are untouched.

## What runs where

| Piece | File | Knows about |
| --- | --- | --- |
| Installation detection | `release.py` | the running copy only |
| Version policy | `release_policy.py` | `packaging.version`; no network, no storage |
| Registry adapters | `release_registry.py` | one registry each; no cache, no Presto |
| Scheduler and cache | `release_checker.py` | the `daemon_release_cache` row |
| Status projection | `device_lifecycle.DeviceLifecycle.release()` | reads the cache only |
| Admin route and page | `admin.py`, `web/admin.js` | the cached view, plus a queue request |

Nothing in the first four mentions Presto, the status payload or the admin page,
so the registry layer can be reused for another public GHCR or PyPI project.

## Which registry, and never another

| Running copy | How it is recognised | Registry | Running version |
| --- | --- | --- | --- |
| pip / pipx wheel | installed distribution whose `INSTALLER` and `RECORD` exist, whose files are the running code, with no editable/directory/VCS `direct_url.json` | PyPI only | `importlib.metadata` version |
| Published image | in a container **and** a valid `DASH_RELEASE_VERSION` | GHCR only | that exact embedded label |
| Checkout, editable install, local image | everything else | none (`disabled`) | shown, never compared |

`release_channel()` no longer treats a missing `DASH_RELEASE_VERSION` as
development: a published wheel has none. A source tree's stale `.egg-info` is not
mistaken for an installed wheel (no `INSTALLER`/`RECORD`). There is no fallback
between registries and no TestPyPI query, ever; a failed check says
`could not check` and keeps the last good result and its time.
`DASH_RELEASE_CHECK=off` switches the checks off (state `disabled`).

## Release policy

* A numbered release is `X.Y.Z` or `X.Y.Z` + `aN`/`bN`/`rcN`. Container tags spell a
  candidate `0.50.0-rc4`, Python spells it `0.50.0rc4`; both order through
  `packaging.version.Version`, and the exact published spelling is the label shown.
* `latest`, `0.50`, `0`, `v0.50.0`, `sha-…`, `.postN`, `.devN`, `+local`, wrong case and
  anything else is ignored, even where a permissive parser would read it.
* A stable install hears about newer **stable** releases only. A candidate hears
  about later candidates **of its own release** and about any newer stable release
  (a `0.51.0rc1` is not on a `0.50.0rc4` user's progression).
* Numbered releases are immutable. Same-tag digest-change detection stays deferred.
* Yanked, empty or file-less PyPI releases are ignored. A listing with nothing
  usable is `no_eligible_release`, never "current".

### Ambiguous historical wheels

Up to `v0.50.0-rc3` every candidate wheel said `0.50.0`. A wheel reporting exactly
`0.50.0` is therefore ambiguous:

* If PyPI has **no** final `0.50.0`, the wheel cannot be it. It is compared as a
  candidate older than the first one carrying its number (`0.50.0rc3` as the
  ordering floor) and offered later candidates and the final release. No candidate
  number is invented or displayed.
* If PyPI has a final `0.50.0`, the wheel is compared as that release. The metadata
  cannot tell it from an old candidate, so the admin page keeps a note saying so, words
  its state as "no newer release was found" rather than "up to date", and gives the
  explicit named install (`pipx install --force "presto-ai-dash==0.50.0"`, or pip
  `--force-reinstall`) for anyone who installed before the final existed. It never claims
  the copy is the final release, and it is not a permanent error, which would nag every
  genuine 0.50.0 user.

Docker images are unambiguous: they embed the exact tag.

## Registry client: dependency decision

`oras-py` 0.2.43 was installed in an isolated venv and run against
`ghcr.io/jake1164/presto-ai-dash` (it listed `0.50.0-rc1…rc3` anonymously). It was
**not adopted**, from reading its source:

* no request timeout is set on any call (a stalled registry hangs the thread);
* the token `realm` from the 401 challenge is fetched without validating its host;
* `requests` follows redirects freely and there is no response-size limit;
* it pulls in `requests`, `urllib3`, `jsonschema` (+ specs) for one `tags/list` call.

`release_registry.py` uses the standard library: https only, fixed host list,
no redirect to an unlisted host (PyPI: at most two same-host redirects with no
credentials), a per-request time budget that covers the whole body (read in chunks, so a trickling
server is cut off) inside a 30 s overall deadline, 2 MiB/256 KiB/16 KiB
body limits, at most 20 pages and 5,000 tags, a validated challenge (Bearer, realm
exactly `https://ghcr.io/token`, service `ghcr.io`, scope exactly the pull scope; the
token URL is built locally, not copied), a real RFC 8288 `Link` parser (`rel` may follow other parameters; an unreadable header is
`incomplete_listing`) with validated next URLs (same host, same path, only `n`/`last`,
no repeats), and `incomplete_listing` whenever a
listing cannot be read to the end. `releaseprobe` was not evaluated further.

The one new dependency is `packaging>=24` (pure Python; Apache-2.0 or BSD-2-Clause):
`daemon/pyproject.toml`, `docker/requirements.txt` (pinned `26.3`), the CI install
steps and `THIRD_PARTY_NOTICES.md`. No Docker socket, privileged container,
credentials or extra service.

## Schedule and cache

* First check about 15 s after startup on a background thread; then about every
  24 h (±10%). A restart never re-asks within 10 minutes of the last attempt.
* Failures retry after 5 min, 15 min, 1 h, then 4 h. `Retry-After` (seconds or an HTTP
  date, bounded to 60 s – 24 h; 1 h if a rate limit names no time) is stored in the cache row
  (`retry_after_at`, schema version 11) so it outlasts a restart, and manual checks are
  refused with `429 rate_limited` and the remaining wait until it passes. Concurrent
  requests coalesce; new starts are limited to one per 60 s.
* `last_attempt_at` moves when a check starts; `checked_at` only after a complete
  successful check. A failure keeps the last `latest`/`checked_at` and sets `state=error`
  with a fixed code. A first check shows `checking`; a recheck keeps showing the
  previous result until the new one lands. Older than 48 h is `stale`.
* Rows for another running version are ignored (an upgraded daemon starts clean);
  `checking` left by a stopped process is cleared at startup.
* Shutdown sets a cancel flag, joins for up to 3 s and writes nothing for an abandoned check.
* `source_url` is generated here: `pypi.org/project/presto-ai-dash/[version]/` or the
  GitHub release/package page.

## Interfaces

* `POST /api/admin/releases/check` — same Host/Origin/session-or-bearer/CSRF checks as
  other admin writes. `202 {"queued":true,"status":"queued"|"coalesced"}`;
  `429 {"error":"check_throttled","retry_after":n}` + `Retry-After`;
  `409 release_checks_disabled`. It only queues work.
* `GET /api/admin/releases`, `GET /api/admin/status` and the device poll read the cache;
  tests prove zero registry calls from them.
* Presto: the existing 10×10 blue arrow appears for a **fresh `available`** result only;
  stale, error, current and unknown draw nothing; the yellow warning outranks it and the
  opened view lists both. No `presto/` file changed, so no device sync was needed.

## RC publication policy (section 0.05) — recommendation, not adopted

Recommendation: **publish future numbered RCs to real PyPI** (keeping TestPyPI as an
optional rehearsal). Each tag now has its own PEP 440 version, and pip/pipx skip
pre-releases unless `--pre` or `==` is given, so stable users are unaffected.

Until that is adopted, `release.yml` is unchanged and **a PyPI-installed RC cannot
discover RCs that were published only to TestPyPI** (it is still offered the next
stable release). The checker never queries TestPyPI. Docker RC users are unaffected.

Proposed `release.yml` change for review (not applied; nothing was published):

```diff
-  # A pre-release (v0.50.0-rc4) goes to TestPyPI, a rehearsal of the real
-  publish-test:
-    needs: release
-    if: ${{ contains(github.ref_name, '-') }}
+  # Every tag, candidates included, goes to PyPI. The "pypi" environment must
+  # allow pre-release tags, and the trusted publisher on pypi.org must trust it.
+  publish:
+    needs: release
     ...
-    environment: testpypi
+    environment: pypi
     ...
-          repository-url: https://test.pypi.org/legacy/
-          skip-existing: true
```

and delete the old `if: ${{ !contains(github.ref_name, '-') }}` on `publish`. A PyPI upload is
permanent, so adopt this deliberately before the first tag that should reach PyPI.

## Acceptance limits

* **No real RC→RC publication test was observed.** The checker first ships in a release;
  its "update available" notice can only be verified against real registries once a
  later release is published. Progression is tested with fixtures only.
* PyPI returned 404 for `presto-ai-dash` when probed (not yet published), so the PyPI
  adapter was exercised against fixtures, and the GHCR adapter against the live registry
  (read-only, anonymous) in addition.
* Admin visuals were inspected on a throwaway server with a scripted stand-in registry; the
  stale admin state was covered by tests and shares the error layout, not a screenshot.

## Verification record (2026-10-29)

* Full suite, repo `.venv`: **2,734 tests, 9 skipped, all passing** (431 s). The earlier
  baseline was 2,576 with 9 skipped; 158 tests were added (policy/installation 37,
  registry 44, checker/instructions 53, admin route/zero-traffic 22, Presto fixtures 2).
  A first run found six failures (stale expectations that a checker-less lifecycle is
  `disabled`, an undocumented route, a banned word in a doc); all were corrected.
* PSL, real `presto/` code via `tools/presto_preview/render.py` and the live preview:
  fresh available (blue arrow), release plus device warning (yellow, opened view lists
  both), current, stale and error (no icon, dock released). New preview fixtures:
  `release-current`, `release-stale`, `release-error`.
* Admin page, throwaway server: available (banner, exact-version pipx/pip commands, copy
  buttons), error (not confirmed up to date, last result kept), current.
* No `presto/` file changed in this work, so no COM3 sync was required or run. There is
  **no new USB evidence**; the last dated check remains the Part 2 record
  (public legacy revision `e272d6cfb8e7`).

### Review follow-up (2026-10-29)

An independent review found five defects that the first suite missed because its scripted
transport returned whole responses instantly. All five were corrected with regression
tests: the request budget now covers body reads (trickle tests went from 24 s to 0.3 s);
`Link` is parsed properly; a registry's delay survives restarts and blocks manual checks;
HTTP-date `Retry-After` is read; an ambiguous wheel gets explicit guidance.
