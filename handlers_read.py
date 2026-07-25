"""Read tools: accounts, workspaces, search, projects, tasks, comments, people,
teams, sections, access report.

Reading has to be genuinely useful before any write flow matters, so the read
layer is the larger half of this connector.

Two Asana constraints shape it, and both are load-bearing:

* `GET /tasks` REFUSES to run without a narrowing filter -- it needs a project,
  a tag, or assignee AND workspace together. Sending a bare workspace is a 400
  with "Missing input". So `list_tasks` checks the combination BEFORE the
  request and explains what to add, rather than forwarding a failure.
* Advanced search (`/workspaces/{gid}/tasks/search`) is PREMIUM-ONLY and answers
  402 for free plans. Making it the default search would break the connector's
  most basic verb for anyone on a free plan, so `search` uses TYPEAHEAD (every
  plan) and `search_tasks` exposes the premium endpoint separately with an
  honest ASANA_PREMIUM_REQUIRED code when it is unavailable.
"""

from __future__ import annotations

from imperal_sdk import ActionResult

import accounts as acct
import asana_client as ac
import asana_objects as ao
import shared
from app import chat
from models import (
    AccessReport,
    AdvancedSearchParams,
    AsanaAccount,
    AsanaAccountList,
    AsanaComment,
    AsanaCommentList,
    AsanaObject,
    AsanaObjectList,
    AsanaProject,
    AsanaProjectList,
    AsanaSection,
    AsanaSectionList,
    AsanaTask,
    AsanaTaskList,
    AsanaTeam,
    AsanaTeamList,
    AsanaUser,
    AsanaUserList,
    AsanaWorkspace,
    AsanaWorkspaceList,
    CheckAccessParams,
    GetTaskParams,
    ListAccountsParams,
    ListCommentsParams,
    ListProjectsParams,
    ListSectionsParams,
    ListTasksParams,
    ListTeamsParams,
    ListUsersParams,
    ListWorkspacesParams,
    SearchParams,
)

# Shared with handlers_write via `shared` so neither tool layer depends on the
# other. Re-exported under short private names to keep call sites readable.
ACCESS_NOTE = shared.ACCESS_NOTE
_error = shared.error
_from_envelope = shared.from_envelope
_resolve = shared.resolve

# Advice appended when a token is configured but reaches nothing. Kept in one
# place because "empty" has exactly two plausible causes and guessing between
# them is what makes a connector feel broken.
_NO_TOKEN_HINT = (
    "No Asana access token is configured yet. Open the Asana Connector app and "
    "use the Connect Asana screen to paste a personal access token."
)


def _task_entity(task: dict) -> AsanaTask:
    """One task payload -> the entity the panel and chat render."""
    gid = ao.gid_of(task)
    name = ao.name_of(task)
    return AsanaTask(
        id=gid,
        title=name or "(unnamed task)",
        gid=gid,
        # `title` is what the card renders; `name` is what the NEXT tool in a
        # chain reads. Declaring it and never filling it hands the chain an
        # empty string instead of the task's name.
        name=name,
        completed=bool(task.get("completed")),
        assignee=ao.nested_name(task, "assignee"),
        due=str(task.get("due_on") or task.get("due_at") or ""),
        start=str(task.get("start_on") or ""),
        notes=str(task.get("notes") or ""),
        projects=", ".join(ao.name_list(task, "projects")),
        parent=ao.nested_name(task, "parent"),
        blocked_by=", ".join(ao.name_list(task, "dependencies")),
        blocking=", ".join(ao.name_list(task, "dependents")),
        followers=", ".join(ao.name_list(task, "followers")),
        tags=", ".join(ao.name_list(task, "tags")),
        subtask_count=int(task.get("num_subtasks") or 0),
        url=str(task.get("permalink_url") or ""),
        modified=str(task.get("modified_at") or ""),
        summary=ao.render_task(task),
    )


def _project_entity(project: dict) -> AsanaProject:
    gid = ao.gid_of(project)
    name = ao.name_of(project)
    return AsanaProject(
        id=gid,
        title=name or "(unnamed project)",
        gid=gid,
        name=name,
        archived=bool(project.get("archived")),
        owner=ao.nested_name(project, "owner"),
        team=ao.nested_name(project, "team"),
        # `current_status` carries a `title`, not a `name` -- the generic
        # nested_name helper would silently return "" for it.
        status=str((project.get("current_status") or {}).get("title") or "")
        if isinstance(project.get("current_status"), dict) else "",
        due=str(project.get("due_on") or ""),
        notes=str(project.get("notes") or ""),
        url=str(project.get("permalink_url") or ""),
        modified=str(project.get("modified_at") or ""),
    )


@chat.function(
    "list_accounts",
    "List the connected Asana accounts and whether each token still works.",
    action_type="read", chain_callable=True,
    data_model=AsanaAccount,
)
async def list_accounts(ctx, params: ListAccountsParams) -> ActionResult:
    """List connected Asana accounts and verify each token still works."""
    entries = await acct.list_accounts(ctx, refresh=params.refresh)
    if not entries:
        return _error(_NO_TOKEN_HINT, ac.ASANA_TOKEN_MISSING)

    records = []
    for entry in entries:
        workspaces = entry.get("workspaces") or []
        names = ", ".join(str(w.get("name", "")) for w in workspaces)
        status = entry.get("status") or "ok"
        records.append(AsanaAccount(
            id=str(entry.get("slot", 0)),
            title=entry.get("user_name") or f"Account {entry.get('slot', 0) + 1}",
            slot=int(entry.get("slot", 0)),
            account_name=str(entry.get("user_name") or ""),
            email=str(entry.get("email") or ""),
            workspaces=names,
            workspace_count=len(workspaces),
            status=status,
            detail=str(entry.get("error") or ""),
        ))

    working = sum(1 for r in records if r.status != "error")
    summary = f"{working} of {len(records)} Asana account(s) responding"
    return ActionResult.success(
        AsanaAccountList(items=records, total=len(records)), summary)


@chat.function(
    "list_workspaces",
    "List every Asana workspace and organization the connected tokens reach.",
    action_type="read", chain_callable=True,
    data_model=AsanaWorkspace,
)
async def list_workspaces(ctx, params: ListWorkspacesParams) -> ActionResult:
    """List all reachable workspaces across every connected account."""
    entries = await acct.list_accounts(ctx, refresh=params.refresh)
    if not entries:
        return _error(_NO_TOKEN_HINT, ac.ASANA_TOKEN_MISSING)

    rows = acct.flatten_workspaces(entries)
    if not rows:
        broken = next((e for e in entries if e.get("status") == "error"), None)
        if broken:
            return _error(
                str(broken.get("error") or ac.message_for(ac.ASANA_TOKEN_REJECTED)),
                str(broken.get("code") or ac.ASANA_TOKEN_REJECTED))
        return _error(
            "The connected token reaches no Asana workspaces. That usually "
            "means the account was removed from its workspace.",
            ac.ASANA_WORKSPACE_UNKNOWN)

    items = [
        AsanaWorkspace(
            id=row.get("gid", ""),
            title=row.get("name", "") or "(unnamed workspace)",
            gid=row.get("gid", ""),
            name=row.get("name", ""),
            is_organization=bool(row.get("is_organization")),
            account_name=row.get("account_name", ""),
        )
        for row in rows
    ]
    return ActionResult.success(
        AsanaWorkspaceList(items=items, total=len(items)),
        f"{len(items)} Asana workspace(s) reachable")


@chat.function(
    "search",
    "Search a workspace for tasks, projects, people, tags, teams or portfolios "
    "by name. Works on every Asana plan.",
    action_type="read", chain_callable=True,
    data_model=AsanaObject,
)
async def search(ctx, params: SearchParams) -> ActionResult:
    """Typeahead search inside one workspace.

    Typeahead rather than advanced search on purpose: advanced search is
    premium-only, and a connector whose basic "find X" verb fails on a free plan
    is not a connector. Typeahead is documented as fast-but-approximate and
    matches on word starts, which the summary says out loud so an incomplete
    result does not read as a bug.
    """
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    query = (params.query or "").strip()
    if not query:
        # Asana's typeahead with an empty query returns an arbitrary slice of
        # the workspace, which reads as "here are some random tasks" rather
        # than as the missing input it actually is.
        return _error(
            "Say what to search for -- a task, project or person name.",
            ac.ASANA_VALIDATION_FAILED)

    kind = (params.kind or "task").strip().lower()
    fields = {
        "task": ao.TASK_COMPACT_FIELDS,
        "project": ao.PROJECT_COMPACT_FIELDS,
        "user": ao.USER_FIELDS,
    }.get(kind, "")

    out = await acct.typeahead(ctx, token, workspace.get("gid", ""), query,
                               resource_type=kind, count=params.limit,
                               opt_fields=fields)
    if not out.get("ok"):
        return _from_envelope(out)

    results = out["results"]
    if not results:
        return ActionResult.success(
            AsanaObjectList(items=[], total=0),
            f"Nothing matching '{params.query}' in "
            f"{workspace.get('name', 'this workspace')}. Asana's quick search "
            f"matches the start of words. {ACCESS_NOTE}")

    items = [
        AsanaObject(
            id=ao.gid_of(item),
            title=ao.name_of(item) or "(unnamed)",
            gid=ao.gid_of(item),
            name=ao.name_of(item),
            object_type=str(item.get("resource_type") or kind),
            url=str(item.get("permalink_url") or ""),
        )
        for item in results
    ]
    return ActionResult.success(
        AsanaObjectList(items=items, total=len(items)),
        f"{len(items)} {kind}(s) matching '{params.query}' in "
        f"{workspace.get('name', 'workspace')}")


@chat.function(
    "search_tasks",
    "Advanced task search with filters (assignee, completion, due dates). "
    "Requires a paid Asana plan.",
    action_type="read", chain_callable=True,
    data_model=AsanaTask,
)
async def search_tasks(ctx, params: AdvancedSearchParams) -> ActionResult:
    """Asana's advanced search -- premium-only, and honest about it.

    Kept separate from `search` so the premium requirement surfaces as its own
    diagnosable code instead of making the everyday verb look broken. Asana
    answers 402 Payment Required for non-premium accounts, which the client maps
    to ASANA_PREMIUM_REQUIRED.
    """
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    query: dict = {"limit": max(1, min(100, params.limit))}
    if params.text:
        query["text"] = params.text
    if params.completed in ("yes", "no"):
        query["completed"] = "true" if params.completed == "yes" else "false"
    if params.due_before:
        query["due_on.before"] = params.due_before
    if params.due_after:
        query["due_on.after"] = params.due_after
    query["opt_fields"] = ao.TASK_FIELDS
    query["sort_by"] = "modified_at"

    if params.assignee:
        who = await shared.resolve_user(ctx, token, workspace.get("gid", ""),
                                        params.assignee)
        if not who.get("ok"):
            return _from_envelope(who)
        query["assignee.any"] = who["gid"]

    out = await ac.request(ctx, "GET",
                           f"workspaces/{workspace.get('gid', '')}/tasks/search",
                           token, params=query)
    if not out.get("ok"):
        return _from_envelope(out)

    tasks = out["data"] if isinstance(out["data"], list) else []
    items = [_task_entity(t) for t in tasks]
    if not items:
        return ActionResult.success(
            AsanaTaskList(items=[], total=0),
            "No tasks matched those filters. Note that Asana's search index "
            "lags writes by 10-60 seconds, so a task created moments ago may "
            "not appear yet.")
    return ActionResult.success(
        AsanaTaskList(items=items, total=len(items)),
        f"{len(items)} task(s) matched in {workspace.get('name', 'workspace')}")


@chat.function(
    "list_projects",
    "List projects in a workspace, optionally filtered by team.",
    action_type="read", chain_callable=True,
    data_model=AsanaProject,
)
async def list_projects(ctx, params: ListProjectsParams) -> ActionResult:
    """List projects, newest activity first where Asana provides it."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    query: dict = {"opt_fields": ao.PROJECT_COMPACT_FIELDS}
    path = f"workspaces/{workspace.get('gid', '')}/projects"

    if params.team:
        team = await acct.resolve_target(ctx, token, workspace.get("gid", ""),
                                         params.team, resource_type="team")
        if not team.get("ok"):
            return _from_envelope(team)
        path = f"teams/{team['gid']}/projects"
    if not params.archived:
        query["archived"] = "false"

    out = await ac.paginate(ctx, path, token, params=query, limit=params.limit)
    if not out.get("ok"):
        return _from_envelope(out)

    projects = out["results"]
    if not projects:
        return ActionResult.success(
            AsanaProjectList(items=[], total=0),
            f"No projects visible in {workspace.get('name', 'this workspace')}. "
            f"{ACCESS_NOTE}")

    items = [_project_entity(p) for p in projects]
    more = " (more available)" if out.get("has_more") else ""
    return ActionResult.success(
        AsanaProjectList(items=items, total=len(items)),
        f"{len(items)} project(s) in {workspace.get('name', 'workspace')}{more}")


@chat.function(
    "list_tasks",
    "List tasks in a project, or tasks assigned to someone in a workspace.",
    action_type="read", chain_callable=True,
    data_model=AsanaTask,
)
async def list_tasks(ctx, params: ListTasksParams) -> ActionResult:
    """List tasks under a narrowing filter that Asana will accept.

    THE IMPORTANT PART: `GET /tasks` is not a "list everything" endpoint. Asana
    requires one of these combinations -- `project`, or `tag`, or `assignee`
    together with `workspace`. A bare workspace answers
    `400 {"errors":[{"message":"workspace: Missing input"}]}`.

    Checking that here, before the request, is what turns an opaque platform
    error into a sentence naming the two things the user can supply.
    """
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    workspace_gid = workspace.get("gid", "")
    query: dict = {"opt_fields": ao.TASK_COMPACT_FIELDS}
    path = "tasks"
    scope = ""

    if params.project:
        project = await shared.resolve_project(ctx, token, workspace_gid,
                                               params.project)
        if not project.get("ok"):
            return _from_envelope(project)
        path = f"projects/{project['gid']}/tasks"
        scope = f"project '{project.get('name') or params.project}'"
    elif params.assignee:
        who = await shared.resolve_user(ctx, token, workspace_gid, params.assignee)
        if not who.get("ok"):
            return _from_envelope(who)
        query["assignee"] = who["gid"]
        query["workspace"] = workspace_gid
        scope = f"assignee '{who.get('name') or params.assignee}'"
    else:
        return _error(
            "Asana needs a narrower filter than a whole workspace: name a "
            "project, or an assignee (use 'me' for yourself). Listing every "
            "task in a workspace is not something the Asana API allows.",
            ac.ASANA_FILTER_REQUIRED)

    if not params.completed:
        # `completed_since=now` is Asana's documented idiom for "incomplete
        # only": it returns tasks completed since that instant, i.e. none.
        query["completed_since"] = "now"

    out = await ac.paginate(ctx, path, token, params=query, limit=params.limit)
    if not out.get("ok"):
        return _from_envelope(out)

    tasks = out["results"]
    if not tasks:
        state = "" if params.completed else "incomplete "
        return ActionResult.success(
            AsanaTaskList(items=[], total=0),
            f"No {state}tasks found for {scope}. {ACCESS_NOTE}")

    items = [_task_entity(t) for t in tasks]
    more = " (more available)" if out.get("has_more") else ""
    return ActionResult.success(
        AsanaTaskList(items=items, total=len(items)),
        f"{len(items)} task(s) for {scope}{more}")


@chat.function(
    "get_task",
    "Read one task in full: notes, assignee, dates, projects and subtasks.",
    action_type="read", chain_callable=True,
    data_model=AsanaTask,
)
async def get_task(ctx, params: GetTaskParams) -> ActionResult:
    """Read a single task by name or gid, optionally with its subtasks."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    target = await shared.resolve_task(ctx, token, workspace.get("gid", ""),
                                       params.task)
    if not target.get("ok"):
        return _from_envelope(target)

    out = await ac.request(ctx, "GET", f"tasks/{target['gid']}", token,
                           params={"opt_fields": ao.TASK_FIELDS})
    if not out.get("ok"):
        return _from_envelope(out)

    task = out["data"]
    entity = _task_entity(task)

    if params.include_subtasks and int(task.get("num_subtasks") or 0) > 0:
        subs = await ac.paginate(ctx, f"tasks/{target['gid']}/subtasks", token,
                                 params={"opt_fields": ao.TASK_COMPACT_FIELDS},
                                 limit=100, max_pages=1)
        if subs.get("ok") and subs["results"]:
            lines = [
                f"  {'[x]' if s.get('completed') else '[ ]'} {ao.name_of(s)}"
                for s in subs["results"]
            ]
            entity.summary = entity.summary + "\n\nSubtasks:\n" + "\n".join(lines)

    return ActionResult.success(
        entity, f"Read task '{entity.title}'")


@chat.function(
    "list_sections",
    "List the sections (columns) of a project.",
    action_type="read", chain_callable=True,
    data_model=AsanaSection,
)
async def list_sections(ctx, params: ListSectionsParams) -> ActionResult:
    """List a project's sections -- needed to place a task in the right column."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    project = await shared.resolve_project(ctx, token, workspace.get("gid", ""),
                                           params.project)
    if not project.get("ok"):
        return _from_envelope(project)

    out = await ac.paginate(ctx, f"projects/{project['gid']}/sections", token,
                            params={"opt_fields": ao.SECTION_FIELDS}, limit=100)
    if not out.get("ok"):
        return _from_envelope(out)

    items = [
        AsanaSection(
            id=ao.gid_of(s),
            title=ao.name_of(s) or "(unnamed section)",
            gid=ao.gid_of(s),
            name=ao.name_of(s),
            project=ao.nested_name(s, "project"),
        )
        for s in out["results"]
    ]
    return ActionResult.success(
        AsanaSectionList(items=items, total=len(items)),
        f"{len(items)} section(s) in '{project.get('name') or params.project}'")


@chat.function(
    "list_comments",
    "Read the comments and activity on a task.",
    action_type="read", chain_callable=True,
    data_model=AsanaComment,
)
async def list_comments(ctx, params: ListCommentsParams) -> ActionResult:
    """Read a task's stories.

    Asana calls these STORIES, and the list mixes user comments with system
    activity ("added to project X"). `comments_only` defaults to true because
    someone asking for comments almost never means the audit trail.
    """
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    target = await shared.resolve_task(ctx, token, workspace.get("gid", ""),
                                       params.task)
    if not target.get("ok"):
        return _from_envelope(target)

    out = await ac.paginate(ctx, f"tasks/{target['gid']}/stories", token,
                            params={"opt_fields": ao.STORY_FIELDS},
                            limit=params.limit)
    if not out.get("ok"):
        return _from_envelope(out)

    stories = out["results"]
    # The parameter is INVERTED relative to the filter: the model asks
    # `include_activity` (default false) because someone requesting comments
    # almost never means the audit trail, and a positive flag reads better in
    # chat than a negative one.
    if not params.include_activity:
        stories = [s for s in stories if ao.is_comment(s)]

    if not stories:
        kind = "activity" if params.include_activity else "comments"
        return ActionResult.success(
            AsanaCommentList(items=[], total=0),
            f"No {kind} on this task yet.")

    items = [
        AsanaComment(
            id=ao.gid_of(s),
            title=(str(s.get("text") or "")[:60] or "(empty)"),
            gid=ao.gid_of(s),
            author=ao.nested_name(s, "created_by"),
            text=str(s.get("text") or ""),
            created=str(s.get("created_at") or ""),
            is_comment=ao.is_comment(s),
        )
        for s in stories
    ]
    return ActionResult.success(
        AsanaCommentList(items=items, total=len(items)),
        f"{len(items)} entr(ies) on the task")


@chat.function(
    "list_users",
    "List the people in an Asana workspace.",
    action_type="read", chain_callable=True,
    data_model=AsanaUser,
)
async def list_users(ctx, params: ListUsersParams) -> ActionResult:
    """List workspace members -- used to name an assignee."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    out = await ac.paginate(ctx, f"workspaces/{workspace.get('gid', '')}/users",
                            token, params={"opt_fields": ao.USER_FIELDS},
                            limit=params.limit)
    if not out.get("ok"):
        return _from_envelope(out)

    items = [
        AsanaUser(
            id=ao.gid_of(u),
            title=ao.name_of(u) or "(unnamed)",
            gid=ao.gid_of(u),
            name=ao.name_of(u),
            email=str(u.get("email") or ""),
        )
        for u in out["results"]
    ]
    return ActionResult.success(
        AsanaUserList(items=items, total=len(items)),
        f"{len(items)} person/people in {workspace.get('name', 'workspace')}")


@chat.function(
    "list_teams",
    "List the teams in an Asana organization.",
    action_type="read", chain_callable=True,
    data_model=AsanaTeam,
)
async def list_teams(ctx, params: ListTeamsParams) -> ActionResult:
    """List teams.

    Teams exist only in ORGANIZATIONS, not in plain workspaces, so a workspace
    that is not an organization gets told that instead of an empty list.
    """
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    if not workspace.get("is_organization"):
        return _error(
            f"'{workspace.get('name', 'This workspace')}' is a workspace, not an "
            "organization -- teams only exist in Asana organizations.",
            ac.ASANA_VALIDATION_FAILED)

    out = await ac.paginate(
        ctx, f"organizations/{workspace.get('gid', '')}/teams", token,
        params={"opt_fields": "gid,name,description"}, limit=params.limit)
    if not out.get("ok"):
        return _from_envelope(out)

    items = [
        AsanaTeam(
            id=ao.gid_of(t),
            title=ao.name_of(t) or "(unnamed team)",
            gid=ao.gid_of(t),
            name=ao.name_of(t),
            description=str(t.get("description") or ""),
        )
        for t in out["results"]
    ]
    return ActionResult.success(
        AsanaTeamList(items=items, total=len(items)),
        f"{len(items)} team(s) in {workspace.get('name', 'organization')}")


@chat.function(
    "check_access",
    "Report what this connector can currently reach in Asana, and explain "
    "anything missing.",
    action_type="read", chain_callable=True,
    data_model=AccessReport,
)
async def check_access(ctx, params: CheckAccessParams) -> ActionResult:
    """Explain reachability.

    The Notion connector needed this because Notion hides anything unshared.
    Asana's model is different -- membership, not sharing -- so this reports the
    real reason a result looked empty: which account the token belongs to, which
    workspaces it reaches, and whether advanced search is even available on that
    plan. Answering "why can't you see my task" without a round of questions is
    the whole point.
    """
    entries = await acct.list_accounts(ctx, refresh=True)
    if not entries:
        return _error(_NO_TOKEN_HINT, ac.ASANA_TOKEN_MISSING)

    broken = [e for e in entries if e.get("status") == "error"]
    usable = [e for e in entries if e.get("status") != "error"]
    if not usable:
        first = broken[0]
        return _error(
            str(first.get("error") or ac.message_for(ac.ASANA_TOKEN_REJECTED)),
            str(first.get("code") or ac.ASANA_TOKEN_REJECTED))

    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    workspace_gid = workspace.get("gid", "")
    projects = await ac.paginate(ctx, f"workspaces/{workspace_gid}/projects",
                                 token, params={"opt_fields": "gid,name",
                                                "archived": "false"},
                                 limit=100, max_pages=2)
    project_count = len(projects.get("results", [])) if projects.get("ok") else 0

    people = await ac.paginate(ctx, f"workspaces/{workspace_gid}/users", token,
                               params={"opt_fields": "gid"}, limit=100,
                               max_pages=1)
    people_count = len(people.get("results", [])) if people.get("ok") else 0

    # One cheap probe answers "is advanced search available", which decides
    # whether search_tasks is worth suggesting to this user at all.
    probe = await ac.request(ctx, "GET", f"workspaces/{workspace_gid}/tasks/search",
                             token, params={"limit": 1, "opt_fields": "gid"})
    premium = bool(probe.get("ok"))
    premium_note = (
        "Advanced task search is available on this plan."
        if premium else
        "Advanced task search is not available on this plan (Asana restricts it "
        "to paid plans), so quick name search is used instead."
    )

    account = usable[0]
    report = AccessReport(
        id=workspace_gid,
        title=f"Access in {workspace.get('name', 'Asana')}",
        account_name=str(account.get("user_name") or ""),
        # These names must match AccessReport EXACTLY. pydantic drops unknown
        # fields without a word, so the previous names (workspace_name,
        # projects_visible, people_visible, explanation) produced an empty
        # report -- and `premium_search` took a bool where the model declares a
        # string, which is the one mismatch loud enough to raise.
        workspace=str(workspace.get("name") or ""),
        workspace_count=len(acct.flatten_workspaces(entries)),
        project_count=project_count,
        # The names are already fetched above; leaving this empty made the
        # report say "1 project" without saying WHICH -- the one question
        # "what can you actually see?" exists to answer.
        reachable_projects=", ".join(
            ao.name_of(p) for p in (projects.get("results") or [])[:12]
            if ao.name_of(p)),
        user_count=people_count,
        premium_search=premium_note,
        note=ACCESS_NOTE,
    )
    summary = (
        f"Token for {report.account_name or 'this account'} reaches "
        f"{report.workspace_count} workspace(s); in "
        f"{report.workspace or 'the selected workspace'}: "
        f"{project_count} project(s), {people_count} person/people"
    )
    if broken:
        summary += f". {len(broken)} configured token(s) are not working"
    return ActionResult.success(report, summary)
