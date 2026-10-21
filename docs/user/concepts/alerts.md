# Alert statuses and thresholds

Alerts are about the dashboard's *own* behaviour: when to make a noise, when
to change the LED colour, and when to scroll the list to a service that just
got worse. They never touch a provider account.

## What raises an alert

A percentage is mapped onto the status vocabulary, so a provider that reports
a percentage needs no code of its own:

| Threshold | Status | LED |
| --- | --- | --- |
| `warning_percent` (default 80) | `near_limit` | yellow |
| `critical_percent` (default 95) | `critical` | red |
| 100% | `rate_limited` | red |

An account takes the status of its worst window. Two thresholds apply to
every service by default, and each service can override them.

Only a **fresh, available** window alerts:

- a stale window keeps its stale presentation and raises nothing,
- an unavailable window never alerts,
- a window whose reset has passed never alerts,
- a window that resets within the next 30 minutes never alerts: it is about
  to clear.

## The sound

**The sound is off until you turn it on.** A desk device must not beep
unannounced, so the *Sound on a new warning* box in the **Alerts** section of
the admin page is unticked until you tick it. The LED, the thresholds and the
pop to top work with the sound off.

The device tracks every service separately, so a second service getting worse
still speaks while another one is already critical.

| Event | Sound |
| --- | --- |
| A service becomes a warning | one chirp |
| A service becomes critical, rate limited or offline | two chirps |
| A warning that persists | nothing, it is not repeated |
| A service recovers | nothing, recovery is silent |
| The first reading after boot | nothing, it is a silent baseline |

A long press on the Presto mutes the sound. The mute ends by itself, and
the screen says how long is left - see [the mute that expires](#the-mute-that-expires).

## The mute that expires

A long press on the Presto mutes the sound, so a desk device cannot chirp
in the middle of a meeting. The mute is not forgotten: it ends by itself.

| Setting | Default | What it does |
| --- | --- | --- |
| `mute_hours` | `4` | How long the mute lasts, in hours. 0 to 48. 0 means it lasts until you press again |

While the sound is muted, a small chip says how long is left:

| The chip | What it means |
| --- | --- |
| `MUTED 3H` | The mute ends in about three hours |
| `MUTED 45M` | The mute ends in about three quarters of an hour |
| `MUTED` | The mute has no end: it lasts until you press again |

The chip is in the bottom-right corner of the Home screen and of every
card, and it disappears when the mute ends.

- A long press while the sound is muted unmutes it at once.
- The mute ends **silently**. The statuses were tracked while it was on, so
  the Presto does not chirp for an alarm that began during the mute.
- A restart clears the mute: it is not stored anywhere.
- Changing `mute_hours` while a mute is running applies to the *next*
  mute, not to the running one.

The Presto has no clock of its own, so it counts the time with its own tick
counter. The daemon only sends the length.

## The LED

The LED shows the worst service on the dashboard, not only the account on the
open card:

| Colour | Meaning |
| --- | --- |
| red | critical, rate limited or offline |
| yellow | a warning |
| blue | a service in use |
| green | everything else |

A service that is merely in use never hides a warning.

## Quiet hours

Quiet hours are off by default. The daemon judges them with its own local
time and sends the flag in the payload, so the device needs no clock and no
time zone. While quiet hours are on, the device is silent and its light is
dimmed - the colour is kept, so a warning is still visible.

| Setting | Default |
| --- | --- |
| `quiet_hours` | off |
| `quiet_start` | `22:00` |
| `quiet_end` | `07:00` |

The times are the local time of the machine the daemon runs on.

## Pop to top

When a service gets worse, the Home list can scroll so that its row is on
screen. The rules exist so that the list never jumps while you are using it:

- the alarm must hold for 30 seconds before the list moves,
- a service moves the list at most once in ten minutes,
- the list never moves within 20 seconds of a touch or a scroll,
- a new alarm only acts when it is worse than the worst one already shown,
- your own position comes back a minute later, unless you scrolled since.

Pop to top is on by default and can be switched off in the **Alerts** section
of the admin page.

## The master switch and per-service settings

**Alerts** in the Settings tab has one master switch that turns alerts off
for every service at once. While the master is off, every per-service control
is disabled but keeps its value, and the service cards say *alerts off
(global)*.

Each service can then override the master:

- **alerts on or off** for that service, and
- its own warning and critical percent. A blank field means *use the global
  value*; the field shows the global value as its placeholder.

These per-service settings are stored in their own rows, keyed by the service
ID. Switching the alerts of one service never rebuilds the source and never
restarts its readers, and they survive an edit, a rename and a rebuild. They
are removed when the service is deleted.

A service's own thresholds are used only when the pair is usable: the
warning percent must sit below the critical one. When it does not - for
example because the global value changed underneath it - the service falls
back to the global pair, the daemon logs one line, and the admin page says
*override ignored*.

## What "alerts off" means

For that service, or for all of them when the master is off:

- no chirp,
- no LED escalation,
- no pop to top,
- no part in the overall accent.

It never hides data. The row, the numbers, the true threshold status colour
and the service's own card are unchanged. The daemon still computes the
service's true status.

## The Presto can switch one service's alerts

The bell in the corner of a service card switches that one service's alerts
on or off. It is the only thing the device ever writes, and it writes to
*your daemon*, not to a provider. See
[Alerts and the card bell](../guides/alerts-and-the-bell.md).

## See also

- [Alerts and the card bell](../guides/alerts-and-the-bell.md)
- [Configuration keys](../reference/configuration.md)
