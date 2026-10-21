# Ollama

The Ollama connector watches a **local** Ollama server: is it up, which
version, which model is loaded, and how fast the last measured request
answered.

It needs no API key. The account row says *local API - no key required*; that
is expected, not a lost credential.

## Set it up

1. Open the admin page and choose **Add source -> Ollama**.
2. Give the account a name.
3. Set `base_url` to the Ollama server, for example
   `http://192.168.1.50:11434`.
4. Press **Save source**.

## What it reads

| Endpoint | Used for |
| --- | --- |
| `GET /api/version` | Reachability and version |
| `GET /api/tags` | The models available locally |
| `GET /api/ps` | The models currently loaded in memory |

All of them are plain `GET` requests. No prompt is ever sent.

## The honest limitation

**Ollama exposes no request history.** There is no prompt log, no token
history and no duration history. The API tells you which models exist, which
is loaded right now, the version, and the timing fields of a request *you
just made*.

So "tokens used today against Ollama" cannot be reconstructed from Ollama
alone. Polling its health tells you it is up, not what it did.

The dashboard therefore reports reachability, the version, the loaded model
and the number of available models. The speed is a different story.

## The measured speed

If a collector has written a samples file, the dashboard reads the last
sample and computes tokens per second from it.

From the Ollama documentation:

> To calculate how fast the response is generated in tokens per second
> (token/s), divide `eval_count` / `eval_duration` * `10^9`.

All Ollama durations are in **nanoseconds**.

Set `samples_file` to the JSON file such a collector writes. When no
readable sample exists, the card reads **last measured: unavailable**. It
does not guess, and it does not show zero.

!!! note "The dashboard never prompts the model"
    The speed is a number someone else measured. The daemon sends no prompt
    and makes no inference request, so it never spends your compute.

## Failure behaviour

An unreachable server makes the account *offline* and keeps the last known
readings, visibly stale. A reachable server with an unexpected response
degrades that one metric to *unavailable* and leaves the rest alone.

## See also

- [Ollama Cloud](ollama-cloud.md), the hosted connector
- [LiteLLM](litellm.md)
