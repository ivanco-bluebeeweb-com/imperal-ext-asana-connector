"""Field selection and rendering.

The interesting cases here are the ones where Asana is picky and a plausible
guess is wrong: `due_on` vs `due_at` (setting BOTH is rejected), gids that must
stay strings, and a `stories` feed that mixes human comments with system
activity.
"""

import asana_objects as ao


# --- field extraction -------------------------------------------------------

def test_gid_stays_a_string():
    """Some gids are big enough that int coercion is lossy, and Asana wants
    the original string back."""
    big = "1209876543210987"
    assert ao.gid_of({"gid": big}) == big
    assert isinstance(ao.gid_of({"gid": 1201}), str)


def test_missing_and_malformed_objects_never_raise():
    """A partial object is normal: `opt_fields` decides what comes back."""
    for bad in (None, "text", 42, [], {}):
        assert ao.name_of(bad) == ""
        assert ao.gid_of(bad) == ""
        assert ao.nested_name(bad, "assignee") == ""
        assert ao.name_list(bad, "projects") == []


def test_nested_name_and_gid_read_expanded_objects():
    task = {"assignee": {"gid": "77", "name": "Vlad"}}
    assert ao.nested_name(task, "assignee") == "Vlad"
    assert ao.nested_gid(task, "assignee") == "77"
    # A compact reference without the name must not invent one.
    assert ao.nested_name({"assignee": {"gid": "77"}}, "assignee") == ""


def test_name_list_skips_entries_without_names():
    task = {"projects": [{"name": "Web"}, {"gid": "5"}, "junk", {"name": ""}]}
    assert ao.name_list(task, "projects") == ["Web"]


# --- gid guard --------------------------------------------------------------

def test_looks_like_gid_accepts_real_gids_and_rejects_names():
    assert ao.looks_like_gid("1201234567890") is True
    # A task can legitimately be NAMED "42" -- treating that as a gid would
    # skip the name lookup and then 404 on a task that exists.
    assert ao.looks_like_gid("42") is False
    assert ao.looks_like_gid("Website Redesign") is False
    assert ao.looks_like_gid("") is False
    assert ao.looks_like_gid("12ab3456") is False


# --- due dates --------------------------------------------------------------

def test_a_bare_date_goes_to_due_on():
    assert ao.due_field_for("2026-08-01") == ("due_on", "2026-08-01")


def test_a_timestamp_goes_to_due_at():
    """Asana REJECTS a request setting both due_on and due_at, so exactly one
    field must be chosen -- a date is a day, a timestamp is a moment."""
    field, value = ao.due_field_for("2026-08-01T15:00:00Z")
    assert field == "due_at"
    assert value == "2026-08-01T15:00:00Z"
    assert ao.due_field_for("2026-08-01 15:00")[0] == "due_at"


def test_an_empty_due_selects_no_field():
    assert ao.due_field_for("") == ("", "")
    assert ao.due_field_for("   ") == ("", "")


# --- rendering --------------------------------------------------------------

def test_render_task_reads_like_a_sentence_not_json():
    task = {
        "name": "Ship the landing page",
        "completed": False,
        "assignee": {"name": "Vlad"},
        "due_on": "2026-08-01",
        "projects": [{"name": "Website"}],
        "tags": [{"name": "urgent"}],
        "num_subtasks": 3,
    }
    out = ao.render_task(task)
    assert "Ship the landing page [open]" in out
    assert "assignee: Vlad" in out
    assert "due: 2026-08-01" in out
    assert "in: Website" in out
    assert "tags: urgent" in out
    assert "subtasks: 3" in out
    assert "{" not in out


def test_render_task_marks_completion():
    assert "[done]" in ao.render_task({"name": "X", "completed": True})
    assert "[open]" in ao.render_task({"name": "X", "completed": False})


def test_render_task_clips_long_notes():
    out = ao.render_task({"name": "X", "notes": "y" * 900})
    assert out.endswith("...")
    assert len(out) < 500


def test_render_task_omits_absent_fields_rather_than_showing_blanks():
    out = ao.render_task({"name": "Bare task", "completed": False})
    assert out == "Bare task [open]"


def test_is_comment_separates_humans_from_system_activity():
    """The stories feed mixes both. Showing "marked this complete" as a comment
    would be a lie about who said what."""
    assert ao.is_comment({"resource_subtype": "comment_added"}) is True
    assert ao.is_comment({"resource_subtype": "marked_complete"}) is False
    assert ao.is_comment({"resource_subtype": "assigned"}) is False
    assert ao.is_comment(None) is False


def test_render_stories_formats_author_date_and_text():
    out = ao.render_stories([
        {"created_by": {"name": "Vlad"}, "created_at": "2026-07-01T10:00:00Z",
         "text": "Looks good"},
        {"created_by": {"name": "Ana"}, "created_at": "2026-07-02T10:00:00Z",
         "text": "Shipping today"},
    ])
    assert out.splitlines() == [
        "Vlad (2026-07-01): Looks good",
        "Ana (2026-07-02): Shipping today",
    ]


def test_render_stories_skips_empty_text_and_survives_junk():
    out = ao.render_stories([{"text": ""}, "junk", None,
                             {"text": "real", "created_by": {"name": "V"}}])
    assert out == "V (): real"


def test_opt_fields_are_comma_separated_without_spaces():
    """Asana parses opt_fields as a comma list; a space makes the field name
    unknown, which is a 400 naming a field the user never typed."""
    for spec in (ao.TASK_FIELDS, ao.TASK_COMPACT_FIELDS, ao.PROJECT_FIELDS,
                 ao.STORY_FIELDS, ao.USER_FIELDS, ao.WORKSPACE_FIELDS,
                 ao.SECTION_FIELDS):
        assert " " not in spec, spec
        assert not spec.startswith(","), spec
        assert not spec.endswith(","), spec
