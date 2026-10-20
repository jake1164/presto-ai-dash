# Limitations

The daemon supports fake data, Ollama health, and LiteLLM routed usage. The
Presto app supports authenticated daemon polling and a separate static preview
mode. On-device Wi-Fi, authenticated polling, and recovery are verified in
`milestones.md`. Trends are deferred. Live Home shows real subscription quota
bars (Codex, Claude, OpenCode Go) and Ollama Cloud activity; only the static
preview mode uses fixed values, labelled DEMO. Known gaps: Codex dashboard
sign-in, Claude quota from the VS Code panel (it does not run the status line,
so only terminal sessions report quota; the optional usage cache reader adds
an unofficial route for panel users), and Ollama Cloud credit balance.

Things this project honestly cannot do, and why. If a number on the Presto
looks wrong or missing, it is probably here.

## Direct Ollama usage cannot be fully measured

Ollama has no endpoint that reports past requests. There is no prompt log, no
token history, no duration history. The API exposes:

- what models exist (`/api/tags`)
- what is loaded right now (`/api/ps`)
- the version (`/api/version`)
- timing fields **for a request you just made** (`/api/generate`)

So "tokens used today against Ollama directly" is not reconstructable from
Ollama alone. The project plan says this in §6.3 and it is correct.

**What the daemon shows:** reachability, version, loaded model, available model count,
and — only if a collector has written a samples file — the last measured
request with a computed tokens/sec.

**What v0 does not do:** invent a number. If there is no measured sample the
card reads "last measured: unavailable".

**A future fix:** a desktop collector, local Ollama shim, wrapper, or voluntary
reporter that observes requests and supplies samples the daemon can read.
Tokens/sec arithmetic is implemented in M4 and uses the sample's nanosecond
duration. The collector is future work without a release assignment.

## LiteLLM sees only routed traffic

The target topology is `toontown-int-srv1 -> LiteLLM -> jake-desktop -> Ollama`,
with direct `jake-desktop -> Ollama` calls as well. LiteLLM usage totals cover
only the first path. They must never be presented as complete desktop usage.

## LiteLLM spend may be permanently unavailable

LiteLLM spend tracking requires a database. If the proxy runs without
`DATABASE_URL`, there is nothing to report. The source shows spend as
unavailable when LiteLLM does not return it.

The card shows **"unavailable"**, not `$0.00`. Zero spend and unknown spend are
different facts and conflating them would be dishonest.

## LiteLLM rate-limit warnings are not synthesised

There is no verified LiteLLM endpoint that reports "you are approaching a rate
limit". Rather than guess, v0 does not report it.

The `rate_limited` and `near_limit` statuses still exist in the status model and
in the `fake` source, so the UI, LED and sound paths are fully exercised. A
future source can emit them properly.

## LiteLLM "recent errors" is a count, not a list

`GET /user/daily/activity` returns `failed_requests` — a number for the day.
It does not return the individual failures. `GET /spend/logs` would, but that
is out of scope for a glanceable desk display.
The count remains visible as `Errors today`; it does not by itself trigger a
warning because it cannot distinguish earlier failures from current ones.
Detecting an ongoing request-failure incident needs a future rule based on
recent failures rather than the whole day's total.

## LiteLLM endpoints are a moving target

LiteLLM's API stability policy is explicit: only endpoints present in the
OpenAPI spec are a public contract. Everything else is private and may change
or disappear in any release without notice.

The planned LiteLLM source must fail soft. An unexpected status code, a
missing field, or a 404 degrades that one metric to "unavailable". It never
raises, and it never takes the card down with it.

If LiteLLM upgrades and a metric goes blank, this is the first place to look.

## Claude's usage cache is an unofficial file

The VS Code panel does not run Claude Code's status line, so a panel user
gets no quota from it. The optional usage cache reader reads the JSON file
Claude Code writes for its own `/usage` dialog (`~/.claude.json` in the
default layout) and turns it into quota numbers. Three limits follow from
that:

- **The format is not an official interface.** It can change without notice.
  The reader then reports the usage cache as unavailable and says which
  field changed; the terminal export and every other source are unaffected.
- **The cache refreshes at irregular moments**, not on every response, and
  the file can be hours old. Opening `/usage` in Claude Code forces a
  refresh. The numbers are always labelled with their age and their source.
- **The file is sensitive.** It also holds the account id, the email, the
  organisation, the project history and the MCP server list. The reader
  opens it read-only, extracts only the quota fields and never writes,
  renames, locks or forwards anything else.

The banked "Full reset" of Claude's usage page is not in this file, and the
reader never guesses at a window it does not have.

## Trends need history

Up/down indicators compare the newest snapshot to the oldest within a 24 hour
lookback. With fewer than two snapshots in that window there is nothing to
compare, and the indicator reads "–" rather than guessing a direction.

## What cannot be automated in tests

The daemon is testable on a development machine without external services;
HTTP tests use localhost networking. The
Presto is not. These require physical hardware and are covered by the
Milestone 13 release manual checklist (formerly M7):

| Area | Why it cannot be automated |
| --- | --- |
| Pixels on the 480×480 IPS panel | Needs the ST7701 driver and PicoGraphics |
| Capacitive touch gestures | Needs the FT6236 controller |
| Seven SK6812 LED colours and brightness | Needs the LED strip on GPIO 33 |
| Piezo sound | Needs the buzzer on GPIO 43 |
| Wi-Fi association | Needs the RM2/CYW43439 radio and a real access point |
| Backlight and battery behaviour | Needs the power hardware |
| Physical boot-button press | Needs the button on GPIO 46 |

The mitigation is structural rather than procedural. Everything decidable
without hardware lives in pure modules (`cards.py`, `gestures.py`, `alerts.py`,
`net.py`) that are unit-tested on the host. Only `main.py` imports `presto` or
`machine`, and it is a thin adapter. A test also enforces that layering, so the
pure modules cannot silently grow a hardware dependency.

### A known touch caveat

Pimoroni documents that two touches aligned horizontally — for example a
two-finger vertical scroll — are not correctly registered. This is a property
of the touch controller, not a bug in this project. The gesture recogniser
therefore only acts on single-touch input and explicitly ignores multi-touch.

## Not a security boundary

The shared token provides bearer authentication for trusted LAN use. It has no
per-user identity or expiry, and travels in a header over plain HTTP. Do not expose the
daemon to the internet.
