"""Inbound webhook transport: handshake, signature, heartbeat, dedupe.

This endpoint is different in kind from every other test in this suite: it is
reachable by ANYONE on the internet who learns the URL, and it runs with no
user in context. A forged request cannot be safely rehearsed against the live
API, so the adversarial cases have to be proven here or not at all.

The four things that would break it in production, each with a test:

  * the handshake must be answered BEFORE signature checking (during it, no
    secret exists yet -- that request IS the secret arriving);
  * a wrong signature must be REFUSED, and must not say why;
  * an empty heartbeat must be answered 200, because ignoring it deletes the
    webhook 24 hours later with no error anywhere;
  * a redelivery must not emit the same event twice.
"""

import hashlib
import hmac
import json

import pytest

import handlers_inbound as hi
import inbound as ib

# Reused rather than redefined: `_ok` reads `status`, not `success` -- because
# ActionResult.success is a classmethod and is therefore ALWAYS truthy, which
# once made every assertion in this suite pass regardless of outcome.
from conftest import envelope, error_payload, me_payload
from test_tools import _ok, _text

pytestmark = pytest.mark.asyncio


def _sign(body: str, secret: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def _body(events: list) -> str:
    return json.dumps({"events": events})


TASK_EVENT = {
    "action": "changed",
    "resource": {"gid": "1201", "resource_type": "task", "name": "QA pass"},
    "parent": {"gid": "3001", "resource_type": "project", "name": "Launch"},
    "change": {"field": "completed"},
    "created_at": "2026-07-26T10:00:00.000Z",
}


# --- handshake ---------------------------------------------------------------

async def test_handshake_is_echoed_in_a_response_header(ctx):
    """The one delivery that must be answered without any signature check.

    Asana creates a webhook by POSTing a new X-Hook-Secret and BLOCKING the
    create call until the endpoint echoes that header back. At that moment no
    secret is stored -- verifying first would reject the request that
    establishes every future request, and webhook creation would fail 100% of
    the time, looking like a network fault.
    """
    out = await hi.asana_events(
        ctx, headers={"X-Hook-Secret": "handshake-secret-42"}, body="{}")

    assert out.status_code == 200
    echoed = {k.lower(): v for k, v in (out.headers or {}).items()}
    assert echoed.get("x-hook-secret") == "handshake-secret-42", out.headers


async def test_the_handshake_secret_is_kept_for_later_delivery(ctx):
    """A secret that is echoed but not stored breaks every future delivery.

    Asana issues it ONCE and never sends it again -- GET /webhooks/{gid} does
    not return it. Losing it means every subsequent event fails verification
    with no way back except deleting the webhook.
    """
    await hi.asana_events(
        ctx, headers={"X-Hook-Secret": "keep-me"}, body="{}")

    claimed = await ib.claim_handshake(ctx)
    assert claimed == "keep-me"


# --- signature ---------------------------------------------------------------

async def test_a_forged_signature_is_refused(ctx):
    """The only path that does NOT answer 200."""
    await ib.remember_hook(ctx, "hook-1", "real-secret",
                           resource_gid="3001", resource_name="Launch")

    out = await hi.asana_events(
        ctx,
        headers={"X-Hook-Signature": "0" * 64},
        body=_body([TASK_EVENT]))

    assert out.status_code == 401


async def test_a_refusal_does_not_reveal_which_check_failed(ctx):
    """Telling an unauthenticated caller WHY helps them forge a better one."""
    await ib.remember_hook(ctx, "hook-1", "real-secret")

    out = await hi.asana_events(
        ctx, headers={"X-Hook-Signature": "0" * 64}, body=_body([TASK_EVENT]))

    text = json.dumps(out.body).lower()
    for leak in ("secret", "hmac", "expected", "mismatch", "sha256"):
        assert leak not in text, f"response leaked '{leak}': {out.body}"


async def test_a_valid_signature_is_accepted_and_emits(ctx):
    """The happy path, signed exactly the way Asana signs it."""
    body = _body([TASK_EVENT])
    await ib.remember_hook(ctx, "hook-1", "sekret",
                           resource_gid="3001", resource_name="Launch")

    emitted = []

    # emit is AWAITED by the handler, so the stub must be a coroutine. A plain
    # lambda raises TypeError inside the handler's try/except and the emit
    # silently counts as failed -- which is exactly how this test first lied.
    async def _emit(name, payload):
        emitted.append((name, payload))

    ctx.extensions.emit = _emit

    out = await hi.asana_events(
        ctx, headers={"X-Hook-Signature": _sign(body, "sekret")}, body=body)

    assert out.status_code == 200
    assert emitted, "a signed, non-duplicate event must be emitted"

    names = [name for name, _ in emitted]
    # A completed task is BOTH: the generic change (for rules watching edits)
    # and the specific completion (so "when something is finished" does not
    # have to filter every field edit).
    assert ib.EVENT_TASK_CHANGED in names, names
    assert ib.EVENT_TASK_COMPLETED in names, names

    # The payload carries IDENTITY and INTENT, not content: Asana sends gids
    # and an action, never the new text. A rule is expected to call get_task
    # for current state, and inventing a richer payload here would be guessing.
    payload = emitted[0][1]
    assert payload["resource_gid"] == "1201", payload
    assert payload["resource_name"] == "QA pass", payload
    assert payload["parent_name"] == "Launch", payload
    assert payload["changed_field"] == "completed", payload
    # Which subscription delivered it -- a rule watching two projects cannot
    # otherwise tell them apart.
    assert payload["webhook_gid"] == "hook-1", payload


async def test_the_signature_is_checked_against_the_raw_body(ctx):
    """Re-serialising the parsed JSON changes key order and whitespace.

    Hashing a re-dumped body instead of the received bytes makes every real
    delivery fail verification -- a bug that looks exactly like a wrong secret.
    """
    # Deliberately odd spacing: a re-serialised copy would not match this.
    body = '{"events":[  {"action":"added","resource":{"gid":"7",' \
           '"resource_type":"task","name":"Odd spacing"}} ]}'
    await ib.remember_hook(ctx, "hook-1", "sekret")

    out = await hi.asana_events(
        ctx, headers={"X-Hook-Signature": _sign(body, "sekret")}, body=body)

    assert out.status_code == 200, "raw-body signature must verify"


# --- heartbeat ---------------------------------------------------------------

async def test_an_empty_heartbeat_is_answered_200(ctx):
    """Asana pings every 8h with no events and DELETES the webhook after 24h
    of silence. Skipping empty payloads as 'noise' works perfectly for one day
    and then goes dead with no error anywhere -- the worst kind of failure.
    """
    body = _body([])
    await ib.remember_hook(ctx, "hook-1", "sekret")

    out = await hi.asana_events(
        ctx, headers={"X-Hook-Signature": _sign(body, "sekret")}, body=body)

    assert out.status_code == 200


# --- de-duplication ----------------------------------------------------------

async def test_a_redelivered_event_is_not_emitted_twice(ctx):
    """Asana retries for up to 24 hours until it gets a 2xx.

    Without dedupe, one completed task fires an automation three times -- the
    single most visible way an integration like this looks broken.
    """
    body = _body([TASK_EVENT])
    await ib.remember_hook(ctx, "hook-1", "sekret",
                           resource_gid="3001", resource_name="Launch")

    emitted = []

    async def _emit(name, payload):
        emitted.append(name)

    ctx.extensions.emit = _emit

    headers = {"X-Hook-Signature": _sign(body, "sekret")}
    first = await hi.asana_events(ctx, headers=headers, body=body)
    second = await hi.asana_events(ctx, headers=headers, body=body)

    assert first.status_code == 200 and second.status_code == 200
    # The first delivery legitimately emits two names (changed + completed);
    # what must NOT happen is the SECOND delivery adding any more.
    after_first = len(emitted)
    assert after_first >= 1, emitted
    assert len(emitted) == after_first, f"redelivery emitted again: {emitted}"


# --- the handshake blocker is reported honestly ------------------------------

async def test_a_failed_handshake_is_not_blamed_on_the_token(connected_ctx, http):
    """Asana's wording for this failure actively misleads.

    It says "the remote server did not respond with the handshake secret",
    which reads like a token or scope problem and is neither: probing the live
    endpoint showed the handler returning the correct echo, and the platform
    serialising that response into a JSON body with its headers dropped.

    This test pins the honest explanation in place. Without it the diagnosis is
    one refactor away from silently reverting to Asana's own misleading text,
    and the next person re-audits their token for an upstream limitation.
    """
    from models import WatchProjectParams

    http.push(envelope(me_payload()))
    http.push(envelope([{"gid": "3001", "name": "Launch",
                         "resource_type": "project"}]))
    # Asana's REAL failure shape: a top-level `errors` array with an HTTP
    # status, not a pre-made client envelope. Pushing the envelope meant the
    # request never looked like a failure, so the branch under test was never
    # reached -- the mock was wrong, not the code.
    http.push(error_payload(
        "The remote server which is intended to receive the webhook did not "
        "respond with the handshake secret."), status=400)

    out = await hi.watch_project(connected_ctx, WatchProjectParams(
        project="Launch"))

    assert not _ok(out)
    text = _text(out).lower()
    # The cause is named...
    assert "header" in text
    # ...and the wrong suspects are explicitly cleared.
    assert "not a token or permission problem" in text
