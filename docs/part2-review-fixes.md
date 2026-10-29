# Part 2 review fixes — 2026-10-29

The fourteen findings from the Part 2 review are corrected in the working tree.
Ordinary OTA remains disabled. The current device remains on the legacy layout;
these changes do not constitute physical acceptance of migration, watchdog,
A/B activation, or rollback.

| Finding | Correction | Regression coverage |
| --- | --- | --- |
| Private/unrelated legacy files deleted | Retirement uses explicit public inventory; config/secret names are excluded. | `test_slot_bundle.py`: private backups and unrelated modules preserved |
| Uploads before directory creation | Board-validated preparation creates directories before the first upload. | `test_slot_bundle.py`: preparation precedes uploads |
| Unsupported JSON argument | Slot hashing constructs canonical sorted ASCII JSON without `sort_keys`. | `test_updater.py`: host parity with JSON serializer unavailable; physical pure-function probe after sync |
| Different targets rejected | An unchanged target is skipped; a different validated target can stage. | `test_slot_app.py`: distinct-revision app/coordinator round trip |
| Preflight bypassed | Resumable staging uses the real updater's readiness and per-manifest preflight checks, including watchdog capability. | `test_slot_app.py`: untrusted transport and incompatible bootstrap block staging |
| Incompatible production filesystem | The real adapter supports binary streams, bounded reads, listing, and free-space measurement. | `test_updater.py`: transfer using real files in a temporary directory |
| Permit identity/handshake lost | Accepted IDs are committed before file writes, retained through activation, and reported through confirmation; usable slot apps advertise capability. | `test_slot_app.py`: durable permit, duplicate offer, and two-poll round trip |
| Stale metadata/extra modules | Bounded final verification checks the inactive inventory and writes both generated revision files before activation. | `test_updater.py`: obsolete module removal, generated identity, and metadata write failure |
| Candidate entered after failed journal commit | Entry requires successful durable boot-state storage. | `test_bootstrap.py`: failed entry commit never calls candidate |
| Corrupt journal invents confirmed A | Missing/corrupt/incompatible state selects explicit USB recovery without importing a slot. | `test_activation.py`, `test_bootstrap.py`: state loss and newer format refusal |
| Later crash clobbers confirmation | Exception handling reloads the current durable journal. | `test_bootstrap.py`: confirm then crash preserves confirmed B and success |
| Lost progress bypasses retry limit | Later-phase/terminal transfer proof spends the attempt once; preflight blocks remain uncharged. | `test_device_ota.py`: three failures with no progress polls exhaust the budget |
| Boot epoch regresses | Migration preserves the public boot counter; rollback increments it; USB activation preserves boots since its previous check. | `test_activation.py`, `test_slot_bundle.py`: rollback increment and migration/check epochs |
| Non-atomic admin idempotency | Report/admin transitions share serialization; state, budgets, and saved response commit in one transaction with revision comparison. | `test_device_ota.py`: failed response INSERT rolls back everything, replay after restart, concurrent duplicates |

The additional hung-candidate gap has an implementation bound: the bootstrap
requires an already available firmware watchdog before importing candidate
code. The app feeds it only within its 180-second confirmation interval or
after durable confirmation. The 8-second watchdog setting, firmware availability,
and behavior under real display/network load still require hardware validation.
No watchdog is armed by a normal legacy-app sync.

Final verification (2026-10-29): **2,576 tests, 9 skipped, all passing** with the
repository `.venv` (409.757 seconds). The complete device suite also passed
separately: 788 tests. There are 23 additional regression tests relative to the
2,553-test handoff.

The real-code PSL Home/card renders and dock open/close interaction were
inspected. COM3 `sync` and independent `check` both succeeded for public legacy
revision **`e272d6cfb8e7`**. A subsequent read-only probe ran the corrected
canonical slot hash on the actual MicroPython runtime and matched the host,
and its in-memory activation journal round trip passed. AI Dash was relaunched
after each USB operation. These are dated observations, not continuous device
telemetry. No device journal, slot or bootstrap was installed by the probe.

The guarded app continues feeding after durable confirmation because a started
watchdog cannot be stopped, as documented by [MicroPython 1.29](https://docs.micropython.org/en/v1.29.0/library/machine.WDT.html).
The canonical serializer uses the supported [MicroPython JSON API](https://docs.micropython.org/en/v1.29.0/library/json.html).

Remaining production gates are unchanged: deliberate migration and power cycle,
actual full-app TLS staging and memory/timing/UI measurements, physical journal
power-loss boundaries, confirmation/lost-message recovery, near-full flash,
real trust/time provisioning, supported firmware profiles, and measured deadlines.
