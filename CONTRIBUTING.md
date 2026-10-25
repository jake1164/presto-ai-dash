# Contributing to Presto AI Dash

Thanks for helping make Presto AI Dash useful for more AI services. You do not
need to write code to suggest a source.

## Choose a path

- **Request a source:** [Open a Source Request](https://github.com/jake1164/presto-ai-dash/issues/new/choose).
  Tell us which service you use and what you want to see. API knowledge is
  optional.
- **Implement a source:** You can open a pull request directly. **An issue is
  not required first**, although checking existing issues and PRs avoids
  duplicate work.

The [contributing-sources guide](docs/user/developers/contributing-sources.md)
explains both paths and what information helps maintainers.

## Requesting a new source

Search [existing issues](https://github.com/jake1164/presto-ai-dash/issues)
first, then select **Request a new source** in the issue chooser.

The short form asks for:

1. Provider/product name and official website or project repository.
2. What you want displayed (quota/reset, requests/tokens, spend, health, etc.).
3. How you access the service: free, subscription, API billing, or self-hosted.

Links to APIs, local exports, usage screens, and willingness to test are helpful
but **not required**. A requester is not expected to research the provider's API.

Never paste API keys, tokens, cookies, private account data, or unredacted
responses into public issues or pull requests.

A requested source may need research before implementation is possible. A paid
subscription, by itself, does not guarantee accessible usage or quota data.

## Contributing an implementation

1. Read [How a source works](docs/user/developers/how-a-source-works.md),
   [the worked example](docs/user/developers/worked-example.md), and, for
   local/push collection, [Writing a collector](docs/user/developers/writing-a-collector.md).
2. Copy `daemon/templates/source_template.py` and
   `daemon/templates/test_source_template.py`. Register the new source with
   a unique ID and describe its admin settings and optional secret.
3. Use a **verified read-only data path**: an official documented reporting
   API, or a well-understood local export/collector where appropriate.
   Document the exact plan or account type and any known limitations.
4. Normalize only data the provider actually reports. Keep subscription quota
   separate from API billing. Preserve window labels, timestamps, freshness,
   unavailable readings, and provenance. **Unknown is not zero.**
5. Add synthetic/sanitized fixtures grounded in a real documented or observed
   response shape, and tests for success, missing fields, malformed data,
   unreachable services, and rejected credentials. Do not invent a response
   shape to make tests pass.
6. Document setup, required permissions, where a collector must run, what
   can and cannot be displayed, and how to verify the source.
7. Run tests, then open a PR using the included checklist. Link an issue if
   there is one; no issue is required.

From the repository root, with the repository's Python virtual environment:

```bash
cd daemon
../.venv/bin/python -m unittest discover -s tests -q
```

On Windows, use `../.venv/Scripts/python` instead.

If you lack access to a paid plan for live verification, say so in the PR.
Fixture tests are necessary but should not be presented as real-account
validation.

## Non-negotiable safety and accuracy rules

- **Observe only.** No provider-side writes, credit/reset redemption, plan
  changes, or model/prompt requests merely to obtain a measurement. The
  project's existing explicitly user-initiated setup actions are separate.
- **Protect credentials.** Use existing secret storage and HTTP safeguards;
  never log, commit, or send provider keys or sensitive prompt content to the
  device.
- **Fail honestly.** Missing data is unavailable, not 0; an unreachable
  service is not the same as an exhausted quota. Source failure must not block
  other sources.
- **Show evidence.** Prefer documented official read APIs. Any unofficial
  local format or reporting path needs its stability and privacy limitations
  explained and reviewed, not hidden.
- **Fit the device.** Respect the slim payload budget and test rendering using
  the project's existing device tests. New quota sources normally need no
  device-specific code.

For vulnerabilities or credential-exposure concerns, read
[SECURITY.md](SECURITY.md) and do not open a public issue with sensitive details.

## Reviews

Maintainers review provenance, security, correct measurements, documentation,
tests, and the user experience. Automated checks are necessary but do not
replace human review. A new source is considered verified only for the access
methods/plans actually tested.
