# Presto update hardware/security checkpoint — 2026-10-28

**Decision: the measured build supports controlled Part 2 updater development
with normal OTA disabled; unattended production OTA is not approved by this
checkpoint. Part 3 reporting is the next delivery step.** TLS authentication,
bounded streaming, filesystem primitives, disposable boot/recovery fixtures,
and physical power loss during active staging and before confirmation have
device evidence. Full-app A/B migration, production confirmation,
activation-journal fault coverage, live UI responsiveness, and the supported
firmware matrix still require validation. USB setup/recovery remains supported
independently of OTA.

These are dated USB observations on one board, not continuous telemetry. This
report accompanies Part 2's mandatory gate in the device lifecycle plan. It does
not install an updater or replace the production launcher.

## Device and measurement scope

| Item | Observed value |
| --- | --- |
| Device | Pimoroni Presto with RP2350, explicit port COM3 |
| MicroPython | 1.29.0 |
| Firmware version string | `3.4.0; MicroPython feature/presto-wireless-august-2026, on 2026-08-26` |
| Repository baseline | `22e0847f5f341d93e059db748f15eb39033288cf`, with concurrent Part 1 work in the shared working tree |
| Public app revision | `e315d0bb966398c83018c8239fc40ff61e3320931a0a54c9da8189d7c1347390` |
| Public bundle | 11 files, 183,665 bytes; two copies would be 367,330 bytes before metadata/filesystem overhead |
| Device filesystem | 14,680,064 bytes (14 MiB), 4,096-byte allocation units |
| Available filesystem bytes | 14,057,472 bytes (13.40625 MiB) at the capability check |
| Free MicroPython GC memory after collection | 8,093,248 bytes in the stopped-app probe |

The firmware string identifies the tested build. It has not been mapped to a
published release artifact, and no oldest supported official firmware is claimed.
No firmware flashing was performed. The measurements do not establish behavior
on other builds or boards.

Only public app files and public firmware `main.py` were hashed/inspected. The
probes did not open private configuration, secrets, database, or encryption-key
files. They did not send provider requests or alter provider accounts. Fixture
certificates/private keys were newly generated synthetic test material, removed
after the local HTTPS servers stopped.

## Storage and streaming observations

- `os.statvfs('/')`, SHA-256, rename, and rename-over-existing worked on this build.
  The standard SHA-256 `abc` vector matched.
- A 262,144-byte file written in 1,024-byte chunks took **35.864257 seconds**.
  Its SHA-256 matched after writing and after rename. The longest sampled write
  call took **52.966 ms**; the minimum sampled free GC memory was 8,082,112 bytes.
- The scratch directory was removed. Filesystem free space returned to the same
  allocation count. Public app/launcher and firmware `main.py` hashes matched
  before and after the capability experiment.
- A 65,536-byte authenticated HTTPS download streamed into scratch storage in
  **10.537 seconds**, using reads of at most 1,024 bytes. Its expected full SHA-256
  matched. Minimum sampled free GC memory was 7,991,424 bytes; the longest sampled
  file write was 52.488 ms.
- Corrupt content produced a hash mismatch; truncated content was rejected before
  accepting completion; oversized Content-Length and redirects were rejected.
  No candidate code from a network transfer was executed.

These memory samples are not complete native/TLS heap measurements. The real
dashboard was interrupted for the experiments, so they do not prove that staging
is responsive alongside the actual display/network loop. A whole-transfer limit
of 30 seconds would already be too short for the measured 256 KiB write. Choose
timeouts from full-app measurements, not from the daemon's offline threshold.

The fixture tools preserve at least 1.5 MiB before their bounded writes; the boot
fixture requires 3 MiB. These are test containment limits, **not a validated
production free-space policy**. The main filesystem was not filled to ENOSPC.
Preflight on nearly full flash and a recoverable real write failure remain open.

## Executable-download authentication candidate

The tested route is the firmware's **`ssl.SSLContext` compatibility layer** with
`verify_mode = ssl.CERT_REQUIRED`, an explicit CA loaded as DER, and an explicit
`server_hostname`. The fixture connects to a LAN IP while verifying the DNS name
in the certificate, without changing device credentials or reading them.

| Vector | Result on the physical board |
| --- | --- |
| Trusted CA, matching hostname, valid dates, corrected RTC | Accepted |
| Different hostname, same trusted server | Rejected during certificate verification |
| Different CA | Rejected during certificate verification |
| Expired server certificate | Rejected |
| Not-yet-valid server certificate | Rejected |
| Missing server hostname | Rejected |
| Trusted server with the board's original 2021 RTC | Rejected; failed closed |
| Trusted HTTPS stream with correct full hash | Accepted as data only |
| Wrong hash / truncated body / excessive length / redirect | Rejected |

This firmware reports the compatibility layer's certificate errors as
`ValueError`; the probe exports only fixed error classifications, not exception
text. The direct `tls.SSLContext.load_verify_locations(cadata=...)` call tested
here raised `TypeError`; the compatible `ssl` route worked. Do not infer success
from an import or the presence of a `CERT_REQUIRED` constant alone.

**Clock/trust provisioning is mandatory.** The observed device RTC was in 2021.
The test temporarily set UTC for the certificate vectors and restored the prior
RTC afterward. A production updater must establish usable calendar time, load a
provisioned trust root, check the daemon hostname, and fail closed if these are
unavailable. It must not fall back to HTTP or `CERT_NONE`. The daemon's existing
plain HTTP polling path and an encrypted setup handoff do not by themselves
authenticate downloaded executable code.

The normal self-hosted installation therefore needs an explicitly supported
HTTPS endpoint/trust provisioning flow before this route can enable OTA. A
signed-manifest alternative has not been validated. The tests do not justify
shipping a home-grown signature verifier or treating an HTTP hash as a signature.

MicroPython documents that TLS support varies by port and that certificate
validation needs correct time and a client hostname:
[MicroPython SSL documentation](https://docs.micropython.org/en/latest/library/ssl.html).

## Disposable startup and rollback fixture

The fixture uses its own randomly named directory and a separately named root
entry module. It leaves `/main.py`, `/ai_dash.py`, `/ai_dash_files`, and private
settings intact. It exercises the real stock launcher's RAMFS handoff followed by
a hardware reset. Candidate slots contain only synthetic markers, not copies of
application logic.

| Scenario | Observed fixture result |
| --- | --- |
| Good candidate | Entered once; synthetic slot B confirmed; known-good A unchanged |
| Malformed candidate | Returned to A; no pending activation remained |
| Reset before first confirmation | Two entry executions; rolled back to A |
| Watchdog hang before confirmation | Two entry executions; rolled back to A |
| Physical power loss after staging, before confirmation | User-confirmed all-power cycle and matching USB event; returned to A with its original hash intact |
| Physical power loss during sustained staging writes | User-confirmed cycle 57.518 seconds after arming; recovered A unchanged and cleared pending activation |

Important implementation findings:

1. **Entry module and app directory names must differ.** MicroPython can resolve
   a same-named directory as a namespace package, even without `__init__.py`.
   Early same-name fixtures never executed their candidate and are excluded
   from successful gate evidence. The corrected fixture passed all four cases.
2. **RAMFS must be mounted in a fresh interpreter.** USB soft resets need an
   explicit `psram.mkramfs()` before writing the volatile handoff. A flash-backed
   mountpoint directory is not evidence that RAMFS is mounted.
3. **The carousel consumes the launch handoff before importing the entry.** The
   fixture re-arms its own handoff before running an unconfirmed candidate so a
   subsequent warm reset/watchdog can reenter it. Cold loss of RAMFS requires
   the normal launch/recovery path; do not assume a volatile marker survives
   removal of power.
4. **Cache a hash digest result.** The tested MicroPython hash object can reject
   repeated finalization; the stream verifier now obtains its digest once.

These are experimental fixture results, not a production bootstrap approval.
The fixture's simple JSON state does not prove recovery from a torn activation
journal, every rename boundary, a corrupt journal, or a crash after production
boot confirmation. It does not implement the daemon's two-poll confirmation
handshake. The actual app still needs a bounded watchdog policy and real UI /
network health criteria. A physical touchscreen launch was not tested.

The stock handoff behavior is also visible in Pimoroni's public
[launcher source](https://github.com/pimoroni/presto/blob/main/examples/main.py).
The installed launcher's required imports were separately checked on the board.

An earlier armed power-cut window expired and cleaned up. A later fresh run,
started at 2026-10-28 19:33 EDT, observed USB disconnect/reconnect after the warm
reset had settled, and the user confirmed removal of all power for that run.
After reconnecting, its persisted state was still unconfirmed. Reentering through
the stock handoff rolled back to A, cleared pending activation, and preserved A's
original SHA-256. Cleanup completed and AI Dash was relaunched.

The staged file measured **1,048,576 bytes**, its full planned length. This proves
the disposable fixture's recovery after physical power loss **after staging and
before confirmation**. That run did not establish a mid-write interruption.
Its strict mid-write case remains false in the raw result; the separate
`assessment.physical_power_cycle_recovery_passed` field records the successful
recovery. The unchanged raw outcome is not a claim that rollback failed. The
initial control's ordinary reset/re-enumeration remains separate from physical
power-loss evidence.

A final sustained-write run began at **2026-10-28 19:40 EDT**. The fixture kept
staging/rewrite activity active for 240,000 ms, longer than the host's 180-second
armed window, while reusing a scratch file occupying at most 1 MiB. The host
observed disconnect **57.518 seconds after arming**, the same board reconnected,
and the user confirmed the physical all-power cycle for this specific window.
Its persisted pre-recovery state had `writing: true` and remained unconfirmed.
The stock handoff then recovered A, preserved its hash, marked it confirmed,
cleared pending activation, and stopped the staging writer.

**Physical power loss during active staging passed in this disposable fixture.**
The recovered staged file again exposed a complete 1 MiB length; length alone
cannot locate an interruption within an ongoing rewrite. This run's phase
evidence is the bounded writer duration, persisted writing state, measured
disconnect time, and explicit user confirmation. It does not claim a precisely
timed flash-instruction/rename-boundary cut or a production updater test.
`power-mid-write.json` records the passing fixture case, cleanup completion, and
matched confirmation separately from `power-final.json`'s earlier post-staging
case. No further user power cycle is needed for these completed runs.

## Gate disposition and remaining evidence

| Gate from the lifecycle plan | Disposition |
| --- | --- |
| 1. Firmware capability matrix | Current build measured; supported official-release matrix remains open |
| 2. Storage headroom | Positive headroom measured; production margin and near-full-flash failure remain open |
| 3. Bounded A/B staging | Bounded file/TLS streaming measured; actual full-app A/B and concurrent UI remain open |
| 4. Stable startup/carousel | Corrected disposable handoff/reset/watchdog fixture passed; production migration remains open |
| 5. Interruption/rollback | Reset/watchdog/malformed plus physical cuts during staging and before confirmation passed in the fixture; production activation-journal boundary coverage remains open |
| 6. Code authentication | Explicit TLS positive/negative vectors passed on this build; deployment trust/time provisioning still required |
| 7. Operational failures | Corrupt/truncated/oversized/redirect responses covered; actual updater retries, daemon restarts, downgrades, and multiple boards remain open |
| 8. Written decision/recovery | This report; retain manual USB recovery and keep unattended OTA disabled |

The lifecycle plan separates this hardware-feasibility decision from production
deployment acceptance. Part 3 can implement reporting on the existing legacy
install now; Part 2 can then build and test the real updater behind its disabled
flag. Work that requires that implementation is not a prerequisite to writing it.
The implementation can use these measured primitives. Enabling
unattended updates requires the production bootstrap/journal and full-app staged
update to repeat the relevant gates, including power interruption at activation
boundaries. Do not label the prototype as that production implementation.

## Reproducing the non-destructive checks

From the repository root, use the repository virtual environment and an explicit
port. The TLS command's host must be this workstation's reachable LAN address;
it temporarily serves generated test bytes only.

```powershell
.venv/Scripts/python tools/presto_hardware_gate.py probe --port COM3 --out .preview/hardware-gate/capabilities-final.json
.venv/Scripts/python tools/presto_hardware_gate.py tls --port COM3 --host <workstation-LAN-IP> --out .preview/hardware-gate/tls-final.json
.venv/Scripts/python tools/presto_hardware_gate.py boot --port COM3 --out .preview/hardware-gate/boot-final.json
.venv/Scripts/python tools/presto_device.py check --port COM3
```

The `power` action requires a passing `boot-final.json` for the same port and a
person available to remove all device power when the fixture announces readiness.
USB re-enumeration from the initial scripted warm reset is explicitly excluded;
USB events alone do not prove physical power loss. A user's confirmation must be
recorded separately from the program's disconnect/reconnect observation.

Local evidence is under `.preview/hardware-gate/`: capability/TLS/boot reports,
stock-launcher metadata, and the separately labeled warm-reset control. Early
transport attempts and fixture corrections are not successful gate evidence.
The first direct-transport attempt stalled USB and required a user reconnect;
the retained runner uses the established captured `mpremote` CLI and always
attempts to relaunch AI Dash. No firmware or production app files were replaced.

Tests cover fixture syntax/path guards, the MicroPython module-name collision,
single-use hash finalization, private-path rejection, result size/ambiguity,
safe output, scratch cleanup, timeout relaunch, and independently verified TLS
fixtures. Full-suite results must distinguish this work from concurrent Part 1
edits; a mixed, changing working tree is not a release-validation snapshot.

The targeted hardware-tool suite passes independently: **16 tests passed**. An earlier required full-suite
run exercised 2,224 tests and ended with 6 failures, 23 errors, and 9 skips while
Part 1 was changing. Examples include the in-progress `Daemon.setup` instance
attribute shadowing the existing method and new admin routes missing from the
route documentation table. This checkpoint does not call that full run green or
validate the unfinished Part 1 implementation.

The full-suite rerun immediately after the sustained-write update exercised
**2,230 tests** in 375.279 seconds: **3 failures, 14 errors, 9 skipped**. At that
point Part 1's startup failure included `Daemon.setup()` being shadowed by an
instance attribute (`TypeError: 'NoneType' object is not callable`). That
historical log is `.preview/hardware-gate/latest-full-tests.txt`.

**Readiness rerun after the Part 1 fixes: all 2,230 tests pass (9 skipped), in
379.566 seconds.** This supersedes the earlier failing software baseline.
The complete passing log is `.preview/hardware-gate/readiness-full-tests.txt`.
The lifecycle plan now names Part 3 reporting as the next assignment and permits
controlled Part 2 implementation afterward, while leaving production OTA disabled
until its separate deployment acceptance passes.

Cleanup audit after the final confirmed power cycle (2026-10-28 19:43 EDT) found no generated gate entries or
scratch directories and no stale gate handoff marker. The installed firmware
`main.py` SHA-256 remained
`6610d28a17e5c221280a1766400656cc57aab1677c3eac558dfbe4ecb639d94e`.
The final repository USB check succeeded on COM3 for public app revision
`e315d0bb9663` and relaunched AI Dash. This is a dated hash observation, not a
claim of ongoing telemetry or a production OTA deployment.
