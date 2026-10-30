# Production OTA commissioning — 2026-10-29

Status: the controlled full-app success scenario **passed twice on the real
board** on the evening of October 29 (EDT), after the journal-save stall was
traced to the firmware's display driver and worked around (see
[Root cause and full-app acceptance](#root-cause-and-full-app-acceptance)).
Crash/hang rollback, weak-Wi-Fi socket release and near-full storage were
then tested on the board too (see the follow-up section). Production power cuts at three
representative points then passed as well (see "Power cuts"); the cold-boot clock source was then built and measured (authenticated daemon
time; see "Cold-boot clock"); a full cold power cycle against the user's own
daemon and release publication remain open. Normal daemon
configuration still defaults to `presto_ota_enabled = false`.

The October 28 TLS, bounded streaming, reset/watchdog, and physical power-cut
results in [the hardware checkpoint](presto-hardware-gates-2026-10-28.md) remain
valid. This work tests the completed updater integration, carrying that evidence
forward rather than repeating all of the earlier experiments.

## Deployment performed

- The device on explicit USB port COM3 had no lifecycle identity or public boot
  counter. A new public `/ai_dash_device_id.txt` and counter were initialized
  only after the installed app reported no existing identity. Private config,
  Wi-Fi secrets, daemon credentials, and firmware were not replaced or printed.
- `tools/presto_device.py migrate --port COM3` installed and verified both real
  dashboard slots and the production bootstrap. Later standard slot sync/check
  operations independently verified the active slot and bootstrap.
- The device remains on the measured MicroPython 1.29.0 build:
  `feature/presto-wireless-august-2026,   on 2026-08-26 (GNU 15.2.1 MinSizeRel)`.

## Integration gaps exposed by the board

1. The app initialized its updater to `None` unconditionally. It now loads an
   explicitly provisioned, matching-firmware TLS profile outside both slots.
   Missing, expired, disabled, or incompatible provisioning leaves OTA inert.
2. MicroPython built-in `machine` was absent from `sys.modules`. Looking only
   in that cache incorrectly rejected watchdog support. The app caches the
   actual module, and the user explicitly approved a narrow bootstrap hardware
   import before candidate code. The rule and structural tests record that
   exception.
3. A confirmed slot also needs watchdog coverage during native network work;
   otherwise a stalled downloader can require physical power removal. The
   bootstrap now supplies the available watchdog to confirmed apps as well.
4. Blocking TLS integration did not progress beyond the first update permit.
   No transfer record or candidate was committed. A physical power cycle
   recovered the confirmed slot. The app now uses verified async TLS with a
   four-chunk receive buffer so the draw loop can continue feeding the watchdog.
   A standalone physical async TLS probe received the expected authenticated
   response. Full-app integration remains under test.
5. Browser demos cannot import socket modules; transport networking imports are
   lazy. An editable checkout can contain stale staged package-data; it now
   serves the current shared source while installed wheels still use their
   bundled files.

The complete local App/Transfer pipeline also fetched the entire shared bundle
through a real certificate-verified HTTPS server and durably activated its
candidate journal in isolated fixture storage. The physical full-app runs have
so far issued a permit but have not established transfer start or confirmation;
they are failures under investigation, not hardware acceptance. The guarded retry
below deployed the confirmed-slot watchdog change after the requested power cycle,
but still did not establish transfer start. The confirmed slot was preserved.

## Reproducible controlled test

`tools/presto_ota_acceptance.py` starts temporary status/download servers, an
isolated database, synthetic quota data and a fresh test token. It generates a
disposable CA and verifies the real device's downloads through that CA and an
explicit hostname. It does not use provider accounts or modify the normal daemon.

```
.venv/Scripts/python tools/presto_ota_acceptance.py --port COM3 --host <LAN-IP> --out .preview/ota-hardware/success --scenario success
```

The `crash` and `hang` scenarios retain the full shared dashboard bundle but
deliberately break the candidate entrypoint to test actual bootstrap recovery.
Success artifacts differ from source only by a harmless public build marker so
the coordinator has a distinct release to offer. These are controlled test
artifacts, never registry publications or alternate app implementations.

The temporary profile is under `/ai_dash_state/ota.json`, with its public CA at
`/ai_dash_state/ota-ca.der`. An expiring acceptance section temporarily redirects
polling to the isolated daemon; cleanup removes both files and restores normal
polling on relaunch. Existing OTA provisioning is refused rather than overwritten.
Results record cleanup failures explicitly. The tool never reads existing
private configuration or echoes raw board tracebacks.

Bootstrap changes require explicit USB maintenance:

```
.venv/Scripts/python tools/presto_device.py sync --port COM3 --maintain-bootstrap
.venv/Scripts/python tools/presto_device.py check --port COM3
```

Routine sync does not replace the bootstrap; no OTA manifest contains it.

## Production provisioning after acceptance

The normal profile contains `enabled`, `host` (an explicit LAN address), `port`,
`hostname` (the certificate name), `firmware` (the exact validated build), and
`ca_file` (`/ai_dash_state/ota-ca.der`). It contains no provider credential. Normal
downloads use the device's existing daemon token. The daemon must be available
through a certificate-verified HTTPS endpoint and opt into device OTA. The
board's calendar clock must be valid; there is no HTTP or unverified-TLS fallback.

The expiring acceptance override is for USB-authorized commissioning only; it is
not required for ordinary polling or future production updates.

The board has no battery-backed clock, so the calendar is only valid after
something sets it. The app carries the time across its own candidate restart
(a warm reset), which is what the acceptance runs used. After a **cold**
power-on nothing sets it yet, so OTA stays unavailable (no `ota-v1` capability)
until a production clock source is chosen. See the open gates below.

## Remaining evidence

Real transfer, candidate boot, two-poll confirmation, resource timings and
cleanup are recorded in the next section. Crash/hang rollback, production
power-cut/journal and near-full-storage cases, cold-boot clock provisioning,
supported-profile scope, and real release publication acceptance remain
explicit gates rather than inferred from unit tests or the earlier disposable
fixture.

## Current verification and physical retry

- The frozen guarded source passes **2,778 tests, 9 skipped, zero failures**
  (442.809 seconds, repository `.venv`). The full log is
  `.preview/ota-guarded-full-tests.log`.
- Focused TLS/provisioning tests validate certificate and hostname rejection,
  bounded buffering, framing/path refusals, opt-in profiles, and preservation
  of existing identities. Structural, bootstrap, and USB maintenance tests pass.
- The real shared app was rendered and visually inspected in PSL; the existing
  display geometry and warning priority remain correct.
- The earlier physical attempt (`.preview/ota-hardware/success-async/result.json`)
  failed with USB cleanup/relaunch timeouts. After the user confirmed the second
  all-power cycle, recovery removed its temporary provisioning and verified a
  primary journal with active/confirmed slot `b`, no transfer, no pending
  candidate and no failure.
- Standard COM3 sync with `--maintain-bootstrap` deployed the guarded source;
  independent check verified bootstrap and slot `a` against slot revision
  `e0e5cd6db0fd6ca8106d3bb195a3e9abd9f4629bf7dde26d89f3dbad06c5f57d`
  (shared app aggregate `bab13fd159ef`).
- The guarded retry ran at **19:38-19:41 EDT on October 29**. Its result is
  `.preview/ota-hardware/success-second-cycle/result.json`, with sanitized trace
  `.preview/ota-second-cycle-test.log`. Verified TLS connected, the request was
  sent, and the manifest was received. No transfer-start acknowledgement,
  candidate or confirmation followed within 180 seconds. The attempt remained
  queued. **Acceptance failed.** Automatic temporary-profile removal and relaunch
  succeeded this time; another physical power cycle was not required for cleanup.
- A subsequent 60-second diagnostic used in-memory wrappers around the installed
  implementation, leaving installed app bytes unchanged. Its sanitized trace
  (`.preview/ota-prepare-trace.log`) shows manifest parsing, command validation,
  free-space measurement, preflight and transfer construction completing, then
  entry into `activation.save()` without a return. Its result is
  `.preview/ota-hardware/prepare-trace/result.json`; cleanup succeeded. This
  localizes the observed stall to journal saving, but does not establish which
  encoding/filesystem operation or firmware interaction causes it. A more
  detailed trace run produced no device reports and is not additional OTA
  acceptance evidence.
- An isolated native read-only preparation probe completed with a diagnostic
  valid clock and reported 13,516,800 free bytes. Its real post-reset clock was
  back in 2021 and correctly failed the trust gate. This does not validate clock
  retention or provisioning across a production reboot.
- Final public-journal recovery verified primary active/confirmed slot `a`, no
  pending candidate, no transfer and no failure; temporary provisioning was
  removed. No app source changed during this retry. Successful production
  transfer/confirmation, crash/hang rollback and production power-loss acceptance
  remain open. Resolve the journal-save stall before repeating those tests.

## Root cause and full-app acceptance

Measured on COM3 on October 29-30, 2026 (times UTC; 20:00-20:45 EDT on
October 29). Diagnostics used scratch files under `/ai_dash_state/diag.*`,
never the real journal, and an 8-second watchdog so a hang reset the board
instead of needing a power cut. Scripts and logs are under `.preview/stall2/`.

### Journal-save stall: the display driver blocks internal-flash writes

- In a bare REPL the scratch save sequence (write, exists, two renames, sync)
  completed; the first write took 107 ms, the rest 0-8 ms.
- After `Presto()` the **first** flash write (`open()` creating a file) never
  returned; the watchdog reset the board (reset cause 3) and the file was never
  created. No TLS, Wi-Fi or OTA code was involved.
- Isolated components: constructing `EzWiFi()` or the FT6236 touch controller
  left writes working; constructing only `_presto.Presto` (the core1 display
  scan-out) reproduced the hang. This matches every earlier observation: the
  bootstrap's saves run before the display starts, the app's first save after
  it does not return, and the two-poll confirmation save would have hung too.
- Stopping core1 with the native object's own finaliser (`__del__`, 3-17 ms)
  made the same writes work (3-54 ms each). A new `_presto.Presto` (about
  370 ms) restarts scan-out on the same static framebuffer; three cycles with
  garbage collection in between left free heap unchanged and `update()` at its
  normal 13-16 ms vsync cadence. A second finaliser call on a stale object is a
  no-op and does not stop the new object's core1 (a write after it still hung,
  as expected).

The fix keeps the journal format, durability order and all verification:

- `presto/updater.py`: `Transfer.step()` reads into RAM only. Each file is
  checked against its manifest hash in RAM; verified files are committed by a
  new `Transfer.flash()` in batches of at most 384 KiB plus the final batch,
  which also re-verifies every file from flash and writes the revision files.
- `presto/main.py`: every flash write the running app makes happens inside
  `_FlashWindow`, which stops core1, writes, and restarts the display:
  transfer acceptance (still durable before the first download), failure
  records, the verifying phase, the batch commits, metrics, the candidate
  record and the two-poll confirmation. If the app crashes after the display
  started, `main()` stops core1 before re-raising so the bootstrap can record it.
- Tests: a store that fails on any write while a stand-in native display is live
  runs the whole update, the confirmation, a failed write, nested windows, and
  the real-coordinator round trip. Removing the window around the confirmation
  save makes two of them fail.

### Post-reset 2021 clock

- Every hard reset (watchdog, `machine.reset()`, power) restarts the RTC at
  2021-01-01T00:00:00Z: readings 3-22 seconds after the diagnostic resets were
  `1609459203`-`1609459222`. The earlier probe's `1609459467` was 267 seconds
  after the stall's watchdog reset. Nothing in the app set wall time.
- Consequence before the fix: after the candidate restart, `configure()`
  rejected the expiring acceptance profile and the updater refused the TLS date
  gate, so the candidate polled its normal daemon and could never be confirmed.
- RAMFS survives `machine.reset()` (measured). `_reboot()` now writes the
  current time to `/ramfs/ai_dash_clock.txt`; at start the app restores it, plus
  the seconds since reset, **only** while the RTC is still unusable. Both runs
  logged `AI Dash: clock restored after reset`.
- `_reboot()` previously mounted RAMFS inside the same `try` as
  `machine.reset()`. A second `psram.mkramfs()` raises `EPERM` (measured), which
  is the normal case when the stock launcher started the app, so the reset was
  silently skipped. The mount is now separate and the reset always happens.
- Open: a cold power-on still leaves the clock in 2021 until a production clock
  source is chosen (for example the daemon's `updated_at` or NTP); that is a
  trust decision, not made here.

### USB sync and the confirmed-slot watchdog

The bootstrap arms the 8-second watchdog for confirmed apps too. An app
interrupted over USB stops feeding it, so a long sync session could be cut by a
reset: one restore sync failed this way ("Could not relaunch AI Dash") right
after an acceptance run. This also fits the earlier cleanup/relaunch timeouts.
`device_install.reset_to_launcher()` now hard-resets to the stock launcher
(which arms none) and waits for USB before the slot check and slot sync
sessions. Afterwards two back-to-back sync/check rounds with AI Dash running
succeeded.

### Full-app acceptance results

| | Run 1 | Run 2 |
| --- | --- | --- |
| Harness | sanitized wide-trace wrapper | standard `tools/presto_ota_acceptance.py` |
| Result | `.preview/ota-hardware/flash-window-wide2/result.json` | `.preview/ota-hardware/flash-window-success-2/result.json` |
| Target | `93794c967ce1...` | `05bcc517e514...` |
| Bytes downloaded | 292,372 | 292,372 |
| Steps / max step | 413 / 9 ms | 377 / 11 ms |
| Transfer elapsed | 84.4 s | 78.9 s |
| Flash windows / time | 1 / 5.70 s | 1 / 5.74 s |
| Minimum free heap | 7,444,448 B | 7,436,128 B |
| Issued / started | 00:33:01Z / 00:33:04Z | 00:40:08Z / 00:40:11Z |
| Confirmation acknowledged | 00:35:01Z | 00:42:25Z |
| Attempt / daemon state | succeeded / current, boot_confirmed | succeeded / current, boot_confirmed |
| Journal after cleanup | primary, active=confirmed=a, success, no pending/transfer/failure | same |

Both runs: verified TLS manifest, durable transfer record, download, one
display-paused write of slot `a` with re-verification from flash, candidate
record, warm reset, clock restored, `confirming`, daemon acknowledgement,
`slot a confirmed`, `succeeded`. Temporary profile and CA were removed and the
clock was usable at cleanup. Flash-window time includes re-reading every file
for its hash; the display was dark for about that long plus the ~0.4 s restart.

An earlier standard run in the same session
(`.preview/ota-hardware/flash-window-success`) saw no reports. A second
diagnostic run showed the app's poll connections timing out, and a probe right
after it failed to join Wi-Fi; a later join measured RSSI -78 dBm. This is
recorded as a weak-signal failure, not proven. In run 2 the candidate's first two
polls after the reset did not reach the daemon, but confirmation still landed
inside the 180-second window. Poll failures that hit `OSError 12` (ENOMEM) after
repeated connect timeouts were also seen, which suggests the status client does
not release sockets on timeout; this is not yet investigated.

After each run the test candidate was replaced by standard sync. At 00:44Z
`check` verified the bootstrap and slot `a` against slot revision `4b37e397efab`
(shared aggregate `0769a8e0be0a`), with slot `b` verified at the same revision
just before; both slots hold current source.

Verification: the frozen source passed the full suite, **2,803 tests, 9 skipped, zero failures** (432.951 s, repository `.venv`, about 21:05 EDT October 29); log `.preview/stall2/full-tests-2.log`. PSL Home and a card were rendered and inspected after the app change (PSL has no core1, so the display pause is a no-op there).

### Follow-up session, October 30, 2026 (UTC morning)

**Crash and hang rollback: both passed on COM3.** The `crash` candidate raises at
import; the `hang` candidate loops forever at import. Both were delivered over
the real OTA path (verified TLS, display-paused write of slot `b`, restart), and
the bootstrap rolled both back to the confirmed slot `a`.

| | crash | hang |
| --- | --- | --- |
| Result | `.preview/ota-hardware/rollback-crash/result.json` | `.preview/ota-hardware/rollback-hang/result.json` |
| Time from permit to terminal report | about 100 s | about 115 s |
| Recorded failure | `rolled_back` / `boot_crash` | `rolled_back` / `boot_crash` |
| Daemon attempt | `failed`, recovery `rolled_back` | same |
| Journal afterwards | active = confirmed = `a`, no pending or transfer | same |

The crash candidate raised immediately and the bootstrap's own crash handler
recorded it; the hang candidate was broken by the 8 s watchdog.
In both, the device kept re-reporting the retained failure until a daemon
settled it, which is the intended behaviour. After each, the board's clock was
back at 2021 (the rollback restart carries no time), which is the same cold-clock
gap recorded above.

A first crash attempt failed before provisioning: the harness's probe opened a
soft-reset session on a running app and the app's 8 s watchdog cut it short.
Nothing was written. `tools/presto_ota_acceptance.py` now calls
`reset_to_launcher()` before each probe, with a test.

**Weak-Wi-Fi socket leak: confirmed and fixed.** On the device, connecting to an
unused LAN address with `wait_for(open_connection(...), 0.5)`: without garbage
collection the first 5 timed out cleanly and the other **35 of 40 failed with
`OSError 12`**; with `gc.collect()` after each, **40 of 40** timed out cleanly.
A timeout cancels the connect before a writer exists and MicroPython leaves
the socket open until a collection runs, so with several MB of free heap the
small socket pool fills first. Every later poll then fails until a collection
happens to run, which also explains the poll failures seen in the run-2 candidate.
`net.StatusClient` (status polls and alert writes) and the OTA transport
(manifest and file streams) now collect after a failed connect. With the shipped
client deployed, 30 polls to the unreachable address gave 30 clean timeouts.
Regression tests for all three paths stub a connect that never completes.

**Near-full storage: policy behaves as designed.** A scratch file in
`/ai_dash_state/` (removed afterwards; free space returned to the 13,504,512
baseline) set free space just around the updater's requirement: the bundle plus
the 1,572,864-byte margin, about 1.87 MB.

| Free space | Outcome |
| --- | --- |
| 1,765,376 B (about 100 KB under) | Refused 3 s after the manifest: device `blocked` / `storage_insufficient`, daemon `manual_action`, attempt never started, nothing written to a slot, slot `b` stayed confirmed |
| 2,015,232 B (about 150 KB over) | Full update, restart, two-poll confirmation, `current`; 294,621 bytes, flash window 5.9 s, minimum heap 7,448,288 B |

The success at 2.0 MB free shows the margin is conservative: an update rewrites
files in an inactive slot that already holds a full copy, so it needs little
extra space. It is a policy choice, not a measured limit; it was left alone.

After these runs both slots were restored to current source by standard sync
(shared aggregate `754a8fb8f288`, slot revision `12d03306564e`), with an independent
`check` verifying the bootstrap and slot `a`.

### Power cuts, October 30, 2026 (EDT morning)

A person physically removed USB power at three points of a real full-app update.
`tools/presto_ota_acceptance.py --cut {download,flash,confirm}` arms each window,
beeps (one low beep to get ready, three high beeps to cut now, because the window
can be about 5 seconds), waits for the board to return, reads the public journal
**as the cut left it** (the stock launcher runs, not the bootstrap), removes the
temporary profile, relaunches the real bootstrap, reads the journal again and
judges it with `cut_verdict()` (unit tested). The confirm cut holds back the
daemon's acknowledgement for 30 seconds so there is a usable window.

| Cut | Where it landed | Journal as the cut left it | After the real bootstrap | Verdict |
| --- | --- | --- | --- | --- |
| 1. Download | Transfer recorded, file stream open, nothing yet written to flash | `a` confirmed, transfer set | `failed` / `connection_lost`, no transfer or pending, `a` confirmed | pass (harness) |
| 2. Flash write | During the display-paused write of slot `b` (log: `writing slot b`, no `staged`) | `a` confirmed, transfer set | `failed` / `connection_lost`, `a` confirmed | pass (harness) |
| 3. Before confirmation | Candidate in slot `b` running, unconfirmed, acknowledgement withheld | `b` active, pending, `a` confirmed | `rolled_back` / `boot_crash`, back on `a`, nothing pending | pass (`recovery.json`) |

Results: `.preview/ota-hardware/cut-download/`, `cut-flash/` and `cut-confirm/`.
Cut 3's harness did not register the cut (see below), so its journals came from
`.preview/stall2/recover_cut.py`, which uses the harness's own probe and verdict.

- Cut 3's update wrote slot `b` **on top of** the half-written files cut 2 left,
  and the candidate ran and reached confirmation, so an update resumes cleanly
  after an interrupted one.
- The cut during download wrote nothing to flash, because downloads are held in
  RAM until the verified batch is committed. The flash-window cut is the only
  one that can leave a partial slot, and it never became active.
- Every cut left the confirmed slot `a` running afterwards, with no manual repair.

Harness mistakes found along the way (no device harm; each is recorded):

- The first cut-1 attempt was wasted: a demo of the cue sound played just before
  the run was taken for the real cue and power was pulled before the update had
  started (`no_device_reports`, journal clean).
- The 'staged' guard that ignores the board's own restart in cuts 1 and 2 was
  also applied to cut 3, which is prompted after that restart, so the loss was
  not detected and the run had to be stopped and recovered by hand. Fixed:
  the guard now applies only to the download and flash cuts.

After these runs both slots were restored to current source by standard sync
(slot `12d03306564e`), with an independent `check` verifying slot `a`.

Not covered: a cut inside the journal's own write/rename sequence (a window of
milliseconds, covered only by the unit tests' simulated crash points) and cuts at
the warm-reset boundary. The three cuts above are the representative windows.

### Cold-boot clock: authenticated time from the daemon

Decision (the user chose it over plain daemon time and over NTP): the daemon
proves the time with a challenge and response under the shared token. Reasoning
and the options considered are in the conversation record; the short version is
that the date check only guards against an expired certificate, a forged LAN
reply must not be able to move the clock back, and NTP would add an internet
dependency this app deliberately avoids.

Before this, the only things that ever gave the board a real date were the test
harness over USB and the app's own carry across its restart, so after the first
real power loss the updater was never built and OTA was silently off.

**Protocol.** A device whose clock is unusable sends `X-Presto-Time-Nonce`
(32 lowercase hex characters, fresh per status request). The status answer then
carries `clock: {t, mac}` with `mac = HMAC-SHA256(token, "presto-time-v1|" +
nonce + "|" + t)`. Documented in `docs/user/reference/ports-and-endpoints.md`.

- `daemon/src/presto_ai_dash/clock_proof.py` signs; exactly one valid nonce is
  answered, only on an authenticated status request, nothing is stored, and a
  payload with no room under the status size bound is left alone.
- `presto/clockproof.py` verifies. MicroPython has no `hmac` module here, so the
  HMAC is built from `hashlib`; tests pin it to the standard library, to RFC 4231
  and to the daemon's signing. Times before 2025-01-01 or at or after 2100-01-01
  are refused even when signed.
- `presto/main.py`: the nonce is asked only while the clock is unusable and is
  spent by the first reply; a real clock is never changed; and once the clock is
  set the updater is built (`ota_transport.configure`), which previously ran only
  at start-up. The RTC setter is shared with the warm-reset carry.

**A bug the hardware found, which unit tests had missed.** `net.request_headers`
writes only a fixed list of header names, so the nonce header was built but never
sent and the daemon never answered. The first unit tests checked the header
dictionary, not what goes on the wire. Fixed, with a test that runs the device's
own `StatusClient` against a real daemon and fails if the fix is removed.

**Measured on COM3** (`.preview/stall2/clock_hw.py`; hard reset to the cold 2021
clock, real Wi-Fi, the deployed app's own `StatusClient` and `App._apply_clock`,
an isolated daemon running the new code, a disposable token):

| Step | Result |
| --- | --- |
| Cold clock | 2021, not usable |
| One authenticated reply | clock 2026, usable |
| Reset again, then a device holding a token the daemon does not know | refused with 401, clock stays 2021 |
| Reset again, then a valid earlier reply replayed against a new pending nonce | clock stays 2021 |
| HMAC under MicroPython | matches RFC 4231 case 2 and the host's long-key result |

Forging a reply *without* the token (a man in the middle) is covered by unit tests
only (wrong token, wrong nonce, moved time and out-of-range time are each
refused); the on-device wrong-token round is refused earlier, by the daemon.

**Not yet exercised:** a real cold power cycle through the whole app against the
user's own daemon. That daemon runs elsewhere on the previous code, so it cannot
send the proof; the isolated test daemon cannot stand in because its profile is
honoured only once the clock is valid. When the user's daemon is upgraded, one
unplug and replug of the Presto, then watching for `AI Dash: clock set from
daemon` and the `ota-v1` capability on the next poll, closes this.

The full suite on the final code passed, **2,834 tests, 9 skipped, zero
failures** (444.120 s). Both slots hold the current source (slot revision
`81884aea350f`), verified by `check`.

### Moving a Presto to a different daemon

**The gap.** Nothing could repoint a set-up Presto at another daemon. `sync` and
`install-presto` deliberately never touch `config.py`, and the only writer of
`DAEMON_HOST`, `DAEMON_PORT` and `SHARED_TOKEN` was the guided `presto_setup.py`.
That script, and the packaged `install-presto`, only know the legacy layout. On a
slot device such as COM3 they would have recreated `/ai_dash.py` and
`/ai_dash_files/*` beside the bootstrap, and the repository tool's layout rule
(`boot and not legacy`) would then have called the device legacy and routed the
next sync past the slot journal. So the documented way to move a daemon was both
manual and unsafe on exactly the device the OTA work targets.

**Built.**

- `presto-ai-dash set-daemon` and `tools/presto_device.py set-daemon`
  (`device_install.set_daemon_operation`). It resets the board to the launcher,
  rewrites only the three lines on the device (the file never travels to the
  host), swaps the new file in through a staged, hashed rename, has the Presto
  join Wi-Fi and read the new daemon's status with the token it now holds, and on
  anything but a 200 puts the old file back and says why. The token comes only
  from an environment variable and is never printed. An interrupted run is undone
  by the next one. IPv4 literals only, never loopback.
- `presto_setup.py` stops before writing anything on a bootstrap device, and
  `install-presto` / `check-presto` refuse one and relaunch it, both pointing at
  `set-daemon`.
- Tests: `daemon/tests/tooling/test_set_daemon.py` (27) runs the real device
  scripts under CPython on a temporary folder, plus a setup-script test.

**Measured on COM3.** The private file was never read; a device-side copy and
SHA-256 digests (first 12 characters below) proved the original was restored.

| Step | Result |
| --- | --- |
| A. Address with nothing listening | Refused ("could not reach a daemon"), file digest `00ffb69feeb7` unchanged, no stage or backup left |
| B. Isolated daemon, disposable token (API) | Reach `ok`; the relaunched app polled the isolated daemon 3 times within 49-62 s |
| C. CLI, daemon with a different token | Exit 2, "refused its device token", digest unchanged; the daemon logged the rejected sign-in from the Presto's address |
| D. CLI with `--token-env` | Exit 0; the token appears in no output; 3 requests from the app |
| Restore | Digest `00ffb69feeb7` matches; `check` verifies slot `b` at `81884aea350f` |

**A Wi-Fi finding.** The first hardware run refused with "could not join the
Wi-Fi network" and rolled back correctly, but it was a false alarm about the new
daemon. A join probe from the launcher (signal about -78 dBm) took 3-9 s
normally, yet one run sat linked without an address for 90 s across three plain
retries after the radio started inactive. Switching the radio off and on between
tries fixed the forced-inactive starts in the probe (5 of 6 cold joins, one run
needing three tries, one failing at the weak signal's worst), so the reach check
makes four cycled attempts and the message tells the user to run the command
again. The rollback stays the safe default for any Wi-Fi failure.

**Not covered.** `--no-reach-check` is unit-tested only. A cut mid-command is
covered by the next-run recovery in unit tests, not by a physical pull.

### Changing a Presto's Wi-Fi network

**The gap.** A changed router, network name or password left the Presto retrying
the old network for ever, and the only writer of `/secrets.py` was the guided
setup script. After the guard added for `set-daemon` that script stops on a slot
device, so on COM3's layout there was no supported route at all.

**Built.** `presto-ai-dash set-wifi` and `tools/presto_device.py set-wifi`
(`device_install.set_wifi_operation`). It shares one session routine with
`set-daemon`: reset to the launcher, rewrite only `WIFI_SSID` and `WIFI_PASSWORD`
on the device (a missing file is created), join the network, and put the old file
back if the Presto cannot. The password comes from an environment variable or a
hidden prompt, never the command line, and is never printed. The deciding rule is
deliberate: the change is **kept when the Presto joins**, even if it then cannot
reach its daemon, because moving networks usually moves the daemon's address too;
the command says so and points at `set-daemon`. The apply and settle device
scripts were generalised to any settings file, and the reach script now reports
`joined` separately from the daemon result.

**Tests.** 46 in `daemon/tests/tooling/test_set_daemon.py` run the real device
scripts under CPython, including passwords with quotes, backslashes and spaces,
an open network, a created and removed file, odd file shapes, interrupted runs
and credential bounds. A mutation (requiring a reachable daemon to keep a
network) was caught by four of them.

**Measured on COM3.** `/secrets.py` was never read; a device-side copy and
SHA-256 digests (`bb88ca0fd481`) proved the original was restored.

| Step | Result |
| --- | --- |
| A. CLI, a network that does not exist | Exit 2 after 55 s ("could not join that Wi-Fi network"); digest unchanged; no stage or backup left; the password appeared in no output |
| B. The tool's real operation, with the device supplying its own current credentials | Joined the real network; daemon check reported `error`; the new file was kept, as designed; no stage or backup left |
| Restore | Digest matches the original; `check` unaffected |

**A finding about the existing daemon address.** Step B's `error` was a real
result, not a script fault. A diagnostic that printed only booleans, an
exception type and timings showed the join succeeding on the first attempt and
the connect to the configured daemon address failing after 24 ms with errno 104
(connection reset). The configured host is an IPv4 literal outside the Presto's
own /24, so the daemon is not accepting that connection at the moment. It is
independent of this work (the config digest was restored byte for byte after
every run) and is for the user to look at.

**Not covered.** The success path used the device's own credentials, so a
different network's join was not exercised; `--no-join-check` is unit-tested only.

### The Wi-Fi checkbox on the admin page

**Why.** The guided setup used to decide about Wi-Fi by itself: ask for it when the
Presto had none, otherwise ask a `[y/N]` question. The user wanted the choice on
the page, ticked by default, and a warning that cannot be missed when it is
unticked.

**Built.** **Set the Wi-Fi network on the Presto** on the admin page's *Set up a
Presto*. The choice rides inside the encrypted handoff as `device.wifi`, `"set"`
or `"keep"` (the daemon's payload schema now requires it; the script accepts it
present or absent, because a script and a handoff from two daemon builds can share
an app revision). Ticked: the script always asks for the network name and
password. Unticked: it keeps the Presto's Wi-Fi and asks nothing, and if the
Presto has no `/secrets.py` it stops before writing anything and says to prepare
the download again with the box ticked. The password is still never typed into the
page. `POST /api/admin/presto-setup` accepts an optional boolean `wifi` (default
true; anything else is a 400).

**Tests.** Script, service and endpoint layers: the four behaviours above, an
older handoff without the key, bad values at every layer, and the existing
failure-path tests moved to the explicit "keep" choice with an added check that
they reach the install prompt (several had been passing only because a prompt
mismatch is reported as a generic failure).

**Looked at in a browser** (a throwaway daemon with its own config and disposable
tokens, never `dash.toml`): ticked by default; unticking shows a warning panel (first
red and uppercase, then softened to the page's amber on the user's feedback that it was
too harsh) headed **Wi-Fi is needed** and highlights the label; re-ticking clears
both; *Prepare download* sent `wifi: false`, the page said "Wi-Fi is not being
set", and decrypting the resulting handoff with its own unlock code showed
`keep`. No device was involved in this change.

### One process for every case: the setup script on an over-the-air Presto

**Direction.** After the CLI commands were built, the user's point was that a
Docker user has only the admin page and its two downloads, so first setup,
re-initialising, changing the Wi-Fi and moving a Presto to another daemon must
all be the same page-and-script flow, with the script working out what the Presto
needs. The CLI stays as a developer route.

**Built.** `presto_setup.py` no longer refuses a Presto on the bootstrap and A/B
layout. It detects it, says so, hard-resets the board to the stock launcher before
any prompt (a running slot app's watchdog would otherwise reset the board in the
middle of the session), changes only the settings the page asked for, never touches
the app files or slots, and starts the app through whichever layout is present.
The page's note and the setup guide describe one process for every case.

**Two defects found by running it on COM3.**

1. *Taking the board over too soon.* Reconnecting one second after a hard reset
   interrupted the stock launcher while it was still starting, and from then on a
   plain Wi-Fi join stuck at status 1 until the next hard reset. A controlled
   comparison showed the in-process session failing and the CLI session (which
   waits for a readiness probe) joining in 9 s. The script now leaves the board
   alone for 8 s after the reset. This also explains most of the "weak signal"
   failures seen earlier in the day; the signal is weak, but the poisoned radio was
   a larger cause than it.
2. *A stale module in the reach check.* The script reads the old settings with
   `import config` in the same MicroPython session, so the reach check's own
   `import config` returned the cached old module and tested the **old** daemon
   address. This predates this work and affects any flow that replaces the daemon
   settings. The check now drops the cached `config` and `secrets` first.

The reach check also gained the radio-cycling join (six attempts), bounded socket
waits and a 180 s USB timeout.

**Measured on COM3** (device-side backup of `config.py`, digests only, original
`00ffb69feeb7` restored after every run):

| Step | Result |
| --- | --- |
| Script, settings-only, daemon address replaced, Wi-Fi kept | Exit 0 in 19 s; the check reached the isolated daemon (1 request); the relaunched app polled it 3 times; the handoff was removed; no legacy launcher or app files appeared; every app and slot file untouched |
| Same run before the two fixes | The check reported "could not join the Wi-Fi", or reached the old address; the app made no polls |
| App followed on its own serial output after a hard reset, against an isolated daemon running this branch's code | `AI Dash: clock set from daemon`, then 5 polls |

The last row is the cold-boot clock check, reached by a hard reset (which restores
the 2021 clock) rather than by pulling the cable.

**Not covered.** A single-folder (legacy) Presto and a blank Presto were not
available, so those paths are covered by unit tests only. The unticked-Wi-Fi case
was exercised on hardware; the ticked case (asking for the password) by tests only.
A literal unplug and replug through the user's own daemon is still the user's test.

### Gates still open
- Cold power cycle through the whole app against the user's own daemon, once it
  runs this change (the clock source itself is built and measured above). It
  can be done without touching the server: run this tree's daemon locally,
  `set-daemon` the Presto to it, unplug and replug, then `set-daemon` back.
  A hard reset followed by the app against a daemon running this code was
  already observed to set the clock and poll (see above).
- Real GHCR/PyPI release progression and supported-profile scope.
- Optional: a cut inside the journal's rename sequence and at the warm-reset
  boundary, if a finer physical test is wanted.
