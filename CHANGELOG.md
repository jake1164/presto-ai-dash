# Changelog

All notable changes to Presto AI Dash are documented here. Newest release at the top.

---

## [Unreleased]

### Website search discovery

- The marketing website now publishes a root sitemap containing its canonical
  public pages and a `robots.txt` referencing both the marketing and documentation
  sitemaps. The Pages build validates these files before publishing; the existing
  MkDocs documentation sitemap remains unchanged.

### Website analytics

- The product page, the documentation and the interactive demo now report a small, fixed set of events to Google Analytics 4: which pages are read, whether the demo is started and which of its scenes are tried, and clicks through to the installation guides, the repository and the Pimoroni shop.
- Nothing is sent, and no cookie is set, until the reader accepts the cookie banner. One choice now covers all three surfaces, so nobody is asked twice, and the cookie settings link in either footer changes it later. Rejecting it, or revoking an earlier acceptance, leaves no analytics code running at all.
- Withdrawing consent on either the product page or documentation removes first-party Google Analytics cookies. Custom events use the documented gtag command, and the introductory overview counts as documentation interest rather than installation intent. Documentation canonical URLs now use `https://presto-dash.com/docs/`.
- The installed application is unchanged and still sends nothing: the daemon, its admin page and the Presto contain no analytics of any kind.

## [v0.50.0] — 2026-11-01

The first release. A Pimoroni Presto becomes a dedicated, always-on monitor for your AI subscription quotas, reset countdowns and local AI services, fed by a small daemon on a computer or server on your network.

### Supported services

- **Claude Code**: subscription quota and recent activity
- **OpenAI Codex**: quotas, resets and banked credits
- **OpenCode Go**: subscription limits and resets
- **Ollama Cloud**: credit balance, quota and rolling activity
- **Ollama**: local models and server health
- **LiteLLM**: gateway usage, health and spend

Collection is read-only: the app never sends prompts, spends credits, or changes plans or limits on any provider account.

### On the Presto

- A Home list of every account with big labelled percentages, one bar per quota window, and the next reset countdown; swipe for a detail card per service
- Clear status at a glance: warning, near-limit, critical and rate-limited states, a "stale" note when a reading is old, and a full-screen notice when the daemon cannot be reached
- Alerts with LED colour and optional sound, a mute that lasts a set time, and quiet hours; alerts can be turned off per service from the Presto itself
- Device and daemon-release notices: a small icon when a new daemon release is out or when the Presto needs attention, with a read-only explanation a tap away

### Updates over the air

- The Presto updates its own app from your daemon: two app slots, a verified TLS download from the daemon's own certificate authority, SHA-256 checks of every file, and an automatic return to the previous app if the new one does not start properly
- The Presto gets a trusted time from the daemon after a power cut, so updates work on a board with no clock battery
- USB is needed only for the first setup and for recovery

### Setup

- **Set up a Presto** in the admin page prepares two downloads: `presto_setup.py` and an encrypted `presto_setup.enc`, opened with a one-time unlock code that is shown once and stored in neither file
- The script checks that the daemon answers, from your computer and then from the Presto over Wi-Fi, before anything on the Presto is changed; nothing is written until you confirm, and declining starts the Presto's existing app again
- An existing Presto keeps its settings, device identity and other files; firmware and the stock launcher are never touched

### The daemon and admin page

- Runs with `pipx install presto-ai-dash` or as a Docker image (`ghcr.io/jake1164/presto-ai-dash`), with a compose file for the container
- A local admin page to add and order services, store provider keys (encrypted in the daemon's database), and change settings
- A Status tab with the daemon, each Presto's installed and expected app revision and update history, connected clients, and the log
- Tells you when a newer Presto AI Dash is published, in the admin page and in the browser tab's title and icon; it never upgrades anything for you
- Remote collectors for Claude and Codex, for a daemon running in Docker or on another machine
- Demo mode: six sample accounts that cycle through every state, marked with a DEMO chip, contacting nothing and writing nothing

**Install**

```text
pipx install presto-ai-dash
ghcr.io/jake1164/presto-ai-dash:0.50.0
ghcr.io/jake1164/presto-ai-dash:latest
```
