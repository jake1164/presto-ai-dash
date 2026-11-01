# What the tests cannot cover

Developer notes. The user-facing limits of each service are on its guide on the
documentation site; this page is only the facts a contributor needs when a test
passes and the device still misbehaves.

## Hardware

The daemon is testable on a development machine with no external services; the
HTTP tests use localhost. The Presto is not. These need the physical board:

| Area | Why it cannot be automated |
| --- | --- |
| Pixels on the 480x480 IPS panel | Needs the ST7701 driver and PicoGraphics |
| Capacitive touch gestures | Needs the FT6236 controller |
| The seven SK6812 LEDs, colour and brightness | Needs the LED strip on GPIO 33 |
| Piezo sound | Needs the buzzer on GPIO 43 |
| Wi-Fi association | Needs the RM2/CYW43439 radio and a real access point |
| Backlight and battery | Needs the power hardware |
| The boot button | Needs the button on GPIO 46 |
| Memory and CPU limits | PSL runs the real app on the host, not on the RP2350 |

PSL (`tools/presto_preview/`) runs the real `main.py` and `ui.py` against an
emulated 240x240 display with the firmware's own bitmap fonts, so layout, text
fit and gestures are checked. It does not show sound, LEDs, memory use or
non-ASCII text. See [The preview and the device sync](https://jake1164.github.io/presto-ai-dash/docs/developers/preview-and-device/).

The mitigation is structural. Everything decidable without hardware lives in
modules that are unit-tested on the host (`cards.py`, `gestures.py`, `alerts.py`,
`net.py`, `home.py`). Only `main.py` and `ui.py` may import `presto`, `machine`,
`picographics` or `picovector`, and `daemon/tests/device/test_presto_structure.py`
enforces it. The one other exception is the production `bootstrap/boot.py`, which
imports `machine` to arm the watchdog.

A change under `presto/` changes the app revision, which is what makes a deployed
Presto fetch an update over the air. That includes a comment or a docstring.

## Touch

Pimoroni documents that two touches aligned horizontally, for example a
two-finger vertical scroll, are not registered correctly. That is the touch
controller, not this project. The gesture recogniser therefore acts only on a
single touch and ignores multi-touch (`presto/gestures.py`).

## Not a security boundary

The shared device token is bearer authentication for a trusted LAN. It has no
per-user identity or expiry and travels in a header over plain HTTP. Do not
expose the device port to the internet. Updates over the air are the exception:
that route is TLS, verified against the daemon's own certificate authority.
