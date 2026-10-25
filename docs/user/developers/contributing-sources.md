# Request or contribute a source

Want to see another AI provider, subscription, API service, or local inference
server on Presto AI Dash? There are two paths. **You do not need to be a
developer to request support, and you do not need to open an issue before
submitting an implementation.**

## Request a source (no code needed)

1. Check [existing source requests](https://github.com/jake1164/presto-ai-dash/issues)
   so you can join an existing request if there is one.
2. [Open the GitHub issue chooser](https://github.com/jake1164/presto-ai-dash/issues/new/choose)
   and select **Request a new source**.
3. Tell us the provider/product, its official website, what you want to
   display, and how you access the service.

That is enough to request a source. API documentation, sample responses, or
knowing how to program are **not** required. If you have a documentation link,
a redacted usage screenshot, or an account you can test with, those are
helpful extras.

!!! warning "Keep credentials private"
    Never post API keys, OAuth/session tokens, cookies, credentials, prompt
    text, or private account data in public GitHub issues or pull requests.
    Remove personal information from screenshots and fixtures.

### What should you request?

You can request more than quota bars. A source might report:

- **Quota:** allowance used/remaining, actual period labels, and reset times.
- **Activity:** requests, model usage, tokens, and last verified activity.
- **Cost:** provider-reported spend or remaining credits, if available.
- **Health:** reachability, loaded models, or service status.

Access method and data type are different questions:

| How you use it | What helps us evaluate it |
| --- | --- |
| Free without an account | Public read-only status endpoint, if any |
| Free account | How usage is displayed and whether a credential is required |
| Paid subscription | Plan/tier and whether subscription allowance is actually reported |
| Pay-as-you-go API | Usage and billing endpoints; do not assume they describe a subscription |
| Local/self-hosted service | API or file/export location and which machine can access it |

If only activity is available, the source can still be useful without a quota
bar. Unknown measurements must never be shown as zero.

### What happens to a request?

A maintainer checks whether there is a verified way to read meaningful data
without altering or spending anything on the provider account. A request may
be ready for implementation, need more research, or be blocked by an absent
reporting interface. A popular provider does not necessarily expose an
accessible subscription quota.

## Submit a source implementation

You can [open a pull request](https://github.com/jake1164/presto-ai-dash/pulls)
without filing an issue first. Please check existing work to avoid duplicates.

1. Read [How a source works](how-a-source-works.md), including registration,
   typed configuration, secret handling, snapshots, provenance, and quota
   windows.
2. Start from the
   [worked example](worked-example.md): it points to the source and test
   templates and shows how to register the class.
3. If readings come from another machine or local files, read
   [Writing a collector](writing-a-collector.md). The Presto never reads
   provider secrets or talks directly to a provider.
4. Document which account/plans work, the verified read-only endpoint or local
   export, credentials needed, freshness, limitations, and deployment location.
5. Add tests using sanitized *real response shapes* (official documentation
   or observed output), including absent fields, malformed responses,
   unreachable services, and invalid credentials.
6. Run the [test suite](running-the-tests.md), verify rendering/payload
   behavior, and describe the results in your PR.

A new source is trusted Python code installed with the daemon. User-uploaded
Python modules are **not** dynamically executed.

### Rules every implementation must preserve

- Collection observes accounts; it never redeems credits, modifies limits,
  sends prompts/model requests to obtain usage, or changes provider settings.
  Existing explicitly user-initiated setup flows are separate.
- Prefer documented read-only APIs. Any unofficial local cache or status
  export must be justified, scoped, and have its limitations disclosed.
- Keep API billing separate from subscription usage. Preserve provider-defined
  windows, observed times, and source labels.
- Missing or expired values mean **unavailable**, not `0%`. Staleness,
  reachability, and quota exhaustion are different states.
- Never leak keys or sensitive request/prompt content to logs, error
  messages, the device, or test fixtures.
- Respect the device view's payload budget and the project's fail-soft
  behavior. One broken source must not stop the rest.

The maintainer reviews technical feasibility, safety, test evidence, and the
user experience. Real-account validation must be stated honestly, particularly
for paid tiers unavailable to the contributor or reviewer.

## More details

- [Architecture](architecture.md)
- [How a source works](how-a-source-works.md)
- [Writing a collector](writing-a-collector.md)
- [Worked example](worked-example.md)
- [Running the tests](running-the-tests.md)
