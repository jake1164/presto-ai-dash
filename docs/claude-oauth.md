# Private Claude OAuth experiment

This collector-only reader is experimental, explicitly opt-in (`oauth_usage`
defaults to false), and uses an undocumented endpoint. Permission for automated
use is unresolved. A successful HTTP response does not establish permission.
The documented-provider-read rule in `AGENTS.md` remains unchanged; live use
requires an explicitly approved exception to that rule.

## Trust and attribution

The access token is opaque. A separate cached account ID cannot establish who
owns it. The `claude-oauth attest --account NAME --confirm-identity` command
therefore records a **manual assertion**, not provider verification: the user
confirms that the Claude profile and the Presto account selected by its daemon
token represent the same account. Attestation reads the existing credential
file without changing it and makes no provider request.

Only hashes of the access token, profile account ID and daemon destination are
stored in the private collector state directory. These hashes and the account
ID are never sent to the daemon. The destination includes both the exact URL
and the daemon ingest token. Missing identity, an absent attestation, or a change
in any binding stops collection. An observed mismatch remains suspended across
restarts and even a switch back. `reenable` does not clear this suspension;
another explicit attestation is required. Ordinary token refresh also requires
reattestation. An unobserved switch away and back cannot be detected.

The profile's normal cache locations corroborate the mapping; an arbitrary
`usage_cache_path` override cannot supply identity. Credentials and identity
are checked before and after the provider request. A result spanning an
observed switch is discarded; undelivered results are checked again before
delivery. Manual mapping cannot prevent the user attesting the wrong account.

## Request budget and failure handling

All profiles and collectors using the same OS user's normal state directory
share one conservative budget, including provider stops and backoff. This
deliberately reduces freshness across distinct accounts rather than assuming
that profile paths identify separate accounts. Different machines, OS users,
or deliberately different state roots cannot share this local budget. Old
unattested experimental collectors must be stopped before using this build.

Requests require recent genuine session-log or optional hook activity, new
activity since the previous attempt, daemon ingestion support and no recent
status-line reading. Startup and activity checks are not activity signals.
The minimum interval is five minutes; idle profiles generate no periodic
provider traffic. OS file locks protect state updates on Windows and POSIX.
Use a local filesystem with reliable locks and atomic replacement.

A request claim is persisted before the GET. Until its outcome is saved, every
other process refuses another request. An interrupted request or a failed
outcome write remains blocked until explicit `reenable`. HTTP 401/403 stop
collection. HTTP 429 uses exponential backoff and honors a longer Retry-After;
attestation and the one-shot command never lift that backoff. Diagnostics
contain fixed reasons and HTTP codes, never response error bodies or secrets.

## One-shot and diagnostics

`claude-oauth test-once --confirm-live-request` selects exactly one configured
account (`--account NAME` is required when ambiguous). It first sends a daemon
heartbeat and requires the OAuth-ingestion capability response. It then runs
the normal rules once and performs at most one provider GET, without retries,
watch mode or scheduling. It reports HTTP status, request duration, fetch time,
quota values and whether the daemon accepted the reading. Source selection is
labelled as the collector's estimate; the daemon merges each window separately.

The preflight heartbeat is an app-owned write and updates collector presence.
The short delay between preflight and ingestion respects the daemon's POST
throttle. Failure to ingest does not repeat the provider request.

`dry-run` reads activity, cache and existing state only. It creates no state
directory, opens no credentials or ingest-token file, contacts no server and
does not change the cooldown. Its eligibility result covers pacing; identity
and current daemon readiness remain unchecked. `status` and `hook-config` are
also read-only. Hooks are printed for manual installation, never installed by
the collector, and save only an event name and time, never prompt input.

## Platforms, timestamps and packaging

The standalone file uses Python 3.8+ and the standard library. File-backed
Claude credentials on Windows and Linux are supported. macOS Keychain access
is not implemented; a Keychain-only login fails closed. Other credential
stores and WSL-to-Windows profile bridging are not supported.

Reading timestamps are preserved rather than refreshed by heartbeats. The
newest valid reading wins per window; ties prefer the official status line,
then OAuth, then the usage cache. Missing OAuth windows do not erase other
sources. During inactivity, old OAuth readings age under the existing quota
staleness threshold and passed-reset rules. Idle activity hints remain
available; an old reading is not presented as newly observed.

`tools/build_collector.py` embeds the canonical reader into the production
single-file collector; `--check` verifies equality. This unreleased compatible
experiment retains collector version 1 and pins a separate private build hash.
Publishing changed collector bytes requires a released version increment.
