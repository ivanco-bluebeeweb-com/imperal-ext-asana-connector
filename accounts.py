"""Account resolution: tokens -> accounts -> workspaces, and name -> gid lookup.

Two jobs, both about never making the user handle a gid.

1. An Asana personal access token belongs to a USER, and that user can be a
   member of several workspaces and organizations. So one token yields a LIST of
   workspaces, discovered from `/users/me` (which returns a `workspaces` array).
   This is the structural difference from the Notion connector, where one token
   meant exactly one workspace: here "which workspace" is a real choice inside a
   single token, so `resolve_workspace` flattens (token, workspace) pairs across
   every configured account before matching a name.

2. The spec requires name-first targeting: the user says "the Website Redesign
   project", not a gid. `resolve_target` uses Asana's TYPEAHEAD endpoint -- the
   only search available on every plan, since advanced search is premium-only --
   and refuses to guess when several things match, because silently picking one
   and then WRITING to it is the expensive kind of wrong.

Tokens live only in the Vault secret. The store caches account and workspace
NAMES and GIDS so the picker can render without hitting Asana; never a token.
"""

from __future__ import annotations

import asana_client as ac
import asana_objects as ao

ACCOUNTS_COLLECTION = "accounts"

SECRET_NAME = "asana_tokens"

# Asana's secret limit mirrors the platform default for a user-scoped secret.
MAX_SECRET_BYTES = 4096


def split_tokens(raw: str) -> list[str]:
    """One token per line, blanks dropped, duplicates removed.

    Blank lines and stray whitespace are tolerated: the user is pasting into a
    textarea, and a trailing newline should not create a phantom account.
    Deduplicated so the same token pasted twice does not show up as two
    identical accounts in the picker.
    """
    seen: set[str] = set()
    tokens: list[str] = []
    for line in (raw or "").splitlines():
        token = line.strip()
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


async def read_tokens(ctx) -> dict:
    """Read the configured tokens, distinguishing EMPTY from UNREADABLE.

    A bare `except: return []` would collapse two very different states into
    one: "the user has not connected yet" and "the secret store did not
    answer". The caller would then tell the user to paste a token -- useless
    advice if the store is simply unavailable, and it hides a real outage
    behind a setup message. Both states travel with their own code instead.
    """
    try:
        raw = await ctx.secrets.get(SECRET_NAME)
    except Exception as exc:
        # No plaintext can appear here: only the exception TYPE is recorded.
        return ac.fail(ac.ASANA_SECRET_UNAVAILABLE,
                       f"{ac.message_for(ac.ASANA_SECRET_UNAVAILABLE)} "
                       f"({type(exc).__name__})")
    return {"ok": True, "tokens": split_tokens(raw or "")}


async def load_tokens(ctx) -> list[str]:
    """Tokens only, for callers that treat unreadable as not-configured.

    Anything that needs to explain WHY there are no tokens should call
    `read_tokens` instead.
    """
    out = await read_tokens(ctx)
    return out.get("tokens", []) if out.get("ok") else []


async def describe_token(ctx, token: str) -> dict:
    """Identify the account behind one token via `/users/me`.

    `opt_fields` is required to get the workspace list: the default compact user
    object carries only gid/name, so without asking, every account would look
    like it had no workspaces at all.

    Returns a plain dict either way -- a bad token yields a describable entry
    (with its structured code) instead of an exception, so ONE broken token
    cannot blank out the whole account list.
    """
    out = await ac.request(
        ctx, "GET", "users/me", token,
        params={"opt_fields": "gid,name,email,workspaces.name,"
                              "workspaces.gid,workspaces.is_organization"})
    if not out.get("ok"):
        return {"ok": False, "code": out.get("code", ""),
                "error": out.get("error", "")}

    user = out["data"]
    if not isinstance(user, dict):
        return {"ok": False, "code": ac.ASANA_RESPONSE_UNEXPECTED,
                "error": ac.message_for(ac.ASANA_RESPONSE_UNEXPECTED)}

    workspaces = []
    for item in (user.get("workspaces") or []):
        if not isinstance(item, dict):
            continue
        workspaces.append({
            "gid": ao.gid_of(item),
            "name": ao.name_of(item),
            "is_organization": bool(item.get("is_organization")),
        })

    return {
        "ok": True,
        "user_gid": ao.gid_of(user),
        "user_name": ao.name_of(user) or "Asana user",
        "email": str(user.get("email") or ""),
        "workspaces": workspaces,
    }


async def add_token(ctx, token: str) -> dict:
    """Validate a token against Asana, then store it.

    Deliberately verify-BEFORE-write. A store-then-check flow is what makes a
    bad paste feel like a silent failure: the value lands, the panel clears, and
    the user only learns it was wrong the next time they ask for something. Here
    an unusable token is rejected with Asana's own reason and NOTHING is
    written, so the app never holds a credential it knows is broken.

    Appends rather than replaces: a user may hold separate PATs for a personal
    and a client account, and connecting the second must not silently destroy
    the first.
    """
    token = (token or "").strip()
    if not token:
        return ac.fail(ac.ASANA_TOKEN_MISSING,
                       "No token was entered. Paste a personal access token "
                       "from app.asana.com/0/my-apps.")

    # Asana's own verdict first -- identifies the account as a side effect.
    info = await describe_token(ctx, token)
    if not info.get("ok"):
        return ac.fail(info.get("code") or ac.ASANA_TOKEN_REJECTED,
                       info.get("error") or ac.message_for(ac.ASANA_TOKEN_REJECTED))

    existing = await read_tokens(ctx)
    if not existing.get("ok"):
        return existing
    tokens = existing["tokens"]

    workspace_names = [w["name"] for w in info.get("workspaces", []) if w.get("name")]

    if token in tokens:
        return {"ok": True, "already_connected": True,
                "user_name": info.get("user_name", ""),
                "email": info.get("email", ""),
                "workspace_names": workspace_names,
                "count": len(tokens)}

    combined = tokens + [token]
    payload = "\n".join(combined)
    if len(payload.encode("utf-8")) > MAX_SECRET_BYTES:
        return ac.fail(
            ac.ASANA_VALIDATION_FAILED,
            f"Adding this token would exceed the {MAX_SECRET_BYTES}-byte limit "
            f"for the stored value ({len(tokens)} already saved). Remove an "
            "unused token in the Secrets manager first.")

    try:
        await ctx.secrets.set(SECRET_NAME, payload)
    except Exception as exc:
        # Only the exception TYPE -- never the value -- is surfaced.
        return ac.fail(ac.ASANA_SECRET_WRITE_FAILED,
                       f"{ac.message_for(ac.ASANA_SECRET_WRITE_FAILED)} "
                       f"({type(exc).__name__})")

    # Drop the cached account list so the new one appears immediately instead
    # of after the cache happens to expire.
    await _forget_cache(ctx)

    return {"ok": True, "already_connected": False,
            "user_name": info.get("user_name", ""),
            "email": info.get("email", ""),
            "workspace_names": workspace_names,
            "count": len(combined)}


async def _forget_cache(ctx) -> None:
    """Clear cached account rows; failure here is not worth failing a save."""
    try:
        page = await ctx.store.query(ACCOUNTS_COLLECTION, limit=100)
        for doc in page.data:
            await ctx.store.delete(ACCOUNTS_COLLECTION, doc.id)
    except Exception:
        pass


async def _cache_account(ctx, entry: dict) -> None:
    """Upsert one account record. Cache failures are never fatal."""
    try:
        page = await ctx.store.query(ACCOUNTS_COLLECTION,
                                     where={"slot": entry["slot"]}, limit=1)
        if page.data:
            await ctx.store.update(ACCOUNTS_COLLECTION, page.data[0].id, entry)
        else:
            await ctx.store.create(ACCOUNTS_COLLECTION, entry)
    except Exception:
        pass


async def list_accounts(ctx, *, refresh: bool = False) -> list[dict]:
    """All configured accounts, in the order their tokens were entered.

    Cached in the store so panels stay fast; `refresh=True` re-reads from
    Asana. The cache key is the slot index, never the token.
    """
    tokens = await load_tokens(ctx)
    if not tokens:
        return []

    cached: dict[str, dict] = {}
    if not refresh:
        try:
            page = await ctx.store.query(ACCOUNTS_COLLECTION, limit=100)
            for doc in page.data:
                data = doc.data or {}
                slot = data.get("slot")
                if isinstance(slot, int):
                    cached[str(slot)] = data
        except Exception:
            cached = {}

    out: list[dict] = []
    for index, token in enumerate(tokens):
        hit = cached.get(str(index))
        if hit and hit.get("user_name"):
            entry = dict(hit)
            entry["slot"] = index
            out.append(entry)
            continue

        info = await describe_token(ctx, token)
        if not info.get("ok"):
            out.append({
                "slot": index,
                "user_name": f"Token #{index + 1} (not usable)",
                "user_gid": "",
                "email": "",
                "workspaces": [],
                "status": "error",
                "error": info.get("error", ""),
                "code": info.get("code", ""),
            })
            continue

        entry = {
            "slot": index,
            "user_name": info["user_name"],
            "user_gid": info["user_gid"],
            "email": info["email"],
            "workspaces": info["workspaces"],
            "status": "ok",
            "error": "",
            "code": "",
        }
        out.append(entry)
        await _cache_account(ctx, entry)

    return out


def flatten_workspaces(accounts: list[dict]) -> list[dict]:
    """Every (account, workspace) pair as one flat row.

    The picker and the resolver both need "all workspaces reachable by any
    configured token", which is a flatten rather than a lookup because the same
    workspace can legitimately appear under two accounts.
    """
    rows: list[dict] = []
    for account in accounts:
        if account.get("status") == "error":
            continue
        for workspace in account.get("workspaces") or []:
            rows.append({
                "slot": account.get("slot", 0),
                "account_name": account.get("user_name", ""),
                "gid": workspace.get("gid", ""),
                "name": workspace.get("name", ""),
                "is_organization": bool(workspace.get("is_organization")),
            })
    return rows


async def resolve_workspace(ctx, name: str = "") -> dict:
    """Pick the workspace to act in.

    No name + exactly one reachable workspace -> that one (the common case: a
    single personal workspace the user should never have to name). No name +
    several -> an error that LISTS them, because picking one at random and then
    writing to it is unrecoverable.
    """
    tokens = await load_tokens(ctx)
    if not tokens:
        return ac.fail(ac.ASANA_TOKEN_MISSING)

    accounts = await list_accounts(ctx)

    # A single configured token that does not work at all should report ITS
    # reason (revoked, rate-limited), not "no workspace matches".
    usable = [a for a in accounts if a.get("status") != "error"]
    if accounts and not usable:
        broken = accounts[0]
        return ac.fail(broken.get("code") or ac.ASANA_TOKEN_REJECTED,
                       broken.get("error") or ac.message_for(ac.ASANA_TOKEN_REJECTED))

    rows = flatten_workspaces(accounts)
    if not rows:
        return ac.fail(
            ac.ASANA_WORKSPACE_UNKNOWN,
            "This token reaches no Asana workspaces. That usually means the "
            "account was removed from its workspace or the token belongs to a "
            "deprovisioned user.")

    wanted = (name or "").strip().lower()

    if not wanted:
        if len(rows) == 1:
            row = rows[0]
            return {"ok": True, "token": tokens[row["slot"]], "workspace": row}
        names = ", ".join(r.get("name", "?") for r in rows)
        # AMBIGUOUS, not UNKNOWN: every one of these workspaces is perfectly
        # well known -- what is missing is the CHOICE between them. The two
        # codes lead to different next steps (name one vs. check your access),
        # so conflating them would send the user to fix the wrong thing.
        return ac.fail(
            ac.ASANA_WORKSPACE_AMBIGUOUS,
            f"Several Asana workspaces are reachable -- name the one to use: {names}.")

    exact = [r for r in rows if str(r.get("name", "")).strip().lower() == wanted]
    partial = [r for r in rows if wanted in str(r.get("name", "")).strip().lower()]
    matches = exact or partial

    if not matches:
        names = ", ".join(r.get("name", "?") for r in rows) or "-"
        return ac.fail(
            ac.ASANA_WORKSPACE_UNKNOWN,
            f"No reachable Asana workspace matches '{name}'. Reachable: {names}.")
    if len(matches) > 1:
        names = ", ".join(r.get("name", "?") for r in matches)
        return ac.fail(ac.ASANA_WORKSPACE_AMBIGUOUS,
                       f"'{name}' matches several workspaces: {names}.")

    row = matches[0]
    slot = row.get("slot", 0)
    if not isinstance(slot, int) or slot >= len(tokens):
        return ac.fail(ac.ASANA_WORKSPACE_UNKNOWN,
                       "That workspace's token is no longer configured.")
    return {"ok": True, "token": tokens[slot], "workspace": row}


# Resource types the typeahead endpoint accepts, mapped from the words a user
# would say. Asana validates this parameter, so an unmapped word must not be
# forwarded verbatim.
TYPEAHEAD_TYPES = {
    "task": "task",
    "project": "project",
    "user": "user",
    "tag": "tag",
    "portfolio": "portfolio",
    "team": "team",
}


async def typeahead(ctx, token: str, workspace_gid: str, query: str, *,
                    resource_type: str = "task", count: int = 20,
                    opt_fields: str = "") -> dict:
    """Asana's typeahead search inside one workspace.

    This is the search that works on EVERY plan. The richer
    `/workspaces/{gid}/tasks/search` endpoint is premium-only (402), so making
    it the default would mean the connector's most basic verb failed for
    free-plan users. Typeahead is explicitly documented as fast-but-approximate
    and returns a single unpaginated page, which is why callers that need
    completeness list by project instead.
    """
    kind = TYPEAHEAD_TYPES.get((resource_type or "task").strip().lower())
    if not kind:
        allowed = ", ".join(sorted(TYPEAHEAD_TYPES))
        return ac.fail(ac.ASANA_VALIDATION_FAILED,
                       f"'{resource_type}' is not a searchable Asana type. "
                       f"Use one of: {allowed}.")

    params: dict = {
        "resource_type": kind,
        # Asana caps typeahead at 100 and rejects anything larger.
        "count": max(1, min(100, count)),
    }
    if query:
        params["query"] = query
    if opt_fields:
        params["opt_fields"] = opt_fields

    out = await ac.request(ctx, "GET", f"workspaces/{workspace_gid}/typeahead",
                           token, params=params)
    if not out.get("ok"):
        return out
    results = out["data"]
    if not isinstance(results, list):
        return ac.fail(ac.ASANA_RESPONSE_UNEXPECTED,
                       "Asana returned an unexpected typeahead response.")
    return {"ok": True, "results": results}


async def resolve_target(ctx, token: str, workspace_gid: str, reference: str, *,
                         resource_type: str = "task") -> dict:
    """Resolve a task/project NAME (or a pasted gid) to a concrete object.

    Ambiguity is an error, not a coin flip: the caller may be about to complete
    or overwrite whatever comes back.
    """
    ref = (reference or "").strip()
    if not ref:
        return ac.fail(ac.ASANA_TARGET_NOT_FOUND,
                       f"No {resource_type} was named.")

    if ao.looks_like_gid(ref):
        return {"ok": True, "gid": ref, "name": "", "resolved_by": "gid"}

    found = await typeahead(ctx, token, workspace_gid, ref,
                            resource_type=resource_type, count=50)
    if not found.get("ok"):
        return found

    results = found["results"]
    if not results:
        return ac.fail(
            ac.ASANA_TARGET_NOT_FOUND,
            f"Nothing named '{ref}' was found in this workspace. Asana's "
            "typeahead only sees items the account has access to, and it "
            "matches on the start of words.")

    wanted = ref.lower()
    scored = [(ao.name_of(item), item) for item in results]
    exact = [(n, i) for n, i in scored if n.strip().lower() == wanted]
    candidates = exact or scored

    if len(candidates) > 1:
        shown = ", ".join(f"'{n or 'unnamed'}'" for n, _ in candidates[:5])
        return ac.fail(
            ac.ASANA_TARGET_AMBIGUOUS,
            f"'{ref}' matches {len(candidates)} items ({shown}). Use a more "
            "specific name or paste the Asana gid from the item's URL.")

    name, item = candidates[0]
    return {"ok": True, "gid": ao.gid_of(item), "name": name, "raw": item,
            "resolved_by": "name"}
