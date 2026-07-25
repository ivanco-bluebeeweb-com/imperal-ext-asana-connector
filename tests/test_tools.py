"""Tool behaviour: the promises the user actually experiences.

Three of these tests exist because of a specific Asana constraint that would
otherwise reach the user as a raw platform error:

* `GET /tasks` refuses a bare workspace -- it needs project, or tag, or
  assignee+workspace. We check BEFORE the request and name the fix.
* Advanced search is premium-only (402), so the default `search` uses typeahead
  and works on every plan.
* Writes are PUT. A PATCH is a 404 that reads like "task not found".
"""

import asana_client as ac
import handlers_read as hr
import handlers_write as hw
from conftest import (envelope, error_payload, me_payload, project_payload,
                      story_payload, task_payload)
from models import (AddCommentParams, CompleteTaskParams, ConnectAccountParams,
                    CreateTaskParams, DeleteTaskParams, GetTaskParams,
                    ListCommentsParams, ListTasksParams, SearchParams,
                    UpdateTaskParams)


def _ok(result) -> bool:
    """Read the REAL contract: ActionResult carries `status`, not `success`.

    `getattr(result, "success", False)` looks right and is always truthy --
    ActionResult.success is a classmethod, so every assertion passed regardless
    of the outcome. A green suite that cannot fail is worse than no suite.
    """
    return result.status == "success"


def _code(result) -> str:
    return result.error_code or ""


def _text(result) -> str:
    """Success prose lives in `summary`, failure prose in `error`."""
    return str((result.summary if result.status == "success" else result.error) or "")


# --- the mandatory-filter guard ---------------------------------------------

async def test_list_tasks_without_a_filter_explains_instead_of_failing(connected_ctx, http):
    """Asana answers a bare workspace with 400 "workspace: Missing input".

    Forwarding that would blame a field the user never typed. We stop first and
    name the two things that DO work.
    """
    http.push(envelope(me_payload()))
    out = await hr.list_tasks(connected_ctx, ListTasksParams())
    assert not _ok(out)
    assert _code(out) == ac.ASANA_FILTER_REQUIRED
    assert "project" in _text(out).lower()
    assert "assignee" in _text(out).lower()
    # No task request was attempted -- only the workspace lookup.
    assert not any("/tasks" in c["url"] for c in http.calls)


async def test_list_tasks_by_project_uses_the_project_route(connected_ctx, http):
    http.push(envelope(me_payload()))
    http.push(envelope([project_payload(gid="500", name="Website")]))
    http.push(envelope([task_payload()]))
    out = await hr.list_tasks(connected_ctx, ListTasksParams(project="Website"))
    assert _ok(out)
    assert any("projects/500/tasks" in c["url"] for c in http.calls)


async def test_list_tasks_by_assignee_sends_assignee_and_workspace(connected_ctx, http):
    """Asana requires the PAIR: assignee alone is a 400."""
    http.push(envelope(me_payload(workspaces=[{"gid": "100", "name": "Acme"}])))
    http.push(envelope([task_payload()]))
    out = await hr.list_tasks(connected_ctx, ListTasksParams(assignee="me"))
    assert _ok(out)
    params = http.calls[-1]["params"]
    assert params["assignee"] == "me"
    assert params["workspace"] == "100"


# --- search -----------------------------------------------------------------

async def test_search_uses_typeahead_so_it_works_on_a_free_plan(connected_ctx, http):
    http.push(envelope(me_payload()))
    http.push(envelope([task_payload()]))
    out = await hr.search(connected_ctx, SearchParams(query="landing"))
    assert _ok(out)
    assert any("typeahead" in c["url"] for c in http.calls)


async def test_advanced_search_reports_premium_honestly(connected_ctx, http):
    """402 is not a bug and not the user's mistake -- it is a plan limit.

    Saying "search failed" would send them debugging their query forever.
    """
    from models import AdvancedSearchParams
    http.push(envelope(me_payload()))
    http.push(error_payload("payment required"), status=402)
    out = await hr.search_tasks(connected_ctx, AdvancedSearchParams(text="x"))
    assert not _ok(out)
    assert _code(out) == ac.ASANA_PREMIUM_REQUIRED


async def test_an_empty_search_query_is_refused_before_a_request(connected_ctx, http):
    http.push(envelope(me_payload()))
    out = await hr.search(connected_ctx, SearchParams(query="  "))
    assert not _ok(out)
    assert _code(out) == ac.ASANA_VALIDATION_FAILED
    assert not any("typeahead" in c["url"] for c in http.calls)


# --- reading ----------------------------------------------------------------

async def test_get_task_returns_readable_content_not_json(connected_ctx, http):
    http.push(envelope(me_payload()))
    http.push(envelope([task_payload(gid="1201")]))
    http.push(envelope(task_payload(gid="1201", notes="Copy is ready.")))
    out = await hr.get_task(connected_ctx, GetTaskParams(task="Ship the landing page"))
    assert _ok(out)
    assert "Ship the landing page" in _text(out)


async def test_list_comments_hides_system_activity_by_default(connected_ctx, http):
    """The stories feed mixes human comments with "marked this complete"."""
    http.push(envelope(me_payload()))
    # `task` is a GID here, so resolve_target short-circuits and there is NO
    # typeahead round trip -- queueing one would shift every later response.
    http.push(envelope([
        story_payload(text="Looks good", is_comment=True),
        story_payload(text="marked this complete", is_comment=False),
    ]))
    out = await hr.list_comments(connected_ctx, ListCommentsParams(task="1201234567"))
    assert _ok(out)
    assert "Looks good" in _text(out) or "1" in _text(out)
    assert "marked this complete" not in _text(out)


# --- writing ----------------------------------------------------------------

async def test_create_task_wraps_the_body_and_names_the_workspace(connected_ctx, http):
    http.push(envelope(me_payload(workspaces=[{"gid": "100", "name": "Acme"}])))
    http.push(envelope(task_payload(gid="1300", name="Write the brief")))
    out = await hw.create_task(connected_ctx, CreateTaskParams(name="Write the brief"))
    assert _ok(out)
    body = http.last_body()
    assert body["data"]["name"] == "Write the brief"
    assert body["data"]["workspace"] == "100"


async def test_create_task_requires_a_name(connected_ctx, http):
    http.push(envelope(me_payload()))
    out = await hw.create_task(connected_ctx, CreateTaskParams(name="  "))
    assert not _ok(out)
    assert _code(out) == ac.ASANA_VALIDATION_FAILED


async def test_update_task_uses_put_not_patch(connected_ctx, http):
    """There is no PATCH route for a task: sending one is a 404 that reads
    like the task does not exist."""
    http.push(envelope(me_payload()))
    http.push(envelope(task_payload(gid="1201")))
    out = await hw.update_task(connected_ctx,
                              UpdateTaskParams(task="1201234567", name="Renamed"))
    assert _ok(out)
    assert http.calls[-1]["method"] == "PUT"


async def test_update_task_with_nothing_to_change_says_so(connected_ctx, http):
    http.push(envelope(me_payload()))
    out = await hw.update_task(connected_ctx, UpdateTaskParams(task="1201234567"))
    assert not _ok(out)
    assert _code(out) == ac.ASANA_VALIDATION_FAILED


async def test_update_task_can_clear_a_due_date(connected_ctx, http):
    """Removing a deadline must be possible, and Asana takes null for it."""
    http.push(envelope(me_payload()))
    http.push(envelope(task_payload(gid="1201")))
    out = await hw.update_task(connected_ctx,
                              UpdateTaskParams(task="1201234567", due="clear"))
    assert _ok(out)
    assert http.last_body()["data"]["due_on"] is None


async def test_complete_task_sets_the_completed_flag(connected_ctx, http):
    http.push(envelope(me_payload()))
    http.push(envelope(task_payload(gid="1201", completed=True)))
    out = await hw.complete_task(connected_ctx, CompleteTaskParams(task="1201234567"))
    assert _ok(out)
    assert http.last_body()["data"]["completed"] is True


async def test_reopening_a_task_sets_completed_false(connected_ctx, http):
    http.push(envelope(me_payload()))
    http.push(envelope(task_payload(gid="1201", completed=False)))
    out = await hw.complete_task(connected_ctx,
                                CompleteTaskParams(task="1201234567", completed=False))
    assert _ok(out)
    assert http.last_body()["data"]["completed"] is False


async def test_add_comment_posts_to_the_stories_route(connected_ctx, http):
    http.push(envelope(me_payload()))
    http.push(envelope(story_payload(text="On it")))
    out = await hw.add_comment(connected_ctx,
                               AddCommentParams(task="1201234567", comment="On it"))
    assert _ok(out)
    assert http.calls[-1]["url"].endswith("/stories")
    assert http.last_body()["data"]["text"] == "On it"


async def test_add_comment_refuses_empty_text(connected_ctx, http):
    http.push(envelope(me_payload()))
    out = await hw.add_comment(connected_ctx,
                              AddCommentParams(task="1201234567", comment="  "))
    assert not _ok(out)
    assert _code(out) == ac.ASANA_VALIDATION_FAILED


async def test_delete_task_states_the_real_stakes(connected_ctx, http):
    """Asana keeps deleted tasks recoverable for 30 days -- saying "permanently
    deleted" would be a scarier lie, and staying silent leaves them guessing."""
    http.push(envelope(me_payload()))
    http.push(envelope([task_payload(gid="1201")]))
    http.push(envelope({}))
    out = await hw.delete_task(connected_ctx, DeleteTaskParams(task="Ship the landing page"))
    assert _ok(out)
    assert http.calls[-1]["method"] == "DELETE"
    assert "30 days" in _text(out)


# --- ambiguity is never a coin flip -----------------------------------------

async def test_an_ambiguous_task_name_refuses_to_pick_before_writing(connected_ctx, http):
    """Two tasks match, and the next step would COMPLETE one of them."""
    http.push(envelope(me_payload()))
    # A PARTIAL reference matching two tasks. Note the reference is deliberately
    # not equal to either name: an EXACT name match is allowed to win (see the
    # next test), so reusing a full name here would resolve rather than refuse.
    http.push(envelope([
        task_payload(gid="1", name="Ship the landing page"),
        task_payload(gid="2", name="Ship the pricing page"),
    ]))
    out = await hw.complete_task(connected_ctx,
                                CompleteTaskParams(task="Ship the"))
    assert not _ok(out)
    assert _code(out) == ac.ASANA_TARGET_AMBIGUOUS
    # The point of the guard: nothing was written.
    assert not any(c["method"] == "PUT" for c in http.calls)


async def test_an_exact_name_match_wins_over_a_longer_partial(connected_ctx, http):
    """"Ship the landing page" must not be ambiguous just because a
    "... v2" exists -- an exact hit is a decision, not a coin flip."""
    http.push(envelope(me_payload()))
    http.push(envelope([
        task_payload(gid="1", name="Ship the landing page"),
        task_payload(gid="2", name="Ship the landing page v2"),
    ]))
    http.push(envelope(task_payload(gid="1", completed=True)))
    out = await hw.complete_task(connected_ctx,
                                CompleteTaskParams(task="Ship the landing page"))
    assert _ok(out)
    # The gid is in the PATH, and opt_fields ride along as query params -- so
    # match the path segment rather than the end of the URL.
    assert any(c["method"] == "PUT" and c["url"].rstrip("/").endswith("/tasks/1")
               for c in http.calls)


async def test_a_missing_task_name_is_not_treated_as_found(connected_ctx, http):
    http.push(envelope(me_payload()))
    http.push(envelope([]))
    out = await hr.get_task(connected_ctx, GetTaskParams(task="Nope"))
    assert not _ok(out)
    assert _code(out) == ac.ASANA_TARGET_NOT_FOUND


# --- connect ----------------------------------------------------------------

async def test_connect_account_validates_before_storing(connected_ctx, http):
    """A rejected token must not be saved -- that is what made a bad paste feel
    like a silent success."""
    http.push(error_payload("Not Authorized"), status=401)
    out = await hw.connect_account(connected_ctx, ConnectAccountParams(token="bad"))
    assert not _ok(out)
    assert _code(out) == ac.ASANA_TOKEN_REJECTED


async def test_connect_account_reports_the_workspaces_it_found(ctx, http):
    http.push(envelope(me_payload(workspaces=[
        {"gid": "1", "name": "Personal"}, {"gid": "2", "name": "Acme"},
    ])))
    out = await hw.connect_account(ctx, ConnectAccountParams(token="2/good"))
    assert _ok(out)
    assert "Personal" in _text(out) and "Acme" in _text(out)


async def test_no_token_configured_tells_the_user_where_to_paste_one(ctx, http):
    out = await hr.search(ctx, SearchParams(query="anything"))
    assert not _ok(out)
    assert _code(out) == ac.ASANA_TOKEN_MISSING
    assert http.calls == []


# --- check_access -----------------------------------------------------------

async def test_check_access_reports_what_the_token_reaches(connected_ctx, http):
    """The tool that answers "why can't you see my task" -- it must not be the
    one that breaks.

    It shipped raising a pydantic ValidationError on every single call, because
    nothing here ever executed it. A structural test caught the field names; only
    running it proves the report is actually populated.
    """
    from models import CheckAccessParams

    http.push(envelope(me_payload(workspaces=[{"gid": "100", "name": "Acme"}])))
    http.push(envelope([project_payload(gid="500", name="Website"),
                        project_payload(gid="501", name="Brand")]))
    http.push(envelope([{"gid": "9001"}, {"gid": "9002"}]))
    http.push(envelope([task_payload()]))          # premium probe succeeds

    out = await hr.check_access(connected_ctx, CheckAccessParams())
    assert _ok(out), _text(out)

    report = out.data
    assert report.account_name == "Vlad Ivanco"
    assert report.workspace == "Acme"
    assert report.workspace_count == 1
    assert report.project_count == 2
    assert report.user_count == 2
    # premium_search is PROSE, not a flag: the model declares a string, and a
    # bool here is what made every call fail.
    assert isinstance(report.premium_search, str)
    assert report.premium_search
    assert "acme" in _text(out).lower()


async def test_check_access_says_when_advanced_search_is_unavailable(connected_ctx, http):
    """A free plan must be reported as a plan limit, not as a failure."""
    from models import CheckAccessParams

    http.push(envelope(me_payload()))
    http.push(envelope([]))
    http.push(envelope([]))
    http.push(error_payload("payment required"), status=402)

    out = await hr.check_access(connected_ctx, CheckAccessParams())
    assert _ok(out)
    assert "not available on this plan" in out.data.premium_search.lower()


async def test_task_entity_keeps_its_start_date_and_parent(connected_ctx, http):
    """Fields fetched from Asana must survive into the rendered entity."""
    http.push(envelope(me_payload()))
    http.push(envelope(task_payload(
        gid="1201", start_on="2026-07-20",
        parent={"gid": "9", "name": "Launch ksrenovationgroup.com"})))

    out = await hr.get_task(connected_ctx, GetTaskParams(task="1201234567"))
    assert _ok(out)
    assert out.data.start == "2026-07-20"
    assert out.data.parent == "Launch ksrenovationgroup.com"


async def test_create_project_omits_public_in_an_organization(connected_ctx, http):
    """`public` is not writable on an organization project.

    Visibility there follows TEAM MEMBERSHIP, and Asana answers
    "public: Cannot write this property". Since CreateProjectParams.public
    defaults to True, sending it unconditionally rejected every project
    creation in an org -- which is the only place a project needs a team, i.e.
    exactly the real-world case.
    """
    from models import CreateProjectParams

    http.push(envelope(me_payload(workspaces=[
        {"gid": "100", "name": "Acme", "is_organization": True}])))
    http.push(envelope([{"gid": "700", "name": "Delivery"}]))   # team lookup
    http.push(envelope(project_payload(gid="900", name="Launch")))

    out = await hw.create_project(connected_ctx, CreateProjectParams(
        name="Launch", team="Delivery"))
    assert _ok(out), _text(out)

    post = [c for c in http.calls if c["method"] == "POST"][-1]
    body = post["json"]["data"]
    assert "public" not in body, f"public must not be sent to an org: {body}"
    assert body["team"] == "700"


async def test_create_project_still_sends_public_in_a_personal_workspace(connected_ctx, http):
    """The flag is legitimate off an organization -- do not drop it everywhere."""
    from models import CreateProjectParams

    http.push(envelope(me_payload(workspaces=[{"gid": "100", "name": "Acme"}])))
    http.push(envelope(project_payload(gid="900", name="Launch")))

    out = await hw.create_project(connected_ctx, CreateProjectParams(name="Launch"))
    assert _ok(out), _text(out)

    post = [c for c in http.calls if c["method"] == "POST"][-1]
    assert post["json"]["data"]["public"] is True
