# Asana Connector

Read and operate on Asana from Imperal: browse workspaces, projects and tasks,
read task details and comments, create and update tasks, complete or reassign
them, move them between projects and sections, and manage comments.

Everything is **name-first**. You say "complete *Ship the landing page* in
*Website*" — you never handle an Asana gid. Pasted gids still work when a name
is genuinely ambiguous.

## Connecting

Asana is not one of the platform's OAuth providers (`ctx.oauth_authorize_url`
supports Google, Microsoft and Yahoo only), so the connector uses **personal
access tokens**:

1. Create a token at [app.asana.com/0/my-apps](https://app.asana.com/0/my-apps).
2. Paste it into the Asana panel's Connect screen (or the Secrets tab).

The token is validated against Asana **before** it is stored, so a bad paste is
reported immediately instead of failing later. Tokens live in the Vault-encrypted
`asana_tokens` secret, one per line, and are never echoed back.

### One token, many workspaces

This is the structural difference from the Notion connector, and the reason this
app is not a copy of it:

| | Notion | Asana |
|---|---|---|
| Token scope | one **workspace** | one **user** |
| Workspaces per token | exactly 1 | every one the user belongs to |
| How they're found | implied by the token | discovered from `/users/me` |

So `workspace` is a parameter on every tool. With one reachable workspace it is
inferred; with several, a tool that is about to **write** refuses to guess and
lists them instead. Multiple accounts are supported the same way — one token per
line — for people holding separate personal and client tokens.

## Tools

**Reading** — `list_accounts`, `list_workspaces`, `search`, `search_tasks`,
`list_projects`, `list_tasks`, `get_task`, `list_sections`, `list_comments`,
`list_attachments`, `list_users`, `list_teams`, `check_access`

**Writing** — `connect_account`, `create_task`, `update_task`, `complete_task`,
`move_task`, `add_comment`, `create_project`, `create_section`,
`set_task_dependency`, `set_task_followers`, `set_task_tags`

**Live updates** — `watch_project`, `list_webhooks`, `unwatch`,
`inbound_status`. Asana pushes changes to
`POST /v1/ext/asana-connector/webhook/events`, which emits
`task_added`, `task_changed`, `task_completed`, `task_deleted`,
`comment_added` and `project_changed` for automations to react to.

**Destructive** — `delete_task` (confirmation-gated; Asana keeps deleted tasks
recoverable for 30 days)

### Known blocker: the handshake cannot complete yet

`watch_project` currently fails, and the cause is upstream of this extension:
**the webhook layer does not apply a handler's response to the wire.**

Asana completes a subscription only if the endpoint echoes `X-Hook-Secret` as an
HTTP **header**. The handler does return it. Two live probes show what happens:

| handler returns | wire status | wire headers | wire body |
|---|---|---|---|
| `{"status_code": 200, "headers": {"X-Hook-Secret": …}}` | 200 | *no echo* | the dict, verbatim |
| `{"status_code": 401, "body": "unauthorised"}` | **200** | — | the dict, verbatim |

The second row is the decisive one: the docs state *“return `{"status_code": N,
…}` to control HTTP status”*, and it does not — a refusal answered as 401 is
delivered as 200. So this is not about headers specifically, and not about
`WebhookResponse` (a type the SDK declares, exports, and references nowhere).
The documented response contract is simply not implemented; the return value is
serialised as the body.

Two things were tried first and ruled out by probe: declaring
`secret_header="X-Hook-Secret"` in the manifest, and returning the documented
plain dict instead of `WebhookResponse`.

Asana words the failure as “the remote server did not respond with the handshake
secret”, which reads like a token or scope failure and is neither. Both
`watch_project` and `inbound_status` say so plainly instead, and a test pins that
wording in place.

Everything else in the inbound channel — signature verification, heartbeat
handling, de-duplication, event mapping, subscription management — is built,
tested and deployed, and starts working the moment a webhook response is applied
to the wire.

> Note for whoever fixes the platform: until then, a webhook endpoint cannot
> refuse anything with a non-200 status. This connector still *processes*
> nothing it cannot verify, so no forged delivery is acted on — but the caller
> is told 200 regardless.

## Webhooks: three rules that are easy to get wrong

**The secret ARRIVES, it is never pasted.** Asana creates a webhook by POSTing
a new `X-Hook-Secret` to the endpoint and blocking the create call until that
header is echoed back. So the handshake is answered *before* any signature
check — at that moment no stored secret exists, because that request is the
secret arriving. Verifying first would reject the delivery that establishes
every later one, and creation would always time out.

**A heartbeat is load-bearing.** Asana pings every 8 hours with an empty event
list and deletes the webhook after 24 hours with no successful response. An
endpoint that skips empty payloads works for a day, then goes dead silently.

**Asana does not say which webhook a delivery belongs to.** There is no gid in
the headers — only a signature. The delivery is matched by trying each stored
secret, which doubles as authentication: a body no stored secret verifies is
not from Asana.

## Asana constraints worth knowing

**Dependencies and dependents come back COMPACT.** Asana returns them as
gid-only resources even when `dependencies.name` is in `opt_fields`, so a
name-based read finds nothing and the field renders empty while the links
plainly exist. `get_task` fetches the names separately.

**A free workspace accepts dependency writes and stores nothing.** Task
dependencies are a paid feature and the API mirrors the product limit without
an error: HTTP 200, no data written. `set_task_dependency` reads the write
back and reports the no-op instead of claiming success.

**Attachment `download_url` expires within minutes.** Only `permanent_url` is
surfaced, because a signed link is already dead by the time a user clicks it.

**`start_on` needs `due_on` in the SAME request.** Not merely present on the
task -- setting a start date on a task that already had a due date still fails
with "You must provide `due_on` or `due_at` when setting `start_on`". Found on
a live call, not by any mock. `update_task` therefore reads the existing due
date and echoes it back unchanged.

These are API facts that shape the connector's behaviour, not design choices:

* **`GET /tasks` refuses a bare workspace.** It needs `project`, or `tag`, or
  `assignee` + `workspace`. `list_tasks` checks first and names the fix instead
  of forwarding a `400: workspace: Missing input`.
* **Advanced search is premium-only.** `/workspaces/{gid}/tasks/search` answers
  `402` on free plans, so plain `search` uses **typeahead**, which works
  everywhere. `search_tasks` exposes the richer filters and reports the premium
  requirement honestly when it hits one.
* **Typeahead is approximate.** It matches on the *start of words* and returns a
  single unpaginated page — the summaries say so, so an incomplete result does
  not read as a bug. Use `list_tasks` by project when completeness matters.
* **`due_on` and `due_at` are mutually exclusive.** A date sets `due_on`, a
  timestamp sets `due_at`; sending both is rejected. `update_task` picks one.
* **Task updates are `PUT`.** A `PATCH` comes back as a 404 that reads like
  "task not found".
* **Comments are "stories"** and the feed mixes human comments with system
  activity. `list_comments` shows comments only unless you ask for the activity.
* **Gids are opaque strings.** Never coerced to int — some are lossy at 64 bits.

## Panels

One center panel (`asana`) with a `view=` parameter — `connect` and `accounts` —
plus a left nav panel. Two panels claiming `slot="center"` fight over one slot,
so there is deliberately only one.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q     # 113 tests
.venv/bin/imperal validate .
.venv/bin/imperal build .
```

The suite is structural, not just behavioural: `tests/test_contract.py` asserts
things a unit test cannot see — that every `params.X` a handler reads is a real
field on that tool's model, that no handler calls a `ctx` method that does not
exist, that errors always carry a machine code, and that tokens never reach a
log line.
