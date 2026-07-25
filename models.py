"""Pydantic parameter models and SDL return entities.

Every parameter that names an Asana object accepts a NAME, not just a gid: the
user says "the Website Redesign project", not a 17-digit number. Gids still work
-- pasting one out of an Asana URL must keep working -- but nothing here ever
requires the user to go find one.

`WorkspaceScoped` is the base for almost every tool. It is optional in practice:
when the token reaches exactly one workspace, omitting it resolves to that one.
"""

from pydantic import BaseModel, Field
from imperal_sdk import sdl


# --------------------------- parameters ---------------------------

class WorkspaceScoped(BaseModel):
    """Base for every tool: which Asana workspace to act in."""
    workspace: str = Field(
        "", description="Workspace or organization name, e.g. 'Acme'. Omit when "
                        "the account reaches only one workspace.")


class ListAccountsParams(BaseModel):
    refresh: bool = Field(
        False, description="Re-read account and workspace details from Asana "
                           "instead of the cache")


class ConnectAccountParams(BaseModel):
    """The token the user pastes on the Connect screen.

    Not WorkspaceScoped: this is the one action that runs BEFORE any workspace
    is known, so asking which workspace to act in would be circular. The
    workspaces are discovered FROM the token.
    """
    token: str = Field(
        "", description="Asana personal access token, starts with '2/'. Create "
                        "one at app.asana.com/0/my-apps.")


class ConnectResult(sdl.Entity):
    """Outcome of connecting a token -- what got connected, and what is next."""
    account_name: str = ""
    email: str = ""
    already_connected: bool = False
    workspace_count: int = 0
    workspaces: str = ""
    next_step: str = ""


class SearchParams(WorkspaceScoped):
    query: str = Field(
        "", description="Text to match against names. Asana matches on the "
                        "start of words, not arbitrary substrings.")
    kind: str = Field(
        "task", description="What to search for: task, project, user, tag, "
                            "portfolio or team.")
    limit: int = Field(
        20, ge=1, le=100, description="Maximum results to return")


class AdvancedSearchParams(WorkspaceScoped):
    """Premium-only filtered task search.

    Separate from `search` on purpose: this endpoint answers 402 Payment
    Required on non-premium plans, so folding it into the default search verb
    would break the connector's most basic action for free-plan users.
    """
    text: str = Field("", description="Free text to match in task names and notes")
    assignee: str = Field("", description="Assignee name, or 'me'")
    project: str = Field("", description="Restrict to one project (name or gid)")
    completed: str = Field(
        "", description="Filter by completion: 'true', 'false', or empty for both")
    due_before: str = Field("", description="Only tasks due before this date (YYYY-MM-DD)")
    due_after: str = Field("", description="Only tasks due after this date (YYYY-MM-DD)")
    limit: int = Field(50, ge=1, le=100, description="Maximum tasks to return")


class ListWorkspacesParams(BaseModel):
    refresh: bool = Field(
        False, description="Re-read workspaces from Asana instead of the cache")


class ListProjectsParams(WorkspaceScoped):
    team: str = Field("", description="Restrict to one team (name or gid)")
    archived: bool = Field(
        False, description="Include archived projects (excluded by default)")
    limit: int = Field(50, ge=1, le=100, description="Maximum projects to return")


class ListTasksParams(WorkspaceScoped):
    """Asana REFUSES to list tasks without a narrowing filter.

    `GET /tasks` requires project, or tag, or assignee+workspace -- a bare call
    is a 400. So these fields are not merely conveniences: at least one of
    project/assignee/section must be given, and the tool says so up front rather
    than forwarding a request Asana will reject.
    """
    project: str = Field("", description="Project name or gid to list tasks from")
    section: str = Field("", description="Section name or gid within the project")
    assignee: str = Field("", description="Assignee name, or 'me'")
    completed: bool = Field(
        False, description="Include completed tasks (open tasks only by default)")
    limit: int = Field(50, ge=1, le=100, description="Maximum tasks to return")


class GetTaskParams(WorkspaceScoped):
    task: str = Field(..., description="Task name or gid")
    include_subtasks: bool = Field(
        True, description="Also list the task's subtasks")


class ListSectionsParams(WorkspaceScoped):
    project: str = Field(..., description="Project name or gid")


class ListCommentsParams(WorkspaceScoped):
    task: str = Field(..., description="Task name or gid whose comments to read")
    limit: int = Field(50, ge=1, le=100, description="Maximum comments to return")
    include_activity: bool = Field(
        False, description="Also include system activity, not just human comments")


class ListAttachmentsParams(WorkspaceScoped):
    task: str = Field(..., description="Task name or gid whose files to list")
    limit: int = Field(50, ge=1, le=100,
                       description="Maximum attachments to return")


class ListUsersParams(WorkspaceScoped):
    query: str = Field("", description="Filter users by name fragment")
    limit: int = Field(50, ge=1, le=100, description="Maximum users to return")


class ListTeamsParams(WorkspaceScoped):
    limit: int = Field(50, ge=1, le=100, description="Maximum teams to return")


class CheckAccessParams(WorkspaceScoped):
    pass


# --------------------------- write parameters ---------------------------

class CreateTaskParams(WorkspaceScoped):
    name: str = Field(..., description="Task title")
    notes: str = Field("", description="Task description as plain text")
    project: str = Field(
        "", description="Project to put the task in (name or gid). Asana "
                        "requires either a project or an assignee.")
    section: str = Field(
        "", description="Section within the project to place the task in")
    assignee: str = Field("", description="Who to assign it to (name, or 'me')")
    due: str = Field(
        "", description="Due date 'YYYY-MM-DD', or a timestamp for a due time")
    start: str = Field(
        "", description="Start date 'YYYY-MM-DD'. Asana requires a due date "
                        "alongside it -- a task cannot start without ending.")
    parent: str = Field(
        "", description="Make this a subtask of the given task (name or gid)")


class UpdateTaskParams(WorkspaceScoped):
    task: str = Field(..., description="Task to update (name or gid)")
    name: str = Field("", description="New title (omit to keep the current one)")
    notes: str = Field("", description="Replace the description")
    assignee: str = Field("", description="Reassign to this person, or 'me'")
    due: str = Field("", description="New due date 'YYYY-MM-DD' or timestamp")
    start: str = Field(
        "", description="New start date 'YYYY-MM-DD'. The task must also have "
                        "a due date, existing or set in the same call.")
    clear_due: bool = Field(False, description="Remove the due date entirely")
    clear_start: bool = Field(False, description="Remove the start date")
    clear_assignee: bool = Field(False, description="Unassign the task")


class CompleteTaskParams(WorkspaceScoped):
    task: str = Field(..., description="Task to complete (name or gid)")
    completed: bool = Field(
        True, description="Set false to reopen a completed task")


class MoveTaskParams(WorkspaceScoped):
    task: str = Field(..., description="Task to move (name or gid)")
    project: str = Field(..., description="Destination project (name or gid)")
    section: str = Field("", description="Destination section within that project")
    remove_from_others: bool = Field(
        False, description="Remove the task from its other projects")


class DeleteTaskParams(WorkspaceScoped):
    task: str = Field(..., description="Task to delete (name or gid)")


class AddCommentParams(WorkspaceScoped):
    task: str = Field(..., description="Task to comment on (name or gid)")
    comment: str = Field(..., description="Comment text")


class CreateProjectParams(WorkspaceScoped):
    name: str = Field(..., description="Project name")
    notes: str = Field("", description="Project description")
    team: str = Field(
        "", description="Team to own the project (name or gid). Required in an "
                        "organization; ignored in a personal workspace.")
    public: bool = Field(
        True, description="Visible to the whole team (set false for private)")


class CreateSectionParams(WorkspaceScoped):
    project: str = Field(..., description="Project to add the section to")
    name: str = Field(..., description="Section name")


class TaskDependencyParams(WorkspaceScoped):
    """Order between two tasks.

    Sequence was previously expressible only in PROSE -- a comment saying "do
    this one first" -- which no timeline, no sorting and no automation can act
    on. Asana models it as dependencies, so this does too.
    """
    task: str = Field(..., description="The task that waits (name or gid)")
    depends_on: str = Field(
        ..., description="The task that must finish first (name or gid). "
                         "Separate several with commas.")
    remove: bool = Field(
        False, description="Set true to REMOVE the dependency instead of "
                           "adding it")


class TaskFollowersParams(WorkspaceScoped):
    """Who gets notified about a task.

    Assignee answers "who does it"; followers answer "who needs to know" --
    the client, the architect, the site manager. Without this a task could be
    created but nobody could be kept in the loop on it.
    """
    task: str = Field(..., description="Task to change (name or gid)")
    people: str = Field(
        ..., description="People to add or remove, by name or email. "
                         "Separate several with commas. 'me' works.")
    remove: bool = Field(
        False, description="Set true to REMOVE these followers instead of "
                           "adding them")


class TaskTagsParams(WorkspaceScoped):
    """Tags on a task.

    Tags were readable but not writable, so a connector could show how work is
    categorised and never categorise any.
    """
    task: str = Field(..., description="Task to change (name or gid)")
    tags: str = Field(
        ..., description="Tag names, comma-separated. A tag that does not "
                         "exist yet is created in the workspace.")
    remove: bool = Field(
        False, description="Set true to REMOVE these tags instead of adding "
                           "them")


# --------------------------- return entities ---------------------------

class AsanaAccount(sdl.Entity):
    """One connected Asana account (one personal access token)."""
    slot: int = 0
    account_name: str = ""
    email: str = ""
    workspaces: str = ""
    workspace_count: int = 0
    status: str = ""
    detail: str = ""


class AsanaAccountList(sdl.EntityList[AsanaAccount]):
    pass


class AsanaWorkspace(sdl.Entity):
    """One workspace or organization reachable by a connected token."""
    gid: str = ""
    name: str = ""
    is_organization: bool = False
    account_name: str = ""


class AsanaWorkspaceList(sdl.EntityList[AsanaWorkspace]):
    pass


class AsanaTask(sdl.Entity):
    """One Asana task, flattened for display."""
    gid: str = ""
    name: str = ""
    completed: bool = False
    assignee: str = ""
    due: str = ""
    # `start` and `parent` are requested in TASK_FIELDS and were being passed
    # by the handler, but the model never declared them -- pydantic DROPS
    # unknown keyword fields silently, so every task rendered without its start
    # date and without saying it was a subtask. No error, just missing facts.
    start: str = ""
    parent: str = ""
    # Dependencies and followers became WRITABLE before they were readable,
    # which meant the connector could change them and then not show what it
    # had done -- the effect of a write was invisible to the tool that made it.
    blocked_by: str = ""
    blocking: str = ""
    followers: str = ""
    # Priority, status, effort, budget -- whatever this workspace tracks.
    custom_fields: str = ""
    projects: str = ""
    tags: str = ""
    notes: str = ""
    subtask_count: int = 0
    url: str = ""
    modified: str = ""
    summary: str = ""


class AsanaTaskList(sdl.EntityList[AsanaTask]):
    pass


class AsanaProject(sdl.Entity):
    """One Asana project."""
    gid: str = ""
    name: str = ""
    archived: bool = False
    owner: str = ""
    team: str = ""
    status: str = ""
    due: str = ""
    notes: str = ""
    url: str = ""
    modified: str = ""


class AsanaProjectList(sdl.EntityList[AsanaProject]):
    pass


class AsanaSection(sdl.Entity):
    """One section inside a project."""
    gid: str = ""
    name: str = ""
    project: str = ""


class AsanaSectionList(sdl.EntityList[AsanaSection]):
    pass


class AsanaComment(sdl.Entity):
    """One comment (or activity entry) on a task."""
    gid: str = ""
    author: str = ""
    text: str = ""
    created: str = ""
    is_comment: bool = True


class AsanaCommentList(sdl.EntityList[AsanaComment]):
    pass


class AsanaAttachment(sdl.Entity):
    """One file attached to a task.

    Attachments carry the actual deliverable -- the drawing, the quote, the
    photo of the wall. A task read without them looks like a task with no
    evidence attached to it.
    """
    gid: str = ""
    name: str = ""
    created: str = ""
    host: str = ""
    size: str = ""
    url: str = ""
    # Declared because the handler fills it. The structural test caught this
    # missing on the first run -- pydantic drops an undeclared field silently,
    # which is the exact bug class that cost this codebase three earlier fixes.
    summary: str = ""


class AsanaAttachmentList(sdl.EntityList[AsanaAttachment]):
    pass


class AsanaUser(sdl.Entity):
    """One person in a workspace."""
    gid: str = ""
    name: str = ""
    email: str = ""


class AsanaUserList(sdl.EntityList[AsanaUser]):
    pass


class AsanaTeam(sdl.Entity):
    """One team inside an organization."""
    gid: str = ""
    name: str = ""
    description: str = ""


class AsanaTeamList(sdl.EntityList[AsanaTeam]):
    pass


class AsanaObject(sdl.Entity):
    """A generic search hit -- task, project, user, tag, portfolio or team."""
    gid: str = ""
    name: str = ""
    object_type: str = ""
    url: str = ""


class AsanaObjectList(sdl.EntityList[AsanaObject]):
    pass


class AccessReport(sdl.Entity):
    """What the connector can currently reach, and why anything is missing."""
    account_name: str = ""
    workspace: str = ""
    workspace_count: int = 0
    project_count: int = 0
    reachable_projects: str = ""
    user_count: int = 0
    premium_search: str = ""
    note: str = ""


class WriteResult(sdl.Entity):
    """Outcome of a write, phrased so the narrator can state what changed."""
    gid: str = ""
    name: str = ""
    action: str = ""
    detail: str = ""
    url: str = ""
