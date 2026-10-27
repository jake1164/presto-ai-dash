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
`pip install presto-ai-dash` works too, in a virtual environment.

To put the app on the Presto over USB, install the USB tools with the package and
copy the app (use your own port, for example `COM3` or `/dev/ttyACM0`):

```bash
pipx install --force "presto-ai-dash[device]"
presto-ai-dash install-presto --port COM3
```

Prefer a container? See the
[Docker guide](https://jake1164.github.io/presto-ai-dash/getting-started/install-with-docker/).

## Where to go next

- [Documentation](https://jake1164.github.io/presto-ai-dash/)
- [Install the daemon](https://jake1164.github.io/presto-ai-dash/getting-started/install-the-daemon/)
- [Set up the Presto](https://jake1164.github.io/presto-ai-dash/getting-started/set-up-the-presto/)
- [Releases and changes](https://github.com/jake1164/presto-ai-dash/releases)
- [Report a problem or request a source](https://github.com/jake1164/presto-ai-dash/issues)

MIT licensed.
