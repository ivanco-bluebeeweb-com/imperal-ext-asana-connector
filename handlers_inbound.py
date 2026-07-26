"""The Asana webhook endpoint, and the tools that manage subscriptions.

TRANSPORT SHAPE. The platform routes
``POST /v1/ext/asana-connector/webhook/events`` here. The handler receives the
raw body as a STRING, the request headers, and a context with NO user
(``user_id="__webhook__"``), because nobody is logged in when Asana pushes.

WHY THE HANDSHAKE IS ANSWERED FIRST. Asana creates a webhook by POSTing a
brand-new ``X-Hook-Secret`` to the target URL and BLOCKING the create call
until that header is echoed back. At that instant there is no stored secret to
verify against -- the request IS the secret arriving. Checking the signature
before the handshake would therefore reject the one delivery that establishes
every future delivery, and webhook creation would fail 100% of the time with a
timeout that looks like a network problem.

WHY AN EMPTY DELIVERY IS NOT SKIPPED. Asana pings every eight hours with an
empty events list and DELETES the webhook if nothing answers for 24 hours. So
answering the heartbeat 200 is not politeness, it is what keeps the
subscription alive -- a handler that quietly ignored empty payloads would work
perfectly for a day and then go dead with no error anywhere.

WHY EVERYTHING ELSE RETURNS 200. Asana retries a non-2xx for up to 24 hours.
Retrying a delivery this app has decided to ignore (a duplicate, a story it
does not care about) just repeats the same decision. "Accepted and dropped" is
200. The single exception is a failed signature check: that is not an Asana
delivery at all, and refusing it is the correct final answer.
"""

from __future__ import annotations

from imperal_sdk import ActionResult
from imperal_sdk.types import WebhookResponse

import asana_client as ac
import asana_objects as ao
import inbound
import shared
from app import chat, ext
from models import (
    AsanaWebhook,
    AsanaWebhookList,
    InboundStatus,
    ListWebhooksParams,
    UnwatchParams,
    WatchProjectParams,
)

_error = shared.error
_from_envelope = shared.from_envelope
_resolve = shared.resolve


# --------------------------- the events endpoint ---------------------------

@ext.emits(inbound.EVENT_TASK_ADDED)
@ext.emits(inbound.EVENT_TASK_CHANGED)
@ext.emits(inbound.EVENT_TASK_COMPLETED)
@ext.emits(inbound.EVENT_TASK_DELETED)
@ext.emits(inbound.EVENT_COMMENT_ADDED)
@ext.emits(inbound.EVENT_PROJECT_CHANGED)
@ext.webhook("events", method="POST")
async def asana_events(ctx, headers: dict | None = None, body: str = "",
                       query_params: dict | None = None):
    """Receive one Asana webhook delivery.

    Returns a WebhookResponse rather than a bare string, because the handshake
    reply lives in a HEADER -- a body-only response cannot establish a webhook
    at all.
    """
    headers = headers or {}

    # 1. HANDSHAKE. Answered before anything else: this request carries the
    #    secret that all later verification depends on, so there is nothing to
    #    verify it against yet. The secret is parked, not filed -- the tool
    #    that created the webhook learns its gid only when the create call
    #    returns, which happens AFTER this response is sent.
    offered = inbound.handshake_secret(headers)
    if offered:
        await inbound.park_handshake(ctx, offered)
        await ctx.log("Asana webhook handshake answered", level="info")
        return WebhookResponse(
            status_code=200, body="",
            headers={"X-Hook-Secret": offered})

    # 2. SIGNATURE. Asana does not name the webhook a delivery belongs to, so
    #    finding the stored secret that reproduces the signature is both the
    #    identification and the verification in one step.
    verdict = await inbound.match_secret(ctx, body, headers)
    if not verdict["ok"]:
        # The reason is logged, never returned: telling an unauthenticated
        # caller WHY their signature failed helps them forge a better one.
        await ctx.log(f"Asana delivery rejected: {verdict['code']}",
                      level="warn")
        return WebhookResponse(status_code=401, body="unauthorised")

    hook = verdict.get("hook") or {}
    payload = inbound.parse_body(body)

    # 3. HEARTBEAT. An empty events list is Asana checking the endpoint is
    #    alive. Answering 200 is what stops it deleting the subscription.
    if inbound.is_heartbeat(payload):
        await inbound.touch_hook(ctx, str(hook.get("webhook_gid") or ""))
        return WebhookResponse(status_code=200, body="ok")

    events = payload.get("events")
    if not isinstance(events, list):
        return WebhookResponse(status_code=200, body="ignored")

    emitted = 0
    for raw in events:
        if not isinstance(raw, dict):
            continue

        event_name = inbound.event_type_for(raw)
        if not event_name:
            # A story subtype this connector does not model, or a resource
            # type it does not watch. Dropped deliberately, not by accident.
            continue

        # 4. DEDUPE. Asana is at-most-once in theory, but a retry after a slow
        #    response is real, and a duplicate "task completed" firing an
        #    automation twice is the most visible way this looks broken.
        event_id = inbound.delivery_id(headers, str(raw))
        if event_id and await inbound.already_seen(ctx, event_id):
            continue

        normalised = inbound.normalise_event(raw, watched=hook)

        # The ledger is written BEFORE emitting: if the emit throws, a retry
        # finds the event recorded and drops it. Losing one event is a smaller
        # failure than firing the same automation twice.
        if event_id:
            await inbound.remember_event(ctx, event_id, kind=event_name)

        try:
            await ctx.extensions.emit(event_name, normalised)
            emitted += 1
        except Exception:
            await ctx.log(f"could not emit {event_name}", level="warn")

        # A completed task is ALSO a change, but an automation that wants
        # "when something is finished" should not have to filter every edit.
        if inbound.completion_event(raw):
            try:
                await ctx.extensions.emit(inbound.EVENT_TASK_COMPLETED,
                                          normalised)
                emitted += 1
            except Exception:
                await ctx.log("could not emit task_completed", level="warn")

    await inbound.touch_hook(ctx, str(hook.get("webhook_gid") or ""))

    # The store has no TTL, so the ledger is pruned after a real delivery --
    # the only moment that creates rows.
    try:
        await inbound.prune_ledger(ctx)
    except Exception:
        pass

    await ctx.log(
        f"Asana delivery: {len(events)} event(s) in, {emitted} emitted",
        level="info")
    return WebhookResponse(status_code=200, body="ok")


# --------------------------- subscription tools ----------------------------

@chat.function(
    "watch_project",
    "Get notified when a project changes -- new tasks, completions, comments. "
    "Sets up a live subscription so automations can react without polling.",
    action_type="write", chain_callable=True,
    data_model=AsanaWebhook,
    event="asana-connector.watch_project",
    effects=["asana.webhook.created"],
)
async def watch_project(ctx, params: WatchProjectParams) -> ActionResult:
    """Create an Asana webhook on a project.

    The handshake happens INSIDE this call: Asana POSTs a secret to the
    endpoint and waits for the echo before answering here. So by the time the
    create returns, the secret is already parked and only needs claiming and
    filing against the gid Asana just issued.

    If the endpoint were unreachable, Asana would fail the create rather than
    return a broken webhook -- which is why an error here is reported as a
    reachability problem, not a permissions one.
    """
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    workspace_gid = workspace.get("gid", "")
    project = await shared.resolve_project(ctx, token, workspace_gid,
                                           params.project)
    if not project.get("ok"):
        return _from_envelope(project)

    target = ctx.webhook_url("events")

    body: dict = {"resource": project["gid"], "target": target}
    if params.tasks_only:
        # Server-side filters: without them a busy project delivers every
        # field edit on every task, and the useful signal drowns.
        body["filters"] = [
            {"resource_type": "task", "action": "added"},
            {"resource_type": "task", "action": "changed"},
            {"resource_type": "task", "action": "deleted"},
            {"resource_type": "story", "action": "added",
             "resource_subtype": "comment_added"},
        ]

    out = await ac.request(ctx, "POST", "webhooks", token, data=body)
    if not out.get("ok"):
        return _from_envelope(out)

    created = out.get("data") or {}
    webhook_gid = ao.gid_of(created)
    if not webhook_gid:
        return ActionResult.error(
            "Asana accepted the subscription but returned no webhook id, so "
            "it cannot be tracked or removed later.",
            code=ac.ASANA_RESPONSE_UNEXPECTED)

    # The secret arrived mid-call and is waiting; file it against the gid.
    secret = await inbound.claim_handshake(ctx)
    if not secret:
        # Without the secret every future delivery fails verification, and it
        # cannot be re-requested -- Asana issues it exactly once. A subscription
        # that can never be verified is worse than none, so it is removed
        # rather than left looking healthy.
        await ac.request(ctx, "DELETE", f"webhooks/{webhook_gid}", token)
        return ActionResult.error(
            "The subscription handshake did not complete, so deliveries could "
            "never be verified. The half-built webhook has been removed. This "
            "usually means the events endpoint was not reachable from Asana.",
            code=ac.ASANA_RESPONSE_UNEXPECTED)

    await inbound.remember_hook(
        ctx, webhook_gid, secret,
        resource_gid=project["gid"],
        resource_name=project.get("name") or params.project,
        workspace=workspace_gid)

    name = project.get("name") or params.project
    entity = AsanaWebhook(
        id=webhook_gid,
        title=f"Watching '{name}'",
        gid=webhook_gid,
        resource_name=name,
        resource_gid=project["gid"],
        active=True,
        created=str(created.get("created_at") or ""),
        summary=(f"Watching '{name}'. New tasks, completions and comments "
                 f"now arrive as events automations can react to."),
    )
    return ActionResult.success(
        entity, f"Now watching '{name}' for changes.")


@chat.function(
    "list_webhooks",
    "List active Asana subscriptions -- which projects are being watched.",
    action_type="read", chain_callable=True,
    data_model=AsanaWebhook,
)
async def list_webhooks(ctx, params: ListWebhooksParams) -> ActionResult:
    """List webhooks Asana holds for this workspace.

    Asana is the source of truth, not the local store: a webhook it deleted
    after 24 hours of failed deliveries is gone regardless of what was filed
    here, and showing a stale local row as active would be the exact lie this
    tool exists to prevent.
    """
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    workspace_gid = workspace.get("gid", "")
    out = await ac.paginate(ctx, "webhooks", token,
                            params={"workspace": workspace_gid,
                                    "opt_fields": ao.WEBHOOK_FIELDS},
                            limit=params.limit)
    if not out.get("ok"):
        return _from_envelope(out)

    if not out["results"]:
        return ActionResult.success(
            AsanaWebhookList(items=[], total=0),
            "No projects are being watched yet.")

    known = {str(row.get("webhook_gid")): row
             for row in await inbound.stored_hooks(ctx)}

    items = []
    for hook in out["results"]:
        gid = ao.gid_of(hook)
        local = known.get(gid) or {}
        resource = hook.get("resource") or {}
        name = (ao.name_of(resource) if isinstance(resource, dict) else "") \
            or str(local.get("resource_name") or "")
        items.append(AsanaWebhook(
            id=gid,
            title=name or "(unnamed resource)",
            gid=gid,
            resource_name=name,
            resource_gid=(ao.gid_of(resource)
                          if isinstance(resource, dict) else ""),
            active=bool(hook.get("active", True)),
            last_delivery=ao.stamp_to_date(local.get("last_seen")),
            created=str(hook.get("created_at") or "")[:10],
            summary=ao.render_webhook(hook, local),
        ))

    return ActionResult.success(
        AsanaWebhookList(items=items, total=len(items)),
        f"{len(items)} project(s) being watched.")


@chat.function(
    "unwatch",
    "Stop getting notified about a project. Removes the subscription.",
    action_type="write", chain_callable=True,
    data_model=AsanaWebhook,
    event="asana-connector.unwatch",
    effects=["asana.webhook.deleted"],
)
async def unwatch(ctx, params: UnwatchParams) -> ActionResult:
    """Delete a webhook, by gid or by the project name it watches."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    reference = (params.webhook or "").strip()
    workspace_gid = workspace.get("gid", "")

    gid = reference if ao.looks_like_gid(reference) else ""
    name = reference
    if not gid:
        # Named by project rather than gid -- the way a person actually
        # remembers a subscription.
        listing = await ac.paginate(ctx, "webhooks", token,
                                    params={"workspace": workspace_gid,
                                            "opt_fields": ao.WEBHOOK_FIELDS},
                                    limit=100)
        if not listing.get("ok"):
            return _from_envelope(listing)
        matches = []
        for hook in listing["results"]:
            resource = hook.get("resource") or {}
            hook_name = ao.name_of(resource) if isinstance(resource, dict) else ""
            if hook_name.strip().lower() == reference.lower():
                matches.append((ao.gid_of(hook), hook_name))
        if not matches:
            return ActionResult.error(
                f"No subscription is watching '{reference}'.",
                code=ac.ASANA_TARGET_NOT_FOUND)
        if len(matches) > 1:
            return ActionResult.error(
                f"{len(matches)} subscriptions watch '{reference}'. Name the "
                f"webhook id instead so the right one is removed.",
                code=ac.ASANA_TARGET_AMBIGUOUS)
        gid, name = matches[0]

    out = await ac.request(ctx, "DELETE", f"webhooks/{gid}", token)
    if not out.get("ok"):
        return _from_envelope(out)

    # The stored secret is useless once the webhook is gone, and keeping it
    # would let a future delivery with a recycled gid verify against it.
    await inbound.forget_hook(ctx, gid)

    return ActionResult.success(
        AsanaWebhook(id=gid, title=f"Stopped watching '{name}'", gid=gid,
                     resource_name=name, active=False,
                     summary=f"No longer watching '{name}'."),
        f"Stopped watching '{name}'.")


@chat.function(
    "inbound_status",
    "Report whether Asana can push changes to this app -- endpoint, active "
    "subscriptions, and what events they produce.",
    action_type="read", chain_callable=True,
    data_model=InboundStatus,
)
async def inbound_status(ctx, params: ListWebhooksParams) -> ActionResult:
    """Explain the state of inbound events in one read.

    Written because "my automation never fires" has several very different
    causes -- no subscription, a subscription Asana already deleted, or an
    endpoint that was never reachable -- and they are indistinguishable from
    the outside.
    """
    endpoint = ctx.webhook_url("events")
    emitted = ", ".join([
        inbound.EVENT_TASK_ADDED, inbound.EVENT_TASK_CHANGED,
        inbound.EVENT_TASK_COMPLETED, inbound.EVENT_TASK_DELETED,
        inbound.EVENT_COMMENT_ADDED, inbound.EVENT_PROJECT_CHANGED,
    ])

    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return ActionResult.success(
            InboundStatus(
                id="inbound", title="Inbound events",
                endpoint_url=endpoint, webhooks_active=0, ready=False,
                events_emitted=emitted,
                detail="No Asana account is connected yet, so nothing can be "
                       "watched. Connect an account first.",
                summary="Inbound events: not ready -- no account connected."),
            "Inbound events are not ready yet.")

    out = await ac.paginate(ctx, "webhooks", token,
                            params={"workspace": workspace.get("gid", ""),
                                    "opt_fields": ao.WEBHOOK_FIELDS},
                            limit=100)
    active = len([h for h in (out.get("results") or [])
                  if h.get("active", True)]) if out.get("ok") else 0

    if active:
        detail = (f"{active} subscription(s) active. Asana pushes changes to "
                  f"the endpoint below, and each becomes an event automations "
                  f"can trigger on.")
    else:
        detail = ("The endpoint is live, but no project is being watched yet. "
                  "Use watch_project to subscribe to one.")

    return ActionResult.success(
        InboundStatus(
            id="inbound", title="Inbound events",
            endpoint_url=endpoint, webhooks_active=active,
            ready=active > 0, events_emitted=emitted, detail=detail,
            summary=f"Inbound events: {active} active subscription(s)."),
        detail)
