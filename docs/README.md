# Documentation

The **documentation site**, <https://jake1164.github.io/presto-ai-dash/docs/>, is
where users and contributors read about the product. It is built from `docs/user/`
only (see `mkdocs.yml`). If something is on the site, it is not repeated here.

What is in this folder is for people working on the code:

| File | What it is |
| --- | --- |
| [limitations.md](limitations.md) | What the tests cannot cover: the hardware, the touch controller's caveat, and the device token's limits |
| [branding.md](branding.md) | The name, tagline, artwork rules, how the generated copies are made, and what the device draws |
| [requirements.txt](requirements.txt) | The pinned versions for building the site |
| `overrides/` | MkDocs theme overrides (the site loads nothing from a third party; see `mkdocs.yml`) |

## The site build

After the strict MkDocs build, `tools/build_pages_site.py` assembles `build/pages/`:
the product page at the root, the documentation under `docs/`, and redirects for
the old documentation URLs (including `/demo/`). Both publish as one GitHub Pages
artifact. The product page is maintained separately in `website/`.

`tools/pages_legacy_urls.json` keeps the published page paths and heading anchors.
The Pages build fails if a target disappears, so when you move a page, update its
replacement path and keep the old heading IDs as aliases. Old homepage heading
links forward to `/docs/`; the bare root opens the product website.

## Where things go

- How to install, configure, run or fix it: a page on the site (`docs/user/`).
- How the code is built, for someone changing it: the site's Developers section, or
  a file in this folder if it is not something a user should land on.
- What was built when, status, plans and to-do lists: not in the repository. The
  release notes are [CHANGELOG.md](../CHANGELOG.md); everything else is git history.
