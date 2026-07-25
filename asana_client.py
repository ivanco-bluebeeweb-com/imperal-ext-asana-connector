"""Asana REST helpers: one request funnel, structured errors, pagination.

Three Asana shapes drive everything in this module, all verified against
developers.asana.com rather than recalled:

* ENVELOPE. Every response wraps its payload in a top-level `data` key, and
  every write REQUEST body must wrap its fields the same way:
  `{"data": {"name": "..."}}`. Sending a bare body is a 400.
* ERRORS. Failures carry a top-level `errors` ARRAY of objects with a
  `message` (and a `phrase` on 500s). There is no machine-readable error code
  in the body -- unlike Notion -- so classification here leans on the HTTP
  status, with the message text used only where it names the offending field.
* PAGINATION. `limit` (1-100) plus an OFFSET TOKEN: a response that has more
  data includes `next_page.offset`, which is fed back as `offset`. It is an
  opaque token, not a numeric index, and it may not be synthesised.

Nothing in this module puts a token into a message, a log line or an error.
"""

from __future__ import annotations

ASANA_API = "https://app.asana.com/api/1.0"

# Asana caps every paginated endpoint at 100 items per request.
MAX_PAGE_SIZE = 100

# --- structured error codes (I-EXT-ERROR-CODE-NORMALIZED) -------------------
# Every error that reaches the user carries a stable code: it is what the
# platform error taxonomy, self-diagnosis and honest narration key on. An
# error emitted without one is stamped EXT_UNSTRUCTURED_ERROR at the dispatch
# boundary, which degrades the user's diagnosis to prose parsing.
#
# Platform taxonomy codes (imperal_sdk.chat.error_codes) are reused where the
# meaning matches exactly: PERMISSION_DENIED, RATE_LIMITED, BACKEND_5XX,
# BACKEND_TIMEOUT. Everything Asana-specific gets an app-declared code matching
# ^[A-Z][A-Z0-9_]{2,63}$. The code never appears in the message prose -- the two
# travel as separate fields.
ASANA_TOKEN_MISSING = "ASANA_TOKEN_MISSING"
ASANA_TOKEN_REJECTED = "ASANA_TOKEN_REJECTED"
ASANA_NOT_FOUND = "ASANA_NOT_FOUND"
ASANA_VALIDATION_FAILED = "ASANA_VALIDATION_FAILED"
ASANA_UNREACHABLE = "ASANA_UNREACHABLE"
ASANA_RESPONSE_NOT_JSON = "ASANA_RESPONSE_NOT_JSON"
ASANA_RESPONSE_UNEXPECTED = "ASANA_RESPONSE_UNEXPECTED"
ASANA_HTTP_ERROR = "ASANA_HTTP_ERROR"
ASANA_ACCOUNT_UNKNOWN = "ASANA_ACCOUNT_UNKNOWN"
ASANA_WORKSPACE_UNKNOWN = "ASANA_WORKSPACE_UNKNOWN"
ASANA_WORKSPACE_AMBIGUOUS = "ASANA_WORKSPACE_AMBIGUOUS"
ASANA_TARGET_NOT_FOUND = "ASANA_TARGET_NOT_FOUND"
ASANA_TARGET_AMBIGUOUS = "ASANA_TARGET_AMBIGUOUS"
ASANA_FILTER_REQUIRED = "ASANA_FILTER_REQUIRED"
ASANA_RESULT_TRUNCATED = "ASANA_RESULT_TRUNCATED"
# Asana answers 402 Payment Required for premium-only endpoints -- advanced
# task search is the big one. That is a PRODUCT boundary, not a bug and not a
# permission problem, so it gets its own code: the honest next step is
# "use the plain task listing instead", never "check your token".
ASANA_PREMIUM_REQUIRED = "ASANA_PREMIUM_REQUIRED"
ASANA_BLOCKED_LEGAL = "ASANA_BLOCKED_LEGAL"
# Credential STORAGE failures -- deliberately distinct from "no token
# configured". Without these, an unreadable or unwritable secret store surfaces
# as ASANA_TOKEN_MISSING: "paste your token" advice for a problem no amount of
# pasting can fix.
ASANA_SECRET_UNAVAILABLE = "ASANA_SECRET_UNAVAILABLE"
ASANA_SECRET_WRITE_FAILED = "ASANA_SECRET_WRITE_FAILED"

_MESSAGES = {
    ASANA_TOKEN_REJECTED: (
        "Asana rejected the access token -- it may have been revoked, pasted "
        "incompletely, or the workspace may have disabled this app. Create a "
        "fresh personal access token and connect again."
    ),
    ASANA_NOT_FOUND: (
        "Asana has no such item, or the account behind this token cannot see "
        "it. Check the name, and check that the task or project is in a "
        "workspace this account belongs to."
    ),
    "PERMISSION_DENIED": (
        "The account behind this token is not allowed to do that in Asana. "
        "Its access to that project or task is read-only or absent."
    ),
    ASANA_VALIDATION_FAILED: "Asana rejected the request as invalid.",
    ASANA_PREMIUM_REQUIRED: (
        "That part of Asana is premium-only, so the API refused it for this "
        "account's plan. Advanced task search needs a premium workspace or "
        "team -- listing tasks by project or assignee works on every plan."
    ),
    ASANA_FILTER_REQUIRED: (
        "Asana refuses to list tasks without a narrower filter: name a "
        "project, or an assignee together with a workspace."
    ),
    ASANA_RESULT_TRUNCATED: (
        "The result set is too large for Asana to page through. Narrow it "
        "down -- a single project or a date range -- and try again."
    ),
    ASANA_BLOCKED_LEGAL: (
        "Asana blocked this request for legal or regional reasons."
    ),
    "RATE_LIMITED": "Asana is rate-limiting requests -- try again shortly.",
    "BACKEND_5XX": "Asana returned a server error -- try again shortly.",
    "BACKEND_TIMEOUT": "Asana took too long to respond -- try again shortly.",
    ASANA_UNREACHABLE: "Could not reach the Asana API.",
    ASANA_SECRET_UNAVAILABLE: (
        "The secure store holding your Asana token could not be read just "
        "now, so the connection state is unknown. This is not a problem with "
        "your token -- try again shortly."
    ),
    ASANA_SECRET_WRITE_FAILED: (
        "The token could not be saved to the secure store, so nothing was "
        "changed. Try again shortly."
    ),
}

_RETRYABLE = {"RATE_LIMITED", "BACKEND_5XX", "BACKEND_TIMEOUT",
              ASANA_UNREACHABLE, ASANA_SECRET_UNAVAILABLE,
              ASANA_SECRET_WRITE_FAILED}


def is_retryable(code: str) -> bool:
    """Whether retrying the identical call could plausibly succeed."""
    return code in _RETRYABLE


def message_for(code: str) -> str:
    """User-facing text for a structured code (prose and code stay separate)."""
    return _MESSAGES.get(code, "The Asana request failed.")


def auth_headers(token: str) -> dict:
    """Auth headers. The token is never logged by this module."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def transport_error_code(exc: BaseException) -> str:
    """Classify a transport-level failure talking to Asana.

    A timeout is a distinct, retryable condition with its own taxonomy code --
    worth separating from "host does not resolve / refused the connection",
    because the useful next step differs.
    """
    name = type(exc).__name__.lower()
    if "timeout" in name or "timedout" in name:
        return "BACKEND_TIMEOUT"
    return ASANA_UNREACHABLE


def first_error_message(body) -> str:
    """Pull `errors[0].message` out of an Asana error body.

    Asana always answers failures with a top-level `errors` ARRAY, even for a
    single problem, so indexing the first element is the documented shape and
    not a guess.
    """
    if not isinstance(body, dict):
        return ""
    errors = body.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("message") or "")
    return ""


def classify(status_code: int, body) -> tuple[str, str]:
    """Map a failed Asana response onto (code, user-facing message).

    Asana sends no machine-readable code in the body, so the HTTP status leads.
    The one exception is 400: Asana overloads it for BOTH "you forgot a
    required filter" and "this result set is too big to page", and those two
    need opposite advice -- so the message text is inspected to tell them
    apart. That is text-sniffing, which is fragile by nature; it only ever
    REFINES a 400 that would otherwise be a generic validation failure, so a
    wording change on Asana's side degrades to the honest generic answer
    instead of breaking.
    """
    detail = first_error_message(body)
    lowered = detail.lower()

    if status_code == 400:
        if "truncat" in lowered:
            code = ASANA_RESULT_TRUNCATED
        elif "missing input" in lowered and (
                "workspace" in lowered or "project" in lowered
                or "assignee" in lowered or "tag" in lowered):
            code = ASANA_FILTER_REQUIRED
        else:
            code = ASANA_VALIDATION_FAILED
    elif status_code == 401:
        code = ASANA_TOKEN_REJECTED
    elif status_code == 402:
        code = ASANA_PREMIUM_REQUIRED
    elif status_code == 403:
        code = "PERMISSION_DENIED"
    elif status_code == 404:
        code = ASANA_NOT_FOUND
    elif status_code == 429:
        code = "RATE_LIMITED"
    elif status_code == 451:
        code = ASANA_BLOCKED_LEGAL
    elif 500 <= status_code < 600:
        code = "BACKEND_5XX"
    else:
        code = ASANA_HTTP_ERROR

    message = _MESSAGES.get(code) or f"Asana request failed (HTTP {status_code})."
    # Asana's own message is echoed ONLY for validation errors: there it names
    # the offending field ("due_on: Invalid date"), which is exactly what makes
    # the failure fixable. It is not echoed for auth failures, where the
    # curated explanation is better and the raw text adds nothing actionable.
    if code == ASANA_VALIDATION_FAILED and detail:
        message = f"Asana rejected the request: {detail}"
    return code, message


def fail(code: str, error: str = "") -> dict:
    """Build the module's error envelope with a stable code."""
    return {"ok": False, "code": code, "retryable": is_retryable(code),
            "error": error or message_for(code)}


async def request(ctx, method: str, path: str, token: str, *,
                  data: dict | None = None, params: dict | None = None,
                  timeout: int = 30) -> dict:
    """Call one Asana endpoint.

    `data` is the FIELDS of a write, not the wire body: this function wraps it
    in Asana's required `{"data": ...}` envelope. Keeping the wrap in exactly
    one place is what stops half the call sites from forgetting it.

    Returns {"ok": True, "data": ...} -- already UNWRAPPED from the response
    envelope -- or {"ok": False, "error", "code", "retryable"}. Every Asana call
    in this app funnels through here, so classification and timeouts cannot
    drift between sites.
    """
    if not token:
        return fail(ASANA_TOKEN_MISSING,
                    "No Asana access token is configured yet -- open the app's "
                    "Connect Asana screen and paste one.")

    url = f"{ASANA_API}/{path.lstrip('/')}"
    fn = getattr(ctx.http, method.lower())
    kwargs: dict = {"headers": auth_headers(token), "timeout": timeout}
    if data is not None:
        kwargs["json"] = {"data": data}
    if params:
        kwargs["params"] = params

    try:
        # Explicit timeout: a hanging call must fail as a diagnosable in-handler
        # exception, not hang until the platform cancels the coroutine (which
        # surfaces to the user as an opaque INTERNAL).
        resp = await fn(url, **kwargs)
    except Exception as e:
        # The exception TYPE is a useful fact (DNS vs refused vs timeout); the
        # raw exception string is not -- it can carry hosts and internal paths.
        return fail(transport_error_code(e))

    body = resp.body
    if isinstance(body, (str, bytes, bytearray)) and body:
        try:
            body = resp.json()
        except Exception:
            if resp.status_code >= 400:
                code, message = classify(resp.status_code, None)
                return {"ok": False, "code": code, "error": message,
                        "retryable": is_retryable(code)}
            return fail(ASANA_RESPONSE_NOT_JSON,
                        "Asana returned a success status but the response body "
                        "wasn't valid JSON.")

    if resp.status_code >= 400:
        code, message = classify(resp.status_code, body)
        return {"ok": False, "code": code, "error": message,
                "retryable": is_retryable(code)}

    if not isinstance(body, dict):
        return fail(ASANA_RESPONSE_UNEXPECTED,
                    "Asana returned an unexpected response shape.")

    if "data" not in body:
        return fail(ASANA_RESPONSE_UNEXPECTED,
                    "Asana returned a response without the expected data field.")

    # Unwrapped here so no caller has to know about the envelope. `next_page`
    # is carried alongside for the paginator.
    return {"ok": True, "data": body["data"], "next_page": body.get("next_page")}


def next_offset(next_page) -> str:
    """Extract the opaque offset token from a `next_page` object.

    Asana's offset is a TOKEN, not an index -- it cannot be computed, only
    echoed back from the previous response.
    """
    if isinstance(next_page, dict):
        return str(next_page.get("offset") or "")
    return ""


async def paginate(ctx, path: str, token: str, *,
                   params: dict | None = None,
                   limit: int = MAX_PAGE_SIZE, max_pages: int = 10) -> dict:
    """Follow Asana's offset pagination until `limit` items or `max_pages`.

    Returns {"ok": True, "results": list, "has_more": bool} or the same error
    envelope as `request`. `max_pages` is a hard stop so one tool call on a huge
    workspace can never turn into an unbounded crawl.
    """
    results: list = []
    offset = ""
    has_more = False

    for _ in range(max_pages):
        want = min(MAX_PAGE_SIZE, max(1, limit - len(results)))
        page_params = dict(params or {})
        page_params["limit"] = want
        if offset:
            page_params["offset"] = offset

        out = await request(ctx, "GET", path, token, params=page_params)
        if not out.get("ok"):
            return out

        batch = out["data"]
        if not isinstance(batch, list):
            return fail(ASANA_RESPONSE_UNEXPECTED,
                        "Asana returned a list endpoint response that was not a list.")
        results.extend(batch)

        offset = next_offset(out.get("next_page"))
        has_more = bool(offset)
        if len(results) >= limit or not has_more:
            break

    return {"ok": True, "results": results[:limit], "has_more": has_more}
