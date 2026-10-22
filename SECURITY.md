# Security policy

## Reporting a problem

Please report a security problem privately, not in a public issue.

Use GitHub's private vulnerability reporting: open the repository's
**Security** tab and choose **Report a vulnerability**. Say what you found,
what it lets someone do, and how to reproduce it. A throwaway token and a
temporary database are enough to show most problems; please do not send real
tokens, a real `dash.toml` or a real `dash.db`.

This is a small project run by one person, so there is no service level, but
a report is read and answered, and a fix is made as soon as it can be.

## What is in scope

- The daemon (`daemon/`): its two HTTP servers (the device API on port 8787
  and the admin page on 8788), the way it stores and encrypts provider keys,
  and what it reads from your computer.
- The Presto app (`presto/`) and the host tools (`tools/`).
- The container image and `compose.yaml`.

## What the project promises

The dashboard only **observes** your provider accounts. It never spends or
redeems credits, never changes a plan or a limit, and never sends a prompt or
a model request to produce a number. The only actions it takes on a provider
account are the ones you start yourself in the admin page: connecting or
disconnecting an account, and installing or removing the Claude status-line
export. A way to make it do anything else is a vulnerability.

The [privacy page](docs/user/privacy/index.md) lists what it reads, stores
and sends, and
[Securing a deployment](docs/user/guides/securing-a-deployment.md) says what
to do beyond a home network.

## Supported versions

The project has no released versions yet. Fixes are made on `main`.
