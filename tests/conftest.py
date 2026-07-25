"""Shared fixtures.

MockHTTP from the SDK only registers GET/POST and returns the first pattern
match, which cannot express "PUT this task" or "the same URL answers differently
on the second call" -- both of which the write tools do. So the HTTP double here
is queue-based: each test states the exact sequence of responses it expects, and
every request is recorded for assertions.

The payload builders below all wrap their body in Asana's top-level `data` key,
because that is the shape the real API returns and the client unwraps.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class FakeResponse:
    """Mirrors imperal_sdk HTTPResponse closely enough for asana_client."""

    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self.body = body
        self.headers: dict = {}

    def json(self):
        # Mirrors imperal_sdk HTTPResponse.json(): a str/bytes body is PARSED,
        # so invalid JSON raises — which is what drives the NOT_JSON path.
        if isinstance(self.body, (dict, list)):
            return self.body
        if isinstance(self.body, (str, bytes, bytearray)):
            import json as _json
            return _json.loads(self.body)
        raise ValueError(f"Cannot parse {type(self.body).__name__} body as JSON")

    def text(self) -> str:
        return self.body if isinstance(self.body, str) else str(self.body)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class QueueHTTP:
    """HTTP double: queue up responses, then inspect what was requested."""

    def __init__(self):
        self.queued: list = []
        self.calls: list[dict] = []

    def push(self, body, status: int = 200):
        """Queue one response (or an Exception instance to raise)."""
        self.queued.append((status, body))
        return self

    def _next(self, method: str, url: str, kwargs) -> FakeResponse:
        self.calls.append({
            "method": method,
            "url": url,
            "json": kwargs.get("json"),
            "params": kwargs.get("params"),
            "headers": kwargs.get("headers") or {},
        })
        if not self.queued:
            raise AssertionError(f"unexpected {method} {url} — no response queued")
        status, body = self.queued.pop(0)
        if isinstance(body, Exception):
            raise body
        return FakeResponse(status, body)

    async def get(self, url, **kw):
        return self._next("GET", url, kw)

    async def post(self, url, **kw):
        return self._next("POST", url, kw)

    async def patch(self, url, **kw):
        return self._next("PATCH", url, kw)

    async def put(self, url, **kw):
        return self._next("PUT", url, kw)

    async def delete(self, url, **kw):
        return self._next("DELETE", url, kw)

    # -- assertion helpers --------------------------------------------------
    def last_body(self) -> dict:
        return self.calls[-1]["json"] or {}

    def last_params(self) -> dict:
        return self.calls[-1]["params"] or {}

    def urls(self) -> list[str]:
        return [c["url"] for c in self.calls]

    def methods(self) -> list[str]:
        return [c["method"] for c in self.calls]


@pytest.fixture
def http():
    return QueueHTTP()


@pytest.fixture
def ctx(http):
    from imperal_sdk.testing import MockContext, MockSecretStore

    mock = MockContext()
    mock.secrets = MockSecretStore({})
    mock.http = http
    return mock


@pytest.fixture
def connected_ctx(ctx):
    """A ctx with one usable personal access token already configured."""
    from imperal_sdk.testing import MockSecretStore

    ctx.secrets = MockSecretStore({"asana_tokens": "2/test_token_one"})
    return ctx


# --- Asana payload builders -------------------------------------------------

def envelope(data, next_offset: str = "") -> dict:
    """Asana's response shape: payload under `data`, cursor under `next_page`."""
    out: dict = {"data": data}
    if next_offset:
        out["next_page"] = {"offset": next_offset, "path": "/x", "uri": "https://x"}
    else:
        out["next_page"] = None
    return out


def me_payload(name: str = "Vlad Ivanco", email: str = "vlad@bluebeeweb.com",
               workspaces: list | None = None) -> dict:
    """`/users/me` — the call every tool makes first to identify the account.

    The `workspaces` array is what makes one Asana token span MANY workspaces,
    unlike a Notion token which is scoped to exactly one.
    """
    if workspaces is None:
        workspaces = [{"gid": "100", "name": "Acme", "resource_type": "workspace"}]
    # Returns the PAYLOAD, not the envelope: the caller wraps it with
    # envelope(...) exactly once. Building the envelope here too produced a
    # double wrap (data.data.workspaces) that read perfectly and resolved to no
    # workspaces at all.
    return {
        "gid": "9001",
        "name": name,
        "email": email,
        "resource_type": "user",
        "workspaces": workspaces,
    }


def task_payload(gid: str = "1201", name: str = "Ship the landing page",
                 **extra) -> dict:
    payload = {
        "gid": gid,
        "name": name,
        "resource_type": "task",
        "completed": False,
        "due_on": "2026-08-01",
        "notes": "Copy is approved, needs images.",
        "assignee": {"gid": "9001", "name": "Vlad Ivanco"},
        "projects": [{"gid": "300", "name": "Website Redesign"}],
        "permalink_url": f"https://app.asana.com/0/0/{gid}",
        "created_at": "2026-07-01T10:00:00.000Z",
        "modified_at": "2026-07-20T12:00:00.000Z",
        "num_subtasks": 0,
    }
    payload.update(extra)
    return payload


def project_payload(gid: str = "300", name: str = "Website Redesign",
                    **extra) -> dict:
    payload = {
        "gid": gid,
        "name": name,
        "resource_type": "project",
        "archived": False,
        "owner": {"gid": "9001", "name": "Vlad Ivanco"},
        "team": {"gid": "500", "name": "Marketing"},
        "workspace": {"gid": "100", "name": "Acme"},
        "current_status": {"title": "On track"},
        "permalink_url": f"https://app.asana.com/0/{gid}",
        "modified_at": "2026-07-20T12:00:00.000Z",
    }
    payload.update(extra)
    return payload


def story_payload(gid: str = "700", text: str = "Looks good to me",
                  is_comment: bool = True) -> dict:
    return {
        "gid": gid,
        "resource_type": "story",
        "text": text,
        "type": "comment" if is_comment else "system",
        "resource_subtype": "comment_added" if is_comment else "assigned",
        "created_at": "2026-07-21T09:00:00.000Z",
        "created_by": {"gid": "9001", "name": "Vlad Ivanco"},
    }


def error_payload(message: str) -> dict:
    """Asana's failure shape: a top-level `errors` ARRAY, no machine code."""
    return {"errors": [{"message": message}]}
