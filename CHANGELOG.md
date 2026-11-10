# Changelog

All notable changes to Presto AI Dash are documented here. Newest release at the top.

---

## [Unreleased]

### Claude quota from the VS Code panel

- The VS Code panel never runs Claude Code's status line, so its quota used to arrive only from the usage cache, and only after you opened `/usage`. A new small Claude Code mod, installed with `python presto_collector.py claude-mod install`, writes the 5-hour and weekly quota to a local file after each response, and the collector's `watch` mode sends it. The reading is labelled `claude mod` and ages honestly: a file that is merely touched, or an idle panel, never looks fresh. Running Claude uses nothing extra: the mod reads no prompts, files or credentials, makes no network request and never asks Claude anything.
- The install command says what it will change, asks first, saves a backup of Claude Code's settings file beside it before adding one setting, and does nothing when run again. `claude-mod status` says whether it is working and `claude-mod remove` takes back exactly what was added. It needs Claude Code mods (the VS Code extension 2.1.299 or later).
- If the mod is installed but Claude Code has not loaded it, the admin page says `Reload VS Code (Claude mod)` and explains, and the Presto shows the same words while there is no reading yet. With the mod installed, a stale reading says `Quota 2h old - use Claude` on the admin page instead of sending you to `/usage` or a terminal; the Presto shows the reading's age (`stale 2h`) as before. The detail card now says `Quota via mod`. A computer that never installed the mod sees none of this.
- The Claude card describes a reading from the mod as the mod's, instead of saying the VS Code panel gets no quota. In `watch` mode, the note that a token file can be read by other users now appears once, not every round.
- `presto_collector.py` is now version 3 (the mod's reader and install command are inside it). Existing installs show "collector update available".

### Quota from several computers

- When two computers report the same Claude account, each quota window now keeps the newest reading. A delayed or repeated older report no longer replaces a newer one, and a report for a window that has since reset no longer replaces the new window. Before, whichever report arrived last won and was stamped as new.
- A report is now dated by when its sender says it was made, as long as that is within a day before and five minutes after the daemon received it, and never later than that. A report that arrives late keeps its real age. A wrong clock can make a reading look older, never newer. Outside those bounds the daemon uses its own receive time, as before.
- A report that only carried the usage cache can no longer hide a status-line reading made just before it.

### Claude readings and containers

- A Claude usage-cache reading now counts as stale after 30 minutes by default, down from two hours. Two hours let a reading that was several points behind real use still look current. Accounts that already saved the old value keep it; change **Mark usage cache readings stale after** to use the new one.
- Where the daemon cannot see any Claude Code files, in a container or on a server that only receives a collector's readings, the Claude form hides the settings that read files on that machine (settings file, activity, usage cache and state file) and points to the remote collector. The stale thresholds stay, and the usage-cache one is labelled as applying to a collector's readings. **Show local reader settings** brings the hidden ones back for a profile in an unusual place, and an account that already turned a local reader on keeps seeing them. If a Claude profile is present, the full form is shown as before.

### Remote token status-line commands

- The status-line commands shown with a new remote token now carry the real daemon address, taken from the host the admin page was opened with, instead of a `REPLACE-WITH-DASH-ADDRESS` placeholder. They match the address already filled in for the setup file.
- Their token file is now `~/.presto-ai-dash/<name>.token`, the folder where `presto_collector.py setup` saves it, instead of a different folder the person had to create. Only the path to `presto_collector.py` itself is still the person's to change.
- A new remote token now shows the simple route first (download the setup file, run `setup`, then `watch`). The terminal status line is an optional extra below it, showing the command for the computer the page is open on and the other kind in a fold-out.
- `presto_collector.py` is now version 2: typed into a terminal by hand, `claude-statusline` prints a one-line hint and exits with status 2 instead of waiting silently for input. Claude Code's own use is unchanged. Existing installs show "collector update available".

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
