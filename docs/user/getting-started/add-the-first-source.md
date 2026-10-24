# Add the first source

A *source* is one thing the dashboard reads: a Claude account, a Codex login,
an OpenCode Go key, a local Ollama server, a LiteLLM gateway. You add them in
the admin page, which stores them in SQLite with the keys encrypted.

## Open the admin page

Start the daemon with `--admin`:

```bash
python -m presto_ai_dash.app --config dash.toml --admin
```

Open <http://127.0.0.1:8788> in a browser on the same machine and unlock the
page with the **admin** token from the startup log. The token is different
from the shared token the Presto uses. If you did not set one, the daemon
generates a fresh one at every start and prints it once.

The browser stays logged in for seven days. **Lock configuration** on the
page revokes the session immediately.

The admin listener binds to `127.0.0.1` only. On a headless server, reach it
over an SSH port forward.

## The three tabs

| Tab | What it is for |
| --- | --- |
| **Services** | Add, edit, order, enable and disable accounts |
| **Status** | Read-only: is everything working, and what is broken |
| **Settings** | What applies to every service: display, remote reports, alerts |

## The first source: pick something local

A local source is the easiest way to see the whole thing work, and it needs
no account anywhere.

1. On the **Services** tab, choose **Add source**.
2. Choose **Ollama** (a local Ollama server) or **LiteLLM** (a gateway).
3. Give it a name and set its `base_url`:
   - Ollama: `http://192.168.1.50:11434`
   - LiteLLM: `http://192.168.1.50:4000`
4. Tick **Enabled** and press **Save source**.

Within one poll interval the account appears on the Presto.

If you do not run Ollama or LiteLLM yet, turn on **Demo mode** on the
**Settings** tab instead. It shows six sample accounts, marks them with a
**DEMO - SAMPLE DATA** chip, and cycles through every status the display can
show, so you can check the LEDs and the sounds without a provider. It overrides
the real readings without changing them, and switching it off is all it takes
to go back.

## Then add a subscription account

Every subscription source is one of the same few steps:

1. **Add source**, choose the type, and give it a name.
2. Fill in the fields the type asks for. The key field, where there is one,
   is encrypted before it is stored and is never sent back to the browser.
   Leaving it blank keeps the key you saved earlier; **Remove stored key**
   deletes it.
3. Press **Save source**.

You can add the same type more than once. Two LiteLLM gateways, or two Claude
profiles, are two separate accounts with their own keys and their own
readings, and they are never added together.

The order of the accounts is the order on the Presto. Drag the grip on an
account card, or use the up and down buttons, to move one; the order is
saved automatically.

## Each source has its own guide

| Source | Guide |
| --- | --- |
| Claude | [Claude](../guides/claude.md) |
| Codex | [Codex](../guides/codex.md) |
| OpenCode Go | [OpenCode Go](../guides/opencode-go.md) |
| Ollama Cloud | [Ollama Cloud](../guides/ollama-cloud.md) |
| Ollama | [Ollama](../guides/ollama.md) |
| LiteLLM | [LiteLLM](../guides/litellm.md) |

## What happens next

The daemon collects from every enabled account on its poll interval, stores
the newest reading per account, and serves the result. The Presto polls the
daemon and draws it.

If an account does not appear, or appears but says *unavailable*, the
**Status** tab says why in plain words. The
[troubleshooting pages](../troubleshooting/stale-readings.md) cover the
common cases.
