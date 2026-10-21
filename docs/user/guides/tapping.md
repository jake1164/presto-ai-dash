# Tapping rows and dots

The Presto's screen is 240x240 and touch is the only input. This page lists
every gesture and what it does.

## The gestures

| Gesture | What it does |
| --- | --- |
| Swipe left | The next card |
| Swipe right | The previous card |
| Drag up or down | Scroll the Home list |
| Tap | Open the thing under your finger |
| Long press | Mute or unmute the sound |

A swipe over a control is never a tap, and a tap is only a tap when it was
short and stayed within the drag slop. That is why a scroll never opens a
card by accident.

## Tapping a Home row

A tap on a row of the Home list opens that account's card. This is the
shortest path from "something looks wrong" to the numbers behind it.

Tapping anywhere else on Home does nothing in particular: both periods of a
window are always visible there, so there is nothing to expand.

## Tapping a dot

The row of dots at the bottom of the screen shows the cards. The current one
is filled. Tapping another dot jumps straight to that card, which is useful
when one of the other services is the interesting one.

The dots are also the page count. With one card there is a single dot and
nothing to tap.

## Tapping a card

A tap on a card, anywhere that is not the bell, opens the **detail view**:
the extra lines the card holds, such as the collector trail, the activity
line or the quota age. A second tap closes it again.

The detail lines are fetched from the daemon when the card is opened, so a
service with a lot of detail does not make every poll bigger.

## Tapping the bell

The bell in the corner of a service card switches that service's alerts on
or off. It has its own large target, and a tap anywhere else on the card
still opens the detail view. See
[Alerts and the card bell](alerts-and-the-bell.md).

## Long press

A long press anywhere toggles the mute state. The MUTED indicator appears in
the Home title bar while the sound is off. Press and hold again to bring it
back.

Muting turns the chirp off. It does not change the LED colour, and it does
not change any number.

## When a tap does nothing

- On the offline screen, taps are ignored: there is nothing to open.
- On a Home row that is a local service with no card of its own, the tap has
  nowhere to go.
- On the bell while the master alert switch is off, the card explains why
  instead of switching anything.

## See also

- [Home display and the overflow strip](home-display.md)
- [Alerts and the card bell](alerts-and-the-bell.md)
