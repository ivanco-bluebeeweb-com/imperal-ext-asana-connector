"""The request funnel: envelopes, error classification, pagination.

Every shape asserted here was verified against developers.asana.com, not
recalled -- the envelope, the `errors[]` array, the offset token, and the two
statuses Notion simply does not have (402 premium, 451 legal block).
"""

import asana_client as ac
from conftest import envelope, error_payload, task_payload


# --- envelopes --------------------------------------------------------------

async def test_response_data_is_unwrapped_for_the_caller(ctx, http):
    """`{"data": {...}}` comes back as just the payload."""
    http.push(envelope(task_payload()))
    out = await ac.request(ctx, "GET", "tasks/1201", "tok")
    assert out["ok"] is True
    assert out["data"]["name"] == "Ship the landing page"


async def test_write_body_is_wrapped_in_the_data_envelope(ctx, http):
    """A bare body is a 400, so `data` must be wrapped exactly once."""
    http.push(envelope(task_payload()))
    await ac.request(ctx, "POST", "tasks", "tok", data={"name": "New task"})
    assert http.last_body() == {"data": {"name": "New task"}}


async def test_missing_data_field_is_not_silently_accepted(ctx, http):
    """A 200 without `data` is an unexpected shape, not an empty result."""
    http.push({"something_else": 1})
    out = await ac.request(ctx, "GET", "tasks/1201", "tok")
    assert out["ok"] is False
    assert out["code"] == ac.ASANA_RESPONSE_UNEXPECTED


async def test_no_token_fails_before_any_request(ctx, http):
    """An empty token must not produce an unauthenticated round trip."""
    out = await ac.request(ctx, "GET", "users/me", "")
    assert out["code"] == ac.ASANA_TOKEN_MISSING
    assert http.calls == []


async def test_token_travels_only_in_the_auth_header(ctx, http):
    http.push(envelope({"gid": "1"}))
    await ac.request(ctx, "GET", "users/me", "secret-token-value")
    call = http.calls[-1]
    assert call["headers"]["Authorization"] == "Bearer secret-token-value"
    assert "secret-token-value" not in call["url"]


# --- error classification ---------------------------------------------------

async def test_401_is_a_rejected_token(ctx, http):
    http.push(error_payload("Not Authorized"), status=401)
    out = await ac.request(ctx, "GET", "users/me", "tok")
    assert out["code"] == ac.ASANA_TOKEN_REJECTED
    assert out["retryable"] is False


async def test_402_is_reported_as_a_premium_requirement(ctx, http):
    """Advanced search answers 402 on free plans.

    This is the status that made typeahead the default search: classifying it
    as a generic failure would tell a free-plan user their setup is broken when
    the endpoint simply is not on their plan.
    """
    http.push(error_payload("Payment Required"), status=402)
    out = await ac.request(ctx, "GET", "workspaces/1/tasks/search", "tok")
    assert out["code"] == ac.ASANA_PREMIUM_REQUIRED


async def test_429_is_retryable(ctx, http):
    http.push(error_payload("Rate Limit Enforced"), status=429)
    out = await ac.request(ctx, "GET", "tasks", "tok")
    assert out["code"] == "RATE_LIMITED"
    assert out["retryable"] is True


async def test_500_is_retryable_and_hides_the_phrase(ctx, http):
    """Asana's 500 carries a joke `phrase`; it must not become the message."""
    http.push({"errors": [{"message": "Server Error",
                           "phrase": "6 sad squid snuggle softly"}]}, status=500)
    out = await ac.request(ctx, "GET", "users/me", "tok")
    assert out["code"] == "BACKEND_5XX"
    assert out["retryable"] is True
    assert "squid" not in out["error"]


async def test_400_missing_filter_is_distinguished_from_validation(ctx, http):
    """`GET /tasks` without a filter must not read as a generic bad request."""
    http.push(error_payload("workspace: Missing input"), status=400)
    out = await ac.request(ctx, "GET", "tasks", "tok")
    assert out["code"] == ac.ASANA_FILTER_REQUIRED


async def test_400_truncated_result_set_gets_its_own_code(ctx, http):
    """A truncated huge query needs narrowing advice, not a retry."""
    http.push(error_payload("Your query resulted in a truncated data set"),
              status=400)
    out = await ac.request(ctx, "GET", "tasks", "tok")
    assert out["code"] == ac.ASANA_RESULT_TRUNCATED


async def test_validation_error_echoes_the_offending_field(ctx, http):
    """Asana names the bad field -- that is what makes it fixable."""
    http.push(error_payload("due_on: Invalid date"), status=400)
    out = await ac.request(ctx, "GET", "tasks/1", "tok")
    assert out["code"] == ac.ASANA_VALIDATION_FAILED
    assert "due_on" in out["error"]


async def test_auth_failure_does_not_echo_asana_prose(ctx, http):
    """For auth, the curated explanation beats the raw text."""
    http.push(error_payload("Not Authorized"), status=401)
    out = await ac.request(ctx, "GET", "users/me", "tok")
    assert "Not Authorized" not in out["error"]


async def test_timeout_is_separated_from_unreachable(ctx, http):
    class ReadTimeout(Exception):
        pass

    http.push(ReadTimeout("timed out"))
    out = await ac.request(ctx, "GET", "users/me", "tok")
    assert out["code"] == "BACKEND_TIMEOUT"
    assert out["retryable"] is True


async def test_transport_failure_never_leaks_the_exception_text(ctx, http):
    class ConnectionRefused(Exception):
        pass

    http.push(ConnectionRefused("cannot reach 10.0.0.5:443 via /etc/hosts"))
    out = await ac.request(ctx, "GET", "users/me", "tok")
    assert out["code"] == ac.ASANA_UNREACHABLE
    assert "10.0.0.5" not in out["error"]


async def test_html_error_body_does_not_become_a_json_complaint(ctx, http):
    """A 502 HTML page is a backend failure, not a parse problem."""
    http.push("<html>502 Bad Gateway</html>", status=502)
    out = await ac.request(ctx, "GET", "users/me", "tok")
    assert out["code"] == "BACKEND_5XX"


async def test_non_json_success_body_is_reported_honestly(ctx, http):
    http.push("not json at all", status=200)
    out = await ac.request(ctx, "GET", "users/me", "tok")
    assert out["code"] == ac.ASANA_RESPONSE_NOT_JSON


# --- pagination -------------------------------------------------------------

async def test_pagination_follows_the_opaque_offset_token(ctx, http):
    """The offset is echoed back verbatim -- it cannot be computed."""
    http.push(envelope([task_payload(gid="1")], next_offset="opaque-abc"))
    http.push(envelope([task_payload(gid="2")]))
    out = await ac.paginate(ctx, "tasks", "tok", limit=50)
    assert out["ok"] is True
    assert [t["gid"] for t in out["results"]] == ["1", "2"]
    assert http.calls[1]["params"]["offset"] == "opaque-abc"
    assert out["has_more"] is False


async def test_pagination_stops_at_the_requested_limit(ctx, http):
    http.push(envelope([task_payload(gid=str(i)) for i in range(5)],
                       next_offset="more"))
    out = await ac.paginate(ctx, "tasks", "tok", limit=3)
    assert len(out["results"]) == 3
    assert out["has_more"] is True


async def test_pagination_never_requests_more_than_asanas_cap(ctx, http):
    http.push(envelope([task_payload()]))
    await ac.paginate(ctx, "tasks", "tok", limit=5000)
    assert http.calls[0]["params"]["limit"] == ac.MAX_PAGE_SIZE


async def test_pagination_has_a_hard_page_ceiling(ctx, http):
    """max_pages stops one tool call becoming an unbounded crawl."""
    for _ in range(6):
        http.push(envelope([task_payload()], next_offset="always-more"))
    out = await ac.paginate(ctx, "tasks", "tok", limit=1000, max_pages=3)
    assert len(http.calls) == 3
    assert out["has_more"] is True


async def test_pagination_propagates_an_error_unchanged(ctx, http):
    http.push(error_payload("Not Authorized"), status=401)
    out = await ac.paginate(ctx, "tasks", "tok")
    assert out["ok"] is False
    assert out["code"] == ac.ASANA_TOKEN_REJECTED


async def test_list_endpoint_returning_an_object_is_caught(ctx, http):
    http.push(envelope({"gid": "1"}))
    out = await ac.paginate(ctx, "tasks", "tok")
    assert out["code"] == ac.ASANA_RESPONSE_UNEXPECTED
