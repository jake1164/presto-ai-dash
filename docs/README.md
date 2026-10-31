# Where documentation lives

The **documentation site**, <https://jake1164.github.io/presto-ai-dash/docs/>, is
the user-facing truth. It is built from `docs/user/` only (see `mkdocs.yml`).
The product placeholder is maintained separately in `website/`. After the strict
MkDocs build, `tools/build_pages_site.py` assembles `build/pages/`: the product
at the root, documentation under `docs/`, and redirects for the old docs
URLs (including `/demo/`). Both are published as one GitHub Pages artifact.
`tools/pages_legacy_urls.json` preserves the published page paths and heading
anchors. The Pages build fails if a target disappears; when moving a page,
update its replacement path and keep old heading IDs as aliases. Old homepage
heading links forward to `/docs/`; the bare root opens the product website.

Everything else under `docs/` is the repository's internal reference:
contracts, status and history, for agents, tests and contributors. It is
never published.

## Internal reference files and their public pages

These files overlap the site. They stay because agents, tests and the plan
link to them, and because they hold detail the public pages leave out. Each
starts with a pointer to its public page.

| Internal file | Public page | What the internal file holds that the public page does not |
| --- | --- | --- |
| [setup.md](setup.md) | [Install the daemon](https://jake1164.github.io/presto-ai-dash/docs/getting-started/install-the-daemon/), [Set up the Presto](https://jake1164.github.io/presto-ai-dash/docs/getting-started/set-up-the-presto/), [The preview and the device sync](https://jake1164.github.io/presto-ai-dash/docs/developers/preview-and-device/) | VS Code debugging tips (where to put breakpoints), the PSL receipt under `.preview/`, and the original static-demo walkthrough of M1 (historical). Everything else it says is on the public pages, more current |
| [configuration.md](configuration.md) | [The admin page](https://jake1164.github.io/presto-ai-dash/docs/guides/admin-page/), [Configuration keys and environment variables](https://jake1164.github.io/presto-ai-dash/docs/reference/configuration/), [Ports and endpoints](https://jake1164.github.io/presto-ai-dash/docs/reference/ports-and-endpoints/) | The full management API table (every `/api/admin/...` route), the master-key recovery rules (missing or wrong key, moving between machines, no automatic rotation), the "what is stored where" table, and the detailed contracts of the lazy card detail and of the device's one write |
| [sources.md](sources.md) | [Sources](https://jake1164.github.io/presto-ai-dash/docs/concepts/sources/), the per-source [Guides](https://jake1164.github.io/presto-ai-dash/docs/guides/claude/), [How a source works](https://jake1164.github.io/presto-ai-dash/docs/developers/how-a-source-works/) | The `SourceSnapshot` sketch, the per-status LED table (the public page groups the colours), the demo scenario's exact scene order, and the LiteLLM notes on what is deliberately not reported |
| [claude.md](claude.md) | [Claude](https://jake1164.github.io/presto-ai-dash/docs/guides/claude/), [No Claude data](https://jake1164.github.io/presto-ai-dash/docs/troubleshooting/no-claude-data/), [The VS Code panel](https://jake1164.github.io/presto-ai-dash/docs/troubleshooting/vs-code-panel/) | The implementation checkpoint of 2026-10-11 with its live verification, the settled panel finding, and the readers' internal rules in more detail (storage, freshness, hint and detail-card order) |
| [codex.md](codex.md) | [Codex](https://jake1164.github.io/presto-ai-dash/docs/guides/codex/) | The verification record of 2026-10-11 (CLI version, the observed window) and the remaining M8 acceptance work |
| [quotas.md](quotas.md) | [Quotas and where numbers come from](https://jake1164.github.io/presto-ai-dash/docs/concepts/quotas-and-provenance/), [The status payload](https://jake1164.github.io/presto-ai-dash/docs/reference/status-payload/) | **The quota status contract**: the window field table, the optional `used_amount`, `limit_amount` and `currency`, the Ollama Cloud `activity` object, the per-source coverage table, and the implementation verification of 2026-10-11 |
| [limitations.md](limitations.md) | No single page: the limits are on each guide, in [Troubleshooting](https://jake1164.github.io/presto-ai-dash/docs/troubleshooting/daemon-offline/) and in [The preview and the device sync](https://jake1164.github.io/presto-ai-dash/docs/developers/preview-and-device/#what-the-preview-does-not-show) | The hardware that tests cannot automate (panel, touch controller, LEDs, buzzer, radio, power and button, with their parts and pins), the two-touch caveat of the touch controller, LiteLLM's endpoint-stability policy, and the trend indicators |
| [architecture.md](architecture.md) | [Architecture overview](https://jake1164.github.io/presto-ai-dash/docs/developers/architecture/) | The milestone-by-milestone history of the architecture, the deployment roadmap (tray, binaries), the LiteLLM/Ollama network topology and the original scope and non-goals. The public page is the current architecture; several statements here are historical and marked |

`docs/docker.md` was deleted on 2026-10-21: it was byte-identical to
[Install with Docker](https://jake1164.github.io/presto-ai-dash/docs/getting-started/install-with-docker/)
(`docs/user/getting-started/install-with-docker.md`), which is now the only copy.

## Status, plans and history

The product name, approved messaging, artwork and integration rules are
recorded in [Branding](branding.md).

| File | What it is |
| --- | --- |
| [milestones.md](milestones.md) | What is built and what is open (a "Current status" at the top), then the dated checkpoints |
| [remote-ingest-plan.md](remote-ingest-plan.md) | The live plan: hard rules, phase table, decisions and the open backlog |
| [history/remote-ingest-done.md](history/remote-ingest-done.md) | The verbatim record of everything finished in that plan |
| [presto_ai_dash_project_plan.md](presto_ai_dash_project_plan.md), [implementation-readiness.md](implementation-readiness.md), [m3-readiness.md](m3-readiness.md), [m6-readiness.md](m6-readiness.md), [subscription-monitoring.md](subscription-monitoring.md), [future.md](future.md) | Historical planning documents, each marked as such, with dated corrections where a statement would mislead about the present |
| [requirements.txt](requirements.txt) | The pinned versions for building the site |
