# TestForWebhook

A working demo of automatic change management: every commit that lands on `main`
raises a Normal Change ticket in the [Serval](https://www.serval.com) AI platform,
which is then risk-assessed and planned by AI before it reaches the CAB.

No GitHub Action, no polling, no middleware. A repository webhook posts GitHub's
`push` event straight to a Serval webhook-triggered workflow.

---

## How it works

```
git push origin main
        │
        ▼
GitHub repository webhook  ──POST push event──►  Serval webhook trigger
                                                         │
                                                         ▼
                                            "GitHub Push to Change" workflow
                                                         │
                                       ┌─────────────────┴─────────────────┐
                                       │                                   │
                              default branch?                     anything else
                                       │                                   │
                                       ▼                                   ▼
                          Normal Change ticket created              skipped, 200 OK
                                       │
                                       ▼
                        serval:ticket-created event fires
                                       │
                       ┌───────────────┴───────────────┐
                       ▼                               ▼
          Auto-Draft Change Plans          Link Change Configuration Items
     risk, impact, justification,        matches files and SHAs to CIs in
     implementation / backout / test      the service map and attaches them
     plans, planned start and end
                       │
                       ▼
                 CAB approval
```

### What reaches the ticket

| Ticket field | Source |
|---|---|
| Title | `owner/repo [branch sha]: <commit subject>` |
| Description | Repo link, branch, pusher, commit count, head SHA, compare URL |
| Commits | Every commit in the push, each linked to GitHub, capped at 25 |
| Files touched | Deduplicated union of added, modified and removed paths, capped at 40 |
| Requester | The pusher's Serval user, matched on their commit email |

### What is deliberately ignored

Pushes to any branch other than the repository's default branch, tag pushes,
branch deletions, empty force-pushes, and GitHub's one-off `ping`. Each returns
HTTP 200 with a `skipped` reason, so GitHub's delivery log stays green and the
change queue stays meaningful.

---

## Part 1: Serval setup

You need a Serval team with the change models installed (Standard, Normal and
Emergency Change), and permission to author workflows on it.

### 1.1 Create the workflow

Create a new workflow on the team and give it a webhook trigger:

```ts
import { workflow, context } from "serval/core";
import * as serval from "serval/integrations/serval";

export const main = workflow({
  trigger: { type: "webhook" },
  fn: async function (
    payload: Record<string, unknown>,
    ctx: context.CurrentTeam & context.CurrentWorkflowRun,
  ) {
    // GitHub's push payload arrives as the first positional argument.
  },
});
```

The full implementation lives in the team's **GitHub Push to Change** workflow.
Three details in it are load-bearing:

1. **Ignore the ping.** GitHub sends a `ping` the moment the webhook is saved.
   It has a `zen` string and no `ref`. Without an explicit guard it becomes a
   junk change ticket on day one.

2. **Compare against `repository.default_branch`, not a hardcoded `"main"`.**
   Read the branch from `ref` by stripping `refs/heads/`. This keeps working on
   repos that still use `master` or something else.

3. **Do not set `createdByWorkflowRunId` on the created ticket.** See
   [Gotcha 3](#gotcha-3-plans-arrive-blank) below. This one is not obvious and it
   silently disables all downstream drafting.

### 1.2 Publish it

A webhook-triggered workflow gets no URL until it is published. Publish, then
reopen it.

### 1.3 Copy the URL and secret

Click the **gear** on the Webhook trigger node to open **Webhook Trigger Details**:

- **Webhook URL**, of the form
  `https://public.api.serval.com/v2/webhooks/<TRIGGER_ID>/trigger`
- **Secret Key**, hidden behind an eye icon. Click it to reveal.

Treat the secret as a credential. Anyone holding it can fire the workflow.

---

## Part 2: GitHub setup

Repository → **Settings** → **Webhooks** → **Add webhook**.

| Field | Value |
|---|---|
| Payload URL | `https://public.api.serval.com/v2/webhooks/<TRIGGER_ID>/trigger?secret=<SECRET>` |
| Content type | `application/json` |
| Secret | **leave empty** |
| SSL verification | Enabled |
| Which events | Just the `push` event |
| Active | Checked |

Then **Add webhook**.

### Why the secret goes in the URL

Serval authenticates on an `X-Webhook-Secret` header or a `secret` query
parameter. GitHub's repository webhook form offers no custom header field, so
the query parameter is the only route available. The query parameter and the
header are equivalent to Serval.

If you are calling the same endpoint from somewhere that *can* set headers, such
as a CI job or `curl`, prefer the header:

```bash
curl -X POST "https://public.api.serval.com/v2/webhooks/<TRIGGER_ID>/trigger" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: <SECRET>" \
  -d '{"ref":"refs/heads/main","repository":{"full_name":"owner/repo","default_branch":"main"},"pusher":{"name":"you","email":"you@example.com"},"head_commit":{"id":"abc123","message":"Test"},"commits":[{"id":"abc123","message":"Test","modified":["README.md"]}]}'
```

> **Security note.** Putting the secret in the payload URL means it is readable
> by anyone with admin on the repository and it appears in GitHub's delivery
> log. Never paste it into a file in this repository, which is public. Rotate it
> from the Serval trigger dialog whenever you need to: the new secret works
> immediately and the old one keeps working for 7 days, so there is no outage
> window.

---

## Part 3: Verify

1. **Settings → Webhooks → Recent Deliveries.** The `ping` should show a green
   check. Open it and confirm the response body reads
   `{"skipped": true, "reason": "github ping event"}`.
2. **Commit to `main`.** Edit any file, commit directly to `main`. Write a real
   commit subject, because it becomes the change ticket title. `Raise payments
   API retry ceiling to 8 attempts` reads far better in a change queue than
   `Update README.md`.
3. **Check Serval.** A Normal Change appears within a second or two. Within
   roughly thirty seconds the Assessment and Planning sections fill in.

Putting a planned window in the commit body is picked up automatically:

```
Raise payments API retry ceiling to 8 attempts

Planned start 2026-09-20 22:00 PDT, planned end 2026-09-20 23:00 PDT.
```

The drafting workflow parses that, converts it to UTC and writes it to the
Planned Start Date and Planned End Date fields.

---

## Troubleshooting

### Gotcha 1: every delivery returns 401

The secret is not reaching Serval. Almost always this means it was typed into
GitHub's **Secret** box instead of appended to the **Payload URL**. Those are
two different mechanisms: GitHub's Secret box never transmits the secret, it
computes an HMAC and sends it as `X-Hub-Signature-256`, which Serval does not
read.

Open the webhook, put the cursor at the end of the Payload URL after `?secret=`,
paste the secret there, and update. Leave the Secret box alone.

### Gotcha 2: the webhook stopped firing entirely

GitHub automatically unchecks **Active** after a run of failed deliveries. If you
spent a while on Gotcha 1, the hook is probably disabled. Re-tick **Active** and
update, then use **Recent Deliveries → Redeliver** on the last failure to test
without needing a new commit.

### Gotcha 3: plans arrive blank

The change ticket is created but Risk, Impact and the four plans stay empty, and
no `Auto-Draft Change Plans` run appears against the ticket.

Cause: the workflow set `createdByWorkflowRunId` on the new ticket. That field
marks a ticket as workflow-created, which suppresses the `serval:ticket-created`
event as loop prevention. No event, no drafting, no CI linking.

Fix: omit that field entirely when creating the ticket. The tradeoff is that the
ticket no longer carries an audit link back to the run that made it; the run's
own output still records the ticket id and reference, so the trail exists in the
other direction.

### Gotcha 4: nothing happens on a feature branch

Working as designed. Only the default branch raises a change. To change that,
adjust the branch comparison in the workflow.

### Reading the evidence

| Where | What it tells you |
|---|---|
| GitHub → Recent Deliveries | Whether GitHub sent it, and the exact HTTP response |
| Serval → workflow run history | Whether the workflow ran, and its `skipped` reason or ticket ref |
| Serval → runs linked to the ticket | Whether drafting and CI linking fired |

A green delivery with no Serval run means the request never reached the
workflow. A Serval run returning `skipped` means it arrived and was filtered on
purpose.

---

## Sample code in this repository

Three small Python modules, here as realistic commit fodder for the demo and as
a worked example of the retry behaviour the change tickets keep describing.

| File | Purpose |
|---|---|
| `RetryPolicy.py` | Exponential backoff with full jitter. Draws the line between transient failures worth retrying and permanent ones that are not. |
| `Payment.py` | Payment capture against a Stripe-style provider. Every retry reuses one idempotency key so a retry can never double-charge. |
| `SnowflakeIngest.py` | Batch load of captured payments into Snowflake through an internal stage, then `MERGE` on the natural key so reruns converge instead of duplicating. |

### Running them

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install requests snowflake-connector-python
```

```bash
export PAYMENTS_API_KEY="..."
export PAYMENTS_API_BASE="https://api.provider.test/v1"

export SNOWFLAKE_ACCOUNT="..."
export SNOWFLAKE_USER="..."
export SNOWFLAKE_PASSWORD="..."
export SNOWFLAKE_WAREHOUSE="COMPUTE_WH"
export SNOWFLAKE_TARGET_TABLE="ANALYTICS.PAYMENTS.CAPTURES"
```

```bash
python Payment.py
python SnowflakeIngest.py
```

Both modules import `RetryPolicy` as a top-level module, so run them from the
repository root. Neither reaches a real provider in this demo; they are written
to be read, and to give commits something plausible to touch.

> The filenames use `CapitalCase` because that is how they were requested. Python
> convention would be `retry_policy.py`, `payment.py` and `snowflake_ingest.py`.
> Rename them and update the two import lines if you would rather follow PEP 8.

---

## Reference

- [Serval workflow types](https://docs.serval.com/sections/documentation/workflows/Configure/types)
- [GitHub webhook `push` event payload](https://docs.github.com/en/webhooks/webhook-events-and-payloads#push)
