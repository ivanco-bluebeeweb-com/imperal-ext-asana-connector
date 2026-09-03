"""Panels: connect first, then show what is reachable.

Three surfaces, in the order a new user meets them:

* ``connect``  -- paste a personal access token and be done. This exists because
                  a first-time user opens the app and needs somewhere obvious to
                  put their token; the auto-injected Secrets tab is correct but
                  not discoverable.
* ``accounts`` -- connected accounts, the workspaces each one reaches, and what
                  to do when something is missing.
* ``asana_nav`` -- left sidebar: connection state at a glance.

ONE CENTER PANEL, SCREENS AS A PARAMETER. The Notion connector first declared
``connect`` and ``accounts`` as two separate panels, both ``slot="center"`` with
``center_overlay=True``. A center slot holds exactly ONE panel with REPLACE
semantics -- no stacking, no tabs -- and the host fetches every slot in one batch
at session init. Two overlay panels claiming one slot therefore race: both load,
one silently replaces the other, and a button dispatching the loser looks like
nothing happening while the shell re-renders. That shipped as visible chaos
("the sidebar reloads and nothing happens") and no button fix could cure it
while two panels owned one slot. So here there is exactly one owner and ``view``
selects the screen.

CREDENTIAL HANDLING (federal EXT-SECRETS-V1)
``asana_tokens`` is declared ``write_mode="both"``, so Panel UI *and* extension
code may write it. The token field therefore submits to ``connect_account`` -- a
function of THIS extension -- which validates the token against Asana before
storing it. Two lessons are baked into that choice:

* A panel form's ``action=`` resolves against the functions of the extension
  that rendered the panel. The docs recipe shows
  ui.Button("Connect with Asana (OAuth 2.0)", variant="primary", size="sm", icon="login"),
  ui.Divider(),
  ui.Text("Or connect via Personal Access Token", variant="caption"),
  ``ui.Form(action="save_app_secret")``, but that action belongs to the
  *developer* extension, so clicking it dies with "Function not found".
* Declaring the secret ``write_mode="user"`` (as Notion's did at first) makes
  ``ctx.secrets.set()`` raise SecretWriteForbidden, which left the Connect
  screen with nothing legal to call: saving looked successful while the runtime
  still saw no token, and the field just cleared. Writing through our own
  handler means "saved" and "visible" travel the same path and cannot disagree.

No panel here ever reads a token back.
"""

from __future__ import annotations

from imperal_sdk import ui

import accounts as acct
from app import ext

_PAT_URL = "https://app.asana.com/0/my-apps"

_SECRET_NAME = "asana_tokens"

# The canonical credential route, for users who would rather manage the stored
# value directly. Derived from the Extension so it can never drift from the real
# app id.
_SECRETS_ROUTE = f"/ext/{ext.app_id}/secrets#{_SECRET_NAME}"


def _errors_of(records: list[dict]) -> list[str]:
    """Human sentences for tokens that came back unusable.

    `list_accounts` returns ONE list of records, each carrying its own status --
    a broken token is a row, not an exception, so the messages are derived here
    rather than returned alongside.
    """
    out: list[str] = []
    for record in records:
        if record.get("status") != "ok":
            label = record.get("user_name") or "A token"
            detail = record.get("error") or "This token is not usable."
            out.append(f"{label}: {detail}")
    return out


def _workspace_count(records: list[dict]) -> int:
    """How many workspaces are reachable across every usable account."""
    return sum(
        len(r.get("workspaces") or [])
        for r in records
        if r.get("status") == "ok"
    )


def _state_alert(records: list[dict]):
    """One banner describing the connection state in the user's terms."""
    errors = _errors_of(records)
    usable = sum(1 for r in records if r.get("status") == "ok")

    if not records:
        return ui.Alert(
            title="No Asana account connected yet",
            message=(
                "Use Connect Asana below: create a personal access token and "
                "paste it in. It reaches every workspace your Asana user is a "
                "member of."
            ),
            type="info",
        )
    if not usable:
        return ui.Alert(
            title="Not connected",
            message=" ".join(errors),
            type="error",
        )
    if errors:
        return ui.Alert(
            title=f"{usable} of {len(records)} account(s) ready",
            message=" ".join(errors),
            type="warning",
        )
    return ui.Alert(
        title=f"Connected -- {_workspace_count(records)} workspace(s) reachable",
        message=(
            "Ready. Ask in chat to list projects, find tasks, or create and "
            "update them -- by name, no ids needed."
        ),
        type="success",
    )


async def connect_panel(ctx, **kwargs):
    """Paste a personal access token and connect an account.

    NOT a panel of its own: it is one VIEW of the single center panel below.

    SKETCH -- connect screen (props checked against ui-components-reference)
      ui.Stack (v, gap=4)
        ui.Header(text="Connect Asana", level=2, subtitle=...)
        ui.Alert(...)                       -- already-connected notice, if any
        ui.Section(title="1. Create a token", children=[
          ui.Text(content=..., variant="body")
          ui.Link(label="Open app.asana.com/0/my-apps", href=...)
        ])
        ui.Section(title="2. Paste the token", children=[
          ui.Text(content=..., variant="body")
          ui.Form(action="connect_account", submit_label="Connect", children=[
            ui.Password(placeholder="2/...", param_name="token")
          ])
          ui.Link(label="Or manage the stored tokens directly", href=_SECRETS_ROUTE)
        ])
        ui.Section(title="3. Check what it reaches", children=[
          ui.Text(content=..., variant="body")
          ui.Button(label="Check what is reachable", ...)
        ])

    Checklist notes (each one is a mistake already made once):
      * ui.Text takes content=, NOT text= -- but ui.Header takes text=. Writing
        ui.Text(text=...) by analogy fails the platform's DUI prop check while
        the LOCAL validator reports zero errors, so it only surfaces at deploy.
      * ui.Section always gets children=.
      * ui.Password returns an Input node with type="password" -- assert on
        Input, not on a "Password" node type.
      * slot="center" REQUIRES center_overlay=True, else it is never fetched.
    """
    try:
        records = await acct.list_accounts(ctx)
    except Exception:
        await ctx.log("connect panel could not read account state", "error")
        records = []

    children = [
        ui.Header(
            text="Connect Asana",
            level=2,
            subtitle="Three steps, about a minute",
        )
    ]

    # Tokens are APPENDED by connect_account, so an existing setup is safe --
    # this is reassurance, not a warning about clobbering another account.
    if records:
        children.append(ui.Alert(
            title=f"{len(records)} account(s) already connected",
            message=(
                "Adding another token here keeps the existing ones: each "
                "account is a separate line in the stored value."
            ),
            type="info",
        ))

    children.append(ui.Section(
        title="1. Create a personal access token",
        children=[
            ui.Text(
                content=(
                    "Open app.asana.com/0/my-apps, choose Create new token, "
                    "name it something like Imperal, and copy the value (it "
                    "starts with 2/). Asana shows it once. The token acts as "
                    "your Asana user and reaches every workspace you belong "
                    "to -- there is no redirect URI or app review to set up."
                ),
                variant="body",
            ),
            ui.Link(
                label="Open app.asana.com/0/my-apps",
                href=_PAT_URL,
            ),
        ],
    ))

    children.append(ui.Section(
        title="2. Paste the token",
        children=[
            ui.Text(
                content=(
                    "Paste it below. The token is checked against Asana "
                    "before it is saved, so you find out immediately whether "
                    "it works -- and it is stored encrypted, never shown back "
                    "here, not even to you."
                ),
                variant="body",
            ),
            ui.Form(
                action="connect_account",
                submit_label="Connect",
                children=[
                    ui.Password(
                        placeholder="2/1234567890abcdef...",
                        param_name="token",
                    ),
                ],
            ),
            ui.Text(
                content=(
                    "Connecting a second Asana account? Paste its token here "
                    "too -- each one is appended, not replaced."
                ),
                variant="caption",
            ),
            ui.Link(
                label="Or manage the stored tokens directly",
                href=_SECRETS_ROUTE,
            ),
        ],
    ))

    children.append(ui.Section(
        title="3. Check what it reaches",
        children=[
            ui.Text(
                content=(
                    "Unlike some connectors, Asana needs no per-project "
                    "sharing step: the token sees whatever your Asana user "
                    "can already see. If a project is missing, it usually "
                    "means that account is not a member of it, or it lives in "
                    "another workspace."
                ),
                variant="body",
            ),
            ui.Row(
                gap=3,
                children=[
                    ui.Button(
                        label="Check what is reachable",
                        variant="primary",
                        on_click=ui.Call("__panel__asana", view="accounts", refresh=True),
                    ),
                    ui.Button(
                        label="Ask in chat instead",
                        variant="ghost",
                        on_click=ui.Send("Check my Asana access"),
                    ),
                ],
            ),
        ],
    ))

    return ui.Stack(direction="v", gap=4, children=children)


async def accounts_panel(ctx, **kwargs):
    """Render connected accounts and the workspaces they reach.

    One VIEW of the single center panel, not a panel itself.

    SKETCH -- accounts panel
      ui.Stack (v, gap=4)
        ui.Header(text="Asana Connector", level=2, subtitle=...)
        ui.Alert(...)                                   -- connection state
        ui.Section(title="Connected accounts", children=[
          ui.DataTable(columns=[DataColumn...], rows=[plain dicts])
          | ui.Empty(message=..., action=ui.Call("__panel__asana", view="connect"))
        ])
        ui.Section(title="How access works", children=[
          ui.Text(content=..., variant="body") x2   -- content=, NOT text=
          ui.Row([ui.Button, ui.Button, ui.Link])
        ])

    Rendering against real fixtures is what catches the bug class that shipped
    in Notion: unpacking a LIST return into two names, and reading record keys
    that never existed. Both were invisible until a panel test rendered them.
    """
    refresh = bool(kwargs.get("refresh"))

    records: list[dict] = []
    load_failed = False
    try:
        # Returns ONE list of records; each row carries its own status.
        records = await acct.list_accounts(ctx, refresh=refresh)
    except Exception:
        # The panel must still render: a blank screen is worse than a banner.
        # Detail goes to the audit log, never into the user-facing string.
        await ctx.log("accounts panel failed to load accounts", "error")
        load_failed = True

    rows = []
    for record in records:
        workspaces = record.get("workspaces") or []
        names = ", ".join(
            str(w.get("name") or "") for w in workspaces if w.get("name")
        )
        rows.append({
            "account": record.get("user_name") or "Unknown account",
            "email": record.get("email") or "",
            "workspaces": names or "--",
            "status": "Ready" if record.get("status") == "ok" else "Needs attention",
        })

    if rows:
        body = ui.DataTable(
            columns=[
                ui.DataColumn(key="account", label="Account"),
                ui.DataColumn(key="email", label="Email"),
                ui.DataColumn(key="workspaces", label="Workspaces"),
                ui.DataColumn(key="status", label="Status"),
            ],
            rows=rows,
        )
    else:
        body = ui.Empty(
            message="No Asana account connected yet.",
            action=ui.Call("__panel__asana", view="connect"),
        )

    alert = (
        ui.Alert(
            title="Could not load accounts",
            message="Something went wrong on our side. Try Re-check access.",
            type="error",
        )
        if load_failed
        else _state_alert(records)
    )

    return ui.Stack(
        direction="v",
        gap=4,
        children=[
            ui.Header(
                text="Asana Connector",
                level=2,
                subtitle="Browse projects, find tasks and update them from Imperal",
            ),
            alert,
            ui.Section(title="Connected accounts", children=[body]),
            ui.Section(
                title="How access works",
                children=[
                    ui.Text(
                        content=(
                            "A personal access token acts as your Asana user: "
                            "it reaches every workspace and organization you "
                            "are a member of, with the same permissions you "
                            "have in the Asana app."
                        ),
                        variant="body",
                    ),
                    ui.Text(
                        content=(
                            "Missing a project? That account is probably not a "
                            "member of it, or it sits in a different "
                            "workspace. Advanced task search also needs a paid "
                            "Asana plan -- listing by project or assignee "
                            "works on every plan."
                        ),
                        variant="body",
                    ),
                    ui.Row(
                        gap=3,
                        children=[
                            ui.Button(
                                label="Re-check access",
                                variant="secondary",
                                on_click=ui.Call("__panel__asana", view="accounts", refresh=True),
                            ),
                            ui.Button(
                                label="Connect another account",
                                variant="ghost",
                                on_click=ui.Call("__panel__asana", view="connect"),
                            ),
                            ui.Link(
                                label="Open app.asana.com/0/my-apps",
                                href=_PAT_URL,
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )


@ext.panel("asana", slot="center", title="Asana", icon="CheckSquare",
           center_overlay=True, refresh="manual")
async def asana_center(ctx, **kwargs):
    """The ONE center panel. `view` picks which screen renders inside it.

    Dispatch targets are always the mounted panel, because there is only one:

        ui.Call("__panel__asana")                    -> accounts (default)
        ui.Call("__panel__asana", view="connect")    -> connect screen
        ui.Call("__panel__asana", refresh=True)      -> accounts, re-read

    A first-time user with no token lands on the connect screen automatically:
    the default view answers "what do I do now?" instead of showing an empty
    table.
    """
    view = str(kwargs.get("view") or "").strip().lower()

    if view not in ("connect", "accounts"):
        # No explicit view: send an unconfigured user straight to the one
        # action that unblocks them, and everyone else to their accounts.
        try:
            records = await acct.list_accounts(ctx)
        except Exception:
            records = []
        view = "accounts" if records else "connect"

    if view == "connect":
        return await connect_panel(ctx, **kwargs)
    return await accounts_panel(ctx, **kwargs)


@ext.panel("asana_nav", slot="left", title="Asana", icon="CheckSquare",
           refresh="manual")
async def asana_nav(ctx, **kwargs):
    """Sidebar entry: connection state at a glance, and a way in.

    SKETCH -- left nav panel
      ui.Stack (v, gap=2)
        ui.Text(content=<state>, variant="body")
        ui.Button("Connect Asana" | "Open Asana panel")
        ui.Button("My tasks", variant="ghost")

    Deliberately tiny: the sidebar is for orientation, not for data. The first
    button changes with state -- an unconfigured app should offer the ONE action
    that unblocks it.
    """
    try:
        records = await acct.list_accounts(ctx)
    except Exception:
        # The sidebar must never be the thing that breaks the shell.
        records = []

    usable = sum(1 for r in records if r.get("status") == "ok")
    if not records:
        state = "Not connected yet"
        primary = ui.Button(
            label="Connect Asana",
            variant="primary",
            on_click=ui.Call("__panel__asana", view="connect"),
        )
    else:
        state = f"{usable} of {len(records)} account(s) ready"
        primary = ui.Button(
            label="Open Asana panel",
            variant="secondary",
            on_click=ui.Call("__panel__asana", view="accounts"),
        )

    return ui.Stack(
        direction="v",
        gap=2,
        children=[
            ui.Text(content=state, variant="body"),
            primary,
            ui.Button(
                label="My tasks",
                variant="ghost",
                on_click=ui.Send("Show my Asana tasks"),
            ),
        ],
    )