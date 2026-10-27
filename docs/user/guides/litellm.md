# LiteLLM

LiteLLM is a gateway in front of your models. The dashboard reads the usage
it routed: requests, tokens, spend, failures and the busiest model of the day.

Presto AI Dash needs access to LiteLLM's usage statistics. **Do not use your
LiteLLM master key.** Create a dedicated read-only user and a management-only
key instead. That key can read usage, but it cannot make model requests or
change LiteLLM's configuration.

## Set it up

Three steps: a read-only user in LiteLLM, a management key for that user, and the
key in Presto AI Dash.

### Step 1: Create a monitoring user

1. Sign in to your **LiteLLM Admin UI**.
2. Go to **Internal Users** and click **+ Invite User**.
3. Fill in the form:

   | Field | Value |
   |---|---|
   | User Email | A unique email or alias for Presto |
   | Global Proxy Role | **Admin (View Only)** |
   | Team | Leave blank |
   | Organization | Leave blank |
   | Metadata | Leave blank |
   | Send invitation email | Uncheck |
   | Personal Key Creation | Disable automatic key creation, if offered |

4. Click **Invite User**.

**Copy the new user's User ID.** Find the entry under **Internal Users** and
copy its UUID (it looks like `c66ea085-3f03-4161-acc5-89e9a552086c`). LiteLLM
does not always list a new user in the key-creation dropdown, so you may need to
paste the ID by hand in the next step.

### Step 2: Create the monitoring key

1. Go to **Virtual Keys** and click **+ Create New Key**.
2. Fill in the form:

   | Field | Value |
   |---|---|
   | Owned By | **Another User** |
   | User ID | The UUID you copied in step 1 |
   | Organization | Leave at the default |
   | Team | Leave blank |
   | Key Name | `presto-ai-dash` |
   | Key Type | **Management** |
   | Models | No selection needed |
   | Budget | Not needed |

3. Check that **Management** is selected, not **AI APIs** or **Full Access**.
4. Create the key and **copy it now**: LiteLLM shows it once.

### Step 3: Add it to Presto AI Dash

1. Open the admin page and choose **Add source -> LiteLLM**.
2. Give the account a name.
3. Set **LiteLLM URL** to your gateway, for example `http://192.168.1.50:4000`.
   If the daemon runs in a container and LiteLLM runs on the same computer, use
   `http://host.docker.internal:4000`: inside a container `localhost` is the
   container itself.
4. Paste the key from step 2 into the key field.
5. Press **Save source**.

The card fills in on the next poll. If something is wrong it shows a warning
that says what, in LiteLLM's own words, and what to do about it.

Two LiteLLM accounts can sit side by side for two different gateways or two
different keys. Their usage is reported separately, never added together,
because two keys may watch overlapping traffic.

## What the card shows

On the admin page, and on the Presto's LiteLLM card and Home row:

| Reading | Meaning |
| --- | --- |
| Requests today | Calls routed through the gateway today (UTC), and how many failed |
| Tokens today | Input plus output, with the split between the two |
| Spend today | What LiteLLM priced them at. `$0.00` for local models, which are free |
| Top model | The model that cost the most today. When everything is free, the one with the most requests |
| Models used | How many different models handled traffic today |
| Version and database | The LiteLLM version, and whether its database is connected |

A number LiteLLM did not report is left out. It is never shown as zero:
unknown and zero are different facts.

## What it reads

| Endpoint | Auth | Used for |
| --- | --- | --- |
| `GET /health/liveliness` | none | Is the process alive |
| `GET /health/readiness` | none | Is it ready, the database, the version |
| `GET /user/daily/activity` | bearer key | Requests, tokens, spend, failures, models |

## Check the key's permissions

| Operation | Expected |
| --- | --- |
| Read daily LiteLLM usage | Allowed |
| Read global usage statistics | Allowed |
| Generate AI completions | Denied |
| Generate new API keys | Denied |
| Modify users or teams | Denied |
| Modify LiteLLM configuration | Denied |

This key is read-only, but it is **not limited to the usage endpoint**: it may
still read other management information. For stricter least privilege, an
administrator can also set the key's `allowed_routes` to only:

```json
["/user/daily/activity"]
```

Test that against your LiteLLM version before relying on it.

## Revoke or replace the key

In the LiteLLM Admin UI, open **Virtual Keys** and delete (or block) the
`presto-ai-dash` key. Presto then shows *Key rejected (HTTP 401)*. To rotate it,
create the new key first, paste it into the source and save, and then delete the
old one, so there is no gap. Delete the monitoring user too when an installation
is retired.

## Security

- Never enter the LiteLLM master key into Presto.
- Use a dedicated key for each Presto installation.
- The key is kept by the daemon, never on the Presto itself.
- Do not expose the daemon's configuration or database publicly.
- Revoke the key when an installation is retired.
- Use HTTPS when LiteLLM is reached over a network you do not trust.

## Troubleshooting

The card's warning names the problem; the admin page adds LiteLLM's own
message and what to do.

| You see | Meaning and fix |
| --- | --- |
| **Key refused (HTTP 403)** | The gateway answered but the key may not read usage. LiteLLM's message names the cause, for example "Only allowed to call routes: ['llm_api_routes']": that key is for model calls. Use the Management key from step 2, owned by the **Admin (View Only)** user |
| **Key rejected (HTTP 401)** | The key is wrong, expired or revoked. Create a new one |
| **No usage route (404)** | The URL points at something that is not LiteLLM, or this LiteLLM version has no `/user/daily/activity` |
| **Cannot reach it** | Nothing answered at the address. In a container, `localhost` is the container: use `host.docker.internal` |
| The new user is missing from the key dropdown | Paste the User ID from **Internal Users** by hand |
| Usage looks too low | Compare with LiteLLM's own global usage page. The totals should be the whole gateway's, not just this key's |

## Three things that matter

**`GET /health` is not used.** That endpoint runs a real test request against
every configured model, and it therefore costs tokens. The dashboard never
makes a paid model call, so it uses the two cheap health endpoints instead.

**The key stays with its address.** The daemon follows a redirect only to the
same host, or from `http` to `https` on the same host, so a gateway that
redirects somewhere else never receives the key; the card shows an error
instead, and the fix is to set the URL to the final address. For the same
reason, when you change the URL of a source that has a saved key, the admin
page asks you to enter the key again (or remove it) before it will save.

**Spend needs a database.** LiteLLM's spend tracking needs a `DATABASE_URL`.
If the proxy runs without one, there is nothing to report, and the card says
**unavailable**. It never shows `$0.00` for unknown spend.

## Limits

- **Only routed traffic is counted.** Usage that bypasses the gateway is not
  in these totals.
- **Today means today in UTC.** That is how LiteLLM groups its daily activity.
- **`Errors today` is history, not a status.** A non-zero count does not turn a
  healthy source yellow: it cannot tell earlier failures from current ones.
  Authentication or permission failures do warn, and a gateway that does not
  answer reports *offline*.
- **Rate-limit warnings are not synthesised.** There is no verified LiteLLM
  endpoint that reports "you are approaching a rate limit".
- **The API is a moving target.** Only endpoints in LiteLLM's OpenAPI spec are
  a public contract; anything else may change in any release.

## See also

- [Ollama](ollama.md)
- [Sources](../concepts/sources.md)
