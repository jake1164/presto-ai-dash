# Presto AI Dash branding

## Approved identity

- Product name: **Presto AI Dash**.
- Primary tagline: **Always in view. Never in your way.**
- Description: Monitor AI usage, subscription limits, reset times, and service
  health across your accounts—all at a glance on a dedicated display.
- Supporting statement: **No windows. No screen clutter. Just the information you need.**

The project is open source and self hosted. Its distinguishing benefit is
always-visible information on a dedicated display without taking up computer
screen space. Keep the full product name in user-facing branding. Repository,
package, container, module and protocol names do not change. The project is
independent of Pimoroni; do not imply an official product or endorsement.

## Artwork and palette

The approved **Watchful Meter** is the friendly orange display face with two
quota-meter eyes and a cyan smile. The supplied masters and exports live in
[`assets/brand/`](../assets/brand/README.md). Preserve its geometry and
transparent background. Do not add lettering, sparkles, a stand or new features.

| Role | Colour |
| --- | --- |
| Bezel and quota arc | `#FF8B3E` |
| Usage ring and smile | `#31D4E8` |
| Face | `#17283F` |
| Inactive ring | `#30425D` |

Use the colour SVG for web UI and the supplied ICO for favicon fallback.
The derived SVG favicon tightens only the outer canvas padding so the mark
fills more of the browser's fixed-size tab slot. Admin, docs, the temporary
homepage and PSL use this same favicon; the master artwork is unchanged.
Monochrome dark and light masters are available for constrained backgrounds.
The 16px mark is a compact fallback; larger welcome and promotional treatments
use at least 32px. Brand colours do not replace status, quota-period, stale,
sample-data, alert or update meanings.

## One source, all delivery paths

Run from the repository root after changing approved artwork:

```powershell
.venv/Scripts/python tools/build_brand_assets.py
.venv/Scripts/python tools/build_brand_assets.py --check
```

The stdlib-only generator copies the SVG and ICO to the admin, MkDocs and
temporary-homepage assets. Those derived files are checked in so ordinary
docs, wheel, Docker and source builds require no graphics tools. The generator
also reduces the approved 16/32/64px RGBA exports to four-colour transparent
scanline spans in `presto/brand.py`. It adds no image decoder or hardware import.
That public Python module is included by the existing shared app inventory in
PSL, the browser demo, setup downloads, release bundles and USB deployments.
Do not hand-edit derived files; the regression check compares them byte for byte.

On the device, `ui.py` draws a prominent logo, full name and two-line tagline
at startup. Connecting, Wi-Fi, offline, error and device-notice screens use
the same large 64px logo centered at the top, with the product name below.
Home and service cards have no product logo: their footer is reserved for
navigation and status. All device
text stays ASCII and uses bitmap fonts.

In a live view without stale/demo/mute chips, the Home list reclaims 16 pixels
and the overflow names and warning/release dock sit closer to the page dots.
When a chip appears, its space is restored and both drawing and hit testing
use the same reserved layout. Scrolling does not change the footer positions.

## Stock carousel limitation

Both app entry points publish the full name and approved tagline in carousel
metadata. On 2026-10-30, inspection of the public `/main.py` on COM3 confirmed
that the stock launcher accepts only ten built-in font icons. It has no custom
image or drawing hook. The `monitoring` chart remains a compatibility fallback.
An SVG path in `# ICON` would not display the logo.

Custom Watchful Meter artwork in the stock carousel is pending a separately
reviewed launcher extension. Routine sync must keep preserving the stock
launcher and firmware; this branding change neither patches it nor migrates
the device's installation layout. Upstream reference:
[Pimoroni carousel](https://github.com/pimoroni/presto/blob/main/examples/main.py).

The stable A/B bootstrap is maintained separately from app slots. Its branding
metadata is updated in source and release bundles; an existing slot-layout
device may require explicit bootstrap maintenance over USB. Do not turn an
ordinary branding update into an implicit migration or bootstrap replacement.

## Website scope

The temporary homepage keeps its existing structure, links and coming-soon
notice, with the approved icon and favicon. The larger product homepage is a
future post-sprint project: do not implement its hero redesign, feature
sections, hardware roadmap, new framework or backend as part of branding.
Preserve the existing Pages routing, old deep links and analytics consent.

Only Pimoroni Presto is currently supported. Its panel is physically 480x480;
the app's logical canvas is 240x240. Additional display support must be
implemented and tested before being advertised. "Not just for Presto anymore."
is a possible future announcement, not a current claim or replacement tagline.

## Verification

Run the required daemon suite, the asset consistency check, the strict MkDocs
build, Pages assembly/link and privacy checks, and release packaging checks.
Inspect the admin and homepage at desktop and phone widths and in supported
themes. Inspect PSL Home, cards, startup, empty, stale, offline, Wi-Fi error,
display-error and lifecycle views. Then run the required explicit-port USB
sync and check and report the exact revision verified. Simulator screenshots
do not prove physical-device memory, performance, sound or LED behaviour.
