"""Extension declaration, secrets, lifecycle hooks.

CONNECTION MODEL -- why personal access tokens and not platform OAuth.

The platform's `ext.oauth(...)` flow only knows three providers: `google`,
`microsoft` and `yahoo` (`ctx.oauth_authorize_url` raises ValueError on
anything else). Asana is not among them, so there is no platform-run OAuth
dance to hand this off to.

So the connector uses Asana *personal access tokens*: the user creates a PAT in
their Asana developer console and pastes it here.

HOW THIS DIFFERS FROM THE NOTION CONNECTOR (deliberately, not by accident).
A Notion integration token is scoped to exactly ONE workspace, so that
connector stores one token per line -- one line per workspace. An Asana PAT is
scoped to a USER, and that user's token reaches EVERY workspace and
organization they are a member of. So here:

  * one token == one Asana ACCOUNT, not one workspace;
  * workspaces are DISCOVERED from `/users/me` (it returns a `workspaces`
    array) rather than implied by the token;
  * `workspace` stays a per-tool parameter, defaulting to the only one when
    the account has just one.

Multiple accounts are still supported the same way -- ONE TOKEN PER LINE --
because a user may hold separate PATs for, say, a personal and a client
account. Account names and workspace lists are cached in the store; the tokens
themselves never leave the Vault-encrypted secret.
"""

from imperal_sdk import Extension, ChatExtension

ext = Extension(
    "asana-connector",
    version="1.0.0",
    # Declared so the kernel enforces `tool.required_scopes subset-of declared`
    # instead of falling back to a WILDCARD scope grant (validator V34).
    capabilities=["asana:read", "asana:write"],
    display_name="Asana Connector",
    description=(
        "Read and operate on Asana: browse workspaces, projects and tasks, "
        "read task details and comments, create and update tasks, complete "
        "them, move them between projects and sections, and comment -- across "
        "multiple Asana accounts."
    ),
    icon="icon.svg",
    actions_explicit=True,
)

chat = ChatExtension(
    ext,
    tool_name="asana",
    description=(
        "Asana Connector -- find and read Asana projects and tasks, create and "
        "update tasks, complete or reassign them, and manage comments."
    ),
)

# Credentials never flow through chat arguments -- the user pastes them into the
# Connect screen or the platform Secrets tab (auto-added because the secret is
# declared here).
ext.secret(
    "asana_tokens",
    "Asana personal access token(s) -- one per line, one line per account. "
    "Create one at app.asana.com/0/my-apps (Personal access token). A token "
    "reaches every workspace its owner belongs to.",
    required=True,
    # "both" -- Panel UI writes it (Secrets manager) AND the app writes it
    # itself from the Connect screen.
    #
    # The Notion connector learned this the hard way: with "user" the app
    # cannot store a token at all, so a panel form has no action it may
    # legally call, and saving through the owner-facing route reports success
    # while the extension runtime still reads nothing back -- a save that looks
    # like a no-op. With "both" the value is written through the very same
    # client that later reads it, so "saved" and "visible" cannot disagree.
    write_mode="both",
    max_bytes=4096,
    rotation_hint_days=180,
)(lambda: None)


@ext.health_check
async def health_check(ctx) -> dict:
    """Liveness probe: report whether at least one Asana token is configured.

    Deliberately does NOT call Asana: a health check must stay fast and must
    not fail because a third party is briefly unreachable. It answers
    "is this app configured", not "is Asana up".
    """
    try:
        raw = await ctx.secrets.get("asana_tokens")
        count = len([ln for ln in (raw or "").splitlines() if ln.strip()])
    except Exception:
        count = 0
    return {
        "healthy": count > 0,
        "tokens_configured": count,
        "detail": ("No Asana access token configured yet."
                   if count == 0 else f"{count} account token(s) configured."),
    }


@ext.on_install
async def on_install(ctx):
    """Make the first step traceable -- and knowable.

    An Asana PAT cannot be provisioned for the user, so a fresh install is
    inert by design until a token is pasted. Recording that at install time
    means "nothing works yet" shows up as an expected state in the audit log
    rather than looking like a broken deployment.
    """
    await ctx.log(
        "Asana Connector installed -- awaiting an access token; "
        "the Connect panel walks the user through it.",
        level="info",
    )
