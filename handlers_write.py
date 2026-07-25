"""Write tools: create/update/complete/move/delete tasks, comments, projects,
sections -- plus `connect_account`, which stores the token.

Two Asana shapes drive this file, both verified against the docs:

* WRITES USE PUT, NOT PATCH. `PUT /tasks/{gid}` updates a task; there is no
  PATCH route, and sending one is a 404 that reads like a missing task.
* EVERY WRITE BODY IS WRAPPED: `{"data": {...}}`. `asana_client.request` does
  that wrapping in exactly one place, so no call site here repeats it.

`connect_account` is the direct descendant of a real bug. The Notion connector
first declared its secret `write_mode="user"`, which meant extension code could
not write it: the Connect form had no legal action, saving appeared to succeed
while the runtime still saw nothing, and the user was left pasting a token into
a field that silently did nothing. The fix -- carried straight over here -- is
`write_mode="both"` plus a tool that VALIDATES the token against the API before
storing it, and writes it through the same client it later reads with, so
"saved" and "visible" cannot disagree.
"""

from __future__ import annotations

from imperal_sdk import ActionResult

import accounts as acct
import asana_client as ac
import asana_objects as ao
import shared
from app import chat
from models import (
    AddCommentParams,
    CompleteTaskParams,
    ConnectAccountParams,
    ConnectResult,
    CreateProjectParams,
    CreateSectionParams,
    CreateTaskParams,
    DeleteTaskParams,
    MoveTaskParams,
    UpdateTaskParams,
    WriteResult,
)

_error = shared.error
_from_envelope = shared.from_envelope
_resolve = shared.resolve


def _result(gid: str, title: str, url: str, detail: str,
            action: str = "") -> WriteResult:
    # `name` and `action` are declared on WriteResult and were never filled, so
    # a chained tool reading the result of a write got blanks.
    return WriteResult(id=gid, title=title, gid=gid, name=title, url=url,
                       detail=detail, action=action)


@chat.function(
    "connect_account",
    "Connect an Asana account by saving its personal access token, after "
    "checking the token actually works.",
    action_type="write", chain_callable=True,
    effects=["asana.account.connected"],
    data_model=ConnectResult,
    event="asana-connector.connect_account",
)
async def connect_account(ctx, params: ConnectAccountParams) -> ActionResult:
    """Validate a token against Asana, then store it.

    Validate-then-store, never store-then-hope: a token Asana rejects is never
    written, so the stored set cannot hold a credential that does not work. The
    reply names the account and its workspaces, which is the only honest way for
    the user to confirm they pasted the right token -- the value itself is never
    echoed back.
    """
    token = (params.token or "").strip()
    if not token:
        return _error(
            "No token was provided. Create a personal access token at "
            "app.asana.com/0/my-apps and paste it here.",
            ac.ASANA_VALIDATION_FAILED)

    out = await acct.add_token(ctx, token)
    if not out.get("ok"):
        return _from_envelope(out)

    # `add_token` hands back workspace NAMES already flattened to strings
    # (`workspace_names`), not workspace objects. Reading a "workspaces" key
    # here silently produced 0 workspaces and an empty list on every successful
    # connect -- the confirmation looked fine and told the user nothing about
    # where their token actually reaches.
    names_list = [str(n) for n in (out.get("workspace_names") or []) if n]
    names = ", ".join(names_list)
    entity = ConnectResult(
        id=str(out.get("slot", 0)),
        title=str(out.get("user_name") or "Asana account"),
        account_name=str(out.get("user_name") or ""),
        email=str(out.get("email") or ""),
        already_connected=bool(out.get("already_connected")),
        workspace_count=len(names_list),
        workspaces=names,
        next_step=(
            "Ask for your projects or tasks in chat, for example "
            "\"list my Asana projects\"."
        ),
    )

    if out.get("already_connected"):
        summary = (f"That token was already connected -- {entity.account_name} "
                   f"({len(names_list)} workspace(s))")
    else:
        summary = (f"Connected {entity.account_name or 'the Asana account'}"
                   + (f" -- workspaces: {names}" if names else ""))
    return ActionResult.success(entity, summary)


@chat.function(
    "create_task",
    "Create a task in an Asana project, optionally assigned, with start and "
    "due dates.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="asana-connector.create_task",
    effects=["asana.task.created"],
)
async def create_task(ctx, params: CreateTaskParams) -> ActionResult:
    """Create a task.

    Asana requires a task to have a home: either a `projects` list or a
    `workspace`. A workspace-only task is legal but lands in the creator's
    private "My tasks", which is rarely what someone means, so a named project
    is resolved first when one is given.
    """
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    workspace_gid = workspace.get("gid", "")
    if not params.name.strip():
        return _error("A task needs a name.", ac.ASANA_VALIDATION_FAILED)

    data: dict = {"name": params.name.strip()}
    if params.notes:
        data["notes"] = params.notes

    if params.project:
        project = await shared.resolve_project(ctx, token, workspace_gid,
                                               params.project)
        if not project.get("ok"):
            return _from_envelope(project)
        data["projects"] = [project["gid"]]
        home = f"project '{project.get('name') or params.project}'"
    else:
        data["workspace"] = workspace_gid
        home = f"{workspace.get('name', 'workspace')} (My tasks)"

    if params.assignee:
        who = await shared.resolve_user(ctx, token, workspace_gid, params.assignee)
        if not who.get("ok"):
            return _from_envelope(who)
        data["assignee"] = who["gid"]

    if params.due:
        field, value = ao.due_field_for(params.due)
        if not field:
            return _error(
                f"'{params.due}' is not a date Asana accepts. Use YYYY-MM-DD, "
                "or a full timestamp for a due time.",
                ac.ASANA_VALIDATION_FAILED)
        data[field] = value

    if params.start:
        # Asana refuses `start_on` without a due date -- a task cannot begin
        # without ending. Its own error for this is opaque, so the requirement
        # is stated here instead of forwarded.
        if not params.due:
            return _error(
                "A start date needs a due date too -- Asana rejects a task "
                "that starts but never ends. Give both.",
                ac.ASANA_VALIDATION_FAILED)
        if "T" in params.start or ":" in params.start:
            return _error(
                "A start date is a day, not a moment. Use YYYY-MM-DD.",
                ac.ASANA_VALIDATION_FAILED)
        data["start_on"] = params.start.strip()

    if params.parent:
        parent = await shared.resolve_task(ctx, token, workspace_gid,
                                          params.parent)
        if not parent.get("ok"):
            return _from_envelope(parent)
        # A subtask inherits its parent's home; sending both is a conflict.
        data.pop("projects", None)
        data.pop("workspace", None)
        data["parent"] = parent["gid"]
        home = f"subtask of '{parent.get('name') or params.parent}'"

    out = await ac.request(ctx, "POST", "tasks", token, data=data,
                           params={"opt_fields": ao.TASK_FIELDS})
    if not out.get("ok"):
        return _from_envelope(out)

    task = out["data"]
    gid = ao.gid_of(task)
    return ActionResult.success(
        _result(gid, ao.name_of(task), str(task.get("permalink_url") or ""),
                f"Created in {home}", action="created"),
        f"Created task '{ao.name_of(task)}' in {home}")


@chat.function(
    "update_task",
    "Update a task's name, notes, assignee, or its start and due dates. Can "
    "also unassign it or clear either date.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="asana-connector.update_task",
    effects=["asana.task.updated"],
)
async def update_task(ctx, params: UpdateTaskParams) -> ActionResult:
    """Update task fields via PUT (Asana has no PATCH route for tasks)."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    workspace_gid = workspace.get("gid", "")
    target = await shared.resolve_task(ctx, token, workspace_gid, params.task)
    if not target.get("ok"):
        return _from_envelope(target)

    data: dict = {}
    changed: list[str] = []
    if params.name:
        data["name"] = params.name
        changed.append("name")
    if params.notes:
        data["notes"] = params.notes
        changed.append("notes")
    if params.assignee:
        who = await shared.resolve_user(ctx, token, workspace_gid, params.assignee)
        if not who.get("ok"):
            return _from_envelope(who)
        data["assignee"] = who["gid"]
        changed.append("assignee")
    elif params.clear_assignee:
        # Declared on the params model and advertised in the tool description,
        # but never read -- so "unassign this" silently did nothing.
        data["assignee"] = None
        changed.append("assignee (cleared)")
    if params.clear_due:
        data["due_on"] = None
        data["due_at"] = None
        changed.append("due date (cleared)")
    elif params.due:
        # An explicit clearing word removes the date; Asana takes null for that.
        if params.due.strip().lower() in ("none", "clear", "remove"):
            data["due_on"] = None
            changed.append("due date (cleared)")
        else:
            field, value = ao.due_field_for(params.due)
            if not field:
                return _error(
                    f"'{params.due}' is not a date Asana accepts. Use YYYY-MM-DD.",
                    ac.ASANA_VALIDATION_FAILED)
            data[field] = value
            changed.append("due date")

    if params.clear_start:
        data["start_on"] = None
        changed.append("start date (cleared)")
    elif params.start:
        if "T" in params.start or ":" in params.start:
            return _error(
                "A start date is a day, not a moment. Use YYYY-MM-DD.",
                ac.ASANA_VALIDATION_FAILED)
        # Asana rejects a start date on a task that has no due date. The due
        # date may already be set on the task, so requiring it in this call
        # would refuse a legitimate update. The resolve envelope cannot answer
        # this -- typeahead returns compact objects with no dates at all -- so
        # ask for the one field, and only in this branch.
        setting_due = "due_on" in data or "due_at" in data
        if params.clear_due:
            return _error(
                "A start date needs a due date too -- clearing the due date "
                "and setting a start date at the same time cannot both apply.",
                ac.ASANA_VALIDATION_FAILED)
        if not setting_due:
            current = await ac.request(
                ctx, "GET", f"tasks/{target['gid']}", token,
                params={"opt_fields": "due_on,due_at"})
            existing = current.get("data") or {} if current.get("ok") else {}
            existing_due = existing.get("due_on") or existing.get("due_at")
            # If the lookup itself failed, do not invent a verdict -- let the
            # PUT below surface the real reason.
            if current.get("ok") and not existing_due:
                return _error(
                    "A start date needs a due date too -- Asana rejects a "
                    "task that starts but never ends. Set a due date as well.",
                    ac.ASANA_VALIDATION_FAILED)
            # Asana wants the due date in the SAME request as `start_on`, not
            # merely present on the task: setting a start date on a task that
            # already had a due date still failed with "You must provide
            # `due_on` or `due_at` when setting `start_on`". So the existing
            # value is echoed back unchanged.
            if existing_due:
                field = "due_on" if existing.get("due_on") else "due_at"
                data[field] = existing_due
        data["start_on"] = params.start.strip()
        # Without this the reply reads "Updated " with an empty list -- the
        # write succeeded but told the user nothing about what changed.
        changed.append("start date")

    if not data:
        return _error(
            "Nothing to update -- give a name, notes, assignee, due date or "
            "start date.",
            ac.ASANA_VALIDATION_FAILED)

    out = await ac.request(ctx, "PUT", f"tasks/{target['gid']}", token, data=data,
                           params={"opt_fields": ao.TASK_FIELDS})
    if not out.get("ok"):
        return _from_envelope(out)

    task = out["data"]
    return ActionResult.success(
        _result(ao.gid_of(task), ao.name_of(task),
                str(task.get("permalink_url") or ""),
                f"Updated {', '.join(changed)}", action="updated"),
        f"Updated {', '.join(changed)} on '{ao.name_of(task)}'")


@chat.function(
    "complete_task",
    "Mark a task complete, or reopen it.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="asana-connector.complete_task",
    effects=["asana.task.completed"],
)
async def complete_task(ctx, params: CompleteTaskParams) -> ActionResult:
    """Toggle completion -- the single most common Asana write."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    target = await shared.resolve_task(ctx, token, workspace.get("gid", ""),
                                       params.task)
    if not target.get("ok"):
        return _from_envelope(target)

    out = await ac.request(ctx, "PUT", f"tasks/{target['gid']}", token,
                           data={"completed": bool(params.completed)},
                           params={"opt_fields": ao.TASK_FIELDS})
    if not out.get("ok"):
        return _from_envelope(out)

    task = out["data"]
    word = "completed" if params.completed else "reopened"
    return ActionResult.success(
        _result(ao.gid_of(task), ao.name_of(task),
                str(task.get("permalink_url") or ""), f"Task {word}",
                action=word),
        f"Marked '{ao.name_of(task)}' {word}")


@chat.function(
    "move_task",
    "Move a task to a different project or section.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="asana-connector.move_task",
    effects=["asana.task.moved"],
)
async def move_task(ctx, params: MoveTaskParams) -> ActionResult:
    """Move a task between projects or sections.

    Membership is not a writable field on a task: Asana uses dedicated
    `addProject`/`removeProject` endpoints, and a section travels as `section` on
    addProject. PUTting `projects` would be silently ignored -- a "successful"
    call that moved nothing.
    """
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    workspace_gid = workspace.get("gid", "")
    target = await shared.resolve_task(ctx, token, workspace_gid, params.task)
    if not target.get("ok"):
        return _from_envelope(target)

    if not params.project and not params.section:
        return _error("Name a project or a section to move the task to.",
                      ac.ASANA_VALIDATION_FAILED)

    project_gid = ""
    project_name = params.project
    if params.project:
        project = await shared.resolve_project(ctx, token, workspace_gid,
                                               params.project)
        if not project.get("ok"):
            return _from_envelope(project)
        project_gid = project["gid"]
        project_name = project.get("name") or params.project

    section_gid = ""
    if params.section:
        # Sections belong to a project, so the project must be known first.
        if not project_gid:
            current = await ac.request(
                ctx, "GET", f"tasks/{target['gid']}", token,
                params={"opt_fields": "projects.gid,projects.name"})
            if not current.get("ok"):
                return _from_envelope(current)
            projects = current["data"].get("projects") or []
            if len(projects) != 1:
                return _error(
                    f"Name the project too: this task is in {len(projects)} "
                    "project(s), so which section to use is ambiguous.",
                    ac.ASANA_TARGET_AMBIGUOUS)
            project_gid = ao.gid_of(projects[0])
            project_name = ao.name_of(projects[0])

        sections = await ac.paginate(ctx, f"projects/{project_gid}/sections",
                                     token, params={"opt_fields": "gid,name"},
                                     limit=100)
        if not sections.get("ok"):
            return _from_envelope(sections)
        wanted = params.section.strip().lower()
        matches = [s for s in sections["results"]
                   if ao.name_of(s).strip().lower() == wanted]
        if not matches:
            matches = [s for s in sections["results"]
                       if wanted in ao.name_of(s).strip().lower()]
        if not matches:
            available = ", ".join(ao.name_of(s) for s in sections["results"]) or "-"
            return _error(
                f"No section named '{params.section}' in '{project_name}'. "
                f"Sections there: {available}.",
                ac.ASANA_TARGET_NOT_FOUND)
        if len(matches) > 1:
            return _error(
                f"'{params.section}' matches several sections in '{project_name}'.",
                ac.ASANA_TARGET_AMBIGUOUS)
        section_gid = ao.gid_of(matches[0])

    data: dict = {"project": project_gid}
    if section_gid:
        data["section"] = section_gid
    out = await ac.request(ctx, "POST", f"tasks/{target['gid']}/addProject",
                           token, data=data)
    if not out.get("ok"):
        return _from_envelope(out)

    where = f"'{project_name}'"
    if params.section:
        where += f" / section '{params.section}'"
    return ActionResult.success(
        _result(target["gid"], target.get("name", "") or "task", "",
                f"Added to {where}", action="moved"),
        f"Moved the task to {where}")


@chat.function(
    "delete_task",
    "Delete a task. Asana keeps deleted tasks recoverable for 30 days.",
    action_type="destructive", chain_callable=True,
    effects=["asana.task.deleted"],
    data_model=WriteResult,
    event="asana-connector.delete_task",
)
async def delete_task(ctx, params: DeleteTaskParams) -> ActionResult:
    """Delete a task.

    Declared `action_type="destructive"`, which is the ONLY correct way to ask
    for a confirmation gate: the kernel decides whether to interrupt, based on
    the user's own confirmation settings (guards._check_confirmation_guard keys
    on action_type, and a tool-level `confirm=` argument does not exist). This is
    the one write here a user cannot undo from chat. Asana does keep deleted
    tasks recoverable for 30 days, which the reply states so the real stakes are
    known rather than guessed.
    """
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    target = await shared.resolve_task(ctx, token, workspace.get("gid", ""),
                                       params.task)
    if not target.get("ok"):
        return _from_envelope(target)

    name = target.get("name") or "the task"
    out = await ac.request(ctx, "DELETE", f"tasks/{target['gid']}", token)
    if not out.get("ok"):
        return _from_envelope(out)

    return ActionResult.success(
        _result(target["gid"], name, "", "Deleted", action="deleted"),
        f"Deleted '{name}'. Asana keeps deleted tasks recoverable for 30 days.")


@chat.function(
    "add_comment",
    "Add a comment to a task.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="asana-connector.add_comment",
    effects=["asana.comment.created"],
)
async def add_comment(ctx, params: AddCommentParams) -> ActionResult:
    """Post a comment (a `story` carrying text) on a task."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    if not params.comment.strip():
        return _error("The comment is empty.", ac.ASANA_VALIDATION_FAILED)

    target = await shared.resolve_task(ctx, token, workspace.get("gid", ""),
                                       params.task)
    if not target.get("ok"):
        return _from_envelope(target)

    out = await ac.request(ctx, "POST", f"tasks/{target['gid']}/stories", token,
                           data={"text": params.comment})
    if not out.get("ok"):
        return _from_envelope(out)

    name = target.get("name") or "the task"
    return ActionResult.success(
        _result(ao.gid_of(out["data"]), name, "", "Comment added",
                action="commented"),
        f"Commented on '{name}'")


@chat.function(
    "create_project",
    "Create a project in a workspace or team.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="asana-connector.create_project",
    effects=["asana.project.created"],
)
async def create_project(ctx, params: CreateProjectParams) -> ActionResult:
    """Create a project.

    In an ORGANIZATION a project must belong to a team -- Asana rejects a
    team-less project there. So when the target is an organization and no team
    was named, this lists the available teams instead of forwarding an error the
    user cannot act on.
    """
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    workspace_gid = workspace.get("gid", "")
    if not params.name.strip():
        return _error("A project needs a name.", ac.ASANA_VALIDATION_FAILED)

    data: dict = {"name": params.name.strip(), "workspace": workspace_gid}
    if params.notes:
        data["notes"] = params.notes
    # `public` is writable ONLY in a personal workspace. In an ORGANIZATION
    # visibility follows team membership and Asana answers
    # "public: Cannot write this property" -- so sending it there rejected
    # every single project creation, since the param defaults to True.
    if params.public and not workspace.get("is_organization"):
        data["public"] = True

    if params.team:
        team = await acct.resolve_target(ctx, token, workspace_gid, params.team,
                                         resource_type="team")
        if not team.get("ok"):
            return _from_envelope(team)
        data["team"] = team["gid"]
    elif workspace.get("is_organization"):
        teams = await ac.paginate(ctx, f"organizations/{workspace_gid}/teams",
                                  token, params={"opt_fields": "gid,name"},
                                  limit=50, max_pages=1)
        available = ", ".join(ao.name_of(t) for t in teams.get("results", [])) \
            if teams.get("ok") else ""
        return _error(
            f"'{workspace.get('name')}' is an Asana organization, so a new "
            "project needs a team."
            + (f" Available teams: {available}." if available else ""),
            ac.ASANA_VALIDATION_FAILED)

    out = await ac.request(ctx, "POST", "projects", token, data=data,
                           params={"opt_fields": ao.PROJECT_FIELDS})
    if not out.get("ok"):
        return _from_envelope(out)

    project = out["data"]
    return ActionResult.success(
        _result(ao.gid_of(project), ao.name_of(project),
                str(project.get("permalink_url") or ""), "Project created",
                action="created"),
        f"Created project '{ao.name_of(project)}' in "
        f"{workspace.get('name', 'the workspace')}")


@chat.function(
    "create_section",
    "Add a section (column) to a project.",
    action_type="write", chain_callable=True,
    data_model=WriteResult,
    event="asana-connector.create_section",
    effects=["asana.section.created"],
)
async def create_section(ctx, params: CreateSectionParams) -> ActionResult:
    """Add a section to a project."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    if not params.name.strip():
        return _error("A section needs a name.", ac.ASANA_VALIDATION_FAILED)

    project = await shared.resolve_project(ctx, token, workspace.get("gid", ""),
                                           params.project)
    if not project.get("ok"):
        return _from_envelope(project)

    out = await ac.request(ctx, "POST", f"projects/{project['gid']}/sections",
                           token, data={"name": params.name.strip()},
                           params={"opt_fields": ao.SECTION_FIELDS})
    if not out.get("ok"):
        return _from_envelope(out)

    section = out["data"]
    project_name = project.get("name") or params.project
    return ActionResult.success(
        _result(ao.gid_of(section), ao.name_of(section), "",
                f"Section added to '{project_name}'", action="created"),
        f"Added section '{ao.name_of(section)}' to '{project_name}'")
