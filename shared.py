"""Helpers shared by the read and write tool layers.

These deliberately do NOT live in `handlers_read.py`: putting them there would
make `handlers_write.py` import PRIVATE names from a sibling layer -- a
dependency that says "write is built on read" when the two are really peers.
That mistake was made once in the Notion connector and had to be undone; both
layers depend on this module instead.
"""

from __future__ import annotations

from imperal_sdk import ActionResult

import asana_client as ac
import accounts as acct


# The one sentence that explains Asana's access model. Reused verbatim wherever
# emptiness might otherwise read as a bug: unlike Notion, Asana does not require
# per-object sharing, so an empty result means membership or filters -- not a
# missing "share" step.
ACCESS_NOTE = (
    "Asana shows whatever the account behind the token can already see -- there "
    "is no per-page sharing step. An empty result usually means the account is "
    "not a member of that project or team, or the task is in another workspace."
)


def error(message: str, code: str, retryable: bool = False) -> ActionResult:
    """Error result carrying a structured code.

    `code` is mandatory on purpose. The kernel stamps EXT_UNSTRUCTURED_ERROR on
    any error emitted without one (I-EXT-ERROR-CODE-NORMALIZED), which turns a
    precise failure into un-actionable prose -- exactly the bug that made WP
    Publisher's failures unreadable. Validator rule V32 only flags literal
    `ActionResult.error(` call sites, so routing every error through a helper
    would hide this app from the rule; hence the positional argument, which
    makes a code-less error a TypeError at authoring time rather than a silent
    downgrade in production.
    """
    return ActionResult.error(message, retryable, code=code)


def from_envelope(out: dict) -> ActionResult:
    """Convert an asana_client error envelope into an ActionResult."""
    return error(out.get("error") or ac.message_for(out.get("code", "")),
                 out.get("code") or ac.ASANA_HTTP_ERROR,
                 bool(out.get("retryable")))


async def resolve(ctx, workspace: str) -> tuple[str, dict, ActionResult | None]:
    """Resolve the token and workspace, or hand back a ready-made error.

    Returns (token, workspace_row, None) on success, or ("", {}, ActionResult)
    when the caller should return that error unchanged.
    """
    picked = await acct.resolve_workspace(ctx, workspace)
    if not picked.get("ok"):
        return "", {}, from_envelope(picked)
    return picked["token"], picked.get("workspace", {}), None


async def resolve_task(ctx, token: str, workspace_gid: str,
                       reference: str) -> dict:
    """Resolve a task reference to a gid, or return an error envelope."""
    return await acct.resolve_target(ctx, token, workspace_gid, reference,
                                     resource_type="task")


async def resolve_project(ctx, token: str, workspace_gid: str,
                          reference: str) -> dict:
    """Resolve a project reference to a gid, or return an error envelope."""
    return await acct.resolve_target(ctx, token, workspace_gid, reference,
                                     resource_type="project")


async def resolve_user(ctx, token: str, workspace_gid: str,
                       reference: str) -> dict:
    """Resolve an assignee reference to a gid.

    'me' is special-cased because it is the single most common assignee a user
    names, and Asana accepts the literal string `me` for it -- so this avoids a
    typeahead round-trip AND avoids matching a colleague who happens to be
    named Me.
    """
    ref = (reference or "").strip()
    if ref.lower() == "me":
        return {"ok": True, "gid": "me", "name": "me", "resolved_by": "literal"}
    return await acct.resolve_target(ctx, token, workspace_gid, ref,
                                     resource_type="user")
