# Website analytics

The public site reports a small, fixed set of events to Google Analytics 4
(`G-82FD55P3RH`). This file is the reference for that list: what each event
means, which parameters it carries, and what has to be configured in GA4 for it
to be readable. `docs/user/privacy/index.md` is the reader-facing version.

**This is website code only.** The daemon, its admin page and the Presto app
import nothing from here and send nothing anywhere; `test_site_analytics.py`
fails if that stops being true.

## The surfaces

| Surface | Files | Live at |
| --- | --- | --- |
| Product page | `website/` | `/` |
| Documentation | `docs/user/` | `/docs/…` |
| Interactive demo | `docs/user/demo/` | `/docs/demo/` |

One shared script, `docs/user/javascripts/analytics.js`, is loaded on all
three: the documentation gets it from `extra_javascript` in `mkdocs.yml`, and
the product page loads it as `docs/javascripts/analytics.js`, which is where the
assembled site puts it.

## Consent

Analytics start only after the reader accepts, and one choice covers all three
surfaces.

Material stores its choices in `localStorage` under
`<scope pathname>.__consent`, and its scope is the **documentation root**, so
every documentation page and the demo already agree on one key — on the live
site, `/docs/.__consent`. It holds `{"analytics": true}` when accepted, `{}`
when rejected, and nothing at all when the reader has not been asked.

The product page sits one level above `docs/`, so it reads and writes that same
key rather than keeping a second one:

```js
new URL("docs/", location.href).pathname   // "/docs/" -> the key Material uses
```

**`extra.scope` is deliberately not set.** Material's scope also covers
`__palette` (the light/dark choice), `__tabs`, `__announce` and `__source`, so
moving it would silently reset every existing visitor's theme *and* orphan the
consent of anyone who has already accepted or rejected. Sharing the key the
theme already uses avoids all of that: existing choices keep working, and
`tools/check_site_analytics.py` asserts that the product page and the
documentation resolve the same path.

Nothing is queued before consent either: the script checks the stored choice
first and only then starts GA4, so a rejected visitor has no analytics code
running at all. Withdrawing consent expires first-party GA cookies (`_ga`,
`_ga_*`, `_gid`, `_gat` and `_gat_*`) at the site and documentation paths,
including parent-domain cookies. Other cookies and Material preferences are
preserved. The introduction (`getting-started/what-it-does/`) is ordinary
documentation interest: it reports `docs_link_click` from the product page
and no custom event within the documentation.

Custom events use Google's documented `gtag("event", name, params)` command.
Material keeps its own wrapper private, so the shared script exposes the same
arguments-based wrapper without starting a second loader.

## Events

Every event is lowercase `snake_case` and describes something a reader did.
There are no timer, scroll, animation or frame events.

| Event | Fires when | Parameters |
| --- | --- | --- |
| `install_click` | A link to a daemon install, Docker install, Presto setup or first-source setup guide is clicked, from any surface. | `install_method`, `link_path`, `link_location` |
| `docs_link_click` | The product page links into the documentation. | `link_path`, `link_location` |
| `outbound_click` | One of the counted links to another site is clicked. | `destination`, `link_domain`, `link_path`, `link_location` |
| `demo_start` | Start is pressed on the demo. | — |
| `demo_ready` | The demo has loaded and is running. | — |
| `demo_error` | The demo failed to start, or stopped. | `error_kind` |
| `demo_interaction` | The first tap, swipe or key press on the running demo. Once per page load. | `interaction` |
| `demo_scene` | A scene is chosen from the demo's menu. | `scene` |
| `demo_restart` | Restart is pressed. | — |

A page view comes from GA4 itself: Material's loader sends one for each
documentation page, and the product page's own loader sends one for itself.
The site sends no `page_view` events of its own, so they cannot be counted
twice.

### Parameters

| Parameter | Values |
| --- | --- |
| `install_method` | `daemon`, `docker`, `presto`, `source` |
| `link_location` | `header`, `footer`, `page`, or the id of the section the link sits in; on documentation pages, `content`, `navigation` or `footer` |
| `link_path` | The path of the link's target, without host or query |
| `destination` | `github_repo`, `github_issues`, `github_sponsors`, `pimoroni_store`, `support` |
| `link_domain` | The target's host |
| `interaction` | `pointer`, `keyboard` |
| `scene` | The scene's name from `demo.json`, with spaces replaced by `_` |
| `error_kind` | `start_failed`, `runtime_stopped` |

Deliberately absent: anything from the demo's sample accounts, any error
message text, any URL query string, and any identifier for the visitor.

## What has to be configured in GA4

Both are done in the GA4 admin page, once, and cannot be set from the repository.

**Custom dimensions** (Admin → Custom definitions → Create custom dimension),
scope *Event*, one per parameter that a report needs to break down by:

| Dimension name | Event parameter |
| --- | --- |
| Install method | `install_method` |
| Link location | `link_location` |
| Link path | `link_path` |
| Destination | `destination` |
| Link domain | `link_domain` |
| Interaction | `interaction` |
| Demo scene | `scene` |
| Error kind | `error_kind` |

`page_location`, `page_referrer`, `page_title` and `session_source` are built
in and need nothing.

**Key events** (Admin → Events → mark as key event). Mark:

- `install_click` — someone went to the installation instructions.
- `demo_start` — someone tried the demo.

Leave the rest unmarked: `docs_link_click` and `outbound_click` are interest,
not intent, and marking them too makes the key-event numbers unreadable.

**Enhanced Measurement** (Admin → Data streams → Enhanced measurement). Its
automatic page views and outbound-click events are separate event names from
the ones above, so enabling them does not double-count anything here. The
outbound-click event is the one place there is any overlap in meaning; the
site's own `outbound_click` knows which project link it was, which the
automatic one does not, so prefer it in reports.

## Checking that it works

- `python tools/check_site_analytics.py` (needs the browser environment,
  `.venv-browser`) drives a real browser against the assembled site and checks
  consent, the shared choice, one event per click, and the demo events. It
  serves `build/pages`, so build first; it reports nothing to the property.
  Add `--real-gtag` to load Google's actual library and verify that it produces
  one page view and one installation event on each surface. This requires
  internet access; all collection requests are intercepted locally and never
  reach the property. The default stub rejects direct array commands, and
  withdrawal tests seed host-only, parent-domain and documentation-path cookies.
  The check also verifies every generated documentation canonical URL.
- `python tools/check_docs_site.py build/pages` fails the build if a page loads
  Google Analytics as a tag (before consent), or without a consent banner and a
  footer notice linking the privacy page.
- `daemon/tests/tooling/test_site_analytics.py` keeps this file and the code in
  step, and keeps analytics out of the installed application.
- For real traffic, GA4's **Realtime** report shows events as they arrive, and
  **Admin → DebugView** shows them with every parameter when the page is opened
  with GA4's debug extension. A first useful check is that one page load
  produces exactly one `page_view`.
