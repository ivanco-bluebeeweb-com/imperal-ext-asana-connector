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


# --- start dates and the clear_* flags --------------------------------------

async def test_create_task_sets_a_start_date(connected_ctx, http):
    """Timeline work is impossible without this.

    `start_on` was READ back on every task but there was no way to SET it, so
    a plan built through this connector could never show a duration.
    """
    from models import CreateTaskParams

    http.push(envelope(me_payload()))
    http.push(envelope([project_payload(gid="500", name="Launch")]))
    http.push(envelope(task_payload(name="Fix title")))

    out = await hw.create_task(connected_ctx, CreateTaskParams(
        name="Fix title", project="Launch",
        start="2026-08-01", due="2026-08-03"))
    assert _ok(out), _text(out)

    body = [c for c in http.calls if c["method"] == "POST"][-1]["json"]["data"]
    assert body["start_on"] == "2026-08-01"
    assert body["due_on"] == "2026-08-03"


async def test_create_task_refuses_a_start_without_a_due_date(connected_ctx, http):
    """Asana rejects a task that starts but never ends.

    Its own message for this is opaque, so the requirement is stated up front
    rather than forwarded -- and no HTTP call should be spent on it.
    """
    from models import CreateTaskParams

    http.push(envelope(me_payload()))
    http.push(envelope([project_payload(gid="500", name="Launch")]))

    out = await hw.create_task(connected_ctx, CreateTaskParams(
        name="Fix title", project="Launch", start="2026-08-01"))
    assert not _ok(out)
    assert "due date" in _text(out).lower()
    assert not [c for c in http.calls if c["method"] == "POST"]


async def test_create_task_rejects_a_timestamp_as_a_start_date(connected_ctx, http):
    """`start_on` is a day. Asana has no start-time field on a task."""
    from models import CreateTaskParams

    http.push(envelope(me_payload()))
    http.push(envelope([project_payload(gid="500", name="Launch")]))

    out = await hw.create_task(connected_ctx, CreateTaskParams(
        name="Fix title", project="Launch",
        start="2026-08-01T09:00:00Z", due="2026-08-03"))
    assert not _ok(out)
    assert "day, not a moment" in _text(out).lower()


async def test_update_task_accepts_a_start_when_the_task_already_has_a_due_date(
        connected_ctx, http):
    """The due date may already exist -- do not demand it be re-sent.

    The resolve envelope cannot answer this: typeahead returns compact objects
    with no dates, so a naive check would have blocked a legitimate update.
    """
    from models import UpdateTaskParams

    http.push(envelope(me_payload()))
    http.push(envelope({"gid": "1201", "due_on": "2026-08-03"}))   # due lookup
    http.push(envelope(task_payload(gid="1201")))

    # A pasted gid skips typeahead, so this test exercises the date logic
    # rather than name resolution.
    out = await hw.update_task(connected_ctx, UpdateTaskParams(
        task="1201234567", start="2026-08-01"))
    assert _ok(out), _text(out)

    # Asana wants the due date in the SAME request, not merely present on the
    # task. Found live: this exact call failed with "You must provide `due_on`
    # or `due_at` when setting `start_on`" on a task that HAD a due date.
    body = [c for c in http.calls if c["method"] == "PUT"][-1]["json"]["data"]
    assert body["start_on"] == "2026-08-01"
    assert body["due_on"] == "2026-08-03", (
        f"the existing due date must be echoed back: {body}")

    # The summary is built from the `changed` list, so a branch that forgets to
    # append leaves the user with a literal "Updated " and no idea what
    # happened. Live, that is exactly what setting a start date returned.
    assert "start date" in _text(out), _text(out)

    body = [c for c in http.calls if c["method"] == "PUT"][-1]["json"]["data"]
    assert body["start_on"] == "2026-08-01"


async def test_update_task_refuses_a_start_when_the_task_has_no_due_date(
        connected_ctx, http):
    """Same rule, checked against the task's real state."""
    from models import UpdateTaskParams

    http.push(envelope(me_payload()))
    http.push(envelope({"gid": "1201"}))           # no due date on the task

    out = await hw.update_task(connected_ctx, UpdateTaskParams(
        task="1201234567", start="2026-08-01"))
    assert not _ok(out)
    assert "due date" in _text(out).lower()
    assert not [c for c in http.calls if c["method"] == "PUT"]


async def test_clear_due_and_clear_assignee_actually_do_something(connected_ctx, http):
    """Both flags were declared, advertised in the tool description -- and never read.

    Same class of bug as the dropped entity fields: the promise was visible in
    the schema, so "unassign this task" looked supported and silently did
    nothing.
    """
    from models import UpdateTaskParams

    http.push(envelope(me_payload()))
    http.push(envelope(task_payload(gid="1201")))

    out = await hw.update_task(connected_ctx, UpdateTaskParams(
        task="1201234567", clear_due=True, clear_assignee=True))
    assert _ok(out), _text(out)

    body = [c for c in http.calls if c["method"] == "PUT"][-1]["json"]["data"]
    assert body["due_on"] is None
    assert body["assignee"] is None


# --- dependencies, followers, tags ------------------------------------------

async def test_dependency_links_tasks_by_name(connected_ctx, http):
    """Order as DATA, not prose.

    The KS launch plan had to say "do the analytics task first" in a comment,
    because there was no way to express sequence. A comment cannot be sorted,
    filtered or shown on a timeline.
    """
    from models import TaskDependencyParams

    http.push(envelope(me_payload()))
    http.push(envelope({}))                 # addDependencies
    http.push(envelope({"dependencies": [{"gid": "1209876543"}]}))  # read-back

    out = await hw.set_task_dependency(connected_ctx, TaskDependencyParams(
        task="1201234567", depends_on="1209876543"))
    assert _ok(out), _text(out)

    post = [c for c in http.calls if c["method"] == "POST"][-1]
    assert "addDependencies" in post["url"]
    assert post["json"]["data"]["dependencies"] == ["1209876543"]


async def test_dependency_removal_uses_the_other_endpoint(connected_ctx, http):
    """Asana has separate add/remove routes -- a flag must not silently add."""
    from models import TaskDependencyParams

    http.push(envelope(me_payload()))
    http.push(envelope({}))

    out = await hw.set_task_dependency(connected_ctx, TaskDependencyParams(
        task="1201234567", depends_on="1209876543", remove=True))
    assert _ok(out), _text(out)
    assert "removeDependencies" in http.calls[-1]["url"]


async def test_a_task_cannot_depend_on_itself(connected_ctx, http):
    """A self-dependency is a deadlock Asana would happily store."""
    from models import TaskDependencyParams

    http.push(envelope(me_payload()))

    out = await hw.set_task_dependency(connected_ctx, TaskDependencyParams(
        task="1201234567", depends_on="1201234567"))
    assert not _ok(out)
    assert "itself" in _text(out).lower()
    assert not [c for c in http.calls if "Dependencies" in c["url"]]


async def test_followers_are_added_by_name(connected_ctx, http):
    """Assignee is who does it; followers are who needs to know."""
    from models import TaskFollowersParams

    http.push(envelope(me_payload()))
    http.push(envelope({}))                 # addFollowers

    out = await hw.set_task_followers(connected_ctx, TaskFollowersParams(
        task="1201234567", people="me"))
    assert _ok(out), _text(out)

    post = [c for c in http.calls if c["method"] == "POST"][-1]
    assert "addFollowers" in post["url"]
    assert post["json"]["data"]["followers"] == ["me"]


async def test_adding_an_unknown_tag_creates_it(connected_ctx, http):
    """Otherwise "tag this urgent" fails in the one workspace that has never
    used the word -- precisely where it is being introduced."""
    from models import TaskTagsParams

    http.push(envelope(me_payload()))
    http.push(envelope([]))                                 # tag typeahead: none
    http.push(envelope({"gid": "77", "name": "urgent"}))    # POST /tags
    http.push(envelope({}))                                 # addTag

    out = await hw.set_task_tags(connected_ctx, TaskTagsParams(
        task="1201234567", tags="urgent"))
    assert _ok(out), _text(out)

    urls = [c["url"] for c in http.calls]
    assert any(u.endswith("/tags") for u in urls), urls
    assert any("addTag" in u for u in urls), urls


async def test_removing_an_unknown_tag_creates_nothing(connected_ctx, http):
    """Inventing a tag in order to detach it would be absurd -- and would
    leave litter in the workspace on every typo."""
    from models import TaskTagsParams

    http.push(envelope(me_payload()))
    http.push(envelope([]))                    # tag typeahead: none

    out = await hw.set_task_tags(connected_ctx, TaskTagsParams(
        task="1201234567", tags="ghost", remove=True))
    assert not _ok(out)
    assert "nothing to remove" in _text(out).lower()
    assert not any(c["method"] == "POST" and c["url"].endswith("/tags")
                   for c in http.calls)


async def test_a_task_name_containing_a_comma_is_not_split(connected_ctx, http):
    """Found live, and it failed SILENTLY.

    Asking a task to wait for 'Instrument analytics and call tracking before
    launch, not after' -- one task, comma in the title -- split into two
    halves, each resolved to something, and linked TWO dependencies from one
    name. No error: just a confidently wrong result nobody would re-check.
    """
    from models import TaskDependencyParams

    whole = "Instrument analytics before launch, not after"

    http.push(envelope(me_payload()))
    http.push(envelope([{"gid": "300", "name": "QA pass"}]))      # target
    http.push(envelope([{"gid": "410", "name": whole}]))          # whole name
    http.push(envelope([{"gid": "410", "name": whole}]))          # resolve it
    http.push(envelope({}))                                       # addDependencies
    http.push(envelope({"dependencies": [{"gid": "410"}]}))       # read-back

    out = await hw.set_task_dependency(connected_ctx, TaskDependencyParams(
        task="QA pass", depends_on=whole))
    assert _ok(out), _text(out)

    post = [c for c in http.calls if "Dependencies" in c["url"]][-1]
    assert post["json"]["data"]["dependencies"] == ["410"], (
        f"one task named, one dependency expected: {post['json']['data']}")


async def test_a_real_comma_separated_list_still_splits(connected_ctx, http):
    """The comma-aware split must not break the list case it exists for."""
    from models import TaskDependencyParams

    http.push(envelope(me_payload()))
    http.push(envelope([{"gid": "300", "name": "QA pass"}]))   # target
    http.push(envelope([]))                                   # not one name
    http.push(envelope([{"gid": "401", "name": "Fix title"}]))
    http.push(envelope([{"gid": "402", "name": "Add DKIM"}]))
    http.push(envelope({}))
    http.push(envelope({"dependencies": [{"gid": "401"}, {"gid": "402"}]}))

    out = await hw.set_task_dependency(connected_ctx, TaskDependencyParams(
        task="QA pass", depends_on="Fix title, Add DKIM"))
    assert _ok(out), _text(out)

    post = [c for c in http.calls if "Dependencies" in c["url"]][-1]
    assert post["json"]["data"]["dependencies"] == ["401", "402"]


async def test_get_task_shows_dependencies_and_followers(connected_ctx, http):
    """A write you cannot read back is a write you cannot verify.

    Dependencies and followers became writable first; until they were readable
    too, the connector could change a task's order or audience and then show
    no trace of it -- including to itself, on the next call.
    """
    from models import GetTaskParams

    http.push(envelope(me_payload()))
    http.push(envelope(dict(
        task_payload(gid="300", name="QA pass"),
        dependencies=[{"gid": "410", "name": "Instrument analytics"}],
        dependents=[{"gid": "420", "name": "Go live"}],
        followers=[{"gid": "9", "name": "Vlad Ivanco"}],
    )))

    out = await hr.get_task(connected_ctx, GetTaskParams(task="1201234567"))
    assert _ok(out), _text(out)

    data = out.data
    assert data.blocked_by == "Instrument analytics", data
    assert data.blocking == "Go live", data
    assert data.followers == "Vlad Ivanco", data


async def test_a_dependency_asana_silently_ignored_is_reported_as_failure(
        connected_ctx, http):
    """The worst failure mode: HTTP 200, nothing stored.

    Task dependencies are a paid-plan feature and the API mirrors the product
    limit -- but a free workspace gets no error. Asana answers 200 and drops
    the write. Verified live: the tool reported "now waits for ..." and the
    task came back with no dependencies at all.

    A write tool that cannot fail teaches the user to trust a lie, so the
    write is read back and a no-op is surfaced as the error it is.
    """
    from models import TaskDependencyParams

    http.push(envelope(me_payload()))
    http.push(envelope({}))                       # addDependencies -> 200 OK
    http.push(envelope({"dependencies": []}))     # ...but nothing was stored

    out = await hw.set_task_dependency(connected_ctx, TaskDependencyParams(
        task="1201234567", depends_on="1209876543"))

    assert not _ok(out)
    text = _text(out).lower()
    assert "paid" in text, text
    assert "descriptions" in text, "tell the user what to do instead: " + text


async def test_dependency_names_are_fetched_when_asana_omits_them(
        connected_ctx, http):
    """The compact-resource trap, found live.

    Asana returns `dependencies` and `dependents` as COMPACT resources -- gid
    only, no name -- even when `dependencies.name` is requested. The task had
    a real dependency, the write's own read-back found it by gid, and get_task
    still showed an empty field. Data present, display empty: the shape of bug
    that makes a user think the write failed.
    """
    from models import GetTaskParams

    http.push(envelope(me_payload()))
    http.push(envelope(dict(
        task_payload(gid="300", name="QA pass"),
        dependencies=[{"gid": "410"}],        # compact: no name
        dependents=[{"gid": "420"}],          # compact: no name
    )))
    http.push(envelope({"gid": "410", "name": "Instrument analytics"}))
    http.push(envelope({"gid": "420", "name": "Go live"}))

    out = await hr.get_task(connected_ctx, GetTaskParams(task="1201234567"))
    assert _ok(out), _text(out)
    assert out.data.blocked_by == "Instrument analytics", out.data
    assert out.data.blocking == "Go live", out.data


async def test_an_unreadable_linked_task_is_not_silently_dropped(
        connected_ctx, http):
    """A link to a task this account cannot read is still a link.

    Dropping it would render an empty field and imply the task is unblocked --
    the opposite of the truth, and the more dangerous direction to be wrong in.
    """
    from models import GetTaskParams

    http.push(envelope(me_payload()))
    http.push(envelope(dict(
        task_payload(gid="300", name="QA pass"),
        dependencies=[{"gid": "999"}],
    )))
    http.push(envelope({}, ), )                      # name lookup: no name
    out = await hr.get_task(connected_ctx, GetTaskParams(task="1201234567"))
    assert _ok(out), _text(out)
    assert out.data.blocked_by, "an unreadable link must not vanish"
    assert "cannot read" in out.data.blocked_by


async def test_custom_fields_are_read_via_asanas_own_rendering(
        connected_ctx, http):
    """Custom fields are where a real workspace keeps its meaning.

    Priority, status, effort, budget -- a task read without them is a task
    read without half its content. Asana has six value types; `display_value`
    is its own string rendering of any of them, which is why this connector
    does not carry six parsers that could each be subtly wrong.
    """
    from models import GetTaskParams

    http.push(envelope(me_payload()))
    http.push(envelope(dict(
        task_payload(gid="300", name="QA pass"),
        custom_fields=[
            {"name": "Priority", "display_value": "High"},        # enum
            {"name": "Estimate", "display_value": "6"},           # number
            {"name": "Client", "display_value": "KS Renovation"},  # text
        ],
    )))

    out = await hr.get_task(connected_ctx, GetTaskParams(task="1201234567"))
    assert _ok(out), _text(out)
    assert "Priority: High" in out.data.custom_fields, out.data.custom_fields
    assert "Estimate: 6" in out.data.custom_fields, out.data.custom_fields
    assert "Client: KS Renovation" in out.data.custom_fields


async def test_an_unset_custom_field_is_not_rendered_as_noise(
        connected_ctx, http):
    """A field with no value is not information.

    Asana attaches EVERY field defined on the project to EVERY task, set or
    not. Rendering the empty ones would bury the two that matter under a dozen
    'Budget: ' fragments -- technically complete, practically unreadable.
    """
    from models import GetTaskParams

    http.push(envelope(me_payload()))
    http.push(envelope(dict(
        task_payload(gid="300", name="QA pass"),
        custom_fields=[
            {"name": "Priority", "display_value": "High"},
            {"name": "Budget", "display_value": None},   # never filled in
            {"name": "Owner", "display_value": ""},      # cleared
        ],
    )))

    out = await hr.get_task(connected_ctx, GetTaskParams(task="1201234567"))
    assert _ok(out), _text(out)
    assert out.data.custom_fields == "Priority: High", out.data.custom_fields


# --- attachments -------------------------------------------------------------

async def test_attachments_are_listed_with_readable_sizes(connected_ctx, http):
    """A task with three files used to read exactly like a task with none.

    Attachments carry the actual deliverable -- the drawing, the quote, the
    photo of the wall. Size is rendered for humans: '15728640' says nothing
    about whether a file can be sent to a client on a phone; '15 MB' does.
    """
    from models import ListAttachmentsParams

    http.push(envelope(me_payload()))
    http.push(envelope([
        {"gid": "10", "name": "kitchen-plan.pdf", "size": 2411724,
         "host": "asana", "created_at": "2026-07-20T10:00:00.000Z",
         "permanent_url": "https://app.asana.com/att/10"},
    ]))

    out = await hr.list_attachments(connected_ctx, ListAttachmentsParams(
        task="1201234567"))
    assert _ok(out), _text(out)

    first = out.data.items[0]
    assert first.name == "kitchen-plan.pdf"
    assert first.size == "2.3 MB", first.size
    assert first.url == "https://app.asana.com/att/10"


async def test_an_external_file_says_where_it_actually_lives(connected_ctx, http):
    """A Drive link and an uploaded file look identical in a list.

    They behave nothing alike: one is archived with the task, the other
    disappears the day someone tidies their Drive. Worth knowing BEFORE
    telling a client the file is safe with the project.
    """
    from models import ListAttachmentsParams

    http.push(envelope(me_payload()))
    http.push(envelope([
        {"gid": "11", "name": "site-photos", "host": "gdrive",
         "created_at": "2026-07-21T10:00:00.000Z",
         "permanent_url": "https://app.asana.com/att/11"},
    ]))

    out = await hr.list_attachments(connected_ctx, ListAttachmentsParams(
        task="1201234567"))
    assert _ok(out), _text(out)
    assert "gdrive" in out.data.items[0].summary.lower(), out.data.items[0].summary


async def test_no_attachments_is_a_clear_answer_not_an_empty_list(
        connected_ctx, http):
    """'No files attached' is information; a blank list looks like a failure."""
    from models import ListAttachmentsParams

    http.push(envelope(me_payload()))
    http.push(envelope([]))

    out = await hr.list_attachments(connected_ctx, ListAttachmentsParams(
        task="1201234567"))
    assert _ok(out)
    assert out.data.total == 0
    assert "no files" in _text(out).lower(), _text(out)


# --- Part D2 (SCENARIO_TESTING_STANDARD.md): idempotency / double-invocation -

async def test_delete_task_twice_fails_clean_on_the_second_call(connected_ctx, http):
    """A retried chat turn (timeout, double-click) must not crash or lie on
    the second delete -- the task is already gone, so the second resolve_task
    lookup finds nothing and the tool must say so, not attempt a DELETE on a
    gid it never confirmed exists."""
    # First call: resolve + delete succeeds normally.
    http.push(envelope(me_payload()))
    http.push(envelope([task_payload(gid="1201")]))
    http.push(envelope({}))
    first = await hw.delete_task(connected_ctx, DeleteTaskParams(task="Ship the landing page"))
    assert _ok(first)
    assert http.calls[-1]["method"] == "DELETE"

    # Second call: Asana's typeahead now returns nothing for the same name --
    # the resolve step must fail closed, not fall through to a stale gid.
    http.push(envelope(me_payload()))
    http.push(envelope([]))
    second = await hw.delete_task(connected_ctx, DeleteTaskParams(task="Ship the landing page"))
    assert not _ok(second)
    # No second DELETE was attempted -- the tool stopped at resolution.
    assert not any(c["method"] == "DELETE" for c in http.calls[3:])


async def test_set_task_dependency_twice_is_all_or_nothing_both_times(connected_ctx, http):
    """set_task_dependency aborts BEFORE writing if any named blocker fails to
    resolve (see its docstring: 'half-built dependency chain is harder to
    notice than a refusal'). Calling it twice with the same params must
    preserve that guarantee on the SECOND call too, not just the first --
    a retried call must not partially apply if one dependency now fails to
    resolve (e.g. renamed/completed between calls)."""
    from models import TaskDependencyParams

    # First call: both blockers resolve, dependency link written once, and
    # the mandatory read-back check confirms the dependency actually stuck
    # (see set_task_dependency's own docstring on the free-plan silent-drop).
    http.push(envelope(me_payload()))
    http.push(envelope([task_payload(gid="1201", name="Ship the landing page")]))
    http.push(envelope([task_payload(gid="1300", name="Design review")]))
    http.push(envelope({}))
    http.push(envelope({"dependencies": [{"gid": "1300"}]}))
    first = await hw.set_task_dependency(connected_ctx, TaskDependencyParams(
        task="Ship the landing page", depends_on="Design review"))
    assert _ok(first), _text(first)
    assert http.calls[-1]["method"] == "GET"
    calls_after_first = len(http.calls)

    # Second, identical call: the blocker task no longer resolves (e.g. it was
    # completed and archived out of typeahead in between) -- must abort with
    # an error and make NO write, not silently succeed or half-apply.
    http.push(envelope(me_payload()))
    http.push(envelope([task_payload(gid="1201", name="Ship the landing page")]))
    http.push(envelope([]))
    second = await hw.set_task_dependency(connected_ctx, TaskDependencyParams(
        task="Ship the landing page", depends_on="Design review"))
    assert not _ok(second)
    assert not any(c["method"] == "POST" for c in http.calls[calls_after_first:])


# --- Part D3 (SCENARIO_TESTING_STANDARD.md): security / SSRF surface -------

async def test_no_ssrf_surface_no_user_supplied_url_fields_exist():
    """Security review, not a runtime test: this checks INPUT (*Params)
    models only -- output models legitimately carry `.url` (Asana's own
    permalink_url / webhook delivery url echoed back), which is not an SSRF
    surface since this app's own code never fetches those. What matters is
    whether any chat.function PARAMETER lets a caller name an arbitrary
    address for this app's own code to fetch -- grep/introspection here finds
    none: every write is scoped to Asana's own fixed API host via
    asana_client.py. Classic SSRF (internal IPs, cloud metadata,
    redirect-to-localhost) has no entry point here. Documented as a
    regression trip-wire: if a future function adds a user-supplied URL
    field (e.g. an attachment-by-URL feature), this assertion's premise
    changes and a real SSRF-probe test must be added then.
    """
    import inspect
    import models
    url_like_fields = []
    for name, cls in inspect.getmembers(models, inspect.isclass):
        if not name.endswith("Params") or not hasattr(cls, "model_fields"):
            continue
        for field_name in cls.model_fields:
            if "url" in field_name.lower():
                url_like_fields.append(f"{name}.{field_name}")
    assert url_like_fields == [], (
        f"New URL-shaped input field(s) found: {url_like_fields} -- SSRF review needed.")
