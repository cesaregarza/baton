"""The MCP server: tools over messages and jobs, each acting as the caller's role.

The role comes from the caller's credential (`auth.role_of`), never from a
tool argument, so a client cannot speak for another role. No tool takes a
path, runs a command or returns anything from the host's environment. Each
call checks its scope, counts against an hourly limit per role and tool, and
appends one audit line naming the tool, the role and any ids, never the text.
"""

# Composition lives here; tools, guard and catalog own the MCP call surface.

import logging
import os
import threading
from urllib.parse import urlparse

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.types import ASGIApp

from baton import __version__, auth, dashboard, datadir, seat
from baton.app.reference_tools import LiteralArguments as LiteralArguments
from baton.app.reference_tools import ReferenceTools as ReferenceTools
from baton.app.topic_tools import TopicTools as TopicTools
from baton.config import Config
from baton.database.store import connect
from baton.integrations import Integration, IntegrationUnavailable, load_enabled
from baton.wakes import Dispatcher

from .catalog import ARTIFACT_TOOLS as ARTIFACT_TOOLS
from .catalog import AUDITED_IDS as AUDITED_IDS
from .catalog import CONVERSATION_TOOLS as CONVERSATION_TOOLS
from .catalog import CORE_PROOF_NOTE as CORE_PROOF_NOTE
from .catalog import GRANT_TOOLS as GRANT_TOOLS
from .catalog import GUEST_IDENTIFIED as GUEST_IDENTIFIED
from .catalog import GUEST_TOOLS as GUEST_TOOLS
from .catalog import IDENTIFIED_CHANGES as IDENTIFIED_CHANGES
from .catalog import INSTRUCTIONS as INSTRUCTIONS
from .catalog import LITERAL_TOOLS as LITERAL_TOOLS
from .catalog import REFERENCE_GUEST_TOOLS as REFERENCE_GUEST_TOOLS
from .catalog import REFERENCE_TOOLS as REFERENCE_TOOLS
from .catalog import SEARCH_TOOLS as SEARCH_TOOLS
from .catalog import TOKEN_ARGUMENT as TOKEN_ARGUMENT
from .catalog import TOOLS as TOOLS
from .guard import HOURLY_LIMITS as HOURLY_LIMITS
from .guard import Guard as Guard
from .guard import _call_context as _call_context
from .guard import _tool_signature as _tool_signature
from .guard import get_access_token as get_access_token
from .tools import MAX_WAIT_SECONDS as MAX_WAIT_SECONDS
from .tools import Tools as Tools
from .tools import _conversation_cursor as _conversation_cursor

log = logging.getLogger("baton.app")


def create_app(config: Config, integrations: dict[str, Integration] | None = None) -> ASGIApp:
    """Build the ASGI app. Enabled integrations add their MCP methods here (loaded from config if not given)."""
    datadir.check(config.data_dir)
    connect(config.db_path).close()
    if integrations is None:
        integrations = load_enabled(config)
    tools, guard = Tools(config), Guard(config)
    middleware = [
        guard.refuse_author_fields,
        guard.refuse_claim_validation,
        guard.refuse_token_validation,
        *(item for integration in integrations.values() for item in integration.middleware()),
    ]
    settings = auth.auth_settings(config)
    provider = auth.PassphraseProvider(config) if config.oauth.enabled else None
    server = MCPServer(
        name="baton",
        version=__version__,
        instructions=INSTRUCTIONS,
        auth=settings,
        middleware=middleware,
        auth_server_provider=provider,
        token_verifier=auth.StaticTokenVerifier(config) if provider is None else None,
    )
    if provider is not None:
        auth.register_login_routes(server, provider)
    for name, scope, read_only in (
        TOOLS
        + (CONVERSATION_TOOLS if config.conversations.enabled else ())
        + (SEARCH_TOOLS + ARTIFACT_TOOLS if config.search.enabled else ())
    ):
        server.add_tool(
            guard.wrap(name, scope, getattr(tools, name)),
            name=name,
            annotations=ToolAnnotations(read_only_hint=read_only, destructive_hint=False, open_world_hint=False),
        )
        if name in LITERAL_TOOLS:  # these take every argument as sent: a reference may be named "null"
            tool = server._tool_manager.get_tool(name)
            assert tool is not None  # add_tool above registered this exact name.
            guard.take_literally(tool)
    for integration in integrations.values():
        integration.register(server)
    public = urlparse(config.public_url)
    app = server.streamable_http_app(
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[public.netloc, "127.0.0.1:*", "localhost:*"],
            allowed_origins=[f"{public.scheme}://{public.netloc}", *config.allowed_origins],
        ),
    )
    if provider is not None:
        app.add_middleware(auth.EndpointGuard, provider=provider)
        app.add_middleware(auth.IssuerIdentification, provider=provider)
    return app


def _prepared(name: str, integration: Integration) -> bool:
    """Run the integration's `prepare()`, if it has one. False when it failed: it is off until the next start.

    Only an `IntegrationUnavailable` message is logged, since the integration wrote it to be safe to log;
    any other failure is logged by its type alone, so nothing it read ever reaches the log.
    """
    prepare = getattr(integration, "prepare", None)
    if prepare is None:
        return True
    try:
        prepare()
    except IntegrationUnavailable as error:
        log.error("integration %s is off for this run: %s", name, error)
        return False
    except Exception as error:
        log.error("integration %s is off for this run: prepare failed (%s)", name, type(error).__name__)
        return False
    return True


def serve(config: Config) -> None:
    """Run the MCP server and, beside it, the wake dispatcher and any enabled dashboard, until interrupted."""
    os.umask(0o077)
    integrations = load_enabled(config)
    app = create_app(config, integrations)
    # Every integration prepares before any worker thread starts or the server accepts anything, and one
    # that can't is switched off for this run: an optional integration never keeps the host from serving.
    integrations = {name: integration for name, integration in integrations.items() if _prepared(name, integration)}
    stop = threading.Event()
    dispatcher = Dispatcher(config, integrations=integrations)
    threading.Thread(target=dispatcher.run, args=(stop,), daemon=True, name="wakes").start()
    for name, integration in integrations.items():
        loop = getattr(integration, "run", None)
        if loop is not None:  # an integration with work of its own runs it beside the server, until stop
            threading.Thread(target=loop, args=(stop,), daemon=True, name=name).start()
    view = dashboard.start(config)
    seat_view = seat.start(config)
    try:
        uvicorn.run(app, host=config.listen_host, port=config.listen_port, log_level="info")
    finally:
        stop.set()
        for bound in (view, seat_view):
            if bound is not None:
                bound.shutdown()
                bound.server_close()
