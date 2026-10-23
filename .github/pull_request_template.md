## Summary

<!-- What does this PR change, and why? -->

## Related issue (optional)

<!-- Link an issue if one exists. An issue is not required before a PR. -->

## Validation

<!-- List the actual tests/checks run and their outcomes. If not run, explain. -->

- [ ] I have run relevant tests (or explained why I could not).
- [ ] I have updated user/developer documentation where behavior changed.
- [ ] No secrets, access tokens, private account data, or unredacted fixtures are included.

## New source checklist (only if this PR adds a source)

- [ ] Registered a unique source ID and documented its settings, supported plans, and access requirements.
- [ ] Verified the read-only data source and documented provenance and limitations.
- [ ] No provider-side write, paid model request, token refresh, or automatic credit/reset action occurs during collection.
- [ ] Missing, stale, zero, offline, and quota-exhausted states are distinguished accurately.
- [ ] Added sanitized tests grounded in a verified response shape, including failures and credential rejection.
- [ ] Updated setup instructions and checked the relevant card/device payload tests.
- [ ] Stated which real provider plans/hosts were tested, or clearly marked live validation as pending.

See [CONTRIBUTING.md](https://github.com/jake1164/presto-ai-dash/blob/main/CONTRIBUTING.md) and the
[source development guide](https://jake1164.github.io/presto-ai-dash/developers/how-a-source-works/).
