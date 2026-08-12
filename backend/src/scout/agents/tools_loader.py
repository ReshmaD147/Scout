import os
from contextlib import AsyncExitStack
from pathlib import Path

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from scout.config import settings

# ─────────────────────────────────────────────────────────
# MCP TOOL LOADING
# Builds the real MCP server configuration and manages the client
# session lifecycle. The "scout" server is always loaded — it's this
# project's own 11-tool stdio server (see mcp_server/server.py). A
# second "stripe" server can optionally be loaded too, but is OFF by
# default (ENABLE_STRIPE_MCP=False) specifically because a real,
# confirmed crash occurred when this defaulted to True with no
# STRIPE_MCP_KEY set: attempting to connect with an empty Bearer token
# raised a cryptic httpx.LocalProtocolError deep in the MCP client
# stack, rather than a clear, actionable error. Fixed by (1) flipping
# the default to opt-in rather than opt-out, and (2) validating the
# key is actually present before attempting the connection at all,
# raising a clear ValueError instead.
# ─────────────────────────────────────────────────────────

SERVER_PATH = Path(__file__).resolve().parent.parent / "mcp_server" / "server.py"


def build_mcp_servers_config() -> dict:
    """Build the MultiServerMCPClient config dict. The "scout" server
    (this project's own tools) is always included. The "stripe" server
    is only included if ENABLE_STRIPE_MCP is explicitly True, and only
    after confirming STRIPE_MCP_KEY is actually set — raises ValueError
    immediately with a clear, actionable message otherwise, rather than
    deferring to a much more confusing failure later when the
    connection is actually attempted.
    """
    config = {
        "scout": {
            "transport": "stdio",
            "command": "python3",
            "args": [str(SERVER_PATH)],
            # The MCP server runs as a fresh subprocess, spawned by
            # its full file path — it does NOT automatically inherit
            # uvicorn's --app-dir src trick, so without PYTHONPATH set
            # explicitly, "from scout..." imports inside server.py fail
            # with ModuleNotFoundError. Confirmed via a real Docker
            # deployment crash: the main process starts fine (uvicorn
            # sets its own path correctly), but the MCP subprocess
            # can't find the scout package at all, crashing the whole
            # app on startup since tool_manager.start() awaits it.
            "env": {
                **os.environ,
                "PYTHONPATH": str(SERVER_PATH.parent.parent.parent),
            },
        },
    }

    if settings.ENABLE_STRIPE_MCP:
        if not settings.STRIPE_MCP_KEY:
            raise ValueError(
                "ENABLE_STRIPE_MCP is True, but STRIPE_MCP_KEY is not set. "
                "Either provide a real STRIPE_MCP_KEY, or set "
                "ENABLE_STRIPE_MCP=false to skip the Stripe MCP connection "
                "entirely (the default, and what this project's own demo "
                "docs recommend)."
            )
        config["stripe"] = {
            "transport": "streamable_http",
            "url": settings.STRIPE_MCP_URL,
            "headers": {
                "Authorization": f"Bearer {settings.STRIPE_MCP_KEY}",
            },
        }

    return config


class MCPToolManager:
    """Owns the MultiServerMCPClient and the async session lifecycle
    for all configured MCP servers. start() must be called once before
    tools are usable; stop() releases everything cleanly via
    AsyncExitStack, regardless of how many server sessions were opened.
    """

    def __init__(self):
        self.servers_config = build_mcp_servers_config()
        self.client = MultiServerMCPClient(self.servers_config)
        self._exit_stack = AsyncExitStack()
        self.tools: list = []

    async def start(self) -> list:
        """Open the scout server's session (always) and load its
        tools, then load Stripe's tools too if that server was
        configured. Returns the combined tool list. IMPORTANT: the
        safety boundary documented in CLAUDE.md is NOT "no payment-
        capable tool exists in this process" — Stripe's tools genuinely
        can be loaded here if explicitly enabled. The real boundary is
        that get_tools_by_name() only ever hands each specialist agent
        its own small, hardcoded allowlist (see specialists.py) — none
        of which includes any Stripe tool name, regardless of what's
        loaded into self.tools here.
        """
        scout_session = await self._exit_stack.enter_async_context(
            self.client.session("scout")
        )
        scout_tools = await load_mcp_tools(scout_session)
        stripe_tools = []
        if "stripe" in self.servers_config:
            stripe_tools = await self.client.get_tools(server_name="stripe")
        self.tools = scout_tools + stripe_tools
        return self.tools

    async def stop(self) -> None:
        """Close every open MCP session cleanly."""
        await self._exit_stack.aclose()


def get_tools_by_name(tools: list, names: list[str]) -> list:
    """Filter a tool list down to only the named tools, in the order
    given. Unknown names are silently skipped rather than raising —
    used by specialists.py to hand each agent its specific, narrow
    tool allowlist.
    """
    by_name = {t.name: t for t in tools}
    return [by_name[n] for n in names if n in by_name]