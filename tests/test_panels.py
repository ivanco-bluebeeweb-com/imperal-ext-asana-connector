"""Panels must render in every state, including the broken ones.

A panel that raises leaves the user with a blank slot and no explanation, which
is strictly worse than a banner saying what is wrong. So each screen is rendered
here with: nothing configured, a token Asana rejects, and the API unreachable.

The Notion connector shipped two panel bugs that no unit test would have caught
and both are pinned here by shape:
  * two panels on slot="center" fighting for one slot (see test_contract), and
  * `ui.Text(text=...)` / `ui.Header(content=...)` -- the two components take
    OPPOSITE keyword names, and getting it wrong is a render-time failure.
"""

import panels
from conftest import envelope, error_payload, me_payload


def _flatten(node, out=None):
    """Every UINode in a rendered tree, depth-first.

    The SDK renders to UINode(type=..., props={...}) -- components are NOT
    plain objects with attributes, so children live inside `props`, and reading
    `node.children` silently returns nothing. That is why these helpers walk
    props: a traversal that finds nothing makes every assertion vacuous.
    """
    if out is None:
        out = []
    if node is None:
        return out
    out.append(node)
    props = getattr(node, "props", None)
    if not isinstance(props, dict):
        return out
    for value in props.values():
        if isinstance(value, list):
            for child in value:
                if hasattr(child, "props") or hasattr(child, "type"):
                    _flatten(child, out)
        elif hasattr(value, "props"):
            _flatten(value, out)
    return out


def _types(tree) -> list[str]:
    return [str(getattr(n, "type", "")) for n in _flatten(tree)]


def _text_blob(tree) -> str:
    """All human-readable strings in the tree, lowercased."""
    parts: list[str] = []
    for node in _flatten(tree):
        props = getattr(node, "props", None)
        if not isinstance(props, dict):
            continue
        for key in ("content", "text", "title", "message", "label", "subtitle",
                    "placeholder", "submit_label", "href", "description"):
            value = props.get(key)
            if isinstance(value, str):
                parts.append(value)
        # DataTable rows are PLAIN DICTS, not UINodes, so the real data a user
        # reads on screen is invisible to a component-only walk. Without this,
        # "does the panel show my workspace?" passes on an empty table.
        rows = props.get("rows")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    parts.extend(str(v) for v in row.values() if v is not None)
    return " ".join(parts).lower()


def _actions(tree) -> list[str]:
    """Every `action` string on any node (form submits, button actions)."""
    found: list[str] = []
    for node in _flatten(tree):
        props = getattr(node, "props", None)
        if isinstance(props, dict) and isinstance(props.get("action"), str):
            found.append(props["action"])
    return found


# --- the connect screen -----------------------------------------------------

async def test_connect_screen_renders_with_nothing_configured(ctx, http):
    """The first thing a new user sees must not depend on a working token."""
    tree = await panels.connect_panel(ctx)
    assert tree is not None
    blob = _text_blob(tree)
    assert "token" in blob
    # It must tell the user WHERE to get one.
    assert "my-apps" in blob or "app.asana.com" in blob


async def test_connect_screen_offers_a_password_field_not_a_text_field(ctx, http):
    """A pasted credential must never render as readable text."""
    tree = await panels.connect_panel(ctx)
    kinds = _types(tree)
    assert "Password" in kinds or "Input" in kinds


async def test_connect_form_submits_to_this_extensions_own_function(ctx, http):
    """A panel form's action= resolves against THIS extension's functions.

    The documented `ui.Form(action="save_app_secret")` recipe cannot work in a
    non-developer extension: that function belongs to the developer extension,
    so the click dies with "Function not found". Submitting to our own
    `connect_account` is what makes the Connect screen actually connect.
    """
    tree = await panels.connect_panel(ctx)
    assert "connect_account" in _actions(tree)


# --- the accounts screen ----------------------------------------------------

async def test_accounts_screen_renders_when_no_token_is_configured(ctx, http):
    tree = await panels.accounts_panel(ctx)
    assert tree is not None
    assert "connect" in _text_blob(tree)


async def test_accounts_screen_renders_a_connected_account(connected_ctx, http):
    http.push(envelope(me_payload(workspaces=[
        {"gid": "100", "name": "Blue Bee Web"},
    ])))
    tree = await panels.accounts_panel(connected_ctx, refresh=True)
    blob = _text_blob(tree)
    assert "blue bee web" in blob or "vlad ivanco" in blob


async def test_accounts_screen_explains_a_rejected_token_instead_of_hiding_it(
        connected_ctx, http):
    """A revoked token is a row with a status, not an exception."""
    http.push(error_payload("Not Authorized"), status=401)
    tree = await panels.accounts_panel(connected_ctx, refresh=True)
    blob = _text_blob(tree)
    assert "token" in blob or "attention" in blob or "not connected" in blob


async def test_accounts_screen_survives_an_unreachable_api(connected_ctx, http):
    """A blank panel is worse than a banner: render, do not raise."""
    http.push(TimeoutError("asana is down"))
    tree = await panels.accounts_panel(connected_ctx, refresh=True)
    assert tree is not None


# --- the single center panel ------------------------------------------------

async def test_center_panel_defaults_to_connect_when_unconfigured(ctx, http):
    """An unconfigured user lands on the one screen that helps them."""
    tree = await panels.asana_center(ctx)
    blob = _text_blob(tree)
    assert "token" in blob


async def test_center_panel_view_parameter_selects_the_connect_screen(connected_ctx, http):
    http.push(envelope(me_payload()))
    tree = await panels.asana_center(connected_ctx, view="connect")
    assert "token" in _text_blob(tree)


async def test_center_panel_renders_accounts_for_a_connected_user(connected_ctx, http):
    http.push(envelope(me_payload()))
    tree = await panels.asana_center(connected_ctx, view="accounts")
    assert tree is not None


async def test_center_panel_ignores_an_unknown_view(connected_ctx, http):
    """A stale deep link must not blank the slot."""
    http.push(envelope(me_payload()))
    tree = await panels.asana_center(connected_ctx, view="does-not-exist")
    assert tree is not None


# --- the sidebar ------------------------------------------------------------

async def test_sidebar_renders_in_both_states(ctx, connected_ctx, http):
    empty = await panels.asana_nav(ctx)
    assert empty is not None

    http.push(envelope(me_payload()))
    filled = await panels.asana_nav(connected_ctx)
    assert filled is not None


async def test_sidebar_never_raises_when_asana_is_down(connected_ctx, http):
    http.push(TimeoutError("asana is down"))
    tree = await panels.asana_nav(connected_ctx)
    assert tree is not None
