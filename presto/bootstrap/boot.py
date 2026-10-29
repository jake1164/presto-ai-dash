"""The device half of the boot manager: pick a slot, then run it.

Everything that can be reasoned about is in ``activation.py``. This module
only does what a CPython test cannot: mount the volatile launch handoff, import
the chosen slot, and reset the board after a crashed candidate.

Deliberate limits:

* A candidate needs a firmware-provided watchdog before any of its modules
  execute. The app feeds it only while its confirmation deadline is valid or
  after durable confirmation. Unsupported runtimes refuse candidate entry.
* **Only an unconfirmed candidate re-arms the handoff.** A confirmed slot keeps
  the stock behaviour: the carousel runs, and the user's own entry launches the
  dashboard.
* **A crashed confirmed slot is not reset in a loop.** The exception is raised
  after the failure is recorded, so the stock launcher shows its own error and
  the next successful boot can report what happened.
"""

import os
import sys
import time

import activation

# Only used for the recorded evidence and for the volatile handoff path.
ENTRY_NAME = "ai_dash_boot.py"
LAUNCH_MARKER = "/ramfs/launch.txt"

SLOT_ROOT = "/ai_dash_slots"
# The private settings folder keeps the path the setup utility already writes,
# so a migration never moves a private file. It is on the path for ``config``
# only, and the slot comes first.
SETTINGS_FOLDER = "/ai_dash_files"


def _rearm():
    """Re-arm the stock launcher handoff so a warm reset re-enters us.

    A flash-backed ``/ramfs`` directory is not evidence that RAMFS is mounted,
    so it is mounted explicitly first. Failure is not fatal: it only means a
    warm reset will not re-enter the candidate, and the next power-on still
    finds the unconfirmed activation in the journal.
    """
    try:
        import psram
        psram.mkramfs()
    except Exception:
        pass
    try:
        with open(LAUNCH_MARKER, "w") as handle:
            handle.write(ENTRY_NAME)
    except Exception:
        pass


def _reset():
    try:
        machine = sys.modules.get("machine")
        time.sleep(1)
        machine.reset()
    except Exception:
        pass


def _crashed(fs, journal, decision):
    """Record a crashed slot and, for a candidate, return to the good one."""
    durable, source = activation.load(fs)
    if durable is None:
        return
    journal = durable
    pending = journal["pending"]
    # A running app may have confirmed since the startup snapshot was taken.
    if pending is None:
        return
    journal = activation.record_failure(
        journal, "rolled_back", "boot_crash",
        target=pending["target"] if pending is not None else None,
        generation=pending["generation"] if pending is not None else None,
        attempt=pending["attempt"] if pending is not None else None)
    try:
        activation.save(fs, journal)
    except Exception:
        pass
    if decision.get("candidate"):
        # The previous confirmed slot is intact and is what the next entry
        # runs; recording the failure first stops a rollback loop.
        _reset()


def main():
    fs = activation.FileStore()
    journal, source = activation.load(fs)
    journal, decision = activation.on_boot(journal, source)
    if decision["action"] != "run":
        raise RuntimeError("Activation state unavailable; repair AI Dash over USB")
    if decision["changed"]:
        try:
            activation.save(fs, journal)
        except Exception:
            raise RuntimeError("Boot state could not be committed; USB recovery required")
    watchdog = None
    if decision["candidate"]:
        try:
            # Hardware imports remain in main.py. Only an already available
            # firmware module may provide the bootstrap's watchdog primitive.
            runtime = sys.modules.get("machine")
            watchdog = runtime.WDT(timeout=8000)
        except Exception:
            pending = journal["pending"]
            journal = activation.record_failure(journal, "blocked", "firmware_unsupported",
                pending["target"], pending["generation"], pending["attempt"])
            activation.save(fs, journal)
            raise RuntimeError("Candidate watchdog unavailable; USB recovery required")
        _rearm()
    slot_folder = SLOT_ROOT + "/" + decision["slot"]
    if SETTINGS_FOLDER not in sys.path:
        sys.path.append(SETTINGS_FOLDER)
    if slot_folder not in sys.path:
        sys.path.insert(0, slot_folder)
    os.chdir(slot_folder)
    try:
        import ai_dash_app
        # The app reads its own start from here: which slot it is, which boot
        # this is, and whether it is still an unconfirmed candidate. The legacy
        # launcher sets nothing, so an old install never guesses a slot.
        ai_dash_app.BOOT_CONTEXT = {"journal": journal,
                                    "context": activation.context(journal, source),
                                    "watchdog": watchdog}
        ai_dash_app.main()
    except Exception:
        _crashed(fs, journal, decision)
        raise
