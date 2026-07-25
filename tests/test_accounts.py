"""Accounts and workspace resolution.

This is where Asana structurally differs from Notion and where a copied design
would have been wrong: a Notion token means exactly ONE workspace, while an
Asana PAT belongs to a USER and reaches EVERY workspace they are a member of.
So "which workspace" is a real choice inside a single token, and these tests pin
that behaviour down.
"""

import accounts as acct
import asana_client as ac
from conftest import envelope, error_payload, me_payload, task_payload


# --- token storage ----------------------------------------------------------

def test_tokens_are_one_per_line_deduplicated():
    raw = "tok-a\n\n tok-b \ntok-a\n"
    assert acct.split_tokens(raw) == ["tok-a", "tok-b"]


def test_a_trailing_newline_does_not_create_a_phantom_account():
    assert acct.split_tokens("only-one\n") == ["only-one"]


async def test_missing_secret_reads_as_no_tokens(ctx):
    assert await acct.load_tokens(ctx) == []


# --- verify before write ----------------------------------------------------

async def test_a_bad_token_is_never_stored(ctx, http):
    """THE bug this design prevents.

    Storing first and checking later is what made a bad paste feel like a
    silent failure: the value lands, the field clears, and the user only finds
    out later. Asana's verdict comes first, and nothing is written on rejection.
    """
    http.push(error_payload("Not Authorized"), status=401)
    out = await acct.add_token(ctx, "bad-token")
    assert out["ok"] is False
    assert out["code"] == ac.ASANA_TOKEN_REJECTED
    assert await acct.load_tokens(ctx) == []


async def test_an_empty_paste_is_rejected_without_a_request(ctx, http):
    out = await acct.add_token(ctx, "   ")
    assert out["code"] == ac.ASANA_TOKEN_MISSING
    assert http.calls == []


async def test_a_good_token_is_stored_and_names_its_workspaces(ctx, http):
    http.push(envelope(me_payload()))
    out = await acct.add_token(ctx, "good-token")
    assert out["ok"] is True
    assert out["user_name"] == "Vlad Ivanco"
    # "Acme" is the fixture default -- the point is that the workspace NAMES
    # come back, so the confirmation can say where the token actually reaches.
    assert "Acme" in out["workspace_names"]
    assert await acct.load_tokens(ctx) == ["good-token"]


async def test_describe_token_asks_for_the_workspace_fields(ctx, http):
    """Without opt_fields, every account looks like it has no workspaces.

    The compact user object carries only gid and name, so the workspace list has
    to be requested explicitly -- a missing field here is a missing REQUEST.
    """
    http.push(envelope(me_payload()))
    await acct.describe_token(ctx, "tok")
    fields = http.calls[0]["params"]["opt_fields"]
    assert "workspaces.gid" in fields
    assert "workspaces.name" in fields


async def test_connecting_a_second_account_appends(ctx, http):
    """A second PAT must not destroy the first."""
    http.push(envelope(me_payload()))
    await acct.add_token(ctx, "token-one")
    http.push(envelope(me_payload(name="Other Person",
                                 workspaces=[{"gid": "9", "name": "Client Co"}])))
    out = await acct.add_token(ctx, "token-two")
    assert out["already_connected"] is False
    assert await acct.load_tokens(ctx) == ["token-one", "token-two"]


async def test_reconnecting_the_same_token_is_not_a_duplicate(ctx, http):
    http.push(envelope(me_payload()))
    await acct.add_token(ctx, "same-token")
    http.push(envelope(me_payload()))
    out = await acct.add_token(ctx, "same-token")
    assert out["already_connected"] is True
    assert await acct.load_tokens(ctx) == ["same-token"]


async def test_a_write_failure_is_reported_without_the_value(ctx, http):
    class Boom(Exception):
        pass

    async def explode(name, value):
        raise Boom("secret backend said no")

    http.push(envelope(me_payload()))
    ctx.secrets.set = explode
    out = await acct.add_token(ctx, "some-token")
    assert out["code"] == ac.ASANA_SECRET_WRITE_FAILED
    assert "some-token" not in out["error"]


# --- one PAT, many workspaces ----------------------------------------------

async def test_one_token_yields_every_workspace_it_reaches(ctx, http):
    """The Asana-specific shape: one token, several workspaces."""
    http.push(envelope(me_payload(workspaces=[
        {"gid": "1", "name": "Personal Projects"},
        {"gid": "2", "name": "Blue Bee Web", "is_organization": True},
    ])))
    info = await acct.describe_token(ctx, "tok")
    assert [w["name"] for w in info["workspaces"]] == [
        "Personal Projects", "Blue Bee Web"]
    assert info["workspaces"][1]["is_organization"] is True


async def test_a_single_workspace_needs_no_naming(connected_ctx, http):
    """The common case must not demand a parameter."""
    http.push(envelope(me_payload(workspaces=[{"gid": "77", "name": "Solo"}])))
    picked = await acct.resolve_workspace(connected_ctx, "")
    assert picked["ok"] is True
    assert picked["workspace"]["gid"] == "77"


async def test_several_workspaces_without_a_name_refuses_to_guess(connected_ctx, http):
    """Picking one at random and then WRITING to it is unrecoverable."""
    http.push(envelope(me_payload(workspaces=[
        {"gid": "1", "name": "Personal"},
        {"gid": "2", "name": "Client Co"},
    ])))
    picked = await acct.resolve_workspace(connected_ctx, "")
    assert picked["ok"] is False
    assert picked["code"] == ac.ASANA_WORKSPACE_AMBIGUOUS
    # The error must LIST them, or the user cannot act on it.
    assert "Personal" in picked["error"] and "Client Co" in picked["error"]


async def test_a_named_workspace_is_matched_case_insensitively(connected_ctx, http):
    http.push(envelope(me_payload(workspaces=[
        {"gid": "1", "name": "Personal"},
        {"gid": "2", "name": "Blue Bee Web"},
    ])))
    picked = await acct.resolve_workspace(connected_ctx, "blue bee web")
    assert picked["ok"] is True
    assert picked["workspace"]["gid"] == "2"


async def test_an_unknown_workspace_name_lists_the_real_ones(connected_ctx, http):
    http.push(envelope(me_payload(workspaces=[{"gid": "1", "name": "Personal"}])))
    picked = await acct.resolve_workspace(connected_ctx, "Nope Ltd")
    assert picked["code"] == ac.ASANA_WORKSPACE_UNKNOWN
    assert "Personal" in picked["error"]


async def test_no_token_reports_missing_not_unknown(ctx):
    """"Paste a token" and "that workspace does not exist" are different fixes."""
    picked = await acct.resolve_workspace(ctx, "Anything")
    assert picked["code"] == ac.ASANA_TOKEN_MISSING


async def test_a_single_broken_token_reports_its_own_reason(connected_ctx, http):
    """A revoked token must not surface as "no workspace matches"."""
    http.push(error_payload("Not Authorized"), status=401)
    picked = await acct.resolve_workspace(connected_ctx, "")
    assert picked["code"] == ac.ASANA_TOKEN_REJECTED


async def test_one_broken_token_does_not_blank_the_account_list(ctx, http):
    """Two tokens, one dead: the good account must still be listed."""
    from imperal_sdk.testing import MockSecretStore

    ctx.secrets = MockSecretStore({"asana_tokens": "good\nbad"})
    http.push(envelope(me_payload()))
    http.push(error_payload("Not Authorized"), status=401)
    accounts = await acct.list_accounts(ctx, refresh=True)
    assert len(accounts) == 2
    assert accounts[0]["status"] == "ok"
    assert accounts[1]["status"] == "error"


# --- name-first targeting ---------------------------------------------------

async def test_typeahead_is_used_for_search(connected_ctx, http):
    """Typeahead works on every plan; advanced search is premium-only."""
    http.push(envelope([task_payload()]))
    out = await acct.typeahead(connected_ctx, "tok", "1", "landing",
                               resource_type="task")
    assert out["ok"] is True
    assert "typeahead" in http.calls[0]["url"]


async def test_resolve_target_refuses_to_guess_between_matches(connected_ctx, http):
    """Two same-named tasks must not be silently disambiguated."""
    http.push(envelope([
        task_payload(gid="1", name="Fix login"),
        task_payload(gid="2", name="Fix login"),
    ]))
    out = await acct.resolve_target(connected_ctx, "tok", "1", "Fix login",
                                    resource_type="task")
    assert out["ok"] is False
    assert out["code"] == ac.ASANA_TARGET_AMBIGUOUS


async def test_resolve_target_accepts_a_pasted_gid_without_searching(connected_ctx, http):
    """A gid from an Asana URL must keep working."""
    out = await acct.resolve_target(connected_ctx, "tok", "1", "1201234567890",
                                    resource_type="task")
    assert out["ok"] is True
    assert out["gid"] == "1201234567890"
    assert http.calls == []


async def test_resolve_target_reports_not_found_with_the_name(connected_ctx, http):
    http.push(envelope([]))
    out = await acct.resolve_target(connected_ctx, "tok", "1", "Ghost task",
                                    resource_type="task")
    assert out["code"] == ac.ASANA_TARGET_NOT_FOUND
    assert "Ghost task" in out["error"]
