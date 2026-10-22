> Historical planning document (written 2026-10-11); the current state is in [docs/milestones.md](milestones.md) and the [documentation site](https://jake1164.github.io/presto-ai-dash/).
>
> Corrections (2026-10-21):
>
> - The Claude quota export, "pending" below, was verified with live readings on 2026-10-11 (M9). The VS Code panel question was settled on 2026-10-20: the panel does not run the status line, and the usage cache reader (Phase 5d) gives panel users quota.

# M6 subscription reporting readiness

Reviewed 2026-10-11 against commit `d705691` on Windows.

**Verdict: Codex and OpenCode Go quota reads are verified for the tested logins/key;
Claude still needs a real quota export.** The user confirmed Go's three reported
percentages match Console. The shared model can be specified next in M7. This review does
not authorize implementation or claim that all adapter acceptance checks passed.

## Results

| Target | Readiness | Evidence | Remaining gate |
| --- | --- | --- | --- |
| OpenAI/Codex | Ready for the observed account/reporting path | Bundled CLI `0.162.0-alpha.17.2`; authenticated ChatGPT Plus account; app-server quota read succeeded. One weekly bucket at 4% used; second window absent. | Other plans/accounts, managed login refresh/recovery, multiple account isolation, and protocol upgrade compatibility need implementation acceptance tests. |
| Claude | Conditional | Bundled Claude Code `2.1.296`; `auth status --json` confirms a first-party claude.ai Pro login. Official statusline documentation provides subscription quota fields. User confirmed both terminal and VS Code panel use. | No configured statusline export or real quota sample was found. Capture fields during normal use and verify panel coverage before treating the adapter as ready. |
| OpenCode Go | Ready for the observed subscription meters | User-provided key, described as read-only, returned HTTP 200 with rolling/weekly/monthly percentages and valid reset timestamps. User confirmed all three 0% readings match the subscribed account in Console. | Nonzero usage, migrated-account/schema/auth recovery, and any model-specific breakdown need implementation acceptance evidence. No special session header was needed for this observed request. |

Ready means the observed read path works, not that production error recovery
or an adapter has been implemented. Conditional means a supported candidate
exists but account-level acceptance evidence is missing. A missing key is not
evidence that Go has no usage API.

## Review boundaries

- The working tree was clean at review start. Runtime inspection covered
  `sources/__init__.py`, `models.py`, `management.py`, `dashboard.py`, and
  `presto/home.py`.
- Probes used the installed executables and existing logins. No model prompt,
  login/logout, earned quota reset, forced token refresh, or daemon/device
  configuration change was requested.
- Account output was filtered before printing: no emails, account IDs, tokens,
  transcripts, or full provider response bodies were retained.
- No existing CLI credential file or VS Code secret store was scraped. The Go
  follow-up used only the key the user supplied in the explicitly created,
  Git-ignored `.local-backups/m6/opencode-go.toml`. It was read in memory and
  sent as a bearer header to the fixed HTTPS usage URL, with redirects disabled.
  Only the presence of Claude's `statusLine` setting was inspected in settings.
- There are no daemon, source, Presto, or automated-test changes in this review.
  The existing application suite was not rerun; these results come from bounded
  protocol/HTTP probes, installed-tool checks, and documentation inspection.

## Codex: real reporting verified

The executable bundled with `openai.chatgpt-26.1007.21434-win32-x64` starts an
independent app-server over stdio. A bounded probe performed `initialize`,
`initialized`, `account/read` with `refreshToken: false`, then
`account/rateLimits/read`. It did not create a thread or turn. The probe closed
its own process after receiving the response.

Redacted reading observed at `2026-10-11T11:36:48Z`:

```json
{
  "cli_version": "0.162.0-alpha.17.2",
  "account": {"type": "chatgpt", "planType": "plus"},
  "rateLimitsByLimitId": {
    "codex": {
      "limitId": "codex",
      "planType": "plus",
      "primary": {
        "usedPercent": 4,
        "windowDurationMins": 10080,
        "resetsAt": 1792317334
      },
      "secondary": null
    }
  }
}
```

This is an observation, not a live display value or a permanent plan limit.
`10080` minutes is seven days. The primary window was weekly in this response:
primary must not be hard-coded to Daily or 5h. The single-bucket `rateLimits`
view repeated the same bucket; do not render or aggregate it a second time.
An absent secondary window means unavailable/not reported, not zero usage.

The [official app-server protocol](https://learn.chatgpt.com/docs/app-server)
documents quota reads, managed account login, refresh, and optional windows.
Scope the first adapter to verified Codex buckets. This probe does not establish
coverage of every ordinary ChatGPT message/model limit.

Remaining implementation checks: subprocess timeout and shutdown, rejected or
expired auth, supported re-login/refresh, absent or changed buckets, per-account
process/profile isolation, and stable executable discovery after extension updates.
Do not couple a deployed daemon to this user's literal extension path.

## Claude: login verified, quota export pending

The executable bundled with `anthropic.claude-code-2.1.296-win32-x64` reported:

```json
{
  "loggedIn": true,
  "authMethod": "claude.ai",
  "apiProvider": "firstParty",
  "subscriptionType": "pro"
}
```

Claude is not on this shell's PATH. The user settings file has no `statusLine`;
this repository has neither `.claude/settings.json` nor `settings.local.json`.
No export was configured as part of this review, and no quota percentages were
observed. Authentication status alone is not quota evidence.

The [documented statusline input](https://code.claude.com/docs/en/statusline#rate-limit-usage)
offers `rate_limits.five_hour` and `seven_day`, with `used_percentage` and
`resets_at` in epoch seconds. These appear after a normal session response;
windows may be independently absent or disappear at reset. Context-window
percentage and session cost are different metrics.

The [VS Code guide](https://code.claude.com/docs/en/vs-code#switch-to-terminal-mode)
offers a terminal mode, but panel quota-export behavior was not verified here.
A terminal statusline helper is therefore a candidate, not proven coverage of
the user's graphical-panel workflow. The user confirmed using both the terminal
and VS Code panel; a terminal-only sample does not prove fresh reporting during
panel-only use. The
[CLI reference](https://code.claude.com/docs/en/cli-reference#cli-commands)
documents the auth-status command used above.

The gate is a real, minimized export during normal Claude use, with version and
observation time. A future helper must preserve existing statusline output,
write atomically, and keep original observation time when re-rendering cached
input. Timer callbacks alone must not make old quotas look fresh. Do not obtain
a sample by sending a throwaway model prompt or scraping private CLI credentials.

## OpenCode Go: authenticated quota read verified

The installed `ltmoerdani.opencode-copilot-chat` extension is version `0.7.8`;
no OpenCode CLI was found on PATH or in the common locations checked. The
extension's installed source pointed to a Go usage route. That clue was then
confirmed independently in the provider's own repository:

- [Go usage route](https://github.com/anomalyco/opencode/blob/dev/packages/console/app/src/routes/zen/go/v1/usage.ts)
  implements bearer-key auth and a legacy response containing
  `usage.rolling`, `weekly`, and `monthly`, each with `status`, `percent`, and
  ISO `resetsAt`. Legacy errors distinguish invalid/missing key (401) from
  missing subscription entitlement (403).
- [Inference proxy](https://github.com/anomalyco/opencode/blob/dev/packages/console/app/src/lib/inference-proxy.ts)
  can forward this path to another backend for migrated/new accounts. The
  legacy response shape must not be assumed to cover every current account.
- [Subscription calculations](https://github.com/anomalyco/opencode/blob/dev/packages/console/core/src/subscription.ts)
  and [date helpers](https://github.com/anomalyco/opencode/blob/dev/packages/console/core/src/util/date.ts)
  define reset calculations. Preserve the returned reset; do not substitute
  local calendar boundaries.

These source-backed findings describe the current `dev` branch, not a promised
stable public API version. The follow-up below verifies the response for the
user-provided key without assuming every account returns the same shape.

At `2026-10-11T11:39:44Z`, a credential-free GET to
`https://opencode.ai/zen/go/v1/usage` returned HTTP 401 with JSON error type
`AuthError` and no usage object. No inference route was called.

At `2026-10-11T11:44:29Z`, the same read-only GET using the user-provided key
returned HTTP 200. The only top-level response field was `usage`; its three
windows each reported status `ok`. Minimized real reading, with reset timestamps
normalized to UTC:

```json
{
  "usage": {
    "rolling": {
      "status": "ok",
      "percent": 0,
      "resetsAt": "2026-10-11T16:44:30.542000+00:00"
    },
    "weekly": {
      "status": "ok",
      "percent": 0,
      "resetsAt": "2026-10-12T00:00:00+00:00"
    },
    "monthly": {
      "status": "ok",
      "percent": 0,
      "resetsAt": "2026-10-24T23:53:39+00:00"
    }
  }
}
```

These zeros are measured provider values, not a missing-data fallback. Reset
timestamps belong to this observation; do not hard-code them or infer new usage
at reset. The request used bearer authorization and standard JSON/User-Agent
headers; no session header or inference request was sent. The user described
the key as read-only; other routes and enforcement of its permissions were not
tested. The key value was neither printed nor copied into the readiness report.

On 2026-10-11, the user confirmed that Console for the subscribed account also
shows 0% used for all three meters. This closes the current account's Console
comparison check. It does not verify nonzero usage or model-specific attribution.

The user's follow-up screenshots also show three 0.0% quota meters alongside
codebase history of 2.0k requests / 424.1M tokens and yesterday's 50 requests /
13.4M tokens. The current request uses `step-5-preview-free`. The
[official Go model table](https://opencode.ai/docs/en/go/#usage-limits) currently
lists that preview as free with unlimited usage for a limited time. Token
activity can therefore coexist with zero paid-allowance consumption; the
screenshots do not establish that all historical requests used free models.
The panel's displayed $12/$30/$60 limits are not fields returned by the tested
usage response and must not be hard-coded for every account or plan.

The [Go documentation](https://opencode.ai/docs/en/go/#usage-limits) describes
5-hour, weekly, and monthly allowances and model-dependent limits. The first
adapter can report the three verified subscription meters. Do not invent
per-model percentages or raw spend absent from the response; any model-specific
breakdown needs separate evidence.

The observed response matches the legacy route shape; compatibility with other
or migrated accounts is not proven by this one key. Nonzero readings, reset
transitions, and authentication/entitlement failures need implementation
acceptance checks. Daemon credential storage and the adapter remain
unimplemented; the ignored file is only the readiness-check input.

## Implications for M7 and later acceptance

M7 can define the shared model without pretending all reporting gates passed:

- Keep configured account identity distinct from provider type, quota bucket,
  and window identity. Same-account credential rotation preserves history;
  replacing the actual account must not blend the two accounts' history.
- Use a window list, with actual duration/period, percentage, provider reset,
  observation time, and freshness. Convert epoch-second and ISO resets into
  one UTC representation. Missing duration/reset is explicit, not guessed.
- Prefer Codex's multi-bucket map when available, falling back to its legacy
  view once. Optional windows must not generate an empty Daily bar.
- Keep health, subscription allowances, API billing, and app budgets distinct.
  Existing source metrics can hold readings, but current Home only renders
  request/token rows and has no live quota contract. Generic metrics storage
  is not completed quota integration.
- Treat CLI/helper login as externally owned credentials. Go's daemon-held
  key can use existing encrypted SQLite storage. Multiple-account isolation
  still needs real verification; two instances pointing to one login are not
  two independently monitored accounts.
- Keep fixed preview quotas labelled DEMO. Live stale/unknown data never falls
  back to synthetic percentages or resets to zero without new evidence.

Implementation acceptance must cover valid/missing/malformed windows, one weekly
Codex window, duplicate legacy/map buckets, fractional percentages, resets,
clock skew, stale exports, auth/entitlement failures, timeouts, account rotation
versus replacement, and existing Ollama/LiteLLM regression behavior. M11 adds
Presto layout/scroll/navigation checks and access to Go's third monthly window.

**Next deliverable: M7's bounded API/config quota contract.** Claude's quota
export remains a readiness gate. Codex and Go have verified reporting paths
for the tested account/key; implementation still needs acceptance checks.
Stop after this review; implementation requires an explicit request.
