# Production OTA commissioning — 2026-10-29

Status: the controlled full-app success scenario **passed twice on the real
board** on the evening of October 29 (EDT), after the journal-save stall was
traced to the firmware's display driver and worked around (see
[Root cause and full-app acceptance](#root-cause-and-full-app-acceptance)).
Crash/hang rollback, production power-cut, near-full storage, cold-boot clock
provisioning and release publication remain open gates. Normal daemon
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

### Gates still open

- `crash` and `hang` candidate rollback on the production bootstrap (now
  possible: success is established).
- Production power cut during download, during the flash window, and before
  confirmation, with the user arming a real window.
- Cold-boot clock source for production OTA readiness.
- Weak-Wi-Fi resilience: socket release on poll timeout (`OSError 12`) and the
  180-second confirmation window under repeated poll loss.
- Near-full storage policy, supported-profile scope, and real GHCR/PyPI
  release progression.
