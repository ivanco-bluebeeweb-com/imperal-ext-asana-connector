"""Static contract sweep over the whole app.

The WP Publisher incident is why this file exists: errors were emitted without a
structured `code`, the kernel stamped EXT_UNSTRUCTURED_ERROR, and no validator
rule caught it because the app routed through a local helper instead of calling
ActionResult.error directly. Validator rule V32 matches the literal call, so it
stayed silent.

A test that walks the AST does not care which helper is used -- it checks every
error path in the source, so the same class of bug cannot come back quietly.

The panel tests below encode the OTHER bug we paid for: two panels declared on
slot="center" fight over one slot, one silently replaces the other, and every
button dispatching the loser looks broken. That is invisible in any single-panel
test and invisible to the validator -- only a slot MAP shows it.
"""

import ast
import pathlib

APP_DIR = pathlib.Path(__file__).resolve().parent.parent
HANDLER_FILES = ["handlers_read.py", "handlers_write.py",
                 # Added with the webhook work: a file missing from this list
                 # is silently exempt from every structural check here, which
                 # is the same "declared but never verified" shape these tests
                 # exist to catch.
                 "handlers_inbound.py", "inbound.py", "shared.py",
                 "accounts.py", "asana_client.py", "panels.py"]


def _tree(name: str) -> ast.AST:
    return ast.parse((APP_DIR / name).read_text())


def _calls(tree: ast.AST, *names: str):
    """Every Call node whose callee is one of `names` (attribute or plain)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        label = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if label in names:
            yield node


# --- error codes ------------------------------------------------------------

def test_every_actionresult_error_carries_a_structured_code():
    """No ActionResult.error() anywhere without an explicit code=."""
    offenders = []
    for name in HANDLER_FILES:
        for call in _calls(_tree(name), "error"):
            fn = call.func
            # Only ActionResult.error(...) -- not ctx.log.error(...)
            if not (isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id == "ActionResult"):
                continue
            if not any(kw.arg == "code" for kw in call.keywords):
                offenders.append(f"{name}:{call.lineno}")
    assert not offenders, (
        "ActionResult.error() without code= -- the kernel will stamp "
        f"EXT_UNSTRUCTURED_ERROR: {offenders}")


def test_error_codes_match_the_platform_pattern():
    """Every declared code matches ^[A-Z][A-Z0-9_]{2,63}$."""
    import re
    import asana_client as ac

    pattern = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
    # ASANA_API holds a URL, not a code -- the codes are the constants whose
    # value equals their own name, which is how they stay greppable.
    codes = [n for n in dir(ac)
             if n.startswith("ASANA_") and getattr(ac, n) == n]
    assert len(codes) > 10, f"expected the full code table, found {codes}"
    bad = [f"{n}={getattr(ac, n)}" for n in codes
           if not pattern.match(getattr(ac, n))]
    assert not bad, f"codes rejected by the platform pattern: {bad}"


def test_error_helper_requires_code_positionally():
    """shared.error must keep `code` as a REQUIRED positional parameter.

    That signature is the whole defence: forgetting a code becomes a TypeError
    at authoring time instead of a silent EXT_UNSTRUCTURED_ERROR downgrade in
    production. A default value here would quietly reopen the WP Publisher bug.
    """
    import inspect
    import shared

    sig = inspect.signature(shared.error)
    code = sig.parameters["code"]
    assert code.default is inspect.Parameter.empty, (
        "shared.error(code=...) gained a default -- a code-less error would "
        "stop being a TypeError and start being a production downgrade")


# --- credential safety ------------------------------------------------------

def test_no_user_facing_text_interpolates_a_token():
    """A token must never reach a message or a log line.

    WP Publisher leaked `{type(e).__name__}: {e}` and a traceback into
    user-facing prose. Credentials are the version of that mistake that cannot
    be walked back.

    A regex over the source is useless here: `f"Bearer {token}"` in the auth
    header is REQUIRED, and `len(tokens)` is a count. So this walks the AST and
    only inspects f-strings actually passed to an error result or a log call --
    the two places text reaches a human.
    """
    offenders = []
    for name in HANDLER_FILES:
        tree = _tree(name)
        for call in _calls(tree, "error", "log", "warning", "info"):
            fn = call.func
            # Skip the auth header helper and anything not text-bound.
            for arg in list(call.args) + [kw.value for kw in call.keywords]:
                if not isinstance(arg, ast.JoinedStr):
                    continue
                for piece in arg.values:
                    if not isinstance(piece, ast.FormattedValue):
                        continue
                    for sub in ast.walk(piece):
                        if isinstance(sub, ast.Name) and sub.id in ("token", "tokens", "raw"):
                            offenders.append(f"{name}:{call.lineno}")
    assert not offenders, (
        f"a token is interpolated into user-facing text: {offenders}")


def test_auth_header_is_the_only_place_a_token_is_formatted():
    """The single legitimate token interpolation lives in auth_headers."""
    import asana_client as ac
    import inspect

    source = inspect.getsource(ac)
    hits = [ln.strip() for ln in source.splitlines()
            if "{token}" in ln and not ln.strip().startswith("#")]
    assert hits == ['"Authorization": f"Bearer {token}",'], (
        f"token formatted outside the auth header: {hits}")


# --- panel/slot structure ---------------------------------------------------

def _panel_decorators() -> list[dict]:
    """Every @ext.panel(...) declaration in panels.py, as plain dicts."""
    out = []
    tree = _tree("panels.py")
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            fn = dec.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "panel"):
                continue
            info = {"func": node.name, "lineno": dec.lineno, "id": None,
                    "slot": None, "center_overlay": False}
            if dec.args and isinstance(dec.args[0], ast.Constant):
                info["id"] = dec.args[0].value
            for kw in dec.keywords:
                if kw.arg == "slot" and isinstance(kw.value, ast.Constant):
                    info["slot"] = kw.value.value
                if kw.arg == "center_overlay" and isinstance(kw.value, ast.Constant):
                    info["center_overlay"] = bool(kw.value.value)
            out.append(info)
    return out


def test_at_most_one_panel_per_slot():
    """THE slot-collision test.

    A center slot holds exactly ONE panel with REPLACE semantics. Two panels
    declaring slot="center" both load at session init and one silently wins,
    which is what made "Check what is reachable" look dead while the shell
    re-rendered.
    """
    panels = _panel_decorators()
    by_slot: dict[str, list[str]] = {}
    for panel in panels:
        by_slot.setdefault(panel["slot"] or "?", []).append(
            f"{panel['id']} ({panel['func']}:{panel['lineno']})")
    clashes = {slot: owners for slot, owners in by_slot.items() if len(owners) > 1}
    assert not clashes, (
        "more than one panel claims a slot -- they will replace each other: "
        f"{clashes}")


def test_center_panels_declare_center_overlay():
    """slot="center" REQUIRES center_overlay=True or it is never fetched."""
    bad = [p for p in _panel_decorators()
           if p["slot"] == "center" and not p["center_overlay"]]
    assert not bad, f"center panel without center_overlay=True: {bad}"


def test_every_panel_call_target_exists():
    """Each ui.Call("__panel__x") names a panel this app really declares."""
    declared = {p["id"] for p in _panel_decorators()}
    missing = []
    for name in ("panels.py", "handlers_read.py", "handlers_write.py"):
        for call in _calls(_tree(name), "Call"):
            if not call.args or not isinstance(call.args[0], ast.Constant):
                continue
            target = call.args[0].value
            if not isinstance(target, str) or not target.startswith("__panel__"):
                continue
            panel_id = target[len("__panel__"):]
            if panel_id not in declared:
                missing.append(f"{name}:{call.lineno} -> {target}")
    assert not missing, (
        f"ui.Call targets a panel that does not exist: {missing}")


def test_every_refresh_panels_name_exists():
    """refresh_panels=[...] must name real panels, or it refreshes nothing."""
    declared = {p["id"] for p in _panel_decorators()}
    missing = []
    for name in ("handlers_read.py", "handlers_write.py"):
        tree = _tree(name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.keyword) or node.arg != "refresh_panels":
                continue
            if not isinstance(node.value, (ast.List, ast.Tuple)):
                continue
            for item in node.value.elts:
                if isinstance(item, ast.Constant) and item.value not in declared:
                    missing.append(f"{name}: {item.value}")
    assert not missing, f"refresh_panels names an unknown panel: {missing}"


def test_ui_text_uses_content_not_text():
    """ui.Text takes content=; ui.Header takes text=.

    Mixing them up is what got the first Notion deploy rejected at 16/20 while
    the local validator reported zero errors, because it does not check DUI
    component props.
    """
    offenders = []
    for call in _calls(_tree("panels.py"), "Text"):
        fn = call.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "Text"):
            continue
        if any(kw.arg == "text" for kw in call.keywords):
            offenders.append(f"panels.py:{call.lineno}")
    assert not offenders, f"ui.Text(text=...) -- must be content=: {offenders}"


# --- Asana API shape --------------------------------------------------------

def test_task_updates_use_put_not_patch():
    """Asana updates tasks with PUT; there is no PATCH route.

    Sending PATCH answers 404, which reads like "no such task" and sends the
    user hunting for a task that is right there.
    """
    source = (APP_DIR / "handlers_write.py").read_text()
    assert '"PATCH"' not in source, (
        "PATCH used against Asana -- task/project updates must use PUT")


def test_handlers_never_wrap_their_own_data_envelope():
    """The `{"data": ...}` write envelope belongs to request(), once."""
    # Docstrings EXPLAIN the envelope, so a raw text scan matches its own
    # documentation. Only real code counts: strip every string literal first.
    tree = _tree("handlers_write.py")
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key in node.keys:
            if isinstance(key, ast.Constant) and key.value == "data":
                offenders.append(f"handlers_write.py:{node.lineno}")
    assert not offenders, (
        "a handler wraps its own body in data -- request() already does that: "
        f"{offenders}")


# --- ctx surface ------------------------------------------------------------

def test_handlers_only_call_ctx_methods_that_exist():
    """No invented ctx APIs.

    This file once called `await ctx.emit(...)` in all eight write tools. It
    reads perfectly and it does not exist: the SDK has no `ctx.emit`, events are
    declared with `event=` on the decorator and published by the KERNEL. Every
    write tool raised AttributeError at the last line before returning -- after
    the task had already been created in Asana. That is the worst shape of bug:
    the side effect lands, the user sees a crash.
    """
    from imperal_sdk.testing import MockContext

    real = {a for a in dir(MockContext()) if not a.startswith("_")}
    used: dict[str, int] = {}
    for name in ("handlers_read.py", "handlers_write.py", "accounts.py",
                 "shared.py", "asana_client.py", "panels.py"):
        for node in ast.walk(_tree(name)):
            if not isinstance(node, ast.Attribute):
                continue
            if isinstance(node.value, ast.Name) and node.value.id == "ctx":
                used.setdefault(node.attr, 0)
                used[node.attr] += 1

    # `secrets` is INJECTED at runtime by the kernel rather than declared on
    # Context, so it is absent from both the model fields and MockContext --
    # yet it is the documented way to read a Vault secret and it is what the
    # live Notion connector uses in production. Allowed explicitly, so the
    # check stays strict about everything else.
    runtime_injected = {"secrets"}

    unknown = sorted(a for a in used if a not in real | runtime_injected)
    assert not unknown, (
        f"these ctx attributes do not exist on the real Context: {unknown}")


def test_events_are_declared_on_the_decorator_not_emitted_by_hand():
    """The declarative route is the only one that works."""
    write_source = (APP_DIR / "handlers_write.py").read_text()
    assert "ctx.emit" not in write_source
    assert write_source.count("event=\"asana-connector.") >= 8


# --- params/model agreement -------------------------------------------------

def test_handlers_only_read_params_fields_that_exist_on_the_model():
    """Every `params.X` must be a real field of that tool's model.

    This caught FOUR live bugs in one sitting: handlers read
    `params.include_completed`, `params.query` (on the advanced-search model),
    `params.parent_task` and `params.comments_only`, while the models declared
    `completed`, `text`, `parent` and `include_activity`. Each one is an
    AttributeError on the user's first call -- invisible to the type checker,
    invisible to the validator, and invisible to any test that does not actually
    invoke the tool.

    Mapping the model per function is what makes this precise: `params` means a
    different class in every handler.
    """
    import importlib

    import models

    offenders: list[str] = []
    for module_name in ("handlers_read", "handlers_write"):
        module = importlib.import_module(module_name)
        tree = _tree(f"{module_name}.py")

        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue

            # The params annotation names the model class for THIS handler.
            model = None
            for arg in node.args.args:
                if arg.arg != "params" or arg.annotation is None:
                    continue
                ann = arg.annotation
                name = ann.id if isinstance(ann, ast.Name) else getattr(ann, "attr", "")
                model = getattr(models, name, None)
            if model is None or not hasattr(model, "model_fields"):
                continue

            allowed = set(model.model_fields)
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Attribute)
                        and isinstance(sub.value, ast.Name)
                        and sub.value.id == "params"
                        and sub.attr not in allowed):
                    offenders.append(
                        f"{module_name}.{node.name}: params.{sub.attr} "
                        f"not on {model.__name__}")

    assert not offenders, "handlers read params fields that do not exist: " + \
        "; ".join(offenders)


def test_entities_are_built_only_from_fields_they_declare():
    """No silently dropped data.

    pydantic IGNORES unknown keyword fields instead of raising, so a handler can
    hand an entity five fields it never declared and get a perfectly valid,
    perfectly empty object back. That is what `check_access` did in production:
    workspace_name / projects_visible / people_visible / explanation all
    vanished, and the only reason anyone noticed is that `premium_search` got a
    bool where the model declares a str -- a type mismatch is loud, a name
    mismatch is silent.

    `AsanaTask` had the same hole: `start` and `parent` were fetched from Asana,
    passed by the handler, and dropped on the floor of every single response.

    This is the mirror of test_handlers_only_read_params_fields_that_exist:
    that one guards READS off the params model, this one guards WRITES into the
    entity models.
    """
    import models

    offenders: list[str] = []
    for module_name in ("handlers_read.py", "handlers_write.py", "panels.py"):
        for node in ast.walk(_tree(module_name)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            model = getattr(models, name, None)
            if model is None or not hasattr(model, "model_fields"):
                continue
            allowed = set(model.model_fields)
            for keyword in node.keywords:
                if keyword.arg and keyword.arg not in allowed:
                    offenders.append(
                        f"{module_name}:{node.lineno} {name}(... {keyword.arg}=)")

    assert not offenders, (
        "these entity fields do not exist and would be silently dropped: "
        + "; ".join(offenders))


def test_declared_entity_fields_are_actually_populated():
    """A field nobody fills is a promise the chain cannot cash.

    The mirror of the previous test: that one catches fields passed but NOT
    declared (silently dropped), this one catches fields declared but NEVER
    passed (silently empty). Both look fine until something downstream reads
    them.

    `name` on AsanaTask/AsanaProject and `name`/`action` on WriteResult were
    exactly this: the card renders from `title`, so a human saw the right
    thing, while a chained tool reading `.name` got "".

    Pagination fields (`page`, `has_more`) are excluded -- their defaults are
    the correct answer for a single unpaged response.
    """
    import models

    base_fields = {"id", "title", "kind", "subtitle", "description",
                   "status", "url"}
    pagination = {"page", "has_more", "total", "items"}

    offenders: list[str] = []
    for module_name in ("handlers_read.py", "handlers_write.py"):
        for node in ast.walk(_tree(module_name)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            model = getattr(models, name, None)
            if model is None or not hasattr(model, "model_fields"):
                continue
            passed = {kw.arg for kw in node.keywords if kw.arg}
            missing = set(model.model_fields) - passed - base_fields - pagination
            if missing:
                offenders.append(
                    f"{module_name}:{node.lineno} {name} never fills {sorted(missing)}")

    assert not offenders, (
        "declared but never populated: " + "; ".join(offenders))
