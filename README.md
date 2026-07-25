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
`list_users`, `list_teams`, `check_access`

**Writing** — `connect_account`, `create_task`, `update_task`, `complete_task`,
`move_task`, `add_comment`, `create_project`, `create_section`,
`set_task_dependency`, `set_task_followers`, `set_task_tags`

**Destructive** — `delete_task` (confirmation-gated; Asana keeps deleted tasks
recoverable for 30 days)

## Asana constraints worth knowing

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
