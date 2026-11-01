<div align="center">

<img src="assets/brand/png/presto-ai-dash-128.png" width="96" height="96" alt="Presto AI Dash Watchful Meter icon">

# Presto AI Dash

**Always in view. Never in your way.**

Monitor AI usage, subscription limits, reset times, and service health across your accounts—all at a glance on a dedicated display.

**No windows. No screen clutter. Just the information you need.**

![License: MIT](https://img.shields.io/badge/License-MIT-blue)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![MicroPython](https://img.shields.io/badge/Device-MicroPython-2b8a73)

[**Website**](https://jake1164.github.io/presto-ai-dash/) · [**Live Demo**](https://jake1164.github.io/presto-ai-dash/demo/) · [**Documentation**](https://jake1164.github.io/presto-ai-dash/docs/) · [**Get Started**](https://jake1164.github.io/presto-ai-dash/docs/getting-started/install-the-daemon/) · [**Request a Source**](https://github.com/jake1164/presto-ai-dash/issues/new/choose) · [**♥ Support**](#support-development)

</div>

<p align="center">
  <img src="docs/user/images/presto-ai-dash-hero.png" width="960" alt="Presto AI Dash on a physical Pimoroni Presto, with subscription quotas, reset countdowns, local AI health, and six supported AI services">
</p>

Try it in your browser, no Presto needed: [see it running](https://jake1164.github.io/presto-ai-dash/demo/).

## Your AI usage at a glance

Presto AI Dash turns a **Pimoroni Presto touchscreen** into a dedicated monitor for AI subscription quotas, reset countdowns, and local AI services. Swipe between service cards, see warnings at a glance, and know when a reading is stale or unavailable.

Provider collection is **read-only**: the app never sends prompts, spends credits, or changes plans or limits. API keys are encrypted in the daemon's database; the Presto never holds a provider key.

> [!IMPORTANT]
> **A separate computer or server is required.** This is **not a standalone app that runs entirely on the Presto**. You must run the **Python daemon as a background process or Docker container** on a computer/server on your network. It collects the data and keeps provider credentials; the Presto connects over Wi-Fi to display readings and control its own alerts.

```mermaid
flowchart TB
    S["Cloud + local AI services"] --> D
    D["Computer / server REQUIRED<br/>Python daemon or Docker"]
    D -->|"Wi-Fi"| P["Presto touchscreen<br/>Display and alerts"]
```

## Supported services <img align="right" src="https://img.shields.io/badge/6-integrations-238636" alt="6 integrations">

<table>
  <tr>
    <td width="400"><a href="https://jake1164.github.io/presto-ai-dash/docs/guides/claude/"><strong>Claude Code ↗</strong></a><br>Subscription quota &amp; activity</td>
    <td width="400"><a href="https://jake1164.github.io/presto-ai-dash/docs/guides/codex/"><strong>OpenAI Codex ↗</strong></a><br>Quotas, resets &amp; banked credits</td>
  </tr>
  <tr>
    <td><a href="https://jake1164.github.io/presto-ai-dash/docs/guides/opencode-go/"><strong>OpenCode Go ↗</strong></a><br>Subscription limits &amp; resets</td>
    <td><a href="https://jake1164.github.io/presto-ai-dash/docs/guides/ollama-cloud/"><strong>Ollama Cloud ↗</strong></a><br>Credit balance, quota &amp; rolling activity</td>
  </tr>
  <tr>
    <td><a href="https://jake1164.github.io/presto-ai-dash/docs/guides/ollama/"><strong>Ollama ↗</strong></a><br>Local models &amp; server health</td>
    <td><a href="https://jake1164.github.io/presto-ai-dash/docs/guides/litellm/"><strong>LiteLLM ↗</strong></a><br>Gateway usage, health &amp; spend</td>
  </tr>
</table>

Each service links to its metrics, setup, collection method, and limitations. Also see [Source concepts](https://jake1164.github.io/presto-ai-dash/docs/concepts/sources/) and [where quota numbers come from](https://jake1164.github.io/presto-ai-dash/docs/concepts/quotas-and-provenance/).

### 🔌 Missing your favorite AI service?

Presto AI Dash is designed to grow. Request a new provider, subscription, or local service—**no coding or API knowledge required, and requests are free.**

[**💡 Request a Source →**](https://github.com/jake1164/presto-ai-dash/issues/new/choose) &nbsp;&nbsp; [**🔧 Contribute a Source →**](CONTRIBUTING.md)

## Get started

1. [Install the daemon](https://jake1164.github.io/presto-ai-dash/docs/getting-started/install-the-daemon/) or [run it with Docker](https://jake1164.github.io/presto-ai-dash/docs/getting-started/install-with-docker/) **on your computer or server**.
2. [Add your first source](https://jake1164.github.io/presto-ai-dash/docs/getting-started/add-the-first-source/), or enable **Demo mode** on the admin page's **Settings** tab. It shows six sample accounts, marks them with a DEMO chip, and writes nothing.
3. [Set up the Presto](https://jake1164.github.io/presto-ai-dash/docs/getting-started/set-up-the-presto/) to connect over Wi-Fi.

For Claude and Codex with a Docker daemon, run a [remote collector](https://jake1164.github.io/presto-ai-dash/docs/guides/remote-collectors/) on the computer where you use them.

For configuration, troubleshooting, security, and detailed limitations, see the [documentation](https://jake1164.github.io/presto-ai-dash/docs/) and [privacy notes](https://jake1164.github.io/presto-ai-dash/docs/privacy/). The app itself sends nothing anywhere; only the documentation website counts page views, with Google Analytics, as the privacy notes explain.

## Support development

If Presto AI Dash is useful, a **GitHub star** helps others discover it. Sponsorships and coffee donations help support maintenance, testing, and future integrations. Donations are optional and don't guarantee a particular integration.

<p align="center">
  <a href="https://github.com/sponsors/jake1164"><img src="docs/user/images/sponsor-github.svg" alt="Support on GitHub Sponsors" height="35"></a>
  <a href="https://buymeacoffee.com/jake1164s"><img src="https://cdn.buymeacoffee.com/buttons/default-orange.png" alt="Buy Me a Coffee" height="35"></a>
</p>

## More projects

**Pimoroni and maker projects:** [Presto Snake](https://github.com/jake1164/presto-snake) · [Awesome Pimoroni Presto](https://github.com/jake1164/awesome-Pimoroni-Presto) · [Waveshare Weather Clock](https://github.com/jake1164/Pico-RGB-Matrix-Weather-Clock) · [Tempest Weather Clock](https://github.com/jake1164/tempest-led-weather-clock)

**Self-hosted software:** [Family Librarian](https://github.com/jake1164/family-librarian) · [M3Undle](https://github.com/Sydney-Elvis/M3Undle)

Developers: [contribution guide](CONTRIBUTING.md) · [running the tests](https://jake1164.github.io/presto-ai-dash/docs/developers/running-the-tests/) · [developer notes](docs/README.md) · [changelog](CHANGELOG.md).

MIT licensed. See [LICENSE](LICENSE) and [third-party notices](THIRD_PARTY_NOTICES.md).
