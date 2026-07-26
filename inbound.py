"""Inbound Asana webhooks: handshake, verify, de-duplicate, normalise.

The transport layer that turns Asana's push delivery into an Imperal event. It
is deliberately separate from the tools: a webhook runs with NO user in context
(`user_id="__webhook__"`), it must answer fast, and it is reachable by anyone on
the internet who knows the URL. None of that is true of a chat tool.

Asana differs from Slack in four ways that shape everything here.

1. THE SECRET IS NOT PASTED -- IT ARRIVES.
   Slack shows you a signing secret to copy. Asana instead POSTs a brand-new
   `X-Hook-Secret` to the endpoint the moment a webhook is created, and the
   creating API call BLOCKS until the endpoint echoes that header back. So the
   handshake is not a formality to get through -- it is the only moment the
   secret ever exists in transit, and the endpoint must both store it and
   reflect it, in a request that arrives while the create call is still open.

   That also means one shared secret per webhook, not per app: each stored
   alongside the resource it belongs to.

2. THE SIGNATURE HAS NO TIMESTAMP.
   Slack signs "v0:timestamp:body" and a five-minute window makes captured
   requests expire. Asana signs the body alone, so there is nothing to expire
   against -- a replay window is not available and pretending otherwise by
   inventing one would reject legitimate traffic. What remains is the part that
   matters: hash the RAW body exactly as received (re-serialising parsed JSON
   changes key order and whitespace, and every signature would fail), and
   compare with hmac.compare_digest so a wrong guess cannot be narrowed down
   one byte at a time.

3. HEARTBEATS ARE LOAD-BEARING.
   Asana pings every eight hours with an EMPTY events list, and if nothing
   answers for 24 hours it DELETES the webhook. So an empty delivery is not
   noise to skip -- answering it 200 is what keeps the subscription alive. A
   handler that quietly ignored empty payloads would work perfectly for a day
   and then go silent forever, which is the hardest kind of failure to
   attribute afterwards.

4. EVENTS ARE COMPACT.
   A delivery carries gids and an action, not the changed content. There is no
   "what did the text become" in the payload -- by design. So the emitted event
   carries identity and intent (what changed, which task, who did it) and the
   automation that reacts is expected to call get_task for the current state.
   Inventing richer-looking fields here would mean fabricating them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import asana_objects as ao

# --- constants ---------------------------------------------------------------

#: One row per established webhook: gid, the shared secret, the watched
#: resource. Asana issues a DIFFERENT secret per webhook, so this cannot be a
#: single app-wide secret the way Slack's signing secret is.
HOOKS_COLLECTION = "asana_webhook_secrets"

# Where a handshake secret waits between arriving and being claimed. The TTL is
# generous relative to a create call (seconds) but short enough that an
# abandoned secret cannot linger: an unclaimed row means the create failed.
PENDING_COLLECTION = "asana_pending_handshakes"
PENDING_TTL_SECONDS = 60 * 10

#: Processed delivery ids, for de-duplication.
EVENTS_COLLECTION = "asana_seen_events"

#: Asana retries a failed delivery with backoff for up to 24 hours, so an hour
#: of memory is not enough on its own -- but a redelivery after that long is
#: better handled as a fresh event than suppressed as a stale duplicate.
EVENT_LEDGER_TTL_SECONDS = 60 * 60 * 6

#: Emitted event types. MUST be app_id-prefixed: the SDK enforces a federal
#: cross-namespace block, so a bare "asana.task_changed" raises at import.
#: These are the names that appear in the automation rule builder.
EVENT_TASK_ADDED = "asana-connector.task_added"
EVENT_TASK_CHANGED = "asana-connector.task_changed"
EVENT_TASK_COMPLETED = "asana-connector.task_completed"
EVENT_TASK_DELETED = "asana-connector.task_deleted"
EVENT_COMMENT_ADDED = "asana-connector.comment_added"
EVENT_PROJECT_CHANGED = "asana-connector.project_changed"

#: Actions Asana can report. Anything outside this set is ignored rather than
#: forwarded: a new Asana action should be silent by default, not a surprise
#: broadcast into someone's automation.
KNOWN_ACTIONS = {"added", "changed", "removed", "deleted", "undeleted"}


# --- handshake ---------------------------------------------------------------

def handshake_secret(headers: dict) -> str:
    """The `X-Hook-Secret` Asana sends when establishing a webhook, if present.

    Presence of this header is what MAKES a request a handshake -- there is no
    other marker, and the body is empty either way.
    """
    lower = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    return lower.get("x-hook-secret", "").strip()


# --- signature verification --------------------------------------------------

def verify_signature(body: str, headers: dict, secret: str) -> dict:
    """Check Asana's HMAC-SHA256 signature over the RAW body.

    Returns {"ok": True} or {"ok": False, "code": ..., "reason": ...}. Reasons
    are for the audit log, never for the caller: a forged request must not be
    told which check it failed, or it learns how to pass the next one.
    """
    if not secret:
        return {"ok": False, "code": "ASANA_HOOK_SECRET_MISSING",
                "reason": "no stored secret for this webhook"}

    lower = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    signature = lower.get("x-hook-signature", "").strip()
    if not signature:
        return {"ok": False, "code": "ASANA_SIGNATURE_MISSING",
                "reason": "signature header absent"}

    # The RAW body, exactly as received. Re-serialising parsed JSON changes key
    # order and whitespace, so the digest would never match.
    digest = hmac.new(secret.encode(), (body or "").encode(),
                      hashlib.sha256).hexdigest()

    # compare_digest, not ==: a short-circuiting comparison leaks how many
    # leading bytes of a forged signature were correct.
    if not hmac.compare_digest(digest, signature):
        return {"ok": False, "code": "ASANA_SIGNATURE_INVALID",
                "reason": "signature mismatch"}
    return {"ok": True}


# --- payload parsing ---------------------------------------------------------

def parse_body(body: str) -> dict:
    """Parse a delivery body, tolerating the empty one a heartbeat sends."""
    text = (body or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def is_heartbeat(payload: dict) -> bool:
    """True when this delivery is Asana's keep-alive ping.

    Asana sends an empty `events` list every eight hours and DELETES the
    webhook if nothing answers for 24 hours. So this is not a case to skip
    quietly -- it is the case that keeps the subscription alive, and it must
    still be answered 200.
    """
    if not isinstance(payload, dict):
        return True
    events = payload.get("events")
    return not events if isinstance(events, list) else True


def delivery_id(headers: dict, body: str) -> str:
    """A stable id for one delivery, for de-duplication.

    Asana provides no delivery-id header, so the id is a hash of the body. Two
    genuinely identical bodies are the same delivery: every event carries a
    `created_at`, so distinct changes never collide.
    """
    lower = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    given = lower.get("x-hook-signature", "")
    basis = given or (body or "")
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


def event_type_for(event: dict) -> str:
    """Map one compact Asana event to an Imperal event type, or "" to ignore.

    Asana describes a change as (resource_type, action) and leaves the meaning
    to the reader. The mapping is an ALLOWLIST: an unrecognised combination
    returns "" and the event is dropped, because a new Asana resource type
    appearing in someone's automation unannounced is worse than missing it.
    """
    if not isinstance(event, dict):
        return ""
    resource = event.get("resource")
    resource = resource if isinstance(resource, dict) else {}
    kind = str(resource.get("resource_type") or "").strip()
    action = str(event.get("action") or "").strip()
    if action not in KNOWN_ACTIONS:
        return ""

    if kind == "task":
        if action == "added":
            return EVENT_TASK_ADDED
        if action == "changed":
            return EVENT_TASK_CHANGED
        if action in ("deleted", "removed"):
            return EVENT_TASK_DELETED
        return ""
    if kind == "story":
        # Only human comments. Asana's other story types are the activity log
        # ("moved this task", "assigned to"), and forwarding those would fire
        # an automation on every routine edit.
        subtype = str(resource.get("resource_subtype") or "").strip()
        if action == "added" and subtype == "comment_added":
            return EVENT_COMMENT_ADDED
        return ""
    if kind == "project" and action == "changed":
        return EVENT_PROJECT_CHANGED
    return ""


def normalise_event(event: dict, watched: dict | None = None) -> dict:
    """One compact Asana event -> the payload an automation rule receives.

    Deliberately does NOT invent content. Asana sends gids and an action, not
    the new text or the new due date, so the event carries identity and intent
    and the rule is expected to call get_task for current state. Fabricating a
    richer-looking payload here would mean guessing.
    """
    event = event if isinstance(event, dict) else {}
    resource = event.get("resource")
    resource = resource if isinstance(resource, dict) else {}
    parent = event.get("parent")
    parent = parent if isinstance(parent, dict) else {}
    user = event.get("user")
    user = user if isinstance(user, dict) else {}
    watched = watched or {}

    return {
        "action": str(event.get("action") or ""),
        "resource_type": str(resource.get("resource_type") or ""),
        "resource_gid": ao.gid_of(resource),
        "resource_name": ao.name_of(resource),
        "parent_gid": ao.gid_of(parent),
        "parent_name": ao.name_of(parent),
        "changed_field": str(event.get("change", {}).get("field") or "")
                         if isinstance(event.get("change"), dict) else "",
        "user_gid": ao.gid_of(user),
        "user_name": ao.name_of(user),
        "created_at": str(event.get("created_at") or ""),
        # Which subscription delivered this -- an automation watching two
        # projects otherwise cannot tell them apart.
        "webhook_gid": str(watched.get("webhook_gid") or ""),
        "watched_resource_gid": str(watched.get("resource_gid") or ""),
        "watched_resource_name": str(watched.get("resource_name") or ""),
        "workspace": str(watched.get("workspace") or ""),
    }


def completion_event(event: dict) -> bool:
    """True when a `changed` event is specifically a completion flip.

    "Task completed" is the single most useful trigger a project automation
    can have, and Asana does not send it as its own action -- it arrives as a
    `changed` event whose change.field is `completed`. Without this it would
    be indistinguishable from a typo fix.
    """
    if not isinstance(event, dict):
        return False
    change = event.get("change")
    if not isinstance(change, dict):
        return False
    return str(change.get("field") or "") == "completed"


# --- store access ------------------------------------------------------------

async def _upsert(ctx, collection: str, key: str, value: str,
                  data: dict) -> None:
    """Insert or update one row identified by `key == value`."""
    existing = None
    try:
        page = await ctx.store.query(collection, limit=100)
        for doc in (getattr(page, "data", None) or []):
            body = getattr(doc, "data", None) or {}
            if str(body.get(key) or "") == value:
                existing = getattr(doc, "id", "")
                break
    except Exception:
        existing = None

    if existing:
        await ctx.store.update(collection, existing, data)
    else:
        await ctx.store.create(collection, data)


async def remember_hook(ctx, webhook_gid: str, secret: str,
                        resource_gid: str = "", resource_name: str = "",
                        workspace: str = "") -> None:
    """Store the shared secret for one established webhook.

    Asana issues the secret ONCE, during the handshake, and never sends it
    again -- `GET /webhooks/{gid}` does not return it. Losing this row means
    every future delivery fails verification with no way to recover except
    deleting the webhook and creating a new one.
    """
    await _upsert(ctx, HOOKS_COLLECTION, "webhook_gid", webhook_gid, {
        "webhook_gid": webhook_gid,
        "secret": secret,
        "resource_gid": resource_gid,
        "resource_name": resource_name,
        "workspace": workspace,
        "at": time.time(),
    })


async def park_handshake(ctx, secret: str) -> None:
    """Hold a handshake secret that has no webhook gid yet.

    The ordering here is genuinely awkward and cannot be designed around:
    Asana POSTs the secret to this endpoint WHILE the create call is still
    open, so at the moment the secret arrives its own webhook gid does not
    exist anywhere yet -- the create call has not returned it.

    So the secret waits in a parking row keyed by the secret itself, and the
    tool that made the create call claims it once Asana answers with a gid.
    Parking rows are short-lived by construction: `claim_handshake` deletes
    the row it claims, and stale ones are swept on the next handshake rather
    than left to accumulate.
    """
    if not secret:
        return
    await _sweep_parked(ctx)
    await _upsert(ctx, PENDING_COLLECTION, "secret", secret, {
        "secret": secret,
        "at": time.time(),
    })


async def claim_handshake(ctx, offered: str = "") -> str:
    """Take the parked secret for a webhook that has just been created.

    `offered` is passed when the caller already knows which secret to expect.
    Otherwise the most recent parking row wins: a create call that has just
    returned is, in practice, the one whose handshake landed last.
    """
    rows = []
    try:
        page = await ctx.store.query(PENDING_COLLECTION, limit=50)
        for doc in (getattr(page, "data", None) or []):
            body = getattr(doc, "data", None) or {}
            if body.get("secret"):
                rows.append({**body, "_id": getattr(doc, "id", "")})
    except Exception:
        return ""
    if not rows:
        return ""

    if offered:
        rows = [r for r in rows if str(r.get("secret")) == offered] or rows
    rows.sort(key=lambda r: float(r.get("at") or 0), reverse=True)
    chosen = rows[0]

    try:
        await ctx.store.delete(PENDING_COLLECTION, chosen.get("_id") or "")
    except Exception:
        pass
    return str(chosen.get("secret") or "")


async def _sweep_parked(ctx) -> None:
    """Drop parking rows old enough that their create call cannot still be
    waiting. A secret nobody claimed is a create that failed."""
    try:
        page = await ctx.store.query(PENDING_COLLECTION, limit=50)
        rows = getattr(page, "data", None) or []
    except Exception:
        return
    cutoff = time.time() - PENDING_TTL_SECONDS
    for doc in rows:
        body = getattr(doc, "data", None) or {}
        if float(body.get("at") or 0) < cutoff:
            try:
                await ctx.store.delete(PENDING_COLLECTION,
                                       getattr(doc, "id", ""))
            except Exception:
                break


async def touch_hook(ctx, webhook_gid: str) -> None:
    """Record that a webhook delivered something.

    Asana deletes a webhook that goes 24 hours without a successful response,
    and the failure is completely silent from this side. A last-seen stamp is
    what lets `inbound_status` say "this subscription is alive" instead of
    "it exists", which are very different facts.
    """
    if not webhook_gid:
        return
    for row in await stored_hooks(ctx):
        if str(row.get("webhook_gid") or "") == webhook_gid:
            try:
                await ctx.store.update(HOOKS_COLLECTION, row.get("_id") or "",
                                       {**{k: v for k, v in row.items()
                                           if k != "_id"},
                                        "last_seen": time.time()})
            except Exception:
                pass
            return


async def stored_hooks(ctx) -> list[dict]:
    """Every stored webhook row."""
    try:
        page = await ctx.store.query(HOOKS_COLLECTION, limit=100)
    except Exception:
        return []
    rows = []
    for doc in (getattr(page, "data", None) or []):
        body = getattr(doc, "data", None) or {}
        if body.get("webhook_gid"):
            rows.append({**body, "_id": getattr(doc, "id", "")})
    return rows


async def forget_hook(ctx, webhook_gid: str) -> bool:
    """Drop a stored secret once its webhook is gone."""
    for row in await stored_hooks(ctx):
        if str(row.get("webhook_gid") or "") == webhook_gid:
            try:
                await ctx.store.delete(HOOKS_COLLECTION, row.get("_id") or "")
                return True
            except Exception:
                return False
    return False


async def match_secret(ctx, body: str, headers: dict) -> dict:
    """Find which stored webhook signed this delivery.

    Asana does NOT say which webhook a delivery belongs to -- there is no gid
    in the headers, only a signature. So the only honest way to identify the
    sender is to find the stored secret that reproduces the signature. That is
    also the verification: a delivery no stored secret can sign is a delivery
    this app did not subscribe to.
    """
    lower = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    signature = lower.get("x-hook-signature", "").strip()
    if not signature:
        return {"ok": False, "code": "ASANA_SIGNATURE_MISSING",
                "reason": "signature header absent"}

    for row in await stored_hooks(ctx):
        secret = str(row.get("secret") or "")
        if not secret:
            continue
        verdict = verify_signature(body, headers, secret)
        if verdict["ok"]:
            return {"ok": True, "hook": row}
    return {"ok": False, "code": "ASANA_SIGNATURE_INVALID",
            "reason": "no stored secret reproduces this signature"}


async def already_seen(ctx, event_id: str) -> bool:
    """Whether this delivery was already processed."""
    if not event_id:
        return False
    try:
        page = await ctx.store.query(EVENTS_COLLECTION, limit=200)
    except Exception:
        return False
    for doc in (getattr(page, "data", None) or []):
        body = getattr(doc, "data", None) or {}
        if str(body.get("event_id") or "") == event_id:
            return True
    return False


async def remember_event(ctx, event_id: str, kind: str = "") -> None:
    """Record a delivery id as processed. Never raises: dedupe is best-effort."""
    if not event_id:
        return
    try:
        await _upsert(ctx, EVENTS_COLLECTION, "event_id", event_id,
                      {"event_id": event_id, "at": time.time(), "kind": kind})
    except Exception:
        await ctx.log("event ledger could not be updated", "warn")


async def prune_ledger(ctx, limit: int = 200) -> int:
    """Drop ledger rows past their TTL.

    The store has no TTL of its own, so without this the collection grows for
    the life of the install. Called after a successful delivery rather than on
    a timer -- traffic is what creates the rows, so traffic is the honest
    moment to clean them up.
    """
    removed = 0
    try:
        page = await ctx.store.query(EVENTS_COLLECTION, limit=limit)
        rows = getattr(page, "data", None) or []
    except Exception:
        return 0
    cutoff = time.time() - EVENT_LEDGER_TTL_SECONDS
    for doc in (rows or [])[:limit]:
        data = getattr(doc, "data", None) or {}
        stamped = float(data.get("at") or 0)
        if stamped and stamped < cutoff:
            try:
                await ctx.store.delete(EVENTS_COLLECTION, getattr(doc, "id", ""))
                removed += 1
            except Exception:
                break
    return removed
