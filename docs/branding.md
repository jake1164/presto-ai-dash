# Presto AI Dash branding

## Identity

- Product name: **Presto AI Dash**.
- Tagline: **Always in view. Never in your way.**
- Description: Monitor AI usage, subscription limits, reset times, and service
  health across your accounts, all at a glance on a dedicated display.
- Supporting line: **No windows. No screen clutter. Just the information you need.**

Use the full product name in anything user-facing. Repository, package, container,
module and protocol names do not change. The project is independent of Pimoroni:
do not imply an official product or endorsement. Only the Pimoroni Presto is
supported. Its panel is 480x480; the app's logical canvas is 240x240.

## Artwork and palette

The mark is the **Watchful Meter**: an orange display face with two quota-meter
eyes and a cyan smile. Masters and exports are in
[`assets/brand/`](../assets/brand/README.md). Keep its geometry and transparent
background. Add no lettering, sparkles, stand or new features.

| Role | Colour |
| --- | --- |
| Bezel and quota arc | `#FF8B3E` |
| Usage ring and smile | `#31D4E8` |
| Face | `#17283F` |
| Inactive ring | `#30425D` |

Use the colour SVG in web UI and the ICO as the favicon fallback. The SVG favicon
only trims the outer canvas padding, so the mark fills the browser's tab slot; the
master is unchanged. There are monochrome dark and light masters for constrained
backgrounds. The 16px mark is a compact fallback; use 32px or larger for anything
promotional. Brand colours never replace the meanings of status, quota-period,
stale, sample-data, alert or update colours.

## Generated copies

After changing approved artwork, from the repository root:

```powershell
.venv/Scripts/python tools/build_brand_assets.py
.venv/Scripts/python tools/build_brand_assets.py --check
```

The stdlib-only generator copies the SVG and ICO into the admin page, the docs and
the website, and writes the admin page's `favicon-update.svg` (the favicon with an
amber badge, shown while a daemon update is available). It also reduces the
approved 16/32/64px RGBA exports to four-colour scanline spans in `presto/brand.py`,
which ships with the Presto app. The derived files are checked in so that docs,
wheel, Docker and source builds need no graphics tools. Do not edit them by hand;
`--check` compares them byte for byte.

## On the device

`ui.py` draws the 64px logo, the full name and the two-line tagline at startup. The
connecting, Wi-Fi, offline, error and device-notice screens use the same 64px logo
centred at the top with the name under it. Home and the service cards carry no logo:
their footer belongs to navigation and status. All device text is ASCII in bitmap
fonts.

Without a stale, demo or mute chip showing, Home takes back the 16 pixels the chip
band would use, so the overflow names and the notice dock sit closer to the page
dots. When a chip appears the space returns, and drawing and hit testing both use
the same reserved layout.

## The stock carousel

Both app entry points put the full name and tagline in the carousel metadata, but
the stock launcher accepts only ten built-in font icons and has no image or drawing
hook, so the carousel cannot show the logo. The `monitoring` icon stays as the
fallback; a path in `# ICON` does nothing. Showing the logo there needs a launcher
extension, and routine sync must keep leaving the stock launcher and firmware alone.
Upstream: [Pimoroni carousel](https://github.com/pimoroni/presto/blob/main/examples/main.py).

The A/B bootstrap is separate from the app slots. Its branding metadata changes in
source and in release bundles, but a device already on the slot layout needs an
explicit bootstrap update over USB to pick it up. A branding change must not turn
into an implicit migration.
