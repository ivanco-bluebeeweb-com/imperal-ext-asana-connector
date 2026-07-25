"""Shaping Asana payloads: field selection, names, dates, task rendering.

WHY `opt_fields` IS EVERYWHERE. Asana returns "compact" objects by default --
usually just `gid`, `name` and `resource_type`. Anything else (assignee, due
date, notes, completion) has to be asked for BY NAME via the `opt_fields` query
parameter. Two consequences shape this module:

* a missing field is usually a missing REQUEST, not missing data, so the field
  lists live here as named constants instead of being inlined per call site;
* `opt_fields` is validated by Asana -- asking for a field that does not exist
  on that resource is a 400 -- so these lists are conservative and only name
  documented fields.

Dates have two forms and they are not interchangeable: `due_on` is a date
(YYYY-MM-DD) and `due_at` is a timestamp. Asana rejects a request that sets
both, which is why `due_field_for` picks exactly one.
"""

from __future__ import annotations

# Field sets, comma-joined into opt_fields. Ordered for readable requests.
TASK_FIELDS = ",".join([
    "gid", "name", "resource_type", "completed", "completed_at", "due_on",
    "due_at", "start_on", "notes", "assignee.name", "assignee.gid",
    "projects.name", "projects.gid", "parent.name", "parent.gid",
    "permalink_url", "created_at", "modified_at", "num_subtasks", "tags.name",
    # Writable since the dependency/follower tools landed, so they have to be
    # readable too -- otherwise a write cannot be verified by a read.
    "dependencies.name", "dependencies.gid", "dependents.name",
    "followers.name",
    # Where real workspaces keep priority, status, effort and budget.
    # `display_value` is Asana's own rendering of any field type, so the
    # connector does not need six parsers to read six value shapes.
    "custom_fields.name", "custom_fields.display_value",
])

TASK_COMPACT_FIELDS = ",".join([
    "gid", "name", "completed", "due_on", "assignee.name", "permalink_url",
    "modified_at",
])

PROJECT_FIELDS = ",".join([
    "gid", "name", "resource_type", "archived", "color", "notes", "public",
    "current_status.title", "due_on", "start_on", "owner.name",
    "team.name", "team.gid", "workspace.name", "workspace.gid",
    "permalink_url", "created_at", "modified_at",
])

PROJECT_COMPACT_FIELDS = ",".join([
    "gid", "name", "archived", "owner.name", "permalink_url", "modified_at",
])

SECTION_FIELDS = "gid,name,project.name,project.gid,created_at"

STORY_FIELDS = ",".join([
    "gid", "text", "created_at", "created_by.name", "type", "resource_subtype",
])

# `download_url` is deliberately NOT requested. Asana issues it as a short-
# lived signed link that expires within minutes, so storing or showing one
# hands the user a URL that is already dead by the time they click it.
# `permanent_url` opens the attachment in Asana and keeps working.
ATTACHMENT_FIELDS = ",".join([
    "gid", "name", "resource_type", "created_at", "permanent_url", "host",
    "size",
])

USER_FIELDS = "gid,name,email,resource_type"

WORKSPACE_FIELDS = "gid,name,resource_type,is_organization"


def name_of(item) -> str:
    """The human name of any Asana object, or '' when absent."""
    if not isinstance(item, dict):
        return ""
    return str(item.get("name") or "")


def gid_of(item) -> str:
    """The gid of any Asana object as a string.

    Asana gids are numeric-looking STRINGS and are documented as opaque. They
    are never coerced to int here: some are large enough to be lossy, and
    string round-tripping is what the API expects back.
    """
    if not isinstance(item, dict):
        return ""
    return str(item.get("gid") or "")


def nested_name(item, key: str) -> str:
    """Name of a nested object (e.g. assignee, workspace), or ''."""
    if not isinstance(item, dict):
        return ""
    nested = item.get(key)
    if isinstance(nested, dict):
        return str(nested.get("name") or "")
    return ""


def nested_gid(item, key: str) -> str:
    """Gid of a nested object, or ''."""
    if not isinstance(item, dict):
        return ""
    nested = item.get(key)
    if isinstance(nested, dict):
        return str(nested.get("gid") or "")
    return ""


def name_list(item, key: str) -> list[str]:
    """Names from a list-valued field such as `projects` or `tags`."""
    if not isinstance(item, dict):
        return []
    values = item.get(key)
    if not isinstance(values, list):
        return []
    return [str(v.get("name") or "") for v in values
            if isinstance(v, dict) and v.get("name")]


def custom_field_pairs(task) -> list[tuple[str, str]]:
    """Custom fields as (label, value) using Asana's own rendering.

    Custom fields are where real workspaces keep priority, status, effort and
    budget -- so a task read WITHOUT them is a task read without half its
    meaning. There are six value types (enum, multi-enum, text, number, date,
    people) and hand-parsing each one would be six chances to be subtly wrong.

    Asana already solves this: `display_value` is the API's own human-readable
    string for ANY type, and the docs recommend it for exactly this case. A
    field with no value set is skipped rather than shown as empty noise.
    """
    if not isinstance(task, dict):
        return []
    values = task.get("custom_fields")
    if not isinstance(values, list):
        return []
    pairs = []
    for field in values:
        if not isinstance(field, dict):
            continue
        label = str(field.get("name") or "").strip()
        shown = field.get("display_value")
        shown = "" if shown is None else str(shown).strip()
        if label and shown:
            pairs.append((label, shown))
    return pairs


def human_size(value) -> str:
    """Byte count -> '2.4 MB'.

    Asana reports size in bytes. '15728640' tells a person nothing about
    whether a file is safe to send to a client on a phone; '15 MB' does.
    """
    try:
        size = float(value)
    except (TypeError, ValueError):
        return ""
    if size < 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            shown = f"{size:.0f}" if unit == "B" or size >= 10 else f"{size:.1f}"
            return f"{shown} {unit}"
        size /= 1024
    return ""


def render_attachment(item) -> str:
    """Human-readable one-line summary of an attachment."""
    if not isinstance(item, dict):
        return ""
    parts = [name_of(item) or "(unnamed file)"]
    size = human_size(item.get("size"))
    if size:
        parts.append(size)
    # Where the file LIVES, not just that it is attached: a Drive link and an
    # uploaded file look identical in a list and behave nothing alike.
    host = str(item.get("host") or "").strip()
    if host and host != "asana":
        parts.append(f"stored in {host}")
    created = str(item.get("created_at") or "")[:10]
    if created:
        parts.append(f"added {created}")
    return " | ".join(parts)


def gid_list(item, key: str) -> list[str]:
    """Gids from a list-valued field.

    The companion to `name_list`, and it exists because of a real gap: Asana
    returns `dependencies` and `dependents` as COMPACT resources -- gid only,
    no name, even when `dependencies.name` is requested in opt_fields. So
    `name_list` correctly found nothing and the field rendered empty while the
    links plainly existed. The names have to be fetched separately, and this
    is what says which gids to fetch.
    """
    if not isinstance(item, dict):
        return []
    values = item.get(key)
    if not isinstance(values, list):
        return []
    return [gid_of(v) for v in values if isinstance(v, dict) and gid_of(v)]


def looks_like_gid(value: str) -> bool:
    """True when the string is plausibly an Asana gid.

    Guard, not validation: it decides whether to skip the name lookup, so it is
    deliberately strict about shape (all digits, long enough not to collide with
    a task literally named "42") and never invents a gid.
    """
    raw = (value or "").strip()
    return raw.isdigit() and len(raw) >= 6


def due_field_for(due: str) -> tuple[str, str]:
    """Choose between `due_on` and `due_at` for a user-supplied due value.

    Asana rejects a request that sets both, and the two mean different things:
    a plain date is a day, a timestamp is a moment. Anything carrying a time
    component ('T' or a ':') goes to `due_at`; a bare date goes to `due_on`.
    """
    raw = (due or "").strip()
    if not raw:
        return "", ""
    if "T" in raw or ":" in raw:
        return "due_at", raw
    return "due_on", raw


def render_task(task: dict) -> str:
    """Human-readable one-block summary of a task.

    Exists because a chat answer about a task should read like a sentence, not
    like JSON: the caller gets structured fields on the entity AND this for
    display.
    """
    if not isinstance(task, dict):
        return ""
    parts: list[str] = []
    status = "done" if task.get("completed") else "open"
    parts.append(f"{name_of(task)} [{status}]")

    assignee = nested_name(task, "assignee")
    if assignee:
        parts.append(f"assignee: {assignee}")

    due = task.get("due_on") or task.get("due_at")
    if due:
        parts.append(f"due: {due}")

    projects = name_list(task, "projects")
    if projects:
        parts.append("in: " + ", ".join(projects))

    tags = name_list(task, "tags")
    if tags:
        parts.append("tags: " + ", ".join(tags))

    for label, shown in custom_field_pairs(task):
        parts.append(f"{label}: {shown}")

    subtasks = task.get("num_subtasks")
    if isinstance(subtasks, int) and subtasks > 0:
        parts.append(f"subtasks: {subtasks}")

    notes = str(task.get("notes") or "").strip()
    summary = " | ".join(parts)
    if notes:
        # Notes can be long; the entity carries the full text, this is a peek.
        clipped = notes if len(notes) <= 400 else notes[:400] + "..."
        summary = f"{summary}\n{clipped}"
    return summary


def render_stories(stories: list) -> str:
    """Render comment stories oldest-first as readable lines.

    Asana's `stories` feed mixes real comments with SYSTEM activity ("marked
    this complete"). Only `comment_added` is a comment a human wrote, so the
    caller filters; this only formats.
    """
    lines: list[str] = []
    for story in stories:
        if not isinstance(story, dict):
            continue
        author = nested_name(story, "created_by") or "Someone"
        when = str(story.get("created_at") or "")[:10]
        text = str(story.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"{author} ({when}): {text}")
    return "\n".join(lines)


def is_comment(story) -> bool:
    """True for a human comment, false for system activity."""
    if not isinstance(story, dict):
        return False
    return str(story.get("resource_subtype") or "") == "comment_added"
