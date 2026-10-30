# Presto AI Dash

See how much of your AI usage and quota you have left, at a glance, on a
[Pimoroni Presto](https://shop.pimoroni.com/products/presto) touchscreen on your
desk. A small daemon collects the readings, an admin page sets it up in your
browser, and the Presto shows it.

It reads usage and limits only. It never sends a prompt, spends credit or
changes anything on a provider account.

**Sources:** Claude Code, Codex, OpenCode Go, Ollama Cloud, Ollama (local) and
LiteLLM.

## Install

Python 3.11 or newer.

```bash
pipx install presto-ai-dash
presto-ai-dash --admin
```

The admin page opens at <http://127.0.0.1:8788>. Add your first source there.

To put the app on the Presto over USB, open the admin page's **Settings** tab and
use **Set up a Presto**. It prepares `presto_setup.py` and `presto-setup.enc`
for the running build and shows a one-time unlock code; run the script on the
computer the Presto is plugged into. No checkout is needed, and the same flow
works when the daemon runs in Docker on another machine.

Once a device is configured, later app-only refreshes are smaller:

```bash
pipx install --force "presto-ai-dash[device]"
presto-ai-dash install-presto --port COM3
```

To move a Presto to another daemon, or after the Wi-Fi changes, use the same
**Set up a Presto** page and script as for a first setup: it works out what the
Presto needs.

Prefer a container? See the
[Docker guide](https://jake1164.github.io/presto-ai-dash/getting-started/install-with-docker/).

## Where to go next

- [Documentation](https://jake1164.github.io/presto-ai-dash/)
- [Install the daemon](https://jake1164.github.io/presto-ai-dash/getting-started/install-the-daemon/)
- [Set up the Presto](https://jake1164.github.io/presto-ai-dash/getting-started/set-up-the-presto/)
- [Releases and changes](https://github.com/jake1164/presto-ai-dash/releases)
- [Report a problem or request a source](https://github.com/jake1164/presto-ai-dash/issues)

MIT licensed.
